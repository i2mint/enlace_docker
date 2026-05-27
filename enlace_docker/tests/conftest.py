"""Test fixtures for enlace_docker.

The unit tests don't need a real docker daemon — they exercise the
strategy/lifecycle code by monkey-patching the ``enlace_docker._docker``
async runners with a ``FakeDockerCLI`` that records calls and returns
scripted results. Integration tests (gated on ``DOCKER_AVAILABLE``) run
against a real docker.
"""

from __future__ import annotations

import os
import shutil
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Optional

import pytest

from enlace_docker import _docker

# -- FakeDockerCLI -----------------------------------------------------------


@dataclass
class FakeDockerCLI:
    """In-process stand-in for the docker CLI.

    Tests append ``(predicate, responder)`` rules; the next docker / docker
    compose call is matched against rules in order. Unmatched calls raise
    so we never silently exercise unintended code paths.
    """

    calls: list[tuple[str, list[str]]] = field(default_factory=list)
    _rules: Deque[
        tuple[Callable[[str, list[str]], bool], Callable[[], _docker.CommandResult]]
    ] = field(default_factory=deque)
    # State for inspect-style queries that the lifecycle polls.
    container_running_state: dict[str, bool] = field(default_factory=dict)
    container_health_state: dict[str, Optional[str]] = field(default_factory=dict)
    container_exit_code_state: dict[str, Optional[int]] = field(default_factory=dict)
    container_exists_state: dict[str, bool] = field(default_factory=dict)
    published_port_state: dict[tuple[str, int], Optional[int]] = field(
        default_factory=dict
    )

    def queue(
        self,
        predicate: Callable[[str, list[str]], bool],
        result: _docker.CommandResult,
    ) -> None:
        """Queue a (one-shot) rule. First matching call consumes it."""
        self._rules.append((predicate, lambda r=result: r))

    def queue_ok(
        self, predicate: Callable[[str, list[str]], bool], stdout: str = ""
    ) -> None:
        self.queue(
            predicate,
            _docker.CommandResult(returncode=0, stdout=stdout, stderr=""),
        )

    def queue_fail(
        self, predicate: Callable[[str, list[str]], bool], stderr: str = "boom"
    ) -> None:
        self.queue(
            predicate,
            _docker.CommandResult(returncode=1, stdout="", stderr=stderr),
        )

    # ---- runner replacements -------------------------------------------------

    async def run_docker(self, *args, check: bool = True, env=None):
        self.calls.append(("docker", list(args)))
        result = self._resolve("docker", list(args))
        if check and not result.ok:
            raise _docker.DockerCLIError(("docker",) + args, result)
        return result

    async def run_docker_compose(self, *args, check: bool = True, env=None):
        self.calls.append(("compose", list(args)))
        result = self._resolve("compose", list(args))
        if check and not result.ok:
            raise _docker.DockerCLIError(("docker", "compose") + args, result)
        return result

    def _resolve(self, kind: str, args: list[str]) -> _docker.CommandResult:
        for i, (pred, responder) in enumerate(self._rules):
            if pred(kind, args):
                del self._rules[i]
                return responder()
        raise AssertionError(
            f"FakeDockerCLI: no rule matched {kind!r} call args={args!r}; "
            f"queued rules left: {len(self._rules)}"
        )

    # ---- inspect-helper replacements ----------------------------------------

    async def container_running(self, name: str) -> bool:
        return self.container_running_state.get(name, False)

    async def container_exists(self, name: str) -> bool:
        return self.container_exists_state.get(name, True)

    async def container_health(self, name: str) -> Optional[str]:
        return self.container_health_state.get(name)

    async def container_exit_code(self, name: str) -> Optional[int]:
        return self.container_exit_code_state.get(name)

    async def container_published_port(
        self, name: str, container_port: int
    ) -> Optional[int]:
        return self.published_port_state.get((name, container_port))

    async def compose_published_port(
        self, project: str, compose_file: str, service: str, service_port: int
    ) -> Optional[int]:
        return self.published_port_state.get((service, service_port))


# -- pytest fixtures ---------------------------------------------------------


@pytest.fixture
def fake_docker(monkeypatch):
    """Replace every shell-out seam in ``enlace_docker._docker`` with the fake.

    The fake records calls and resolves rules pushed by the test; unmatched
    docker invocations fail loudly so we never silently exercise the real
    daemon.
    """
    fake = FakeDockerCLI()
    monkeypatch.setattr(_docker, "run_docker", fake.run_docker)
    monkeypatch.setattr(_docker, "run_docker_compose", fake.run_docker_compose)
    monkeypatch.setattr(_docker, "container_running", fake.container_running)
    monkeypatch.setattr(_docker, "container_exists", fake.container_exists)
    monkeypatch.setattr(_docker, "container_health", fake.container_health)
    monkeypatch.setattr(_docker, "container_exit_code", fake.container_exit_code)
    monkeypatch.setattr(
        _docker, "container_published_port", fake.container_published_port
    )
    monkeypatch.setattr(
        _docker, "compose_published_port", fake.compose_published_port
    )
    return fake


@pytest.fixture
def docker_available() -> bool:
    """Whether a real ``docker`` CLI is on PATH (gates integration tests)."""
    return (
        shutil.which("docker") is not None
        and os.environ.get("DOCKER_AVAILABLE", "").lower() in ("1", "true", "yes")
    )


# Surface a clean async event loop policy for asyncio_mode = "auto".
@pytest.fixture(scope="session")
def anyio_backend() -> str:  # pragma: no cover — pytest-asyncio compat shim
    return "asyncio"
