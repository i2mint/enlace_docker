"""End-to-end strategy tests: AppConfig → make_asgi / make_lifecycle.

These verify that each strategy assembles the right Lifecycle and proxy
from a fully-validated AppConfig, including the extras-flow (``dockerfile``,
``image``, ``service``, ``container``) made possible by enlace 0.1.15's
``model_config = ConfigDict(extra="allow")``.
"""

import pytest
from enlace.base import AppConfig, PlatformConfig

from enlace_docker.lifecycle import (
    ComposeStackLifecycle,
    DockerContainerLifecycle,
)
from enlace_docker.strategies import (
    ComposeStrategy,
    DockerAttachedStrategy,
    DockerStrategy,
    ImageStrategy,
)


def _app(mode: str, **kwargs):
    base = dict(name="myapp", route_prefix="/api/myapp", app_type="asgi_app", mode=mode)
    base.update(kwargs)
    return AppConfig(**base)


# -- DockerStrategy ----------------------------------------------------------


def test_docker_strategy_builds_lifecycle_with_dockerfile(tmp_path):
    app = _app(
        "docker",
        port=8080,
        dockerfile=tmp_path / "Dockerfile",
        context=tmp_path,
        build_args={"X": "y"},
        env={"LOG_LEVEL": "debug"},
        source_dir=tmp_path,
    )
    lifecycle = DockerStrategy().make_lifecycle(app, PlatformConfig())
    assert isinstance(lifecycle, DockerContainerLifecycle)
    assert lifecycle.image == "enlace/myapp:dev"
    assert lifecycle.container_port == 8080
    assert lifecycle.host_port == 8080
    assert lifecycle.dockerfile == tmp_path / "Dockerfile"
    assert lifecycle.build_args == {"X": "y"}
    assert lifecycle.env == {"LOG_LEVEL": "debug"}


def test_docker_strategy_default_dockerfile_path(tmp_path):
    """Default to ``<app_dir>/Dockerfile`` when ``dockerfile`` is omitted."""
    apps_dir = tmp_path
    app_dir = apps_dir / "myapp"
    app_dir.mkdir()
    app = _app("docker", port=8080, source_dir=apps_dir)
    lifecycle = DockerStrategy().make_lifecycle(app, PlatformConfig())
    assert lifecycle.dockerfile == app_dir / "Dockerfile"


def test_docker_strategy_make_asgi_is_proxy_to_local_port():
    app = _app("docker", port=8080)
    proxy = DockerStrategy().make_asgi(app, PlatformConfig())
    assert proxy is not None
    # The enlace proxy stores upstream/strip_prefix on itself; spot-check
    assert getattr(proxy, "upstream", "").endswith(":8080")


# -- ImageStrategy -----------------------------------------------------------


def test_image_strategy_builds_pulling_lifecycle():
    app = _app("image", image="ghcr.io/org/app:1.0", port=8080)
    lifecycle = ImageStrategy().make_lifecycle(app, PlatformConfig())
    assert isinstance(lifecycle, DockerContainerLifecycle)
    assert lifecycle.image == "ghcr.io/org/app:1.0"
    assert lifecycle.pull_before_start is True
    # Image strategy doesn't build, so no dockerfile/context
    assert lifecycle.dockerfile is None
    assert lifecycle.build_context is None


# -- ComposeStrategy ---------------------------------------------------------


def test_compose_strategy_builds_lifecycle(tmp_path):
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services:\n  web:\n    image: alpine\n")
    app = _app(
        "compose",
        service="web",
        port=8080,
        compose_file=compose_file,
        source_dir=tmp_path,
    )
    lifecycle = ComposeStrategy().make_lifecycle(app, PlatformConfig())
    assert isinstance(lifecycle, ComposeStackLifecycle)
    assert lifecycle.compose_file == compose_file
    assert lifecycle.service == "web"
    assert lifecycle.service_port == 8080


def test_compose_strategy_default_compose_file(tmp_path):
    """Default to ``<app_dir>/docker-compose.yml`` when not specified."""
    apps_dir = tmp_path
    app_dir = apps_dir / "myapp"
    app_dir.mkdir()
    app = _app("compose", service="web", port=8080, source_dir=apps_dir)
    lifecycle = ComposeStrategy().make_lifecycle(app, PlatformConfig())
    assert lifecycle.compose_file == app_dir / "docker-compose.yml"


def test_compose_strategy_make_asgi_is_deferred_proxy():
    """ComposeStrategy returns an indirection that resolves at first request,
    because the published host port isn't known until ``compose up`` runs."""
    app = _app("compose", service="web", port=8080)
    proxy = ComposeStrategy().make_asgi(app, PlatformConfig())
    assert proxy is not None
    # Internal sentinel: the wrapper holds an unresolved _proxy until called.
    assert hasattr(proxy, "_resolve_proxy")
    assert proxy._proxy is None


# -- DockerAttachedStrategy --------------------------------------------------


def test_docker_attached_has_no_lifecycle():
    app = _app("docker_attached", container="some-running", port=8080)
    assert DockerAttachedStrategy().make_lifecycle(app, PlatformConfig()) is None


def test_docker_attached_make_asgi_carries_container_name():
    app = _app("docker_attached", container="some-running", port=8080)
    proxy = DockerAttachedStrategy().make_asgi(app, PlatformConfig())
    assert proxy is not None
    assert proxy.container == "some-running"
    assert proxy.container_port == 8080
    assert proxy.route_prefix == "/api/myapp"


# -- exposure checks (loopback-only publishing) --------------------------------


@pytest.mark.asyncio
async def test_attached_container_published_everywhere_is_flagged(fake_docker, capsys):
    from enlace_docker.strategies import _AttachedProxy

    fake_docker.published_port_state[("my-running", 8080)] = 8080
    fake_docker.published_hosts = ["0.0.0.0"]
    proxy = _AttachedProxy(
        container="my-running", container_port=8080, route_prefix="/api/x"
    )
    assert await proxy._get_proxy() is not None
    out = capsys.readouterr().out
    assert "not loopback" in out
    assert "-p 127.0.0.1:8080:8080" in out


@pytest.mark.asyncio
async def test_attached_container_on_loopback_is_quiet(fake_docker, capsys):
    from enlace_docker.strategies import _AttachedProxy

    fake_docker.published_port_state[("my-running", 8080)] = 8080
    proxy = _AttachedProxy(
        container="my-running", container_port=8080, route_prefix="/api/x"
    )
    assert await proxy._get_proxy() is not None
    assert "not loopback" not in capsys.readouterr().out


def test_proxies_connect_to_the_publish_interface():
    """One constant decides where ports are published AND where the gateway
    connects, so the two cannot drift apart."""
    from enlace_docker import _docker
    from enlace_docker.lifecycle import _publish_spec

    assert _publish_spec(8080, 80).startswith(_docker.LOOPBACK + ":")
    assert _docker.is_loopback_host(_docker.LOOPBACK)
