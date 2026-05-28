"""Docker-aware diagnosers for ``enlace diagnose``.

Registered via the ``enlace.diagnosers`` entry-point group (see
``pyproject.toml``), so ``enlace diagnose <app>`` automatically surfaces
docker-specific issues once ``enlace_docker`` is installed — no enlace core
change.

The single entry point ``docker_diagnoser(app_dir, report)`` reads the app's
``app.toml`` to decide which checks apply (``mode = docker|image|compose``)
and appends ``Issue`` objects with plugin-defined string categories.

Checks are intentionally text-based (no YAML/Docker SDK dependency): they
catch the common, high-signal mistakes without parsing the full Dockerfile
or compose grammar.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from enlace.diagnose import DiagnosticReport, Issue, Severity

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 fallback
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

# Plugin-defined issue categories (free-form strings; enlace renders them).
CAT_MISSING_DOCKERFILE = "docker_missing_dockerfile"
CAT_NO_HEALTHCHECK = "docker_no_healthcheck"
CAT_EXPOSE_MISMATCH = "docker_expose_mismatch"
CAT_MISSING_COMPOSE = "docker_missing_compose_file"
CAT_COMPOSE_SERVICE = "docker_compose_service"

_DOCKER_MODES = {"docker", "image", "compose"}


def _load_app_toml(app_dir: Path) -> dict:
    """Read the app's ``app.toml`` (returns ``{}`` if absent or unparseable)."""
    path = app_dir / "app.toml"
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def docker_diagnoser(app_dir: Path, report: DiagnosticReport) -> None:
    """Append docker-related issues for an app, if it declares a docker mode.

    No-op for non-docker apps so it's safe to run against every app dir.
    """
    app_dir = Path(app_dir)
    toml_data = _load_app_toml(app_dir)
    mode = toml_data.get("mode")
    if mode not in _DOCKER_MODES:
        return

    if mode == "docker":
        _check_dockerfile(app_dir, toml_data, report)
    elif mode == "compose":
        _check_compose(app_dir, toml_data, report)
    # mode == "image": nothing on disk to scan (image is remote); validation
    # already enforces image + port, so there's no filesystem check to add.


# -- docker mode --------------------------------------------------------------


def _check_dockerfile(app_dir: Path, toml_data: dict, report: DiagnosticReport) -> None:
    dockerfile_name = toml_data.get("dockerfile", "Dockerfile")
    dockerfile = app_dir / dockerfile_name

    if not dockerfile.is_file():
        report.issues.append(
            Issue(
                severity=Severity.CRITICAL,
                category=CAT_MISSING_DOCKERFILE,
                summary=f"mode='docker' but {dockerfile_name} not found",
                file_path=dockerfile_name,
                detail=(
                    "DockerStrategy builds this file on serve; without it the "
                    "build fails at startup."
                ),
                suggestion=(
                    f"Add a {dockerfile_name}, or set 'dockerfile = ...' in "
                    "app.toml, or switch to mode='image' with a pre-built image."
                ),
            )
        )
        return

    text = dockerfile.read_text(encoding="utf-8", errors="replace")

    if not _has_healthcheck(text):
        report.issues.append(
            Issue(
                severity=Severity.MEDIUM,
                category=CAT_NO_HEALTHCHECK,
                summary="Dockerfile has no HEALTHCHECK",
                file_path=dockerfile_name,
                detail=(
                    "Without a HEALTHCHECK, enlace falls back to a plain TCP "
                    "probe on the published port — it reports 'ready' as soon "
                    "as the socket accepts, which can be a beat before the app "
                    "actually serves requests."
                ),
                suggestion=(
                    "Add a HEALTHCHECK to the Dockerfile so readiness reflects "
                    "the app's real health, not just an open socket."
                ),
            )
        )

    declared_port = toml_data.get("port")
    exposed = _exposed_ports(text)
    if declared_port is not None and exposed and declared_port not in exposed:
        report.issues.append(
            Issue(
                severity=Severity.MEDIUM,
                category=CAT_EXPOSE_MISMATCH,
                summary=(
                    f"app.toml port={declared_port} not in Dockerfile "
                    f"EXPOSE {sorted(exposed)}"
                ),
                file_path=dockerfile_name,
                detail=(
                    "enlace publishes the declared 'port' as the in-container "
                    "port. If the app actually listens on a different port, the "
                    "proxy will not reach it."
                ),
                suggestion=(
                    f"Set app.toml 'port' to one of {sorted(exposed)}, or fix "
                    "the Dockerfile EXPOSE / app's listen port to match."
                ),
            )
        )


def _has_healthcheck(dockerfile_text: str) -> bool:
    """Whether the Dockerfile declares an active HEALTHCHECK.

    ``HEALTHCHECK NONE`` explicitly disables it, so it doesn't count.
    """
    for line in dockerfile_text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("HEALTHCHECK"):
            rest = stripped[len("HEALTHCHECK") :].strip()
            if rest.upper().startswith("NONE"):
                return False
            return True
    return False


def _exposed_ports(dockerfile_text: str) -> set[int]:
    """Parse integer ports from ``EXPOSE`` directives (ignores proto suffix)."""
    ports: set[int] = set()
    for line in dockerfile_text.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("EXPOSE"):
            continue
        for token in stripped[len("EXPOSE") :].split():
            # token may be "8080" or "8080/tcp"
            m = re.match(r"(\d+)", token)
            if m:
                ports.add(int(m.group(1)))
    return ports


# -- compose mode -------------------------------------------------------------


def _check_compose(app_dir: Path, toml_data: dict, report: DiagnosticReport) -> None:
    compose_name = toml_data.get("compose_file", "docker-compose.yml")
    compose_file = app_dir / compose_name

    if not compose_file.is_file():
        report.issues.append(
            Issue(
                severity=Severity.CRITICAL,
                category=CAT_MISSING_COMPOSE,
                summary=f"mode='compose' but {compose_name} not found",
                file_path=compose_name,
                suggestion=(
                    f"Add a {compose_name}, or set 'compose_file = ...' in "
                    "app.toml."
                ),
            )
        )
        return

    service = toml_data.get("service")
    if not service:
        # ComposeStrategy.validate already requires 'service'; this is a
        # belt-and-suspenders hint for the pre-serve diagnose pass.
        report.issues.append(
            Issue(
                severity=Severity.CRITICAL,
                category=CAT_COMPOSE_SERVICE,
                summary="mode='compose' requires 'service' in app.toml",
                detail="enlace must know which service receives HTTP traffic.",
                suggestion="Add 'service = \"<name>\"' to app.toml.",
            )
        )
        return

    # Shallow check: the declared service should appear as a YAML key in the
    # compose file. We avoid a YAML dependency — a word-boundary match on
    # '<service>:' catches the typo case without full parsing.
    text = compose_file.read_text(encoding="utf-8", errors="replace")
    if not re.search(rf"(?m)^\s+{re.escape(service)}\s*:", text):
        report.issues.append(
            Issue(
                severity=Severity.MEDIUM,
                category=CAT_COMPOSE_SERVICE,
                summary=f"service '{service}' not found in {compose_name}",
                file_path=compose_name,
                detail=(
                    "The declared service isn't an obvious key in the compose "
                    "file (shallow text check). If it's defined via an extension "
                    "or include, ignore this."
                ),
                suggestion=(
                    f"Check that '{service}:' is a service in {compose_name}, "
                    "or fix the 'service' value in app.toml."
                ),
            )
        )
