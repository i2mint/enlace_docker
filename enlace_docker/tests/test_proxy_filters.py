"""Every docker mode keeps the platform's credentials inside the platform.

A container is a separate program: by default it must not see the visitor's
platform session/CSRF cookies or ``X-CSRF-Token``, and it may never set a
platform cookie or send ``Clear-Site-Data`` / ``Service-Worker-Allowed`` on the
platform's origin. Same policy as enlace's ``mode="process"`` (enlace#60).

No docker daemon is touched: upstream traffic goes to an ``httpx.MockTransport``
and ``docker_attached`` port resolution to the ``fake_docker`` fixture.
"""

from __future__ import annotations

import httpx
import pytest
from enlace.base import AppConfig, PlatformConfig
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from enlace_docker.strategies import (
    ComposeStrategy,
    DockerAttachedStrategy,
    DockerStrategy,
    ImageStrategy,
    _AttachedProxy,
    _ComposeProxyProxy,
)

PREFIX = "/api/myapp"

VISITOR_COOKIES = (
    "enlace_session=SECRET-SESSION; enlace_csrf=SECRET-CSRF; "
    "enlace_oauth_state=SECRET-STATE; shared_auth_vault=SECRET-SHARED; "
    "app_pref=light"
)


def _echo_transport(seen: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie")
        seen["x-csrf-token"] = request.headers.get("x-csrf-token")
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            headers=[
                ("set-cookie", "enlace_session=forged; Path=/"),
                ("set-cookie", "shared_auth_vault=forged; Path=/"),
                ("set-cookie", "=enlace_csrf=forged; Path=/"),
                ("set-cookie", "app_pref=dark; Path=/api/myapp"),
                ("clear-site-data", '"cookies"'),
                ("service-worker-allowed", "/"),
                ("x-upstream", "kept"),
            ],
            json={"ok": True},
        )

    return httpx.MockTransport(handler)


def _mock_upstream(proxy, seen: dict) -> None:
    """Route the (already built) inner ``_HttpxProxy`` to the echo transport."""

    async def _get_client():
        return httpx.AsyncClient(transport=_echo_transport(seen))

    proxy._get_client = _get_client


class _MockingWrapper:
    """For the lazy compose/attached wrappers: mock the inner proxy once built."""

    def __init__(self, wrapper, seen: dict):
        self.wrapper = wrapper
        self.seen = seen

    async def __call__(self, scope, receive, send):
        if isinstance(self.wrapper, _ComposeProxyProxy):
            if self.wrapper._proxy is None:
                self.wrapper._proxy = self.wrapper._resolve_proxy()
                _mock_upstream(self.wrapper._proxy, self.seen)
        else:
            if self.wrapper._proxy is None:
                _mock_upstream(await self.wrapper._get_proxy(), self.seen)
        await self.wrapper(scope, receive, send)


def _get(asgi):
    client = TestClient(Starlette(routes=[Mount(PREFIX, app=asgi)]))
    return client.get(
        f"{PREFIX}/x",
        headers={
            "Cookie": VISITOR_COOKIES,
            "X-CSRF-Token": "SECRET-CSRF",
            "Authorization": "Bearer app-own-token",
        },
    )


class _ResolvedCompose:
    host_port = 18080


def _app(mode: str, **extra) -> AppConfig:
    fields = dict(
        name="myapp", route_prefix=PREFIX, app_type="asgi_app", mode=mode, port=8080
    )
    fields.update(
        {
            "docker": {},
            "image": {"image": "ghcr.io/org/app:1"},
            "compose": {"service": "web"},
            "docker_attached": {"container": "my-running"},
        }[mode]
    )
    return AppConfig(**{**fields, **extra})


STRATEGIES = {
    "docker": DockerStrategy,
    "image": ImageStrategy,
    "compose": ComposeStrategy,
    "docker_attached": DockerAttachedStrategy,
}
MODES = list(STRATEGIES)


def _through(mode: str, seen: dict, fake_docker, *, platform=None, **extra):
    platform = platform or PlatformConfig()
    app = _app(mode, **extra)
    strategy = STRATEGIES[mode]()
    asgi = strategy.make_asgi(app, platform)
    if mode == "compose":
        app.__dict__["_compose_lifecycle"] = _ResolvedCompose()
        asgi = _MockingWrapper(asgi, seen)
    elif mode == "docker_attached":
        fake_docker.published_port_state[("my-running", 8080)] = 18080
        asgi = _MockingWrapper(asgi, seen)
    else:
        _mock_upstream(asgi, seen)
    return _get(asgi)


def _assert_response_filtered(r) -> None:
    assert r.status_code == 200
    assert r.headers.get_list("set-cookie") == ["app_pref=dark; Path=/api/myapp"]
    assert "clear-site-data" not in r.headers
    assert "service-worker-allowed" not in r.headers
    assert r.headers["x-upstream"] == "kept"


@pytest.mark.parametrize("mode", MODES)
def test_platform_credentials_are_withheld_by_default(mode, fake_docker):
    seen: dict = {}
    r = _through(mode, seen, fake_docker)
    assert seen["cookie"] == "app_pref=light"
    assert seen["x-csrf-token"] is None
    assert seen["authorization"] == "Bearer app-own-token"
    _assert_response_filtered(r)


@pytest.mark.parametrize("mode", MODES)
def test_opt_in_forwards_credentials_but_never_lets_them_be_set(mode, fake_docker):
    """``forward_platform_credentials`` is a read grant, not a write grant."""
    seen: dict = {}
    r = _through(mode, seen, fake_docker, forward_platform_credentials=True)
    assert seen["cookie"] == VISITOR_COOKIES
    assert seen["x-csrf-token"] == "SECRET-CSRF"
    _assert_response_filtered(r)


@pytest.mark.parametrize("mode", MODES)
def test_custom_session_cookie_name_is_withheld(mode):
    platform = PlatformConfig(auth={"session_cookie_name": "my_sess"})
    asgi = STRATEGIES[mode]().make_asgi(_app(mode), platform)
    filters = getattr(asgi, "_filters", None)  # the lazy compose/attached wrappers
    keep = filters["cookie_filter"] if filters else asgi.cookie_filter
    keep_set = filters["set_cookie_filter"] if filters else asgi.set_cookie_filter
    assert not keep("my_sess") and not keep_set("my_sess")
    assert keep("app_pref") and keep_set("app_pref")


@pytest.mark.parametrize("wrapper", [_ComposeProxyProxy, _AttachedProxy])
def test_lazy_wrappers_never_default_to_an_unfiltered_proxy(wrapper):
    if wrapper is _ComposeProxyProxy:
        w = wrapper(_app("compose"))
    else:
        w = wrapper(container="c", container_port=8080, route_prefix=PREFIX)
    assert not w._filters["cookie_filter"]("enlace_session")
    assert not w._filters["set_cookie_filter"]("enlace_session")
    assert "x-csrf-token" in w._filters["drop_request_headers"]
    assert "clear-site-data" in w._filters["drop_response_headers"]


@pytest.mark.parametrize("mode", MODES)
def test_opt_in_with_custom_session_cookie_still_blocks_setting_it(mode):
    platform = PlatformConfig(auth={"session_cookie_name": "my_sess"})
    app = _app(mode, forward_platform_credentials=True)
    asgi = STRATEGIES[mode]().make_asgi(app, platform)
    filters = getattr(asgi, "_filters", None)
    keep = filters["cookie_filter"] if filters else asgi.cookie_filter
    keep_set = filters["set_cookie_filter"] if filters else asgi.set_cookie_filter
    assert keep is None  # read grant: every cookie forwarded
    assert not keep_set("my_sess") and not keep_set("enlace_session")


@pytest.mark.parametrize("opt_in", [False, True])
def test_app_toml_opt_in_reaches_a_docker_app(tmp_path, opt_in):
    """The flat app.toml key (no ``[app]`` table) lands on a docker-mode app."""
    from enlace.discover import discover_apps

    app_dir = tmp_path / "apps" / "myservice"
    app_dir.mkdir(parents=True)
    (app_dir / "Dockerfile").write_text("FROM alpine\n")
    toml = 'mode = "docker"\nport = 8080\n'
    if opt_in:
        toml += "forward_platform_credentials = true\n"
    (app_dir / "app.toml").write_text(toml)

    platform = PlatformConfig(apps_dirs=[tmp_path / "apps"])
    (app,) = discover_apps(platform).apps
    assert app.forward_platform_credentials is opt_in
    proxy = DockerStrategy().make_asgi(app, platform)
    assert (proxy.cookie_filter is None) is opt_in
