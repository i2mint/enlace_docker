"""End-to-end integration tests against a real docker daemon.

Gated on the ``DOCKER_AVAILABLE`` env var AND a ``docker`` on PATH, so CI
runners without docker skip these cleanly while local/integration runs
exercise the real build → run → health → proxy → stop cycle.

Run locally with::

    DOCKER_AVAILABLE=1 pytest enlace_docker/tests/test_integration.py -v

A second test (no docker needed) proves the discovery → strategy →
build_backend wiring: an ``apps/<name>/`` declaring ``mode="docker"`` is
discovered and mounted as a proxy without importing any Python.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import urllib.error
import urllib.request

import pytest

_DOCKER = (
    shutil.which("docker") is not None
    and os.environ.get("DOCKER_AVAILABLE", "").lower() in ("1", "true", "yes")
)

requires_docker = pytest.mark.skipif(
    not _DOCKER,
    reason="set DOCKER_AVAILABLE=1 (and have docker on PATH) to run real-docker tests",
)


def _free_port() -> int:
    """Grab an ephemeral port the OS isn't using, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# A trivial app: serves the working directory over HTTP on $port. No
# HEALTHCHECK, so the lifecycle exercises the TCP-probe fallback path.
_DOCKERFILE = """\
FROM python:3.12-alpine
WORKDIR /app
RUN echo "hello from enlace_docker" > index.html
EXPOSE {port}
CMD ["python", "-m", "http.server", "{port}"]
"""


# -- Real-docker lifecycle ---------------------------------------------------


@requires_docker
@pytest.mark.asyncio
async def test_docker_lifecycle_build_run_health_proxy_stop(tmp_path):
    """Full cycle on the live daemon: build → run → healthy → HTTP → stop."""
    from enlace_docker import _docker
    from enlace_docker.lifecycle import DockerContainerLifecycle

    container_port = 8080
    host_port = _free_port()

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(_DOCKERFILE.format(port=container_port))

    lifecycle = DockerContainerLifecycle(
        name="enlace-docker-itest",
        image=_docker.image_tag_for("enlace-docker-itest"),
        container_port=container_port,
        host_port=host_port,
        dockerfile=dockerfile,
        build_context=tmp_path,
        ready_timeout=60.0,
    )

    try:
        await lifecycle.start()
        healthy = await lifecycle.wait_healthy()
        assert healthy, "container never became healthy"

        # Real HTTP request through the published host port.
        body = await _http_get(f"http://127.0.0.1:{host_port}/")
        assert "hello from enlace_docker" in body
    finally:
        await lifecycle.stop(timeout=5)

    # --rm means the container is gone after stop.
    assert not await _docker.container_exists(lifecycle._container_name)


@requires_docker
@pytest.mark.asyncio
async def test_docker_lifecycle_restart_accounting_on_crash(tmp_path):
    """A container that exits non-zero is reported as not-healthy / restartable."""
    from enlace_docker import _docker
    from enlace_docker.lifecycle import DockerContainerLifecycle

    host_port = _free_port()
    dockerfile = tmp_path / "Dockerfile"
    # Exits immediately with code 1 — never serves anything.
    dockerfile.write_text("FROM alpine\nCMD [\"sh\", \"-c\", \"exit 1\"]\n")

    lifecycle = DockerContainerLifecycle(
        name="enlace-docker-itest-crash",
        image=_docker.image_tag_for("enlace-docker-itest-crash"),
        container_port=9999,
        host_port=host_port,
        dockerfile=dockerfile,
        build_context=tmp_path,
        ready_timeout=8.0,
        restart_policy="on-failure",
    )

    try:
        await lifecycle.start()
        healthy = await lifecycle.wait_healthy()
        assert healthy is False
    finally:
        await lifecycle.stop(timeout=5)


# -- Discovery → strategy → build_backend (no docker needed) -----------------


def test_discovery_mounts_docker_app_as_proxy(tmp_path):
    """A ``mode="docker"`` app is discovered and mounted without importing it.

    Proves the full enlace wiring: app.toml → discovery (skips Python
    introspection) → DockerStrategy.make_asgi → proxy mount. The container
    is never started, so no docker daemon is required.
    """
    from enlace.base import PlatformConfig
    from enlace.compose import build_backend
    from enlace.discover import discover_apps

    # Lay out apps/myservice/ with just a Dockerfile + app.toml — no Python.
    apps_dir = tmp_path / "apps"
    app_dir = apps_dir / "myservice"
    app_dir.mkdir(parents=True)
    (app_dir / "Dockerfile").write_text("FROM alpine\n")
    (app_dir / "app.toml").write_text(
        'mode = "docker"\nport = 8080\n'
    )

    platform = PlatformConfig(apps_dirs=[apps_dir])
    discovered = discover_apps(platform)

    names = {a.name: a for a in discovered.apps}
    assert "myservice" in names
    app = names["myservice"]
    assert app.mode == "docker"
    assert app.port == 8080
    # Discovery must NOT have tried to import Python (there is none).
    assert app.app_type == "asgi_app"  # opaque-to-enlace default for non-asgi

    # build_backend should mount a proxy at the app's route prefix.
    backend = build_backend(discovered)
    mounted_paths = {
        r.path for r in backend.routes if getattr(r, "path", None)
    }
    assert any("/myservice" in p for p in mounted_paths), mounted_paths


# -- helpers -----------------------------------------------------------------


async def _http_get(url: str, *, attempts: int = 10, delay: float = 0.5) -> str:
    """Fetch ``url`` in a thread, retrying briefly.

    A TCP probe reports "ready" the instant docker's port-forwarder accepts
    a connection — which can be a beat before the in-container server serves
    its first real HTTP request. A real reverse proxy (httpx) retries the
    connect, so the test does too rather than racing the app's accept loop.
    """
    import asyncio

    def _fetch() -> str:
        with contextlib.closing(urllib.request.urlopen(url, timeout=5)) as resp:
            return resp.read().decode("utf-8", errors="replace")

    loop = asyncio.get_event_loop()
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            return await loop.run_in_executor(None, _fetch)
        except (OSError, urllib.error.URLError) as e:
            last_exc = e
            await asyncio.sleep(delay)
    raise AssertionError(f"GET {url} never succeeded: {last_exc!r}")
