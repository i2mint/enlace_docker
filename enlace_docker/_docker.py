"""Thin async wrappers around the ``docker`` / ``docker compose`` CLIs.

Centralizes shell-out so tests can monkey-patch a single seam
(``run_docker`` / ``run_docker_compose``) instead of every call site.
No third-party dependency — uses ``asyncio.create_subprocess_exec``.

Conventions:

- Container/image naming is namespaced with an ``enlace-`` / ``enlace/``
  prefix so we don't collide with user-managed containers or images.
- All commands are non-interactive (no ``-it``, no TTY) — they're driven by
  the dev supervisor, not a human.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from dataclasses import dataclass
from typing import Optional, Sequence

#: The only interface enlace publishes container ports on, and the address the
#: gateway (proxy + health probes) connects to. Loopback keeps a container
#: reachable solely through the gateway, which is where auth is applied.
LOOPBACK = "127.0.0.1"

#: Ceiling, in seconds, on one docker CLI call that only asks or tells the daemon
#: something (inspect, ps, port, rm, run -d). A healthy daemon answers in well under
#: a second; a hung one would otherwise hang whatever awaits the call (#9).
DFLT_TIMEOUT = 30.0
#: Ceiling for a call that produces an image (build, pull, compose up), which can
#: legitimately take minutes. Still finite, so a hung daemon fails the start.
BUILD_TIMEOUT = 1800.0
#: Exit code reported for a call cut off by its timeout (coreutils ``timeout``'s).
TIMEOUT_RETURNCODE = 124
#: After killing a timed-out call, how long to wait for its pipes to close before
#: dropping them: a grandchild that escaped the kill (one that started its own
#: session) can hold them open indefinitely.
KILL_GRACE = 5.0

# -- Naming conventions ------------------------------------------------------


def container_name_for(app_name: str) -> str:
    """Container name we use for an app under ``mode=docker`` / ``image``.

    Prefixed with ``enlace-`` so we never collide with the user's own
    containers and so ``docker ps --filter name=enlace-`` lists what
    enlace owns.
    """
    return f"enlace-{app_name}"


def image_tag_for(app_name: str) -> str:
    """Local image tag we build into for ``mode=docker``."""
    return f"enlace/{app_name}:dev"


def compose_project_for(app_name: str) -> str:
    """Compose project (``-p``) namespace for ``mode=compose``."""
    return f"enlace-{app_name}"


# -- Command runners ---------------------------------------------------------


@dataclass
class CommandResult:
    """Result of a non-streaming docker CLI invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class DockerCLIError(RuntimeError):
    """Raised when a required docker CLI invocation fails.

    Carries stderr verbatim so the caller's log shows the real cause
    (build error, name conflict, image-not-found, etc.) instead of a
    bare non-zero exit code.
    """

    def __init__(self, argv: Sequence[str], result: CommandResult):
        self.argv = list(argv)
        self.result = result
        msg = (
            f"docker command failed (exit {result.returncode}): "
            f"{' '.join(argv)}\n"
            f"  stderr: {result.stderr.strip() or '(empty)'}"
        )
        super().__init__(msg)


def docker_available() -> bool:
    """Whether ``docker`` is on PATH. Cheap; no subprocess."""
    return shutil.which("docker") is not None


async def run_docker(
    *args: str,
    check: bool = True,
    env: Optional[dict] = None,
    timeout: Optional[float] = DFLT_TIMEOUT,
) -> CommandResult:
    """Run ``docker <args>``, capturing stdout/stderr.

    If ``check`` is True and the command fails, raises ``DockerCLIError``. A call
    still running after ``timeout`` seconds (``None``: no limit) is killed and counts
    as a failure with exit code ``TIMEOUT_RETURNCODE``.
    """
    return await _run("docker", *args, check=check, env=env, timeout=timeout)


async def run_docker_compose(
    *args: str,
    check: bool = True,
    env: Optional[dict] = None,
    timeout: Optional[float] = DFLT_TIMEOUT,
) -> CommandResult:
    """Run ``docker compose <args>``, capturing stdout/stderr (see ``run_docker``)."""
    return await _run(
        "docker", "compose", *args, check=check, env=env, timeout=timeout
    )


async def _run(
    *argv: str,
    check: bool,
    env: Optional[dict],
    timeout: Optional[float] = DFLT_TIMEOUT,
) -> CommandResult:
    """Run *argv*; a child still running after *timeout* seconds is killed.

    A timeout is reported like any other failure — a non-ok ``CommandResult``
    (raised as ``DockerCLIError`` only when ``check``) — never as an
    ``asyncio.TimeoutError``, so a ``check=False`` probe on a request path degrades
    to "unknown" instead of failing the request.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        # Its own process group, so a kill reaches what it spawned: ``docker
        # compose`` and ``docker buildx`` run a plugin as a child of ``docker``,
        # and that plugin, hung on the same daemon, would keep the pipes open.
        # (POSIX only; ignored on Windows.)
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        await _kill(proc)
        result = CommandResult(
            returncode=TIMEOUT_RETURNCODE,
            stdout="",
            stderr=f"timed out after {timeout:g}s (docker daemon not answering?)",
        )
    except BaseException:
        # Cancelled by the caller: do not leave the child behind.
        await _kill(proc)
        raise
    else:
        result = CommandResult(
            returncode=proc.returncode or 0,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
        )
    if check and not result.ok:
        raise DockerCLIError(argv, result)
    return result


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill *proc* and its process group, then reap it, in bounded time.

    ``Process.wait()`` resolves only once the child has exited AND every pipe it
    shares is closed, so a descendant holding a pipe would hang it as surely as
    the daemon did. Killing the group covers the docker CLI's plugins; for a
    descendant that left the group, our ends of the pipes are dropped after
    ``KILL_GRACE``.
    """
    if proc.returncode is None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
    try:
        await asyncio.wait_for(proc.wait(), KILL_GRACE)
    except asyncio.TimeoutError:
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()
        await proc.wait()


# -- Inspection helpers ------------------------------------------------------


async def container_exists(name: str) -> bool:
    """Whether a container with this name (running or stopped) exists.

    Uses ``docker ps -a -q --filter name=^<name>$`` so we don't false-match
    a substring (``enlace-foo`` against ``enlace-foobar``).
    """
    result = await run_docker(
        "ps", "-a", "-q", "--filter", f"name=^{name}$", check=False
    )
    return bool(result.stdout.strip())


async def inspect_format(target: str, fmt: str) -> Optional[str]:
    """``docker inspect --format <fmt> <target>``; returns None if missing."""
    result = await run_docker("inspect", "--format", fmt, target, check=False)
    if not result.ok:
        return None
    return result.stdout.strip()


async def container_health(name: str) -> Optional[str]:
    """Return ``"healthy" | "unhealthy" | "starting" | None``.

    ``None`` means the container has no HEALTHCHECK declared — the caller
    should fall back to a TCP probe.
    """
    raw = await inspect_format(name, "{{.State.Health.Status}}")
    if not raw or raw in ("<no value>", "<nil>"):
        return None
    return raw


async def container_running(name: str) -> bool:
    """Whether the container is currently in the ``running`` state."""
    raw = await inspect_format(name, "{{.State.Running}}")
    return raw == "true"


async def container_exit_code(name: str) -> Optional[int]:
    """Exit code of a stopped container, or None if still running / unknown."""
    raw = await inspect_format(name, "{{.State.ExitCode}}")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def container_published_host_ips(name: str, container_port: int) -> list[str]:
    """Host interfaces the container's ``container_port`` is published on.

    ``docker inspect`` reports an empty ``HostIp`` for "all interfaces"; that
    is returned as ``0.0.0.0``.
    """
    fmt = (
        '{{range (index .NetworkSettings.Ports "%d/tcp")}}{{.HostIp}},{{end}}'
        % container_port
    )
    raw = await inspect_format(name, fmt)
    if not raw:
        return []
    parts = raw.split(",")[:-1]  # every entry ends with the separator
    return [ip.strip() or "0.0.0.0" for ip in parts]


async def container_published_port(name: str, container_port: int) -> Optional[int]:
    """Host port the container's ``container_port`` is published on.

    Returns ``None`` if the port isn't mapped (caller should error clearly).
    """
    # `{{(index (index .NetworkSettings.Ports "<port>/tcp") 0).HostPort}}` is
    # the canonical recipe; if the port isn't published we get an error and
    # `inspect_format` returns None.
    fmt = (
        '{{(index (index .NetworkSettings.Ports "%d/tcp") 0).HostPort}}'
        % container_port
    )
    raw = await inspect_format(name, fmt)
    if raw is None or not raw.isdigit():
        return None
    return int(raw)


async def compose_published_port(
    project: str,
    compose_file: str,
    service: str,
    service_port: int,
) -> Optional[int]:
    """Resolve ``docker compose port`` to a host port.

    Compose outputs ``0.0.0.0:54321`` on stdout. We return the port int.
    """
    addresses = await compose_published_addresses(
        project, compose_file, service, service_port
    )
    return addresses[0][1] if addresses else None


def parse_published_addresses(output: str) -> list[tuple[str, int]]:
    """Parse ``docker compose port`` output into ``[(host, port), ...]``.

    One line per binding (Compose prints v4 and v6 separately).

    >>> parse_published_addresses("0.0.0.0:54321")
    [('0.0.0.0', 54321)]
    >>> parse_published_addresses("127.0.0.1:1\\n[::]:1")
    [('127.0.0.1', 1), ('::', 1)]
    >>> parse_published_addresses(":::54321")
    [('::', 54321)]
    >>> parse_published_addresses("nonsense")
    []
    """
    out = []
    for line in output.strip().splitlines():
        host, sep, port = line.strip().rpartition(":")
        if not sep:
            continue
        try:
            out.append((host.strip("[]").rstrip(":") or "::", int(port)))
        except ValueError:
            continue
    return out


def is_loopback_host(host: str) -> bool:
    """True if a published address is reachable only from this machine.

    >>> is_loopback_host("127.0.0.1"), is_loopback_host("::1")
    (True, True)
    >>> is_loopback_host("0.0.0.0"), is_loopback_host("::")
    (False, False)
    """
    import ipaddress

    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def compose_published_addresses(
    project: str,
    compose_file: str,
    service: str,
    service_port: int,
) -> list[tuple[str, int]]:
    """Resolve ``docker compose port`` to every ``(host_address, host_port)``."""
    result = await run_docker_compose(
        "-f",
        compose_file,
        "-p",
        project,
        "port",
        service,
        str(service_port),
        check=False,
    )
    if not result.ok:
        return []
    return parse_published_addresses(result.stdout)
