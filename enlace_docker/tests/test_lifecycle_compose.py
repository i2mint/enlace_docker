"""Unit tests for ``ComposeStackLifecycle`` using the fake docker CLI."""

from pathlib import Path

import pytest

from enlace_docker.lifecycle import ComposeStackLifecycle


def _lifecycle(**overrides):
    defaults = dict(
        name="myapp",
        compose_file=Path("docker-compose.yml"),
        service="web",
        service_port=8080,
    )
    defaults.update(overrides)
    return ComposeStackLifecycle(**defaults)


# -- start() -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_runs_compose_up_then_resolves_port(fake_docker):
    lifecycle = _lifecycle()
    fake_docker.queue_ok(
        lambda k, a: k == "compose" and a[: 1] == ["-f"] and "up" in a
    )
    # Pretend compose published the service on a high host port.
    fake_docker.published_port_state[("web", 8080)] = 54321

    await lifecycle.start()

    assert lifecycle.host_port == 54321
    assert lifecycle.state == "starting"


@pytest.mark.asyncio
async def test_start_uses_project_namespace(fake_docker):
    """``-p enlace-<app>`` keeps us out of the user's own compose stacks."""
    captured: dict = {}

    def capture(k, a):
        if k == "compose" and "up" in a:
            captured["argv"] = a
            return True
        return False

    lifecycle = _lifecycle()
    fake_docker.queue_ok(capture)
    fake_docker.published_port_state[("web", 8080)] = 54321

    await lifecycle.start()

    argv = captured["argv"]
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "enlace-myapp"


@pytest.mark.asyncio
async def test_start_warns_on_missing_published_port(fake_docker, capsys):
    lifecycle = _lifecycle()
    fake_docker.queue_ok(lambda k, a: k == "compose" and "up" in a)
    # Don't populate published_port_state — resolution fails.

    await lifecycle.start()

    captured = capsys.readouterr()
    assert "no published port" in captured.out
    assert lifecycle.host_port is None


# -- stop() ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_runs_compose_down(fake_docker):
    lifecycle = _lifecycle()
    lifecycle.state = "running"

    fake_docker.queue_ok(lambda k, a: k == "compose" and "down" in a)

    await lifecycle.stop(timeout=5)

    assert any("down" in a for k, a in fake_docker.calls if k == "compose")
    assert lifecycle.state == "exited"
