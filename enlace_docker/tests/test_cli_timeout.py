"""A hung docker CLI call is cut off, not awaited forever (#9).

No docker is involved: ``_run`` is pointed at a Python child that sleeps (the
stand-in for a daemon that never answers), and ``run_docker`` is checked by
replacing ``_run`` with a recorder.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from enlace_docker import _docker
from enlace_docker.lifecycle import ComposeStackLifecycle, DockerContainerLifecycle

SLOW = (sys.executable, "-c", "import time; time.sleep(60)")

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="process groups and signal-0 probes are POSIX"
)


def _parent_of_a_sleeper(pidfile, *, own_session):
    """A child that spawns a sleeping grandchild sharing its pipes, then hangs.

    The stand-in for ``docker compose``: the CLI runs the compose plugin as its
    child, and both inherit the pipes ``_run`` reads.
    """
    code = (
        "import subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        f" start_new_session={own_session!r})\n"
        f"open({str(pidfile)!r}, 'w').write(str(g.pid))\n"
        "time.sleep(60)\n"
    )
    return (sys.executable, "-c", code)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_a_call_past_its_timeout_is_a_failed_result_not_a_hang():
    t0 = time.monotonic()
    result = await _docker._run(*SLOW, check=False, env=None, timeout=0.3)
    assert time.monotonic() - t0 < 10
    assert not result.ok and result.returncode == _docker.TIMEOUT_RETURNCODE
    assert "timed out after 0.3s" in result.stderr


async def test_with_check_a_timeout_raises_the_cli_error_not_timeouterror():
    with pytest.raises(_docker.DockerCLIError, match="timed out"):
        await _docker._run(*SLOW, check=True, env=None, timeout=0.3)


@posix_only
async def test_the_timed_out_child_is_killed(monkeypatch):
    spawned = []
    real = asyncio.create_subprocess_exec

    async def spy(*a, **k):
        proc = await real(*a, **k)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(_docker.asyncio, "create_subprocess_exec", spy)
    await _docker._run(*SLOW, check=False, env=None, timeout=0.3)
    (proc,) = spawned
    assert proc.returncode is not None  # reaped
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)


async def test_a_cancelled_call_does_not_leave_its_child_running(monkeypatch):
    spawned = []
    real = asyncio.create_subprocess_exec

    async def spy(*a, **k):
        proc = await real(*a, **k)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(_docker.asyncio, "create_subprocess_exec", spy)
    task = asyncio.create_task(_docker._run(*SLOW, check=False, env=None, timeout=None))
    while not spawned:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert spawned[0].returncode is not None


@posix_only
async def test_a_grandchild_holding_the_pipes_is_killed_with_it(tmp_path):
    """Review of #10: killing only ``docker`` left its plugin holding the pipes."""
    pidfile = tmp_path / "grandchild.pid"
    t0 = time.monotonic()
    result = await asyncio.wait_for(
        _docker._run(
            *_parent_of_a_sleeper(pidfile, own_session=False),
            check=False, env=None, timeout=3.0,
        ),
        timeout=20,
    )
    assert result.returncode == _docker.TIMEOUT_RETURNCODE
    assert time.monotonic() - t0 < 12
    grandchild = int(pidfile.read_text())
    for _ in range(50):  # reaped by init, not by us: give it a moment
        if not _alive(grandchild):
            break
        await asyncio.sleep(0.1)
    assert not _alive(grandchild)


@posix_only
async def test_a_descendant_that_left_the_group_cannot_hang_the_call(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(_docker, "KILL_GRACE", 0.5)
    pidfile = tmp_path / "grandchild.pid"
    try:
        result = await asyncio.wait_for(
            _docker._run(
                *_parent_of_a_sleeper(pidfile, own_session=True),
                check=False, env=None, timeout=3.0,
            ),
            timeout=20,
        )
        assert result.returncode == _docker.TIMEOUT_RETURNCODE
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 9)
            except ProcessLookupError:
                pass


async def test_a_fast_call_is_unaffected():
    result = await _docker._run(
        sys.executable, "-c", "print('hi')", check=True, env=None, timeout=10
    )
    assert result.ok and result.stdout.strip() == "hi"


@pytest.fixture
def recorded_run(monkeypatch):
    calls = []

    async def fake_run(*argv, check, env, timeout=_docker.DFLT_TIMEOUT):
        calls.append((argv, timeout))
        return _docker.CommandResult(0, "", "")

    monkeypatch.setattr(_docker, "_run", fake_run)
    return calls


async def test_runners_default_to_the_short_timeout(recorded_run):
    await _docker.run_docker("ps")
    await _docker.run_docker_compose("port", "web", "80")
    assert [t for _, t in recorded_run] == [_docker.DFLT_TIMEOUT] * 2


async def test_a_probe_on_a_hung_daemon_degrades_to_unknown(monkeypatch):
    async def hung(*argv, check, env, timeout=_docker.DFLT_TIMEOUT):
        return _docker.CommandResult(_docker.TIMEOUT_RETURNCODE, "", "timed out")

    monkeypatch.setattr(_docker, "_run", hung)
    assert await _docker.container_health("enlace-x") is None
    assert await _docker.container_running("enlace-x") is False
    assert await _docker.compose_published_addresses("p", "f.yml", "web", 80) == []


async def test_long_calls_get_a_longer_ceiling(fake_docker, monkeypatch, tmp_path):
    """pull may take minutes; stop/down get their own grace period on top."""
    timeouts = {}

    def recording(kind):
        inner = getattr(fake_docker, kind)

        async def wrapper(*args, timeout=_docker.DFLT_TIMEOUT, **k):
            verb = next(a for a in args if a in ("pull", "stop", "down"))
            timeouts[verb] = timeout
            return await inner(*args, **k)

        return wrapper

    for kind in ("run_docker", "run_docker_compose"):
        monkeypatch.setattr(_docker, kind, recording(kind))

    fake_docker.queue_ok(lambda k, a: a[:1] == ["pull"])
    life = DockerContainerLifecycle(name="x", image="img:1", host_port=1, container_port=2)
    await life._pull_image()
    fake_docker.queue_ok(lambda k, a: a[:1] == ["stop"])
    life.state = "running"
    await life.stop(timeout=5)

    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    stack = ComposeStackLifecycle(name="s", compose_file=compose, service="web", service_port=80)
    fake_docker.queue_ok(lambda k, a: k == "compose" and "down" in a)
    stack.state = "running"
    await stack.stop(timeout=5)

    assert timeouts == {
        "pull": _docker.BUILD_TIMEOUT,
        "stop": 5 + _docker.DFLT_TIMEOUT,
        "down": 5 + _docker.DFLT_TIMEOUT,
    }
