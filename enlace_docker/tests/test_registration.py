"""Entry-point registration: installing the package wires up the four modes.

These tests are the canonical proof that ``pip install enlace_docker``
is sufficient — no user code change. They hit the real enlace registry.
"""

from enlace.strategies import get_strategy, known_modes

from enlace_docker.strategies import (
    ComposeStrategy,
    DockerAttachedStrategy,
    DockerStrategy,
    ImageStrategy,
)


def test_four_modes_become_known():
    modes = known_modes()
    for name in ("docker", "image", "compose", "docker_attached"):
        assert name in modes, f"mode {name!r} missing from registry"


def test_each_mode_resolves_to_expected_class():
    assert isinstance(get_strategy("docker"), DockerStrategy)
    assert isinstance(get_strategy("image"), ImageStrategy)
    assert isinstance(get_strategy("compose"), ComposeStrategy)
    assert isinstance(get_strategy("docker_attached"), DockerAttachedStrategy)


def test_only_supervisable_modes_have_lifecycles():
    """``docker_attached`` is a routing-only convenience; the others are supervised."""
    assert DockerStrategy.is_supervisable is True
    assert ImageStrategy.is_supervisable is True
    assert ComposeStrategy.is_supervisable is True
    assert DockerAttachedStrategy.is_supervisable is False


def test_all_modes_skip_python_introspection():
    """None of these modes import the app's Python — they may not even be Python."""
    for name in ("docker", "image", "compose", "docker_attached"):
        assert get_strategy(name).skip_python_introspection is True
