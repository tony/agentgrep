"""Tests for project-local telemetry helpers."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import pathlib
import sqlite3
import subprocess
import sys
import typing as t

import pytest


def _write_codex_session(home: pathlib.Path, *, text: str) -> pathlib.Path:
    """Write a minimal Codex session file for telemetry integration tests."""
    path = home / ".codex" / "sessions" / "2026" / "05" / "match.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "response_item", "payload": {"role": "user", "content": text}}
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def _run_git(repo: pathlib.Path, *args: str) -> str:
    """Run git in a test repository and return stripped stdout."""
    completed = subprocess.run(
        ("git", *args),
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _init_vcs_repo(
    repo: pathlib.Path,
    *,
    remote_url: str = "https://github.com/tony/agentgrep.git",
) -> str:
    """Create a small Git repository with one branch and one remote."""
    repo.mkdir()
    _run_git(repo, "init", "-b", "feature/vcs")
    _run_git(repo, "config", "user.email", "agentgrep@example.invalid")
    _run_git(repo, "config", "user.name", "agentgrep test")
    _ = (repo / "README.md").write_text("vcs\n", encoding="utf-8")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "initial")
    _run_git(repo, "remote", "add", "origin", remote_url)
    return _run_git(repo, "rev-parse", "HEAD")


class VcsRepositoryUrlCase(t.NamedTuple):
    """Parametrized case for canonical telemetry repository URLs."""

    test_id: str
    raw_url: str
    expected_url: str


class SensitiveLogExtraCase(t.NamedTuple):
    """Parametrized case for sensitive OTel log extras."""

    test_id: str
    key: str
    value: str


class PytestXdistAttributeCase(t.NamedTuple):
    """Parametrized case for pytest-xdist telemetry attributes."""

    test_id: str
    env_worker: str | None
    workerinput: dict[str, object] | None
    dist: str | None
    expected: dict[str, object]


class TelemetryServiceNameCase(t.NamedTuple):
    """Parametrized case for telemetry service-name resolution."""

    test_id: str
    env_service_name: str | None
    service_name: str | None
    expected_service_name: str


VCS_REPOSITORY_URL_CASES: tuple[VcsRepositoryUrlCase, ...] = (
    VcsRepositoryUrlCase(
        test_id="https-userinfo",
        raw_url="https://user:token@github.com/tony/agentgrep.git",
        expected_url="https://github.com/tony/agentgrep",
    ),
    VcsRepositoryUrlCase(
        test_id="https-userinfo-with-port",
        raw_url="https://user:token@example.invalid:8443/org/repo.git",
        expected_url="https://example.invalid:8443/org/repo",
    ),
    VcsRepositoryUrlCase(
        test_id="ssh-url",
        raw_url="ssh://git@example.invalid/org/repo.git",
        expected_url="https://example.invalid/org/repo",
    ),
    VcsRepositoryUrlCase(
        test_id="scp-like",
        raw_url="git@example.invalid:org/repo.git",
        expected_url="https://example.invalid/org/repo",
    ),
)

TELEMETRY_SERVICE_NAME_CASES: tuple[TelemetryServiceNameCase, ...] = (
    TelemetryServiceNameCase(
        test_id="default",
        env_service_name=None,
        service_name=None,
        expected_service_name="agentgrep",
    ),
    TelemetryServiceNameCase(
        test_id="entrypoint-default",
        env_service_name=None,
        service_name="agentgrep-cli",
        expected_service_name="agentgrep-cli",
    ),
    TelemetryServiceNameCase(
        test_id="standard-env",
        env_service_name="custom-agentgrep",
        service_name=None,
        expected_service_name="custom-agentgrep",
    ),
    TelemetryServiceNameCase(
        test_id="standard-env-overrides-entrypoint",
        env_service_name="custom-agentgrep",
        service_name="agentgrep-cli",
        expected_service_name="custom-agentgrep",
    ),
)

SENSITIVE_LOG_EXTRA_CASES: tuple[SensitiveLogExtraCase, ...] = (
    SensitiveLogExtraCase(
        test_id="query",
        key="agentgrep_query",
        value="secret query token",
    ),
    SensitiveLogExtraCase(
        test_id="path",
        key="agentgrep_path",
        value="/tmp/agentgrep/private/session.jsonl",
    ),
    SensitiveLogExtraCase(
        test_id="argv",
        key="agentgrep_argv",
        value="agentgrep search secret-token",
    ),
)

PYTEST_XDIST_ATTRIBUTE_CASES: tuple[PytestXdistAttributeCase, ...] = (
    PytestXdistAttributeCase(
        test_id="env-worker",
        env_worker="gw0",
        workerinput=None,
        dist=None,
        expected={
            "agentgrep_pytest_worker_id": "gw0",
            "agentgrep_pytest_xdist": True,
        },
    ),
    PytestXdistAttributeCase(
        test_id="workerinput",
        env_worker=None,
        workerinput={"workerid": "gw1"},
        dist="loadscope",
        expected={
            "agentgrep_pytest_worker_id": "gw1",
            "agentgrep_pytest_xdist": True,
            "agentgrep_pytest_dist": "loadscope",
        },
    ),
    PytestXdistAttributeCase(
        test_id="no-xdist",
        env_worker=None,
        workerinput=None,
        dist=None,
        expected={"agentgrep_pytest_xdist": False},
    ),
)


def test_resolve_mode_uses_only_agentgrep_otel(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AGENTGREP_OTEL_ENABLED`` must not affect telemetry mode."""
    import agentgrep._telemetry as telemetry

    env = {
        "AGENTGREP_OTEL": "debug",
        "AGENTGREP_OTEL_ENABLED": "0",
    }

    assert telemetry.resolve_mode(env=env, repo_root=pathlib.Path.cwd()) == "debug"

    monkeypatch.delenv("AGENTGREP_OTEL", raising=False)
    monkeypatch.setenv("AGENTGREP_OTEL_ENABLED", "1")

    assert telemetry.resolve_mode(env=os.environ, repo_root=None) == "off"


def test_resolve_mode_keeps_pytest_off_by_default() -> None:
    """Default pytest should not create an OTel SDK or network dependency."""
    import agentgrep._telemetry as telemetry

    env = {"PYTEST_CURRENT_TEST": "tests/test_telemetry.py::test_name (call)"}

    assert telemetry.resolve_mode(env=env, repo_root=pathlib.Path.cwd()) == "off"
    assert (
        telemetry.resolve_mode(
            env={**env, "AGENTGREP_OTEL": "test"},
            repo_root=pathlib.Path.cwd(),
        )
        == "test"
    )


def test_service_version_is_not_debug_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Debug identifiers live in separate attributes."""
    import agentgrep._telemetry as telemetry

    monkeypatch.setenv("AGENTGREP_DEBUG_SESSION_ID", "session-1")
    monkeypatch.setenv("AGENTGREP_DEBUG_ATTEMPT", "3")
    monkeypatch.setenv("AGENTGREP_DEBUG_CANDIDATE_ID", "candidate-1")

    attributes = telemetry.build_resource_attributes(
        env=os.environ,
        service_version="0.1.0a24",
    )

    assert attributes["service.version"] == "0.1.0a24"
    assert attributes["agentgrep.debug.session_id"] == "session-1"
    assert attributes["agentgrep.debug.attempt"] == 3
    assert attributes["agentgrep.debug.candidate_id"] == "candidate-1"


@pytest.mark.parametrize(
    "case",
    TELEMETRY_SERVICE_NAME_CASES,
    ids=[case.test_id for case in TELEMETRY_SERVICE_NAME_CASES],
)
def test_resource_attributes_include_service_identity(case: TelemetryServiceNameCase) -> None:
    """Telemetry resources should carry stable service identity."""
    import agentgrep._telemetry as telemetry

    env = {}
    if case.env_service_name is not None:
        env["OTEL_SERVICE_NAME"] = case.env_service_name

    attributes = telemetry.build_resource_attributes(
        env=env,
        service_name=case.service_name,
        service_version="0.1.0a24",
    )

    assert attributes["service.name"] == case.expected_service_name
    assert attributes["service.namespace"] == "agentgrep"
    assert attributes["service.version"] == "0.1.0a24"


@pytest.mark.parametrize(
    "case",
    VCS_REPOSITORY_URL_CASES,
    ids=[case.test_id for case in VCS_REPOSITORY_URL_CASES],
)
def test_canonical_repository_url_strips_credentials(case: VcsRepositoryUrlCase) -> None:
    """Telemetry repository URLs must never include remote credentials."""
    import agentgrep._telemetry as telemetry

    canonical = telemetry._canonical_repository_url(case.raw_url)

    assert canonical == case.expected_url
    assert "token" not in str(canonical)
    assert "user:" not in str(canonical)


def test_resource_attributes_include_current_vcs_identity(tmp_path: pathlib.Path) -> None:
    """Telemetry resource attributes should identify the current Git ref."""
    import agentgrep._telemetry as telemetry

    repo = tmp_path / "repo"
    head_revision = _init_vcs_repo(repo)

    attributes = telemetry.build_resource_attributes(
        env={},
        service_version="0.1.0",
        repo_root=repo,
    )

    assert attributes["vcs.ref.head.name"] == "feature/vcs"
    assert attributes["vcs.ref.head.revision"] == head_revision
    assert attributes["vcs.ref.head.type"] == "branch"
    assert attributes["vcs.repository.name"] == "agentgrep"
    assert attributes["vcs.repository.url.full"] == "https://github.com/tony/agentgrep"
    assert "vcs.repository.ref.name" not in attributes


def test_resource_attributes_strip_vcs_url_credentials(tmp_path: pathlib.Path) -> None:
    """Exported VCS resource URLs should be browser URLs without userinfo."""
    import agentgrep._telemetry as telemetry

    repo = tmp_path / "repo"
    _ = _init_vcs_repo(repo, remote_url="https://user:token@github.com/tony/agentgrep.git")

    attributes = telemetry.build_resource_attributes(
        env={},
        service_version="0.1.0",
        repo_root=repo,
    )

    assert attributes["vcs.repository.url.full"] == "https://github.com/tony/agentgrep"
    assert "token" not in str(attributes)
    assert "user:" not in str(attributes)


def test_resource_attributes_recover_branch_for_detached_head(
    tmp_path: pathlib.Path,
) -> None:
    """Detached acceptance workloads should still carry a branch ref when exact."""
    import agentgrep._telemetry as telemetry

    repo = tmp_path / "repo"
    head_revision = _init_vcs_repo(repo)
    _run_git(repo, "checkout", "--detach", head_revision)

    attributes = telemetry.build_resource_attributes(
        env={},
        service_version="0.1.0",
        repo_root=repo,
    )

    assert attributes["vcs.ref.head.name"] == "feature/vcs"
    assert attributes["vcs.ref.head.revision"] == head_revision
    assert attributes["vcs.ref.head.type"] == "branch"


def test_logs_metrics_and_traces_are_linked_under_non_single_root() -> None:
    """A representative app root emits child spans, metrics, and trace-linked logs."""
    import agentgrep._telemetry as telemetry

    logger = logging.getLogger("agentgrep.test.telemetry")
    backend = telemetry.InMemoryTelemetryBackend()
    telemetry.configure_backend(backend)
    remove_handler = telemetry.install_logging_exporter(backend)
    try:
        with telemetry.span("agentgrep.cli.invocation", agentgrep_surface="cli"):
            logger.warning("inside trace", extra={"agentgrep_operation": "smoke"})
            with telemetry.span("agentgrep.cli.dispatch", agentgrep_surface="cli"):
                telemetry.record_metric("agentgrep.acceptance.count", 1, agentgrep_surface="cli")
    finally:
        remove_handler()
        telemetry.configure_backend(None)

    assert backend.single_root_trace_ids() == ()
    assert {span.name for span in backend.finished_spans} == {
        "agentgrep.cli.invocation",
        "agentgrep.cli.dispatch",
    }
    assert {metric.name for metric in backend.metric_records} >= {
        "agentgrep.span.count",
        "agentgrep.span.duration",
        "agentgrep.acceptance.count",
    }
    assert len(backend.log_records) == 1
    assert backend.log_records[0].trace_id == backend.finished_spans[1].trace_id
    assert backend.log_records[0].span_id == backend.finished_spans[1].span_id


def test_engine_search_and_find_emit_structured_trace_linked_logs(
    tmp_path: pathlib.Path,
) -> None:
    """Engine boundaries should emit content-free logs linked to active spans."""
    import agentgrep
    import agentgrep._telemetry as telemetry

    home = tmp_path / "home"
    _ = _write_codex_session(home, text="agentic signal")
    query = agentgrep.SearchQuery(
        terms=("agentic",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=5,
    )
    backend = telemetry.InMemoryTelemetryBackend()
    telemetry.configure_backend(backend)
    remove_handler = telemetry.install_logging_exporter(backend)
    try:
        with telemetry.span("agentgrep.cli.invocation", agentgrep_surface="cli"):
            records = agentgrep.run_search_query(
                home,
                query,
                backends=agentgrep.BackendSelection(None, None, None),
            )
            find_records = agentgrep.run_find_query(
                home,
                ("codex",),
                pattern="sessions",
                limit=10,
                backends=agentgrep.BackendSelection(None, None, None),
            )
    finally:
        remove_handler()
        telemetry.configure_backend(None)

    assert len(records) == 1
    assert find_records
    logs_by_message = {record.message: record for record in backend.log_records}
    assert "search sources planned" in logs_by_message
    assert "search query completed" in logs_by_message
    assert "find query completed" in logs_by_message
    search_log = logs_by_message["search query completed"]
    assert search_log.attributes["agentgrep_surface"] == "engine"
    assert search_log.attributes["agentgrep_component"] == "core"
    assert search_log.attributes["agentgrep_component_kind"] == "in_process"
    assert search_log.attributes["agentgrep_operation"] == "search.run"
    assert search_log.attributes["agentgrep_outcome"] == "ok"
    assert search_log.attributes["agentgrep_result_count"] == 1
    find_log = logs_by_message["find query completed"]
    assert find_log.attributes["agentgrep_component"] == "core"
    assert find_log.attributes["agentgrep_component_kind"] == "in_process"
    find_source_count = find_log.attributes["agentgrep_source_count"]
    find_result_count = find_log.attributes["agentgrep_result_count"]
    assert isinstance(find_source_count, int)
    assert isinstance(find_result_count, int)
    assert find_source_count >= 1
    assert find_result_count >= 1
    search_span = next(
        span for span in backend.finished_spans if span.name == "agentgrep.search.run"
    )
    find_span = next(span for span in backend.finished_spans if span.name == "agentgrep.find.run")
    assert search_span.attributes["agentgrep_component"] == "core"
    assert search_span.attributes["agentgrep_component_kind"] == "in_process"
    assert find_span.attributes["agentgrep_component"] == "core"
    assert find_span.attributes["agentgrep_component_kind"] == "in_process"
    assert {record.trace_id for record in backend.log_records} == {
        backend.finished_spans[-1].trace_id,
    }
    assert all(record.span_id is not None for record in backend.log_records)
    assert "agentic signal" not in str([record.attributes for record in backend.log_records])


def test_tui_session_emits_structured_trace_linked_log(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TUI session completion should be visible in Loki without console output."""
    import agentgrep
    import agentgrep._telemetry as telemetry
    from agentgrep.ui import app as ui_app

    class FakeApp:
        def run(self) -> None:
            """Stand in for the blocking Textual application."""

    query = agentgrep.SearchQuery(
        terms=("agentic",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=5,
    )
    monkeypatch.setattr(ui_app, "build_streaming_ui_app", lambda *_args, **_kwargs: FakeApp())

    backend = telemetry.InMemoryTelemetryBackend()
    telemetry.configure_backend(backend)
    remove_handler = telemetry.install_logging_exporter(backend)
    try:
        with telemetry.span("agentgrep.cli.invocation", agentgrep_surface="cli"):
            ui_app.run_ui(
                tmp_path,
                query,
                control=agentgrep.SearchControl(),
                initial_search_text="agentic signal",
            )
    finally:
        remove_handler()
        telemetry.configure_backend(None)

    session_log = next(
        record for record in backend.log_records if record.message == "tui session completed"
    )
    assert session_log.attributes["agentgrep_surface"] == "tui"
    assert session_log.attributes["agentgrep_operation"] == "tui.session"
    assert session_log.attributes["agentgrep_outcome"] == "ok"
    assert session_log.attributes["agentgrep_initial_query_present"] is True
    tui_span = next(span for span in backend.finished_spans if span.name == "agentgrep.tui.session")
    cli_span = next(
        span for span in backend.finished_spans if span.name == "agentgrep.cli.invocation"
    )
    assert tui_span.parent_id is None
    assert tui_span.trace_id != cli_span.trace_id
    assert session_log.trace_id == tui_span.trace_id
    assert session_log.span_id == tui_span.span_id
    lifecycle_span = next(
        span for span in backend.finished_spans if span.name == "agentgrep.tui.lifecycle"
    )
    assert lifecycle_span.parent_id == tui_span.span_id
    assert lifecycle_span.trace_id == tui_span.trace_id
    shutdown_span = next(
        span for span in backend.finished_spans if span.name == "agentgrep.tui.shutdown"
    )
    assert shutdown_span.parent_id == tui_span.span_id
    assert shutdown_span.trace_id == tui_span.trace_id
    shutdown_log = next(
        record for record in backend.log_records if record.message == "tui shutdown completed"
    )
    assert shutdown_log.trace_id == shutdown_span.trace_id
    assert shutdown_log.span_id == shutdown_span.span_id
    assert tui_span.trace_id not in backend.single_root_trace_ids()
    assert "agentic signal" not in str(session_log.attributes)


def test_tui_empty_input_quit_emits_trace_linked_log(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Focused-input quit requests should be visible under the TUI trace."""
    import agentgrep
    import agentgrep._telemetry as telemetry

    home = tmp_path / "home"
    home.mkdir()
    query = agentgrep.SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    app = agentgrep.build_streaming_ui_app(home, query, control=agentgrep.SearchControl())
    exits: list[bool] = []
    monkeypatch.setattr(app, "exit", lambda: exits.append(True))

    backend = telemetry.InMemoryTelemetryBackend()
    telemetry.configure_backend(backend)
    remove_handler = telemetry.install_logging_exporter(backend)
    try:
        with telemetry.root_span("agentgrep.tui.session", agentgrep_surface="tui"):
            t.cast("t.Any", app)._quit_from_empty_input(input_id="search", key="q")
    finally:
        remove_handler()
        telemetry.configure_backend(None)

    assert exits == [True]
    quit_span = next(span for span in backend.finished_spans if span.name == "agentgrep.tui.quit")
    session_span = next(
        span for span in backend.finished_spans if span.name == "agentgrep.tui.session"
    )
    assert quit_span.parent_id == session_span.span_id
    assert quit_span.trace_id == session_span.trace_id
    assert quit_span.attributes["agentgrep_operation"] == "tui.quit"
    assert quit_span.attributes["agentgrep_tui_input_id"] == "search"
    assert quit_span.attributes["agentgrep_tui_key"] == "q"
    quit_log = next(
        record for record in backend.log_records if record.message == "tui quit requested"
    )
    assert quit_log.trace_id == quit_span.trace_id
    assert quit_log.span_id == quit_span.span_id
    assert quit_log.attributes["agentgrep_outcome"] == "exit"


def test_metrics_include_debug_session_when_otel_is_enabled() -> None:
    """OTel-on metrics carry run identity for Grafana QA."""
    import agentgrep._telemetry as telemetry

    handle = telemetry.setup(
        mode="test",
        env={
            "AGENTGREP_OTEL": "live",
            "AGENTGREP_DEBUG_SESSION_ID": "session-1",
        },
        service_version="0.1.0",
    )
    assert isinstance(handle.backend, telemetry.InMemoryTelemetryBackend)
    backend = handle.backend
    try:
        with (
            telemetry.span(
                "agentgrep.cli.invocation",
                agentgrep_surface="cli",
            ),
            telemetry.span("agentgrep.cli.dispatch", agentgrep_surface="cli"),
        ):
            telemetry.record_metric("agentgrep.acceptance.count", 1, agentgrep_surface="cli")
    finally:
        handle.shutdown()

    assert backend.metric_records
    assert {
        record.attributes.get("agentgrep_debug_session_id") for record in backend.metric_records
    } == {"session-1"}


def test_metrics_include_vcs_identity_when_otel_is_enabled(tmp_path: pathlib.Path) -> None:
    """OTel-on metrics carry VCS identity for Grafana QA."""
    import agentgrep._telemetry as telemetry

    repo = tmp_path / "repo"
    head_revision = _init_vcs_repo(repo)
    handle = telemetry.setup(
        mode="test",
        env={"AGENTGREP_OTEL": "live"},
        repo_root=repo,
        service_version="0.1.0",
    )
    assert isinstance(handle.backend, telemetry.InMemoryTelemetryBackend)
    backend = handle.backend
    try:
        with telemetry.span("agentgrep.cli.invocation", agentgrep_surface="cli"):
            telemetry.record_metric("agentgrep.acceptance.count", 1, agentgrep_surface="cli")
    finally:
        handle.shutdown()

    assert backend.metric_records
    assert {record.attributes.get("vcs_ref_head_name") for record in backend.metric_records} == {
        "feature/vcs",
    }
    assert {
        record.attributes.get("vcs_ref_head_revision") for record in backend.metric_records
    } == {head_revision}
    assert {record.attributes.get("vcs_ref_head_type") for record in backend.metric_records} == {
        "branch",
    }
    assert {record.attributes.get("vcs_repository_name") for record in backend.metric_records} == {
        "agentgrep",
    }
    assert {
        record.attributes.get("vcs_repository_url_full") for record in backend.metric_records
    } == {"https://github.com/tony/agentgrep"}


def test_profiles_reuse_resource_vcs_identity_as_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pyroscope tags should be derived from the same resource VCS attributes."""
    from agentgrep import _telemetry_otel

    calls: list[dict[str, object]] = []

    class FakePyroscope:
        @staticmethod
        def configure(**kwargs: object) -> None:
            calls.append(kwargs)

    monkeypatch.setitem(sys.modules, "pyroscope", FakePyroscope)

    started = _telemetry_otel._configure_profiles(
        {
            "service.name": "agentgrep-cli",
            "service.namespace": "agentgrep",
            "service.version": "0.1.0",
            "vcs.ref.head.name": "feature/vcs",
            "vcs.ref.head.revision": "abc123",
            "vcs.ref.head.type": "branch",
            "vcs.repository.name": "agentgrep",
            "vcs.repository.url.full": "https://github.com/tony/agentgrep",
        },
    )

    assert started is True
    assert len(calls) == 1
    assert calls[0]["application_name"] == "agentgrep-cli"
    tags = calls[0]["tags"]
    assert tags == {
        "service_namespace": "agentgrep",
        "service_git_ref": "abc123",
        "service_repository": "https://github.com/tony/agentgrep",
        "service_root_path": ".",
        "vcs_ref_head_name": "feature/vcs",
        "vcs_ref_head_revision": "abc123",
        "vcs_ref_head_type": "branch",
        "vcs_repository_name": "agentgrep",
        "vcs_repository_url_full": "https://github.com/tony/agentgrep",
    }


def test_telemetry_handle_shutdown_is_idempotent() -> None:
    """Double cleanup should not call backend shutdown twice."""
    import agentgrep._telemetry as telemetry

    class CountingBackend(telemetry.InMemoryTelemetryBackend):
        """In-memory backend with visible shutdown count."""

        def __init__(self) -> None:
            super().__init__()
            self.shutdown_count = 0

        def shutdown(self) -> None:
            """Count backend shutdown calls."""
            self.shutdown_count += 1

    backend = CountingBackend()
    handle = telemetry.TelemetryHandle(mode="test", backend=backend)

    handle.shutdown()
    handle.shutdown()

    assert backend.shutdown_count == 1
    assert handle.backend is None


def test_sql_span_requires_active_project_span() -> None:
    """SQL helper spans should never become orphaned root traces."""
    import agentgrep._telemetry as telemetry

    backend = telemetry.InMemoryTelemetryBackend()
    root_span_id: str | None = None
    telemetry.configure_backend(backend)
    try:
        with telemetry.sql_span("agentgrep.sqlite.execute", **{"db.system": "sqlite"}):
            pass
        with telemetry.span("agentgrep.cli.invocation", agentgrep_surface="cli"):
            root_span_id = telemetry.current_span_id()
            with telemetry.sql_span("agentgrep.sqlite.execute", **{"db.system": "sqlite"}):
                pass
    finally:
        telemetry.configure_backend(None)

    assert [span.name for span in backend.finished_spans] == [
        "agentgrep.sqlite.execute",
        "agentgrep.cli.invocation",
    ]
    assert backend.finished_spans[0].parent_id == root_span_id
    assert backend.single_root_trace_ids() == ()


def test_sqlite_connection_factory_traces_connection_shortcuts_without_parameters() -> None:
    """Connection shortcut methods get SQL spans without recording bound values."""
    import agentgrep._telemetry as telemetry

    backend = telemetry.InMemoryTelemetryBackend()
    root_span_id: str | None = None
    telemetry.configure_backend(backend)
    try:
        connection = sqlite3.connect(":memory:", factory=telemetry.sqlite_connection_factory())
        try:
            connection.execute("create table outside_root (value integer)")
            with telemetry.span("agentgrep.cli.invocation", agentgrep_surface="cli"):
                root_span_id = telemetry.current_span_id()
                connection.execute("create table smoke (value integer)")
                connection.executemany(
                    "insert into smoke (value) values (?)",
                    [(12345,), (67890,)],
                )
                connection.execute("select value from smoke where value=?", (12345,)).fetchone()
        finally:
            connection.close()
    finally:
        telemetry.configure_backend(None)

    sql_spans = [
        span for span in backend.finished_spans if span.name.startswith("agentgrep.sqlite.")
    ]
    assert [span.name for span in sql_spans] == [
        "agentgrep.sqlite.execute",
        "agentgrep.sqlite.executemany",
        "agentgrep.sqlite.execute",
    ]
    assert all(span.parent_id == root_span_id for span in sql_spans)
    assert all(span.attributes["db.system"] == "sqlite" for span in sql_spans)
    assert {span.attributes["agentgrep_sql_method"] for span in sql_spans} == {
        "execute",
        "executemany",
    }
    assert "12345" not in str([span.attributes for span in sql_spans])
    assert backend.single_root_trace_ids() == ()
    sqlite_metrics = [
        metric for metric in backend.metric_records if metric.name == "agentgrep.otel.sqlite_total"
    ]
    assert [metric.value for metric in sqlite_metrics] == [1, 1, 1]
    assert {metric.attributes["agentgrep_sql_method"] for metric in sqlite_metrics} == {
        "execute",
        "executemany",
    }
    assert all(metric.attributes["agentgrep_surface"] == "sqlite" for metric in sqlite_metrics)


def test_record_work_metric_keeps_debug_identity() -> None:
    """CPU-impacting work counters should be real app metrics, not smoke-only."""
    import agentgrep._telemetry as telemetry

    handle = telemetry.setup(
        mode="test",
        env={
            "AGENTGREP_OTEL": "live",
            "AGENTGREP_DEBUG_SESSION_ID": "session-work",
            "AGENTGREP_DEBUG_CANDIDATE_ID": "candidate-work",
        },
        service_version="0.1.0",
    )
    assert isinstance(handle.backend, telemetry.InMemoryTelemetryBackend)
    backend = handle.backend
    try:
        with telemetry.span("agentgrep.cli.invocation", agentgrep_surface="cli"):
            telemetry.record_work_metric(
                42,
                work_kind="source_records_scanned",
                agentgrep_surface="engine",
                agentgrep_source_strategy="root_full_scan",
            )
    finally:
        handle.shutdown()

    work_metric = next(
        metric for metric in backend.metric_records if metric.name == "agentgrep.otel.cpu_loops"
    )
    assert work_metric.value == 42
    assert work_metric.attributes["agentgrep_work_kind"] == "source_records_scanned"
    assert work_metric.attributes["agentgrep_surface"] == "engine"
    assert work_metric.attributes["agentgrep_component"] == "core"
    assert work_metric.attributes["agentgrep_component_kind"] == "in_process"
    assert work_metric.attributes["agentgrep_source_strategy"] == "root_full_scan"
    assert work_metric.attributes["agentgrep_debug_session_id"] == "session-work"
    assert "agentgrep_debug_candidate_id" not in work_metric.attributes


def test_open_readonly_sqlite_uses_traced_connection_factory(tmp_path: pathlib.Path) -> None:
    """The source-parser SQLite helper should use the traced shortcut factory."""
    import agentgrep
    import agentgrep._telemetry as telemetry

    db_path = tmp_path / "store.sqlite"
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("create table messages (value text)")
        writer.execute("insert into messages values ('hello')")
        writer.commit()
    finally:
        writer.close()

    backend = telemetry.InMemoryTelemetryBackend()
    telemetry.configure_backend(backend)
    try:
        with telemetry.span("agentgrep.cli.invocation", agentgrep_surface="cli"):
            root_span_id = telemetry.current_span_id()
            reader = agentgrep.open_readonly_sqlite(db_path)
            try:
                assert reader.execute("select value from messages").fetchone() == ("hello",)
            finally:
                reader.close()
    finally:
        telemetry.configure_backend(None)

    sql_spans = [
        span for span in backend.finished_spans if span.name.startswith("agentgrep.sqlite.")
    ]
    assert len(sql_spans) == 1
    (sql_span,) = sql_spans
    assert sql_span.name == "agentgrep.sqlite.execute"
    assert sql_span.parent_id == root_span_id
    assert sql_span.attributes["db.operation.name"] == "select"


def test_executor_submit_preserves_current_trace_context() -> None:
    """Thread-pool work should remain inside the current trace."""
    import agentgrep._telemetry as telemetry

    backend = telemetry.InMemoryTelemetryBackend()
    telemetry.configure_backend(backend)
    try:
        with telemetry.span("agentgrep.cli.invocation"):
            root_span_id = telemetry.current_span_id()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = telemetry.executor_submit(executor, telemetry.current_span_id)
                worker_span_id = future.result(timeout=5)
    finally:
        telemetry.configure_backend(None)

    assert worker_span_id == root_span_id


def test_span_mirrors_native_otel_trace_ids() -> None:
    """In live mode the facade span ids mirror the native OTel ids."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import format_span_id, format_trace_id

    import agentgrep._telemetry as telemetry
    from agentgrep import _telemetry_otel

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class _Noop:
        def add(self, *args: object, **kwargs: object) -> None:
            return None

        def record(self, *args: object, **kwargs: object) -> None:
            return None

        def force_flush(self, *args: object, **kwargs: object) -> bool:
            return True

        def shutdown(self) -> None:
            return None

    noop = _Noop()
    backend = _telemetry_otel.OtelTelemetryBackend(
        tracer=provider.get_tracer("test"),
        tracer_provider=provider,
        meter=noop,
        meter_provider=noop,
        logger_provider=noop,
        logging_handler=logging.NullHandler(),
        span_counter=noop,
        span_duration=noop,
        instrumentations=(),
        profiles_started=False,
    )

    telemetry.configure_backend(backend)
    try:
        with telemetry.span("agentgrep.cli.invocation", agentgrep_surface="cli"):
            root_trace_id = telemetry.current_trace_id()
            root_span_id = telemetry.current_span_id()
            with telemetry.span("agentgrep.cli.parse"):
                child_trace_id = telemetry.current_trace_id()
                child_state = telemetry._CURRENT_SPAN.get()
                child_parent_id = None if child_state is None else child_state.parent_id
    finally:
        telemetry.configure_backend(None)
        provider.shutdown()

    by_name = {span.name: span for span in exporter.get_finished_spans()}
    native_root = by_name["agentgrep.cli.invocation"]
    native_child = by_name["agentgrep.cli.parse"]
    assert root_trace_id == format_trace_id(native_root.context.trace_id)
    assert root_span_id == format_span_id(native_root.context.span_id)
    assert child_trace_id == format_trace_id(native_child.context.trace_id)
    assert child_trace_id == root_trace_id
    assert child_parent_id == format_span_id(native_root.context.span_id)


def test_pytest_item_span_helper_covers_custom_items() -> None:
    """The pytest hook wrapper opens one pytest.test root for custom items."""
    import agentgrep._telemetry as telemetry
    import conftest as root_conftest

    class FakeItem:
        nodeid = "docs.md::documentation-example"

    backend = telemetry.InMemoryTelemetryBackend()
    telemetry.configure_backend(backend)
    try:
        with root_conftest._agentgrep_otel_pytest_item_span(FakeItem()):
            assert telemetry.current_span_id() is not None
    finally:
        telemetry.configure_backend(None)

    assert [span.name for span in backend.finished_spans] == ["agentgrep.pytest.test"]
    (test_span,) = backend.finished_spans
    assert test_span.parent_id is None
    assert test_span.attributes["agentgrep_pytest_test"] == FakeItem.nodeid


def test_pytest_session_root_brackets_test_traces(monkeypatch: pytest.MonkeyPatch) -> None:
    """The session hooks open a pytest.session root; item traces stay independent."""
    import agentgrep._telemetry as telemetry
    import conftest as root_conftest

    class FakeItem:
        nodeid = "tests/test_x.py::test_y"

    monkeypatch.setenv("AGENTGREP_OTEL", "test")
    root_conftest.pytest_sessionstart(t.cast("t.Any", None))
    backend = telemetry.active_backend()
    assert isinstance(backend, telemetry.InMemoryTelemetryBackend)
    try:
        with root_conftest._agentgrep_otel_pytest_item_span(FakeItem()):
            pass
    finally:
        root_conftest.pytest_sessionfinish(t.cast("t.Any", None), 0)

    by_name = {span.name: span for span in backend.finished_spans}
    assert "agentgrep.pytest.session" in by_name
    session_span = by_name["agentgrep.pytest.session"]
    test_span = by_name["agentgrep.pytest.test"]
    assert session_span.parent_id is None
    assert test_span.parent_id is None
    assert test_span.trace_id != session_span.trace_id


class _ExplicitCase(t.NamedTuple):
    """Parametrized case for explicit telemetry opt-in resolution."""

    test_id: str
    mode: str | None
    env: dict[str, str]
    expected_explicit: bool


_EXPLICIT_CASES: tuple[_ExplicitCase, ...] = (
    _ExplicitCase("passive-local", mode=None, env={}, expected_explicit=False),
    _ExplicitCase("env-enabled", mode=None, env={"AGENTGREP_OTEL": "1"}, expected_explicit=True),
    _ExplicitCase("env-live", mode=None, env={"AGENTGREP_OTEL": "live"}, expected_explicit=True),
    _ExplicitCase("explicit-mode", mode="live", env={}, expected_explicit=True),
)


@pytest.mark.parametrize(
    "case",
    _EXPLICIT_CASES,
    ids=[case.test_id for case in _EXPLICIT_CASES],
)
def test_resolve_explicit_distinguishes_passive_local(case: _ExplicitCase) -> None:
    """Only the auto-resolved local default with AGENTGREP_OTEL unset is passive."""
    import agentgrep._telemetry as telemetry

    resolved = telemetry.resolve_explicit(t.cast("t.Any", case.mode), case.env)

    assert resolved is case.expected_explicit


class _SpanStatusCase(t.NamedTuple):
    """Parametrized case for non-exception span status marking."""

    test_id: str
    mark_error: bool
    expected_status: str


_SPAN_STATUS_CASES: tuple[_SpanStatusCase, ...] = (
    _SpanStatusCase("clean", mark_error=False, expected_status="ok"),
    _SpanStatusCase("marked-error", mark_error=True, expected_status="error"),
)


@pytest.mark.parametrize(
    "case",
    _SPAN_STATUS_CASES,
    ids=[case.test_id for case in _SPAN_STATUS_CASES],
)
def test_mark_span_error_sets_span_status(case: _SpanStatusCase) -> None:
    """mark_span_error flips the active span to error without raising."""
    import agentgrep._telemetry as telemetry

    backend = telemetry.InMemoryTelemetryBackend()
    telemetry.configure_backend(backend)
    try:
        with telemetry.span("agentgrep.cli.invocation", agentgrep_surface="cli"):
            if case.mark_error:
                telemetry.mark_span_error("boom")
    finally:
        telemetry.configure_backend(None)

    (recorded,) = backend.finished_spans
    assert recorded.status == case.expected_status


class _MetricKindCase(t.NamedTuple):
    """Parametrized case for OTel metric instrument-kind classification."""

    test_id: str
    metric_name: str
    expected_counter: bool


_METRIC_KIND_CASES: tuple[_MetricKindCase, ...] = (
    _MetricKindCase("grep-candidate-count", "agentgrep.grep.candidate.count", True),
    _MetricKindCase("benchmark-subprocess-count", "agentgrep.benchmark.subprocess.count", True),
    _MetricKindCase("span-count", "agentgrep.span.count", True),
    _MetricKindCase("cpu-loops", "agentgrep.otel.cpu_loops", True),
    _MetricKindCase("sqlite-total", "agentgrep.otel.sqlite_total", True),
    _MetricKindCase("grep-duration", "agentgrep.grep.duration", False),
    _MetricKindCase("span-duration", "agentgrep.span.duration", False),
    _MetricKindCase("search-results", "agentgrep.search.results", False),
)


@pytest.mark.parametrize(
    "case",
    _METRIC_KIND_CASES,
    ids=[case.test_id for case in _METRIC_KIND_CASES],
)
def test_metric_is_counter_classifies_work_metrics(case: _MetricKindCase) -> None:
    """cpu_loops and sqlite_total are counters despite lacking a .count suffix."""
    import agentgrep._telemetry_otel as telemetry_otel

    assert telemetry_otel._metric_is_counter(case.metric_name) is case.expected_counter


@pytest.mark.parametrize(
    "case",
    PYTEST_XDIST_ATTRIBUTE_CASES,
    ids=[case.test_id for case in PYTEST_XDIST_ATTRIBUTE_CASES],
)
def test_pytest_item_span_helper_adds_xdist_worker_attributes(
    case: PytestXdistAttributeCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit pytest telemetry should label xdist worker context when present."""
    import agentgrep._telemetry as telemetry
    import conftest as root_conftest

    class FakeOption:
        def __init__(self, dist: str | None) -> None:
            self.dist = dist

    class FakeConfig:
        def __init__(self) -> None:
            self.workerinput = case.workerinput
            self.option = FakeOption(case.dist)

    class FakeItem:
        nodeid = "tests/test_example.py::test_name"
        config = FakeConfig()

    if case.env_worker is None:
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    else:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", case.env_worker)

    backend = telemetry.InMemoryTelemetryBackend()
    telemetry.configure_backend(backend)
    try:
        with root_conftest._agentgrep_otel_pytest_item_span(FakeItem()):
            assert telemetry.current_span_id() is not None
    finally:
        telemetry.configure_backend(None)

    for span in backend.finished_spans:
        assert span.attributes["agentgrep_pytest_test"] == FakeItem.nodeid
        for key, expected in case.expected.items():
            assert span.attributes[key] == expected


class McpSensitiveScalarArgCase(t.NamedTuple):
    """Parametrized case for scalar MCP argument redaction."""

    test_id: str
    key: str
    value: str


MCP_SENSITIVE_SCALAR_ARG_CASES: tuple[McpSensitiveScalarArgCase, ...] = (
    McpSensitiveScalarArgCase(
        test_id="pattern",
        key="pattern",
        value="secret-pattern",
    ),
    McpSensitiveScalarArgCase(
        test_id="sample-text",
        key="sample_text",
        value="secret sample text",
    ),
    McpSensitiveScalarArgCase(
        test_id="cursor",
        key="cursor",
        value="agcur1:secret-cursor",
    ),
    McpSensitiveScalarArgCase(
        test_id="query",
        key="query",
        value="agent:codex secret-query",
    ),
)


@pytest.mark.parametrize(
    "case",
    MCP_SENSITIVE_SCALAR_ARG_CASES,
    ids=[case.test_id for case in MCP_SENSITIVE_SCALAR_ARG_CASES],
)
def test_summarize_args_redacts_sensitive_scalar_args(
    case: McpSensitiveScalarArgCase,
) -> None:
    """Sensitive MCP scalar arguments should be summarized by digest."""
    from agentgrep.mcp.middleware import _summarize_args

    summary = _summarize_args({case.key: case.value})

    assert isinstance(summary[case.key], dict)
    assert set(summary[case.key]) == {"len", "sha256_prefix"}
    assert summary[case.key]["len"] == len(case.value)
    assert case.value not in str(summary)


def test_flatten_safe_attributes_keeps_redacted_mcp_args_safe() -> None:
    """MCP telemetry attributes should carry redacted shape metadata only."""
    import agentgrep._telemetry as telemetry
    from agentgrep.mcp.middleware import _summarize_args

    query = "agent:codex secret-query"
    source_path = "/tmp/agentgrep/history.json"
    summary = _summarize_args(
        {
            "query": query,
            "terms": ["secret-token"],
            "pattern": "another-secret",
            "source_path": source_path,
        },
    )
    attributes = telemetry.flatten_safe_attributes("agentgrep_mcp_args", summary)

    rendered = str(attributes)
    assert "secret-query" not in rendered
    assert "secret-token" not in rendered
    assert "another-secret" not in rendered
    assert source_path not in rendered
    assert attributes["agentgrep_mcp_args.query.len"] == len(query)
    assert attributes["agentgrep_mcp_args.terms.0.len"] == len("secret-token")
    assert attributes["agentgrep_mcp_args.pattern.len"] == len("another-secret")
    assert attributes["agentgrep_mcp_args.source_path.kind"] == "path"
    assert attributes["agentgrep_mcp_args.source_path.len"] == len(source_path)
    assert attributes["agentgrep_mcp_args.source_path.is_absolute"] is True


def test_cli_main_emits_non_single_trace_with_linked_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI dispatch should emit a root, a child, metrics, and linked logs."""
    import agentgrep
    import agentgrep._telemetry as telemetry

    backend = telemetry.InMemoryTelemetryBackend()

    def fake_setup(**_kwargs: object) -> telemetry.TelemetryHandle:
        telemetry.configure_backend(backend)
        remove_handler = telemetry.install_logging_exporter(backend)
        return telemetry.TelemetryHandle(
            mode="test",
            backend=backend,
            _remove_logging=remove_handler,
        )

    args = agentgrep.SearchArgs(
        terms=("bliss",),
        agents=("codex",),
        scope="prompts",
        case_sensitive=False,
        limit=1,
        output_mode="text",
        color_mode="never",
        progress_mode="never",
    )
    monkeypatch.setattr(telemetry, "setup", fake_setup)
    monkeypatch.setattr(agentgrep, "parse_args", lambda _argv: args)
    monkeypatch.setattr(agentgrep, "run_search_command", lambda _args: 0)

    try:
        assert agentgrep.main(["search", "bliss"]) == 0
    finally:
        telemetry.configure_backend(None)

    assert backend.single_root_trace_ids() == ()
    assert [span.name for span in backend.finished_spans] == [
        "agentgrep.cli.parse",
        "agentgrep.cli.dispatch",
        "agentgrep.cli.invocation",
    ]
    assert {record.trace_id for record in backend.log_records} == {
        backend.finished_spans[-1].trace_id,
    }
    assert all(record.span_id is not None for record in backend.log_records)


def test_cli_help_emits_non_single_trace_without_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI help should be traced without changing argparse output."""
    import agentgrep
    import agentgrep._telemetry as telemetry

    backend = telemetry.InMemoryTelemetryBackend()

    def fake_setup(**_kwargs: object) -> telemetry.TelemetryHandle:
        telemetry.configure_backend(backend)
        remove_handler = telemetry.install_logging_exporter(backend)
        return telemetry.TelemetryHandle(
            mode="test",
            backend=backend,
            _remove_logging=remove_handler,
        )

    monkeypatch.setattr(telemetry, "setup", fake_setup)

    try:
        exit_code = agentgrep.main([])
    finally:
        telemetry.configure_backend(None)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "grep examples:" in captured.out
    assert captured.err == ""
    assert backend.single_root_trace_ids() == ()
    assert [span.name for span in backend.finished_spans] == [
        "agentgrep.cli.parse",
        "agentgrep.cli.invocation",
    ]
    root = backend.finished_spans[-1]
    assert root.attributes["agentgrep_outcome"] == "help"
    assert root.attributes["agentgrep_exit_code"] == 0


def test_cli_parse_error_emits_non_single_trace_with_argparse_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI parse errors should be traced while preserving argparse stderr."""
    import agentgrep
    import agentgrep._telemetry as telemetry

    backend = telemetry.InMemoryTelemetryBackend()

    def fake_setup(**_kwargs: object) -> telemetry.TelemetryHandle:
        telemetry.configure_backend(backend)
        remove_handler = telemetry.install_logging_exporter(backend)
        return telemetry.TelemetryHandle(
            mode="test",
            backend=backend,
            _remove_logging=remove_handler,
        )

    monkeypatch.setattr(telemetry, "setup", fake_setup)

    try:
        with pytest.raises(SystemExit) as exc_info:
            agentgrep.main(["grep", "["])
        exit_code = exc_info.value.code
    finally:
        telemetry.configure_backend(None)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "invalid regex '['" in captured.err
    assert backend.single_root_trace_ids() == ()
    assert [span.name for span in backend.finished_spans] == [
        "agentgrep.cli.parse",
        "agentgrep.cli.invocation",
    ]
    parse_span, root = backend.finished_spans
    assert root.attributes["agentgrep_outcome"] == "parse_error"
    assert root.attributes["agentgrep_exit_code"] == 2
    assert root.status == "error"
    assert parse_span.status == "error"


def test_cli_main_uses_cli_service_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI process should report an entrypoint-specific OTel service."""
    import agentgrep
    import agentgrep._telemetry as telemetry

    setup_kwargs: list[dict[str, object]] = []

    def fake_setup(**kwargs: object) -> telemetry.TelemetryHandle:
        setup_kwargs.append(kwargs)
        return telemetry.TelemetryHandle(mode="off")

    monkeypatch.setattr(telemetry, "setup", fake_setup)
    monkeypatch.setattr(agentgrep, "parse_args", lambda _argv: None)

    assert agentgrep.main([]) == 0
    assert setup_kwargs
    assert setup_kwargs[0]["service_name"] == "agentgrep-cli"


def test_mcp_main_uses_mcp_service_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The MCP server process should report an entrypoint-specific OTel service."""
    import agentgrep._telemetry as telemetry
    import agentgrep.mcp.server as mcp_server

    setup_kwargs: list[dict[str, object]] = []

    class FakeServer:
        def run(self) -> None:
            return None

    def fake_setup(**kwargs: object) -> telemetry.TelemetryHandle:
        setup_kwargs.append(kwargs)
        return telemetry.TelemetryHandle(mode="off")

    monkeypatch.setattr(telemetry, "setup", fake_setup)
    monkeypatch.setattr(mcp_server, "build_mcp_server", lambda: FakeServer())

    assert mcp_server.main() == 0
    assert setup_kwargs
    assert setup_kwargs[0]["service_name"] == "agentgrep-mcp"


def test_mcp_main_traces_lifecycle_and_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short MCP stdio processes should expose lifecycle and flush boundaries."""
    import agentgrep._telemetry as telemetry
    import agentgrep.mcp.server as mcp_server

    class CountingBackend(telemetry.InMemoryTelemetryBackend):
        """In-memory backend that exposes flush and shutdown calls."""

        def __init__(self) -> None:
            super().__init__()
            self.force_flush_calls: list[int] = []
            self.shutdown_count = 0

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            self.force_flush_calls.append(timeout_millis)
            return True

        def shutdown(self) -> None:
            self.shutdown_count += 1

    class FakeServer:
        def run(self) -> None:
            run_calls.append("run")

    backend = CountingBackend()
    run_calls: list[str] = []

    def fake_setup(**_kwargs: object) -> telemetry.TelemetryHandle:
        telemetry.configure_backend(backend)
        remove_handler = telemetry.install_logging_exporter(backend)
        return telemetry.TelemetryHandle(
            mode="test",
            backend=backend,
            _remove_logging=remove_handler,
        )

    monkeypatch.setattr(telemetry, "setup", fake_setup)
    monkeypatch.setattr(mcp_server, "build_mcp_server", lambda: FakeServer())

    try:
        assert mcp_server.main() == 0
    finally:
        telemetry.configure_backend(None)

    assert run_calls == ["run"]
    assert backend.force_flush_calls == [2_000]
    assert backend.shutdown_count == 1
    server_span = next(
        span for span in backend.finished_spans if span.name == "agentgrep.mcp.server"
    )
    lifecycle_span = next(
        span for span in backend.finished_spans if span.name == "agentgrep.mcp.server.lifecycle"
    )
    flush_span = next(span for span in backend.finished_spans if span.name == "agentgrep.mcp.flush")
    assert server_span.parent_id is None
    assert lifecycle_span.parent_id == server_span.span_id
    assert flush_span.parent_id == server_span.span_id
    assert server_span.trace_id not in backend.single_root_trace_ids()
    flush_log = next(
        record for record in backend.log_records if record.message == "mcp telemetry flushed"
    )
    complete_log = next(
        record for record in backend.log_records if record.message == "mcp server completed"
    )
    assert flush_log.trace_id == flush_span.trace_id
    assert flush_log.span_id == flush_span.span_id
    assert complete_log.trace_id == server_span.trace_id
    assert complete_log.span_id == server_span.span_id
    assert flush_log.attributes["agentgrep_mcp_flush_ok"] is True


async def test_mcp_tool_span_is_non_single_and_redacted(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP tool telemetry should be parented and redacted."""
    from fastmcp import Client

    import agentgrep._telemetry as telemetry
    from agentgrep import mcp as agentgrep_mcp

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    session = home / ".codex" / "sessions" / "2026" / "01" / "01" / "session.jsonl"
    session.parent.mkdir(parents=True)
    _ = session.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "type": "session_meta",
                    "payload": {"id": "session-1", "model_provider": "openai"},
                },
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "secret-token"}],
                    },
                },
            ]
        ),
        encoding="utf-8",
    )

    backend = telemetry.InMemoryTelemetryBackend()
    telemetry.configure_backend(backend)
    remove_handler = telemetry.install_logging_exporter(backend)
    try:
        async with Client(agentgrep_mcp.build_mcp_server()) as client:
            _ = await client.call_tool(
                "search",
                {"terms": ["secret-token"], "agent": "codex", "scope": "prompts", "limit": 1},
            )
    finally:
        remove_handler()
        telemetry.configure_backend(None)

    assert backend.single_root_trace_ids() == ()
    tool_span = next(span for span in backend.finished_spans if span.name == "agentgrep.mcp.tool")
    request_span = next(
        span
        for span in backend.finished_spans
        if span.name == "agentgrep.mcp.request" and span.trace_id == tool_span.trace_id
    )
    operation_span = next(
        span
        for span in backend.finished_spans
        if span.name == "agentgrep.mcp.operation" and span.trace_id == tool_span.trace_id
    )
    assert request_span.parent_id is None
    assert operation_span.parent_id == request_span.span_id
    assert tool_span.parent_id == operation_span.span_id
    assert "secret-token" not in str(tool_span.attributes)
    assert tool_span.attributes["agentgrep_mcp_args.terms.0.len"] == len("secret-token")
    assert any(record.trace_id == request_span.trace_id for record in backend.log_records)
    assert all(record.trace_id is not None for record in backend.log_records)
    assert all(record.span_id is not None for record in backend.log_records)
    assert "secret-token" not in str([record.attributes for record in backend.log_records])


async def test_mcp_validate_query_span_redacts_query_arg() -> None:
    """``validate_query(query=...)`` should not export raw query text."""
    from fastmcp import Client

    import agentgrep._telemetry as telemetry
    from agentgrep import mcp as agentgrep_mcp

    query = "agent:codex secret-query"
    backend = telemetry.InMemoryTelemetryBackend()
    telemetry.configure_backend(backend)
    remove_handler = telemetry.install_logging_exporter(backend)
    try:
        async with Client(agentgrep_mcp.build_mcp_server()) as client:
            _ = await client.call_tool("validate_query", {"query": query})
    finally:
        remove_handler()
        telemetry.configure_backend(None)

    tool_span = next(span for span in backend.finished_spans if span.name == "agentgrep.mcp.tool")
    assert "secret-query" not in str(tool_span.attributes)
    assert "agentgrep_mcp_args.query" not in tool_span.attributes
    assert tool_span.attributes["agentgrep_mcp_args.query.len"] == len(query)
    assert "secret-query" not in str([record.attributes for record in backend.log_records])


async def test_mcp_list_tools_gets_request_root() -> None:
    """MCP list operations should not rely on tool-only roots or logs."""
    from fastmcp import Client

    import agentgrep._telemetry as telemetry
    from agentgrep import mcp as agentgrep_mcp

    backend = telemetry.InMemoryTelemetryBackend()
    telemetry.configure_backend(backend)
    remove_handler = telemetry.install_logging_exporter(backend)
    try:
        async with Client(agentgrep_mcp.build_mcp_server()) as client:
            _ = await client.list_tools()
    finally:
        remove_handler()
        telemetry.configure_backend(None)

    assert backend.single_root_trace_ids() == ()
    assert [span.name for span in backend.finished_spans] == [
        "agentgrep.mcp.operation",
        "agentgrep.mcp.request",
    ]
    request_span = backend.finished_spans[-1]
    operation_span = backend.finished_spans[0]
    assert request_span.parent_id is None
    assert request_span.attributes["agentgrep_mcp_method"] == "tools/list"
    assert operation_span.parent_id == request_span.span_id
    assert operation_span.attributes["agentgrep_mcp_method"] == "tools/list"
    request_log = next(
        record for record in backend.log_records if record.message == "mcp request completed"
    )
    assert request_log.attributes["agentgrep_surface"] == "mcp"
    assert request_log.attributes["agentgrep_operation"] == "mcp.request"
    assert request_log.attributes["agentgrep_mcp_method"] == "tools/list"
    assert request_log.attributes["agentgrep_outcome"] == "ok"
    assert request_log.trace_id == request_span.trace_id
    assert request_log.span_id == request_span.span_id


def test_otel_backend_records_named_custom_metrics() -> None:
    """OTel custom metrics should use their own instruments."""
    from agentgrep import _telemetry_otel

    class FakeInstrument:
        def __init__(self) -> None:
            self.points: list[tuple[int | float, dict[str, object]]] = []

        def add(self, value: int | float, *, attributes: dict[str, object]) -> None:
            self.points.append((value, attributes))

        def record(self, value: int | float, *, attributes: dict[str, object]) -> None:
            self.points.append((value, attributes))

    class FakeMeter:
        def __init__(self) -> None:
            self.counters: dict[str, FakeInstrument] = {}
            self.histograms: dict[str, FakeInstrument] = {}

        def create_counter(self, name: str, **_kwargs: object) -> FakeInstrument:
            instrument = FakeInstrument()
            self.counters[name] = instrument
            return instrument

        def create_histogram(self, name: str, **_kwargs: object) -> FakeInstrument:
            instrument = FakeInstrument()
            self.histograms[name] = instrument
            return instrument

    class FakeProvider:
        def force_flush(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

    class FakeHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            return None

    fake_meter = FakeMeter()
    backend = _telemetry_otel.OtelTelemetryBackend(
        tracer=None,
        tracer_provider=FakeProvider(),
        meter=fake_meter,
        meter_provider=FakeProvider(),
        logger_provider=FakeProvider(),
        logging_handler=FakeHandler(),
        span_counter=FakeInstrument(),
        span_duration=FakeInstrument(),
        instrumentations=(),
        profiles_started=False,
    )

    backend.record_metric("agentgrep.otel.cpu_loops", 42, {"agentgrep_surface": "otel"})
    backend.record_metric("agentgrep.otel.event.count", 1, {"agentgrep_surface": "otel"})
    backend.record_metric("agentgrep.grep.duration", 0.5, {"agentgrep_surface": "otel"})

    assert set(fake_meter.counters) == {"agentgrep.otel.cpu_loops", "agentgrep.otel.event.count"}
    assert set(fake_meter.histograms) == {"agentgrep.grep.duration"}
    assert fake_meter.counters["agentgrep.otel.cpu_loops"].points == [
        (42, {"agentgrep_surface": "otel"}),
    ]
    assert fake_meter.counters["agentgrep.otel.event.count"].points == [
        (1, {"agentgrep_surface": "otel"}),
    ]
    assert fake_meter.histograms["agentgrep.grep.duration"].points == [
        (0.5, {"agentgrep_surface": "otel"}),
    ]


def test_otel_log_record_sanitizes_absolute_paths() -> None:
    """Exported OTel logs should not carry absolute local source paths."""
    from agentgrep import _telemetry_otel

    env_path = "/tmp/agentgrep/private-env"
    override_path = "/tmp/agentgrep/private-config"
    source_path = "/tmp/agentgrep/source.jsonl"
    record = logging.LogRecord(
        name="agentgrep.test",
        level=logging.INFO,
        pathname="/tmp/agentgrep/src/agentgrep/example.py",
        lineno=12,
        msg="message",
        args=(),
        exc_info=None,
    )
    record.agentgrep_env_path = env_path
    record.agentgrep_env_path_status = "not_found"
    record.agentgrep_override_path = override_path
    record.agentgrep_override_path_status = "not_a_directory"
    record.agentgrep_path = source_path
    record.agentgrep_path_kind = "session_file"

    sanitized = _telemetry_otel._sanitized_log_record(record)

    assert sanitized.pathname == "example.py"
    assert sanitized.filename == "example.py"
    rendered = str(sanitized.__dict__)
    assert env_path not in rendered
    assert override_path not in rendered
    assert source_path not in rendered
    assert "agentgrep_env_path" not in sanitized.__dict__
    assert sanitized.__dict__["agentgrep_env_path_redacted"] is True
    assert sanitized.__dict__["agentgrep_env_path_len"] == len(env_path)
    assert "agentgrep_override_path" not in sanitized.__dict__
    assert sanitized.__dict__["agentgrep_override_path_redacted"] is True
    assert "agentgrep_path" not in sanitized.__dict__
    assert sanitized.__dict__["agentgrep_path_redacted"] is True
    assert sanitized.__dict__["agentgrep_env_path_status"] == "not_found"
    assert sanitized.__dict__["agentgrep_override_path_status"] == "not_a_directory"
    assert sanitized.__dict__["agentgrep_path_kind"] == "session_file"
    assert record.pathname == "/tmp/agentgrep/src/agentgrep/example.py"


def test_otel_log_record_keeps_safe_extras_as_attributes() -> None:
    """Exported OTel logs keep the plain message body and safe extras as attributes."""
    from agentgrep import _telemetry_otel

    record = logging.LogRecord(
        name="agentgrep.test",
        level=logging.INFO,
        pathname="/tmp/agentgrep/src/agentgrep/example.py",
        lineno=12,
        msg="search completed",
        args=(),
        exc_info=None,
    )
    record.agentgrep_surface = "cli"
    record.agentgrep_operation = "search.run"
    record.agentgrep_result_count = 3

    sanitized = _telemetry_otel._sanitized_log_record(record)
    attributes = sanitized.__dict__

    assert sanitized.getMessage() == "search completed"
    assert attributes["agentgrep_surface"] == "cli"
    assert attributes["agentgrep_operation"] == "search.run"
    assert attributes["agentgrep_result_count"] == 3


@pytest.mark.parametrize(
    "case",
    SENSITIVE_LOG_EXTRA_CASES,
    ids=[case.test_id for case in SENSITIVE_LOG_EXTRA_CASES],
)
def test_otel_log_record_redacts_sensitive_extras(case: SensitiveLogExtraCase) -> None:
    """Redacted log extras become shape metadata attributes, not private values."""
    from agentgrep import _telemetry_otel

    record = logging.LogRecord(
        name="agentgrep.test",
        level=logging.WARNING,
        pathname="/tmp/agentgrep/src/agentgrep/example.py",
        lineno=12,
        msg="warning fired",
        args=(),
        exc_info=None,
    )
    setattr(record, case.key, case.value)

    sanitized = _telemetry_otel._sanitized_log_record_dict(record)

    assert case.key not in sanitized
    assert sanitized[f"{case.key}_redacted"] is True
    assert sanitized[f"{case.key}_len"] == len(case.value)
    assert len(str(sanitized[f"{case.key}_sha256_prefix"])) == 12


def test_otel_backend_exports_logs_with_current_otel_span() -> None:
    """Live OTel logs should link when only an OTel span is current."""
    from agentgrep import _telemetry_otel

    class FakeSpanContext:
        trace_id = int("1" * 32, 16)
        span_id = int("2" * 16, 16)

        @property
        def is_valid(self) -> bool:
            return True

    class FakeSpan:
        def get_span_context(self) -> FakeSpanContext:
            return FakeSpanContext()

    class FakeTraceModule:
        INVALID_SPAN = object()

        @staticmethod
        def get_current_span() -> FakeSpan:
            return FakeSpan()

    class FakeProvider:
        def force_flush(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

    class FakeMeter:
        def create_counter(self, _name: str, **_kwargs: object) -> object:
            return object()

        def create_histogram(self, _name: str, **_kwargs: object) -> object:
            return object()

    class CapturingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    class FakeInstrument:
        def add(self, _value: int | float, *, attributes: dict[str, object]) -> None:
            del attributes

        def record(self, _value: int | float, *, attributes: dict[str, object]) -> None:
            del attributes

    handler = CapturingHandler()
    backend = _telemetry_otel.OtelTelemetryBackend(
        tracer=None,
        tracer_provider=FakeProvider(),
        meter=FakeMeter(),
        meter_provider=FakeProvider(),
        logger_provider=FakeProvider(),
        logging_handler=handler,
        span_counter=FakeInstrument(),
        span_duration=FakeInstrument(),
        instrumentations=(),
        profiles_started=False,
        trace_api=t.cast("object", FakeTraceModule),
    )
    record = logging.LogRecord(
        name="agentgrep.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="otel linked",
        args=(),
        exc_info=None,
    )

    backend.emit_log(record, active_span=None)

    assert len(handler.records) == 1
    assert handler.records[0].getMessage() == "otel linked"
