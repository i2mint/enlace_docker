"""Unit tests for ``DockerContainerLifecycle`` using the fake docker CLI."""

import pytest

from enlace_docker.lifecycle import DockerContainerLifecycle


def _lifecycle(**overrides):
    defaults = dict(
        name="myapp",
        image="enlace/myapp:dev",
        container_port=8080,
        host_port=8080,
    )
    defaults.update(overrides)
    return DockerContainerLifecycle(**defaults)


# -- start(): build then run -------------------------------------------------


@pytest.mark.asyncio
async def test_start_with_dockerfile_builds_and_runs(fake_docker, tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM alpine\n")

    lifecycle = _lifecycle(
        dockerfile=dockerfile, build_context=tmp_path
    )

    # Three calls expected in order: build, rm -f leftover, run.
    fake_docker.queue_ok(lambda k, a: k == "docker" and a[0] == "build")
    fake_docker.queue_ok(lambda k, a: k == "docker" and a[:2] == ["rm", "-f"])
    fake_docker.queue_ok(lambda k, a: k == "docker" and a[0] == "run")

    await lifecycle.start()

    kinds_ops = [(k, a[0]) for k, a in fake_docker.calls]
    assert kinds_ops == [
        ("docker", "build"),
        ("docker", "rm"),
        ("docker", "run"),
    ]


@pytest.mark.asyncio
async def test_start_with_image_pulls_instead_of_builds(fake_docker):
    lifecycle = _lifecycle(pull_before_start=True)

    fake_docker.queue_ok(lambda k, a: k == "docker" and a[0] == "pull")
    fake_docker.queue_ok(lambda k, a: k == "docker" and a[:2] == ["rm", "-f"])
    fake_docker.queue_ok(lambda k, a: k == "docker" and a[0] == "run")

    await lifecycle.start()

    assert ("docker", "pull") in [(k, a[0]) for k, a in fake_docker.calls]
    # No build call when pulling.
    assert not any(a[0] == "build" for k, a in fake_docker.calls if k == "docker")


@pytest.mark.asyncio
async def test_start_passes_env_and_port_mapping(fake_docker):
    """``docker run`` argv carries -p, -e ENLACE_MANAGED=1, and user env."""
    captured: dict = {}

    def capture_run(k, a):
        if k == "docker" and a[0] == "run":
            captured["argv"] = a
            return True
        return False

    lifecycle = _lifecycle(env={"LOG_LEVEL": "debug"}, pull_before_start=True)
    fake_docker.queue_ok(lambda k, a: k == "docker" and a[0] == "pull")
    fake_docker.queue_ok(lambda k, a: k == "docker" and a[:2] == ["rm", "-f"])
    fake_docker.queue_ok(capture_run)

    await lifecycle.start()

    argv = captured["argv"]
    assert "8080:8080" in argv  # -p host:container
    # env propagation
    assert "LOG_LEVEL=debug" in argv
    # enlace contract: managed apps see ENLACE_MANAGED=1
    assert "ENLACE_MANAGED=1" in argv
    # image is the final positional
    assert argv[-1] == "enlace/myapp:dev"


# -- wait_healthy() ----------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_healthy_returns_true_when_health_endpoint_reports_healthy(
    fake_docker,
):
    lifecycle = _lifecycle(ready_timeout=2.0)
    # Pretend the container is running and reports healthy on first probe.
    fake_docker.container_running_state["enlace-myapp"] = True
    fake_docker.container_health_state["enlace-myapp"] = "healthy"

    assert await lifecycle.wait_healthy() is True
    assert lifecycle.state == "running"


@pytest.mark.asyncio
async def test_wait_healthy_returns_false_if_container_exited_early(fake_docker):
    lifecycle = _lifecycle(ready_timeout=2.0)
    fake_docker.container_running_state["enlace-myapp"] = False  # exited

    assert await lifecycle.wait_healthy() is False


# -- stop() ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_issues_docker_stop_when_running(fake_docker):
    lifecycle = _lifecycle()
    lifecycle.state = "running"

    fake_docker.queue_ok(lambda k, a: k == "docker" and a[0] == "stop")

    await lifecycle.stop(timeout=5)

    assert any(a[0] == "stop" for k, a in fake_docker.calls if k == "docker")
    assert lifecycle.state == "exited"


@pytest.mark.asyncio
async def test_stop_is_noop_when_already_exited(fake_docker):
    lifecycle = _lifecycle()
    lifecycle.state = "exited"

    await lifecycle.stop()

    assert fake_docker.calls == []


# -- restart accounting ------------------------------------------------------


def test_restart_policy_never_disables_restart():
    lifecycle = _lifecycle(restart_policy="never")
    lifecycle._last_exit_code = 1
    assert lifecycle.should_restart() is False


def test_restart_policy_on_failure_skips_clean_exit():
    lifecycle = _lifecycle(restart_policy="on-failure")
    lifecycle._last_exit_code = 0
    assert lifecycle.should_restart() is False


def test_restart_policy_always_restarts_even_on_clean_exit():
    lifecycle = _lifecycle(restart_policy="always")
    lifecycle._last_exit_code = 0
    assert lifecycle.should_restart() is True


def test_max_retries_stops_restart():
    lifecycle = _lifecycle(max_retries=2)
    lifecycle._last_exit_code = 1
    lifecycle._consecutive_failures = 2
    assert lifecycle.should_restart() is False


def test_backoff_grows_exponentially():
    lifecycle = _lifecycle(restart_delay_ms=100)
    lifecycle._consecutive_failures = 0
    assert lifecycle.backoff_delay() == pytest.approx(0.1)
    lifecycle._consecutive_failures = 2
    assert lifecycle.backoff_delay() == pytest.approx(0.1 * 1.5**2)
