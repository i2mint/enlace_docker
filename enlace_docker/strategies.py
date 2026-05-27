"""``BackendStrategy`` subclasses that register against enlace's mode registry.

Four strategies, all registered via the ``enlace.backend_strategies``
entry-point group (see ``pyproject.toml``):

- ``DockerStrategy`` (``mode="docker"``) — Dockerfile build + supervised run.
- ``ImageStrategy`` (``mode="image"``) — pre-built image, pull + supervised run.
- ``ComposeStrategy`` (``mode="compose"``) — ``docker compose up -d`` an app stack.
- ``DockerAttachedStrategy`` (``mode="docker_attached"``) — route to an already-
  running container; enlace does not manage the lifecycle.

The first three are ``is_supervisable=True`` so the dev supervisor drives
them via the ``Lifecycle`` protocol (see ``enlace_docker.lifecycle``). All
four use enlace's built-in ``enlace.proxy.make_proxy_app`` to route HTTP —
there is no docker-specific proxy code in this package.

Plugin-specific TOML keys (``dockerfile``, ``image``, ``compose_file``,
``service``, ``container``, ``build_args``) flow onto ``AppConfig`` via
``model_config = ConfigDict(extra="allow")`` (added in enlace 0.1.15) and
are read here via attribute access.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from enlace.strategies import BackendStrategy

from enlace_docker import _docker
from enlace_docker.lifecycle import (
    ComposeStackLifecycle,
    DockerContainerLifecycle,
)

# Sentinel used to spot missing optional fields without confusing None with
# "set to None on purpose".
_MISSING = object()


def _get(app, name: str, default=_MISSING):
    """Read an extra field off AppConfig; default if absent."""
    return getattr(app, name, default)


def _proxy(app):
    """Build the standard reverse-proxy app for ``app`` at its route prefix."""
    from enlace.proxy import make_proxy_app

    upstream = f"http://127.0.0.1:{app.port}"
    return make_proxy_app(upstream=upstream, strip_prefix=app.route_prefix)


# -- DockerStrategy (mode=docker) --------------------------------------------


class DockerStrategy(BackendStrategy):
    """Build the app's Dockerfile, run the container, supervise, proxy.

    Conventions:

    - Image tag is ``enlace/<app>:dev`` (local, never pushed).
    - Container is named ``enlace-<app>``.
    - ``port`` is the in-container port; we publish it to the same port on
      the host (matching how the Dockerfile's ``EXPOSE`` directive reads).
    """

    name = "docker"
    is_supervisable = True
    toml_field_map = {
        "dockerfile": "dockerfile",
        "context": "context",
        "port": "port",
        "env": "env",
        "build_args": "build_args",
        "ready_timeout": "ready_timeout",
        "restart_policy": "restart_policy",
        "max_retries": "max_retries",
        "restart_delay_ms": "restart_delay_ms",
    }
    path_keys = {"dockerfile", "context"}

    def validate(self, app):
        if app.port is None:
            raise ValueError(
                f"App '{app.name}': mode='docker' requires 'port' "
                "(the in-container port to publish)"
            )

    def make_asgi(self, app, platform):
        if app.port is None:
            return None
        return _proxy(app)

    def make_lifecycle(self, app, platform):
        dockerfile = _get(app, "dockerfile", None)
        context = _get(app, "context", None)

        # Resolve defaults relative to the app's source directory. discover.py
        # already resolves path_keys, but defaults must be filled here.
        app_dir = _app_dir(app)
        if dockerfile is None:
            dockerfile = app_dir / "Dockerfile"
        if context is None:
            context = app_dir

        return DockerContainerLifecycle(
            name=app.name,
            image=_docker.image_tag_for(app.name),
            container_port=app.port,
            host_port=app.port,
            env=dict(app.env) if app.env else {},
            dockerfile=Path(dockerfile),
            build_context=Path(context),
            build_args=dict(_get(app, "build_args", {}) or {}),
            ready_timeout=app.ready_timeout,
            restart_policy=app.restart_policy,
            max_retries=app.max_retries,
            restart_delay_ms=app.restart_delay_ms,
        )


# -- ImageStrategy (mode=image) ----------------------------------------------


class ImageStrategy(BackendStrategy):
    """Pull a pre-built image, run the container, supervise, proxy.

    Use this when CI publishes images to a registry and the app shouldn't
    rebuild on every ``enlace serve``. No Dockerfile needs to exist in
    the app directory.
    """

    name = "image"
    is_supervisable = True
    toml_field_map = {
        "image": "image",
        "port": "port",
        "env": "env",
        "ready_timeout": "ready_timeout",
        "restart_policy": "restart_policy",
        "max_retries": "max_retries",
        "restart_delay_ms": "restart_delay_ms",
    }

    def validate(self, app):
        if not _get(app, "image", None):
            raise ValueError(
                f"App '{app.name}': mode='image' requires 'image' "
                "(e.g. 'ghcr.io/org/app:1.2.3')"
            )
        if app.port is None:
            raise ValueError(
                f"App '{app.name}': mode='image' requires 'port' "
                "(the in-container port to publish)"
            )

    def make_asgi(self, app, platform):
        if app.port is None:
            return None
        return _proxy(app)

    def make_lifecycle(self, app, platform):
        return DockerContainerLifecycle(
            name=app.name,
            image=_get(app, "image"),
            container_port=app.port,
            host_port=app.port,
            env=dict(app.env) if app.env else {},
            pull_before_start=True,
            ready_timeout=app.ready_timeout,
            restart_policy=app.restart_policy,
            max_retries=app.max_retries,
            restart_delay_ms=app.restart_delay_ms,
        )


# -- ComposeStrategy (mode=compose) ------------------------------------------


class ComposeStrategy(BackendStrategy):
    """``docker compose up -d`` an app stack; route HTTP to one declared service.

    The compose project is namespaced ``enlace-<app>`` so it doesn't collide
    with the user's own compose stacks. Routing requires the user to declare
    which service receives HTTP (``service``) and its in-container port
    (``port``) — auto-detection is intentionally not done; multi-service
    routing semantics get hairy fast.
    """

    name = "compose"
    is_supervisable = True
    toml_field_map = {
        "compose_file": "compose_file",
        "service": "service",
        "port": "port",
        "env": "env",
        "ready_timeout": "ready_timeout",
        "restart_policy": "restart_policy",
        "max_retries": "max_retries",
        "restart_delay_ms": "restart_delay_ms",
    }
    path_keys = {"compose_file"}

    def validate(self, app):
        if not _get(app, "service", None):
            raise ValueError(
                f"App '{app.name}': mode='compose' requires 'service' "
                "(the compose service that receives HTTP)"
            )
        if app.port is None:
            raise ValueError(
                f"App '{app.name}': mode='compose' requires 'port' "
                "(the service's in-container port)"
            )

    def make_asgi(self, app, platform):
        # The host port isn't known until the stack starts and we resolve
        # it via `docker compose port`. The lifecycle stores the resolved
        # host port on itself; build a proxy that reads from there.
        return _ComposeProxyProxy(app)

    def make_lifecycle(self, app, platform):
        compose_file = _get(app, "compose_file", None)
        app_dir = _app_dir(app)
        if compose_file is None:
            compose_file = app_dir / "docker-compose.yml"

        lifecycle = ComposeStackLifecycle(
            name=app.name,
            compose_file=Path(compose_file),
            service=_get(app, "service"),
            service_port=app.port,
            env=dict(app.env) if app.env else {},
            ready_timeout=app.ready_timeout,
            restart_policy=app.restart_policy,
            max_retries=app.max_retries,
            restart_delay_ms=app.restart_delay_ms,
        )
        # Stash on the AppConfig so the (already-created) proxy can find it.
        # Using a private attribute name to avoid colliding with any TOML key.
        app.__dict__["_compose_lifecycle"] = lifecycle
        return lifecycle


class _ComposeProxyProxy:
    """Indirection ASGI app: looks up the compose host port at first request.

    The host port assigned to a compose service isn't known until
    ``docker compose up -d`` runs — but ``make_asgi`` is called *before*
    ``make_lifecycle``. This wrapper defers proxy construction until the
    first request, by which time the lifecycle has resolved the port.
    """

    def __init__(self, app):
        self._app_config = app
        self._proxy = None

    async def __call__(self, scope, receive, send):
        if self._proxy is None:
            self._proxy = self._resolve_proxy()
        if self._proxy is None:
            await _send_error(send, 502, b"compose service not yet healthy")
            return
        await self._proxy(scope, receive, send)

    def _resolve_proxy(self):
        from enlace.proxy import make_proxy_app

        lifecycle = self._app_config.__dict__.get("_compose_lifecycle")
        if lifecycle is None or lifecycle.host_port is None:
            return None
        upstream = f"http://127.0.0.1:{lifecycle.host_port}"
        return make_proxy_app(
            upstream=upstream, strip_prefix=self._app_config.route_prefix
        )


# -- DockerAttachedStrategy (mode=docker_attached) ---------------------------


class DockerAttachedStrategy(BackendStrategy):
    """Route to a container the user manages out-of-band.

    Convenience over ``mode=external``: instead of declaring a fixed
    ``upstream_url``, the user names the container and the in-container port
    — we resolve the host-side published port via ``docker inspect`` on
    each platform start.

    No lifecycle is registered, so enlace doesn't try to stop or restart
    the container. The user owns its lifetime.
    """

    name = "docker_attached"
    is_supervisable = False
    toml_field_map = {
        "container": "container",
        "port": "port",
    }

    def validate(self, app):
        if not _get(app, "container", None):
            raise ValueError(
                f"App '{app.name}': mode='docker_attached' requires 'container' "
                "(the running container's name)"
            )
        if app.port is None:
            raise ValueError(
                f"App '{app.name}': mode='docker_attached' requires 'port' "
                "(the in-container port to route to)"
            )

    def make_asgi(self, app, platform):
        return _AttachedProxy(
            container=_get(app, "container"),
            container_port=app.port,
            route_prefix=app.route_prefix,
        )

    # make_lifecycle: inherited no-op (None) — strategy is not supervisable.


class _AttachedProxy:
    """ASGI app that resolves an already-running container's host port lazily.

    Resolution happens on the first request rather than at startup so that
    the platform doesn't refuse to boot just because the user hasn't started
    their detached container yet — they can ``docker run`` it any time and
    the next request routes through.
    """

    def __init__(self, *, container: str, container_port: int, route_prefix: str):
        self.container = container
        self.container_port = container_port
        self.route_prefix = route_prefix
        self._proxy = None

    async def __call__(self, scope, receive, send):
        proxy = await self._get_proxy()
        if proxy is None:
            await _send_error(
                send,
                502,
                (
                    f"container {self.container!r} not reachable on port "
                    f"{self.container_port}"
                ).encode(),
            )
            return
        await proxy(scope, receive, send)

    async def _get_proxy(self) -> Optional[object]:
        if self._proxy is not None:
            return self._proxy
        host_port = await _docker.container_published_port(
            self.container, self.container_port
        )
        if host_port is None:
            return None
        from enlace.proxy import make_proxy_app

        self._proxy = make_proxy_app(
            upstream=f"http://127.0.0.1:{host_port}",
            strip_prefix=self.route_prefix,
        )
        return self._proxy


# -- Shared helpers ----------------------------------------------------------


def _app_dir(app) -> Path:
    """Return the app's on-disk directory (best-effort).

    Discovery sets ``source_dir`` to the parent (apps_dir) and the app dir
    is conventionally ``source_dir / name``. Falls back to CWD if neither
    is set — for hand-constructed AppConfigs in tests.
    """
    if app.source_dir is not None:
        return Path(app.source_dir) / app.name
    return Path.cwd()


async def _send_error(send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        }
    )
