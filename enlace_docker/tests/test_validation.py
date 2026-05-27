"""Per-strategy ``validate()`` enforcement of required app.toml fields.

These tests run through ``AppConfig`` construction so they exercise the
same path as discovery: pydantic's model validator delegates to the
strategy registry, the strategy raises ``ValueError``, pydantic wraps it
in ``ValidationError``.
"""

import pytest
from enlace.base import AppConfig
from pydantic import ValidationError


def _make(mode: str, **kwargs):
    base = dict(name="x", route_prefix="/x", app_type="asgi_app", mode=mode)
    base.update(kwargs)
    return AppConfig(**base)


# -- docker mode -------------------------------------------------------------


def test_docker_requires_port():
    with pytest.raises(ValidationError, match="requires 'port'"):
        _make("docker")


def test_docker_minimum_valid():
    app = _make("docker", port=8080)
    assert app.mode == "docker"
    assert app.port == 8080


# -- image mode --------------------------------------------------------------


def test_image_requires_image():
    with pytest.raises(ValidationError, match="requires 'image'"):
        _make("image", port=8080)


def test_image_requires_port():
    with pytest.raises(ValidationError, match="requires 'port'"):
        _make("image", image="alpine:3")


def test_image_minimum_valid():
    app = _make("image", image="ghcr.io/org/app:1.0", port=8080)
    assert app.image == "ghcr.io/org/app:1.0"


# -- compose mode ------------------------------------------------------------


def test_compose_requires_service():
    with pytest.raises(ValidationError, match="requires 'service'"):
        _make("compose", port=8080)


def test_compose_requires_port():
    with pytest.raises(ValidationError, match="requires 'port'"):
        _make("compose", service="web")


def test_compose_minimum_valid():
    app = _make("compose", service="web", port=8080)
    assert app.service == "web"


# -- docker_attached mode ----------------------------------------------------


def test_docker_attached_requires_container():
    with pytest.raises(ValidationError, match="requires 'container'"):
        _make("docker_attached", port=8080)


def test_docker_attached_requires_port():
    with pytest.raises(ValidationError, match="requires 'port'"):
        _make("docker_attached", container="my-running")


def test_docker_attached_minimum_valid():
    app = _make("docker_attached", container="my-running", port=8080)
    assert app.container == "my-running"
