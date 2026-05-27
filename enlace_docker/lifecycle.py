"""Container-backed implementations of enlace's ``Lifecycle`` protocol.

Two classes cover the supervisable docker modes:

- ``DockerContainerLifecycle`` — single container started via ``docker run``,
  used by ``DockerStrategy`` (Dockerfile build) and ``ImageStrategy`` (pull).
- ``ComposeStackLifecycle`` — multi-service stack started via
  ``docker compose up -d``, used by ``ComposeStrategy``.

Both speak the same supervisor surface as ``enlace.supervise.ManagedProcess``
so ``enlace.supervise.supervise_all`` drives them identically to subprocess
lifecycles. Restart accounting (``record_failure`` / ``backoff_delay`` /
``should_restart`` / ``maybe_reset_backoff``) is shared via the
``_RestartAccounting`` mixin so the two classes don't diverge.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from enlace_docker import _docker

# ANSI reset; the supervisor sets ``self.color`` per-lifecycle for log labels.
_RESET = "\033[0m"


# -- Restart accounting (shared mixin) ---------------------------------------


@dataclass
class _RestartAccounting:
    """Restart-policy plumbing shared by every docker-backed lifecycle.

    Mirrors the semantics of ``enlace.supervise.ManagedProcess`` so the
    user's restart policy / max-retries / backoff behave identically
    whether the backend is a subprocess or a container.
    """

    restart_policy: str = "on-failure"  # always | on-failure | never
    max_retries: int = 5
    restart_delay_ms: int = 100

    _consecutive_failures: int = field(default=0, repr=False)
    _started_at: Optional[float] = field(default=None, repr=False)
    _last_exit_code: Optional[int] = field(default=None, repr=False)

    def should_restart(self) -> bool:
        if self.restart_policy == "never":
            return False
        if self.restart_policy == "on-failure" and self._last_exit_code == 0:
            return False
        if self._consecutive_failures >= self.max_retries:
            return False
        return True

    def record_failure(self) -> None:
        self._consecutive_failures += 1

    def maybe_reset_backoff(self) -> None:
        if self._started_at is not None and time.monotonic() - self._started_at > 30.0:
            self._consecutive_failures = 0

    def backoff_delay(self) -> float:
        delay = (self.restart_delay_ms / 1000.0) * (1.5**self._consecutive_failures)
        return min(delay, 15.0)


# -- DockerContainerLifecycle ------------------------------------------------


@dataclass
class DockerContainerLifecycle(_RestartAccounting):
    """Lifecycle for a single container started via ``docker run -d``.

    ``ImageStrategy`` and ``DockerStrategy`` both produce instances of this
    class — the only difference is whether ``build_step`` runs
    ``docker build`` or ``docker pull`` (or nothing) before each start.

    The container is named ``enlace-<app>`` and the in-container ``port`` is
    published to the same port on the host. Health is observed via
    ``docker inspect`` ``State.Health.Status`` when a ``HEALTHCHECK`` is
    declared; otherwise we fall back to a plain TCP probe on the host port.
    """

    name: str = ""
    image: str = ""  # local tag (build) or remote ref (pull)
    container_port: int = 0
    host_port: int = 0
    env: dict[str, str] = field(default_factory=dict)
    extra_run_args: list[str] = field(default_factory=list)
    ready_timeout: float = 30.0

    # Build-time hooks (populated by DockerStrategy when a Dockerfile is in
    # play; left as no-op for ImageStrategy which pulls instead).
    dockerfile: Optional[Path] = None
    build_context: Optional[Path] = None
    build_args: dict[str, str] = field(default_factory=dict)
    pull_before_start: bool = False  # set True for ImageStrategy

    # Supervisor-visible state
    color: str = ""
    state: str = "stopped"

    # Streaming-log subprocess handle (populated by stream_logs)
    _log_proc: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    _container_name: str = field(default="", repr=False)

    def __post_init__(self):
        self._container_name = _docker.container_name_for(self.name)

    # ---- enlace.strategies.Lifecycle protocol -------------------------------

    async def start(self) -> None:
        self.state = "starting"

        # 1. Build or pull the image so it's locally available.
        if self.dockerfile is not None and self.build_context is not None:
            await self._build_image()
        elif self.pull_before_start:
            await self._pull_image()

        # 2. Remove any leftover container with the same name (from a prior
        #    run / crash) so ``docker run --name`` doesn't conflict.
        await self._remove_existing_container()

        # 3. Spawn the container detached, with --rm so the container is
        #    removed when it exits (we recreate on restart).
        run_argv: list[str] = [
            "run",
            "-d",
            "--rm",
            "--name",
            self._container_name,
            "-p",
            f"{self.host_port}:{self.container_port}",
        ]
        for k, v in self.env.items():
            run_argv += ["-e", f"{k}={v}"]
        # enlace conventionally exports ENLACE_MANAGED=1 to managed apps so
        # they can short-circuit standalone-only startup blocks.
        run_argv += ["-e", "ENLACE_MANAGED=1"]
        run_argv += self.extra_run_args
        run_argv += [self.image]

        await _docker.run_docker(*run_argv)
        self._started_at = time.monotonic()
        self.log(f"started ({self._container_name} → :{self.host_port})")

    async def stop(self, timeout: float = 10.0) -> None:
        if self.state in ("stopped", "exited"):
            return
        self.state = "stopping"
        # Stop is best-effort: the container may already have died, in which
        # case the exit-code path below records the result anyway.
        await _docker.run_docker(
            "stop",
            "-t",
            str(int(timeout)),
            self._container_name,
            check=False,
        )
        if self._log_proc is not None and self._log_proc.returncode is None:
            self._log_proc.terminate()
            try:
                await asyncio.wait_for(self._log_proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._log_proc.kill()
                await self._log_proc.wait()
        self.state = "exited"

    async def wait_healthy(self) -> bool:
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if not await _docker.container_running(self._container_name):
                # Container exited before becoming healthy.
                return False
            # Prefer the container's declared HEALTHCHECK when present —
            # it knows the app's notion of "ready" (e.g. DB warm, cache
            # loaded). Fall back to a TCP probe on the published port.
            health = await _docker.container_health(self._container_name)
            if health == "healthy":
                self.state = "running"
                self._consecutive_failures = 0
                self._started_at = time.monotonic()
                self.log("healthy")
                return True
            if health is None and await self._tcp_ready():
                self.state = "running"
                self._consecutive_failures = 0
                self._started_at = time.monotonic()
                self.log("healthy (tcp probe)")
                return True
            await asyncio.sleep(0.5)
        self.log(f"not healthy after {self.ready_timeout}s")
        return False

    async def stream_logs(self) -> None:
        """Stream ``docker logs -f`` and print with the colored name prefix."""
        try:
            self._log_proc = await asyncio.create_subprocess_exec(
                "docker",
                "logs",
                "-f",
                self._container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:  # pragma: no cover — docker CLI missing
            self.log("docker CLI not found; cannot stream logs")
            return
        label = f"{self.color}{self.name:>15}{_RESET}"
        assert self._log_proc.stdout is not None
        while True:
            line = await self._log_proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            print(f"{label} | {text}", flush=True)

    async def wait_exit(self) -> Optional[int]:
        """Block until the container is no longer running; return exit code."""
        # Poll inspect; cheap (one fork per second).  The container has
        # ``--rm`` so once it exits the name disappears; treat that as
        # exit-code-unknown rather than crashing.
        while True:
            if not await _docker.container_exists(self._container_name):
                self._last_exit_code = self._last_exit_code or 0
                return self._last_exit_code
            if not await _docker.container_running(self._container_name):
                rc = await _docker.container_exit_code(self._container_name)
                self._last_exit_code = rc if rc is not None else 1
                return self._last_exit_code
            await asyncio.sleep(1.0)

    def is_alive(self) -> bool:
        """Whether we *believe* the container is still running.

        This is a synchronous spotcheck used by the supervisor between
        async steps; for the authoritative answer it calls ``wait_exit``.
        """
        return self.state in ("starting", "running")

    def log(self, msg: str) -> None:
        label = f"{self.color}{self.name:>15}{_RESET}"
        print(f"{label} | [enlace-docker] {msg}", flush=True)

    # ---- internals ----------------------------------------------------------

    async def _build_image(self) -> None:
        argv: list[str] = ["build", "-t", self.image]
        if self.dockerfile is not None:
            argv += ["-f", str(self.dockerfile)]
        for k, v in self.build_args.items():
            argv += ["--build-arg", f"{k}={v}"]
        argv.append(str(self.build_context))
        self.log(f"building image {self.image}")
        await _docker.run_docker(*argv)

    async def _pull_image(self) -> None:
        self.log(f"pulling image {self.image}")
        await _docker.run_docker("pull", self.image)

    async def _remove_existing_container(self) -> None:
        # ``docker rm -f`` is a no-op if the name doesn't exist (with check=False).
        await _docker.run_docker("rm", "-f", self._container_name, check=False)

    async def _tcp_ready(self) -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.host_port),
                timeout=1.0,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False


# -- ComposeStackLifecycle ---------------------------------------------------


@dataclass
class ComposeStackLifecycle(_RestartAccounting):
    """Lifecycle wrapping ``docker compose up -d`` / ``down``.

    Routes HTTP to a single declared service+port. Multi-route compose
    stacks (each service exposed under its own URL prefix) are out of
    scope for v1 — see ``enlace#3`` for the rationale.
    """

    name: str = ""
    compose_file: Path = field(default_factory=Path)
    service: str = ""
    service_port: int = 0
    env: dict[str, str] = field(default_factory=dict)
    ready_timeout: float = 60.0  # compose stacks are typically slower

    # Resolved at start() time so make_asgi's proxy can route to it.
    host_port: Optional[int] = None

    color: str = ""
    state: str = "stopped"
    _log_proc: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    _project: str = field(default="", repr=False)

    def __post_init__(self):
        self._project = _docker.compose_project_for(self.name)

    async def start(self) -> None:
        self.state = "starting"
        argv = [
            "-f",
            str(self.compose_file),
            "-p",
            self._project,
            "up",
            "-d",
            "--remove-orphans",
        ]
        # Compose merges env from .env files; we pass override via the
        # subprocess env so the user can interpolate ``${LOG_LEVEL}`` in
        # docker-compose.yml.
        import os

        child_env = {**os.environ, **self.env, "ENLACE_MANAGED": "1"}
        self.log(f"compose up ({self.compose_file.name}, project={self._project})")
        await _docker.run_docker_compose(*argv, env=child_env)

        # Resolve the host port for routing.
        self.host_port = await _docker.compose_published_port(
            self._project,
            str(self.compose_file),
            self.service,
            self.service_port,
        )
        if self.host_port is None:
            self.log(
                f"WARNING: service '{self.service}' has no published port for "
                f"{self.service_port} — proxy will not be reachable."
            )
        self._started_at = time.monotonic()

    async def stop(self, timeout: float = 10.0) -> None:
        if self.state in ("stopped", "exited"):
            return
        self.state = "stopping"
        argv = [
            "-f",
            str(self.compose_file),
            "-p",
            self._project,
            "down",
            "-t",
            str(int(timeout)),
        ]
        await _docker.run_docker_compose(*argv, check=False)
        if self._log_proc is not None and self._log_proc.returncode is None:
            self._log_proc.terminate()
            try:
                await asyncio.wait_for(self._log_proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._log_proc.kill()
                await self._log_proc.wait()
        self.state = "exited"

    async def wait_healthy(self) -> bool:
        if self.host_port is None:
            return False
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if await self._tcp_ready(self.host_port):
                self.state = "running"
                self._consecutive_failures = 0
                self._started_at = time.monotonic()
                self.log(f"healthy (tcp probe on host port {self.host_port})")
                return True
            await asyncio.sleep(0.5)
        self.log(f"not healthy after {self.ready_timeout}s")
        return False

    async def stream_logs(self) -> None:
        try:
            self._log_proc = await asyncio.create_subprocess_exec(
                "docker",
                "compose",
                "-f",
                str(self.compose_file),
                "-p",
                self._project,
                "logs",
                "-f",
                "--no-color",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:  # pragma: no cover
            self.log("docker CLI not found; cannot stream logs")
            return
        label = f"{self.color}{self.name:>15}{_RESET}"
        assert self._log_proc.stdout is not None
        while True:
            line = await self._log_proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            print(f"{label} | {text}", flush=True)

    async def wait_exit(self) -> Optional[int]:
        # Compose stacks don't have a single exit code; we wait until the
        # declared service's container stops responding. When it does,
        # treat it as a non-zero exit so the supervisor will restart.
        while True:
            if self.host_port is None:
                return 1
            if not await self._tcp_ready(self.host_port):
                # Give the supervisor a beat to decide on restart.
                self._last_exit_code = 1
                return self._last_exit_code
            await asyncio.sleep(2.0)

    def is_alive(self) -> bool:
        return self.state in ("starting", "running")

    def log(self, msg: str) -> None:
        label = f"{self.color}{self.name:>15}{_RESET}"
        print(f"{label} | [enlace-docker] {msg}", flush=True)

    async def _tcp_ready(self, port: int) -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port),
                timeout=1.0,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False
