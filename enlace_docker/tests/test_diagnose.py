"""Tests for the docker diagnoser (registered via enlace.diagnosers)."""

from enlace.diagnose import DiagnosticReport, Severity

from enlace_docker.diagnose import (
    CAT_COMPOSE_SERVICE,
    CAT_EXPOSE_MISMATCH,
    CAT_MISSING_COMPOSE,
    CAT_MISSING_DOCKERFILE,
    CAT_NO_HEALTHCHECK,
    docker_diagnoser,
)


def _report(app_dir) -> DiagnosticReport:
    report = DiagnosticReport(app_dir=app_dir, app_name=app_dir.name)
    docker_diagnoser(app_dir, report)
    return report


def _cats(report) -> set:
    return {i.category for i in report.issues}


# -- no-op for non-docker apps ------------------------------------------------


def test_noop_when_no_app_toml(tmp_path):
    assert _report(tmp_path).issues == []


def test_noop_for_non_docker_mode(tmp_path):
    (tmp_path / "app.toml").write_text('mode = "asgi"\n')
    assert _report(tmp_path).issues == []


# -- docker mode --------------------------------------------------------------


def test_docker_missing_dockerfile_is_critical(tmp_path):
    (tmp_path / "app.toml").write_text('mode = "docker"\nport = 8080\n')
    report = _report(tmp_path)
    assert CAT_MISSING_DOCKERFILE in _cats(report)
    assert report.critical_count == 1


def test_docker_without_healthcheck_is_medium(tmp_path):
    (tmp_path / "app.toml").write_text('mode = "docker"\nport = 8080\n')
    (tmp_path / "Dockerfile").write_text("FROM alpine\nEXPOSE 8080\n")
    report = _report(tmp_path)
    assert CAT_NO_HEALTHCHECK in _cats(report)


def test_docker_with_healthcheck_no_warning(tmp_path):
    (tmp_path / "app.toml").write_text('mode = "docker"\nport = 8080\n')
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine\n"
        "EXPOSE 8080\n"
        "HEALTHCHECK CMD wget -q -O- localhost:8080 || exit 1\n"
    )
    report = _report(tmp_path)
    assert CAT_NO_HEALTHCHECK not in _cats(report)


def test_docker_healthcheck_none_counts_as_missing(tmp_path):
    (tmp_path / "app.toml").write_text('mode = "docker"\nport = 8080\n')
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine\nEXPOSE 8080\nHEALTHCHECK NONE\n"
    )
    report = _report(tmp_path)
    assert CAT_NO_HEALTHCHECK in _cats(report)


def test_docker_expose_mismatch_is_flagged(tmp_path):
    (tmp_path / "app.toml").write_text('mode = "docker"\nport = 9000\n')
    (tmp_path / "Dockerfile").write_text("FROM alpine\nEXPOSE 8080/tcp\n")
    report = _report(tmp_path)
    assert CAT_EXPOSE_MISMATCH in _cats(report)


def test_docker_expose_match_no_mismatch(tmp_path):
    (tmp_path / "app.toml").write_text('mode = "docker"\nport = 8080\n')
    (tmp_path / "Dockerfile").write_text("FROM alpine\nEXPOSE 8080/tcp\n")
    report = _report(tmp_path)
    assert CAT_EXPOSE_MISMATCH not in _cats(report)


# -- compose mode -------------------------------------------------------------


def test_compose_missing_file_is_critical(tmp_path):
    (tmp_path / "app.toml").write_text(
        'mode = "compose"\nservice = "web"\nport = 8080\n'
    )
    report = _report(tmp_path)
    assert CAT_MISSING_COMPOSE in _cats(report)
    assert report.critical_count == 1


def test_compose_service_present_no_warning(tmp_path):
    (tmp_path / "app.toml").write_text(
        'mode = "compose"\nservice = "web"\nport = 8080\n'
    )
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  web:\n    image: alpine\n    ports:\n      - 8080:8080\n"
    )
    report = _report(tmp_path)
    assert report.issues == []


def test_compose_service_missing_from_file_is_flagged(tmp_path):
    (tmp_path / "app.toml").write_text(
        'mode = "compose"\nservice = "api"\nport = 8080\n'
    )
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  web:\n    image: alpine\n"
    )
    report = _report(tmp_path)
    assert CAT_COMPOSE_SERVICE in _cats(report)
    assert any(i.severity == Severity.MEDIUM for i in report.issues)


# -- end-to-end through enlace.diagnose_app -----------------------------------


def test_runs_via_diagnose_app_entry_point(tmp_path):
    """The diagnoser fires through enlace's diagnose_app (entry-point wired)."""
    from enlace.diagnose import diagnose_app

    (tmp_path / "app.toml").write_text('mode = "docker"\nport = 8080\n')
    # No Dockerfile → expect the critical docker-missing-dockerfile issue.
    report = diagnose_app(tmp_path, app_name="svc")
    assert CAT_MISSING_DOCKERFILE in {i.category for i in report.issues}
