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
there is no docker-specific proxy code in this package — with the same
platform-credential filters as enlace's ``mode="process"`` (enlace's public
``enlace.proxy.platform_proxy_filters``, so the policy has one owner).

Plugin-specific TOML keys (``dockerfile``, ``image``, ``compose_file``,
``service``, ``container``, ``build_args``) flow onto ``AppConfig`` via
``model_config = ConfigDict(extra="allow")`` (added in enlace 0.1.15) and
are read here via attribute access.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Optional

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


def _proxy_filters(*, platform=None, forward: bool = False) -> dict[str, Any]:
    """``make_proxy_app`` filter kwargs keeping platform credentials in the platform.

    Delegates to ``enlace.proxy.platform_proxy_filters``, the same policy as
    enlace's ``mode="process"``: a container gets the visitor's platform
    cookies and ``X-CSRF-Token`` only when its app.toml sets
    ``forward_platform_credentials = true`` (*forward*), and may never set a
    platform cookie or send origin-wide headers, whatever it asks for.
    """
    from enlace.proxy import platform_proxy_filters

    return platform_proxy_filters(platform=platform, forward_credentials=forward)


def _app_proxy_filters(app, platform) -> dict[str, Any]:
    """``_proxy_filters`` for ``app``, honouring its credential opt-in."""
    forward = bool(_get(app, "forward_platform_credentials", False))
    return _proxy_filters(platform=platform, forward=forward)


def _proxy(app, platform):
    """Build the standard reverse-proxy app for ``app`` at its route prefix."""
    from enlace.proxy import make_proxy_app

    upstream = f"http://{_docker.LOOPBACK}:{app.port}"
    return make_proxy_app(
        upstream=upstream,
        strip_prefix=app.route_prefix,
        **_app_proxy_filters(app, platform),
    )


# -- DockerStrategy (mode=docker) --------------------------------------------


class DockerStrategy(BackendStrategy):
    """Build the app's Dockerfile, run the container, supervise, proxy.

    Conventions:

    - Image tag is ``enlace/<app>:dev`` (local, never pushed).
    - Container is named ``enlace-<app>``.
    - ``port`` is the in-container port; we publish it to the same port on
      the host's loopback interface (matching how the Dockerfile's ``EXPOSE``
      directive reads), so only the gateway can reach it.
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
        "forward_platform_credentials": "forward_platform_credentials",
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
        return _proxy(app, platform)

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
        "forward_platform_credentials": "forward_platform_credentials",
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
        return _proxy(app, platform)

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
        "forward_platform_credentials": "forward_platform_credentials",
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
        return _ComposeProxyProxy(app, filters=_app_proxy_filters(app, platform))

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
    """Indirection ASGI app: routes to the compose service's *current* host port.

    The host port assigned to a compose service isn't known until
    ``docker compose up -d`` runs — but ``make_asgi`` is called *before*
    ``make_lifecycle``. And the lifecycle re-resolves it on every start, so
    after a supervised restart it can be a different ephemeral port. This
    wrapper therefore reads ``lifecycle.host_port`` on every request and
    rebuilds its inner proxy (closing the old one's client) whenever the port
    changed, instead of caching the first one forever (enlace_docker#4).
    """

    def __init__(self, app, *, filters: Optional[dict] = None):
        self._app_config = app
        # Never default to an unfiltered proxy: no filters means the defaults.
        self._filters = _proxy_filters() if filters is None else filters
        self._proxy = None
        self._port: Optional[int] = None

    async def __call__(self, scope, receive, send):
        proxy = await self._current_proxy()
        if proxy is None:
            await _send_error(send, 502, b"compose service not yet healthy")
            return
        await proxy(scope, receive, send)

    async def _current_proxy(self):
        """The proxy for the lifecycle's current port, or None while it has none.

        No ``await`` between reading the port and installing the proxy, so
        concurrent requests cannot each build one.
        """
        lifecycle = self._app_config.__dict__.get("_compose_lifecycle")
        port = None if lifecycle is None else lifecycle.host_port
        if port is None:
            return None
        if self._proxy is None or port != self._port:
            old, self._proxy, self._port = self._proxy, self._build(port), port
            await _close_proxy(old)
        return self._proxy

    def _build(self, port: int):
        from enlace.proxy import make_proxy_app

        return make_proxy_app(
            upstream=f"http://{_docker.LOOPBACK}:{port}",
            strip_prefix=self._app_config.route_prefix,
            **self._filters,
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
        "forward_platform_credentials": "forward_platform_credentials",
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
            filters=_app_proxy_filters(app, platform),
        )

    # make_lifecycle: inherited no-op (None) — strategy is not supervisable.


#: How long (seconds) a ``docker_attached`` container's resolved host port is
#: trusted before the next request asks docker again. The user restarts that
#: container out of band, possibly on a new port; nothing tells the gateway.
DFLT_ATTACHED_PORT_TTL_S = 30.0


class _AttachedProxy:
    """ASGI app that resolves an already-running container's host port lazily.

    Resolution happens on the first request rather than at startup so that
    the platform doesn't refuse to boot just because the user hasn't started
    their detached container yet — they can ``docker run`` it any time and
    the next request routes through.

    The port is re-resolved at most every ``port_ttl`` seconds, so a
    container restarted on a different host port is followed within that
    window instead of 502ing until the platform restarts (enlace_docker#4).
    Only the very first resolution blocks a request; after that a stale port
    is refreshed by one background task while requests keep using the current
    proxy (stale-while-revalidate), so a slow docker daemon never queues every
    request behind it. A resolution that finds no port keeps the existing
    proxy: ``docker inspect`` also fails transiently, and a container that is
    really gone already fails at connect time.
    """

    def __init__(
        self,
        *,
        container: str,
        container_port: int,
        route_prefix: str,
        filters: Optional[dict] = None,
        port_ttl: float = DFLT_ATTACHED_PORT_TTL_S,
        clock=time.monotonic,
    ):
        self.container = container
        self.container_port = container_port
        self.route_prefix = route_prefix
        # Never default to an unfiltered proxy: no filters means the defaults.
        self._filters = _proxy_filters() if filters is None else filters
        self.port_ttl = port_ttl
        self._clock = clock
        self._proxy = None
        self._port: Optional[int] = None
        self._resolved_at: Optional[float] = None
        # Created on first use, inside the running loop (a Lock made in
        # __init__ binds to whichever loop first contends it).
        self._lock: Optional[asyncio.Lock] = None
        self._refresh_task: Optional[asyncio.Task] = None

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

    def _stale(self) -> bool:
        return (
            self._resolved_at is None
            or self._clock() - self._resolved_at >= self.port_ttl
        )

    async def _get_proxy(self) -> Optional[object]:
        if self._proxy is not None:
            if self._stale() and (
                self._refresh_task is None or self._refresh_task.done()
            ):
                self._refresh_task = asyncio.create_task(self._refresh())
            return self._proxy
        await self._refresh()
        return self._proxy

    async def _refresh(self) -> None:
        """Resolve the port once (serialised) and adopt it if it is new."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._proxy is not None and not self._stale():
                return  # another caller refreshed while we waited
            try:
                host_port = await _docker.container_published_port(
                    self.container, self.container_port
                )
            except Exception as error:  # a background task must not die silently
                print(
                    f"[enlace_docker] could not resolve {self.container!r}'s "
                    f"port: {error!r}",
                    flush=True,
                )
                return
            if host_port is None:
                return  # keep what we have; retried on the next stale request
            self._resolved_at = self._clock()
            if host_port == self._port and self._proxy is not None:
                return
            await self._warn_if_exposed(host_port)
            from enlace.proxy import make_proxy_app

            old = self._proxy
            self._proxy = make_proxy_app(
                upstream=f"http://{_docker.LOOPBACK}:{host_port}",
                strip_prefix=self.route_prefix,
                **self._filters,
            )
            self._port = host_port
            await _close_proxy(old)

    async def _warn_if_exposed(self, host_port: int) -> None:
        exposed = [
            ip
            for ip in await _docker.container_published_host_ips(
                self.container, self.container_port
            )
            if not _docker.is_loopback_host(ip)
        ]
        if exposed:
            print(
                f"[enlace_docker] WARNING: container {self.container!r} publishes "
                f"port {self.container_port} on {', '.join(exposed)} (not "
                "loopback), so it is reachable directly, around the gateway's "
                f"auth. Start it with -p {_docker.LOOPBACK}:{host_port}:"
                f"{self.container_port}.",
                flush=True,
            )


# -- Shared helpers ----------------------------------------------------------


async def _close_proxy(proxy) -> None:
    """Close a replaced inner proxy's httpx client, if it ever opened one.

    ``enlace.proxy._HttpxProxy`` opens its client lazily and has no public
    close, so this reads the attribute defensively: a proxy without one (or
    one never used) has nothing to close.
    """
    client = getattr(proxy, "_client", None)
    if client is None:
        return
    try:
        await client.aclose()
    except Exception as error:  # the new proxy is installed; don't fail the request
        print(f"[enlace_docker] closing a replaced proxy failed: {error!r}", flush=True)


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
