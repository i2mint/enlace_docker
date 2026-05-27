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
import shutil
from dataclasses import dataclass
from typing import Optional, Sequence

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
) -> CommandResult:
    """Run ``docker <args>``, capturing stdout/stderr.

    If ``check`` is True and the command fails, raises ``DockerCLIError``.
    """
    return await _run("docker", *args, check=check, env=env)


async def run_docker_compose(
    *args: str,
    check: bool = True,
    env: Optional[dict] = None,
) -> CommandResult:
    """Run ``docker compose <args>``, capturing stdout/stderr."""
    return await _run("docker", "compose", *args, check=check, env=env)


async def _run(
    *argv: str,
    check: bool,
    env: Optional[dict],
) -> CommandResult:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    out, err = await proc.communicate()
    result = CommandResult(
        returncode=proc.returncode or 0,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
    )
    if check and not result.ok:
        raise DockerCLIError(argv, result)
    return result


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
        return None
    line = result.stdout.strip()
    if ":" not in line:
        return None
    try:
        return int(line.rsplit(":", 1)[1])
    except ValueError:
        return None
