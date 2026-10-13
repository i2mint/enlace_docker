"""A restarted service on a new host port is followed, not 502'd forever (#4).

No real docker: the compose lifecycle is a stand-in object and the attached
container's port comes from the ``fake_docker`` fixture.
"""

from __future__ import annotations

import asyncio

import pytest
from enlace.base import AppConfig

from enlace_docker.strategies import _AttachedProxy, _ComposeProxyProxy

PREFIX = "/api/myapp"


class _Lifecycle:
    def __init__(self, port):
        self.host_port = port


class _SpyClient:
    closed = False

    async def aclose(self):
        self.closed = True


def _compose(port):
    app = AppConfig(
        name="myapp",
        route_prefix=PREFIX,
        app_type="asgi_app",
        mode="compose",
        port=8080,
        service="web",
    )
    lifecycle = _Lifecycle(port)
    app.__dict__["_compose_lifecycle"] = lifecycle
    return _ComposeProxyProxy(app), lifecycle


@pytest.mark.asyncio
async def test_compose_follows_a_new_port_and_closes_the_old_client():
    wrapper, lifecycle = _compose(18080)
    first = await wrapper._current_proxy()
    assert first.upstream.endswith(":18080")
    assert await wrapper._current_proxy() is first  # same port: reused
    first._client = spy = _SpyClient()

    lifecycle.host_port = 18099  # supervised restart, new ephemeral port
    second = await wrapper._current_proxy()
    assert second is not first and second.upstream.endswith(":18099")
    assert spy.closed


@pytest.mark.asyncio
async def test_compose_without_a_port_is_unrouted():
    wrapper, lifecycle = _compose(None)
    assert await wrapper._current_proxy() is None
    lifecycle.host_port = 18080
    assert (await wrapper._current_proxy()).upstream.endswith(":18080")
    lifecycle.host_port = None  # restarting: never route to the stale port
    assert await wrapper._current_proxy() is None


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _attached(clock, **kw):
    return _AttachedProxy(
        container="c", container_port=8080, route_prefix=PREFIX, clock=clock, **kw
    )


@pytest.mark.asyncio
async def test_attached_re_resolves_after_the_ttl_without_blocking(fake_docker):
    fake_docker.published_port_state[("c", 8080)] = 18080
    clock = _Clock()
    proxy = _attached(clock, port_ttl=30)
    first = await proxy._get_proxy()
    assert first.upstream.endswith(":18080")
    first._client = spy = _SpyClient()

    fake_docker.published_port_state[("c", 8080)] = 18099  # restarted elsewhere
    clock.now += 29
    assert await proxy._get_proxy() is first  # within the TTL
    assert proxy._refresh_task is None  # no docker call scheduled
    clock.now += 2
    # Stale: this request is still served by the current proxy, and one
    # background refresh adopts the new port.
    assert await proxy._get_proxy() is first
    await proxy._refresh_task
    second = await proxy._get_proxy()
    assert second is not first and second.upstream.endswith(":18099") and spy.closed
    # The rebuilt proxy keeps the platform's credential filters.
    assert second.cookie_filter is not None
    assert "x-csrf-token" in second._drop_request
    assert "clear-site-data" in second._drop_response


@pytest.mark.asyncio
async def test_attached_same_port_keeps_the_proxy(fake_docker):
    fake_docker.published_port_state[("c", 8080)] = 18080
    clock = _Clock()
    proxy = _attached(clock, port_ttl=30)
    first = await proxy._get_proxy()
    clock.now += 60
    await proxy._get_proxy()
    await proxy._refresh_task
    assert await proxy._get_proxy() is first


@pytest.mark.asyncio
async def test_a_failed_inspect_keeps_the_working_proxy(fake_docker, monkeypatch):
    """Review of #8: docker inspect fails transiently; that must not drop the proxy."""
    from enlace_docker import _docker

    fake_docker.published_port_state[("c", 8080)] = 18080
    clock = _Clock()
    proxy = _attached(clock, port_ttl=30)
    first = await proxy._get_proxy()
    first._client = spy = _SpyClient()
    del fake_docker.published_port_state[("c", 8080)]  # inspect -> None
    clock.now += 31
    assert await proxy._get_proxy() is first
    await proxy._refresh_task
    assert await proxy._get_proxy() is first and not spy.closed

    async def boom(name, port):
        raise RuntimeError("docker daemon hiccup")

    monkeypatch.setattr(_docker, "container_published_port", boom)
    await proxy._get_proxy()
    await proxy._refresh_task  # logged, not raised
    assert await proxy._get_proxy() is first and not spy.closed


def test_the_lock_survives_a_second_event_loop(fake_docker):
    fake_docker.published_port_state[("c", 8080)] = 18080
    proxy = _attached(_Clock())
    assert asyncio.run(proxy._get_proxy()) is not None
    proxy._proxy = None  # force a second, blocking resolution in a new loop
    assert asyncio.run(proxy._get_proxy()) is not None


@pytest.mark.asyncio
async def test_compose_rebuilt_proxy_keeps_the_filters():
    wrapper, lifecycle = _compose(18080)
    await wrapper._current_proxy()
    lifecycle.host_port = 18099
    second = await wrapper._current_proxy()
    assert second.cookie_filter is not None and "x-csrf-token" in second._drop_request


@pytest.mark.asyncio
async def test_concurrent_first_requests_build_one_proxy(fake_docker, monkeypatch):
    from enlace_docker import _docker

    calls = []

    async def slow_port(name, port):
        calls.append(name)
        await asyncio.sleep(0.01)  # an await between check and assignment
        return 18080

    monkeypatch.setattr(_docker, "container_published_port", slow_port)
    proxy = _attached(_Clock())
    results = await asyncio.gather(*(proxy._get_proxy() for _ in range(5)))
    assert len({id(r) for r in results}) == 1
    assert calls == ["c"]
