"""Tests for project-local telemetry helpers."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import pathlib
import sqlite3

import pytest


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

    sql_span = next(
        span for span in backend.finished_spans if span.name == "agentgrep.sqlite.execute"
    )
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


def test_flatten_safe_attributes_keeps_redacted_mcp_args_safe() -> None:
    """MCP telemetry attributes should carry redacted shape metadata only."""
    import agentgrep._telemetry as telemetry
    from agentgrep.mcp.middleware import _summarize_args

    summary = _summarize_args({"terms": ["secret-token"], "pattern": "another-secret"})
    attributes = telemetry.flatten_safe_attributes("agentgrep_mcp_args", summary)

    rendered = str(attributes)
    assert "secret-token" not in rendered
    assert "another-secret" not in rendered
    assert attributes["agentgrep_mcp_args.terms.0.len"] == len("secret-token")
    assert attributes["agentgrep_mcp_args.pattern.len"] == len("another-secret")


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
        "agentgrep.cli.dispatch",
        "agentgrep.cli.invocation",
    ]
    assert {record.trace_id for record in backend.log_records} == {
        backend.finished_spans[-1].trace_id,
    }
    assert all(record.span_id is not None for record in backend.log_records)


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
    assert tool_span.parent_id is None
    assert "secret-token" not in str(tool_span.attributes)
    assert tool_span.attributes["agentgrep_mcp_args.terms.0.len"] == len("secret-token")
    assert all(record.trace_id == tool_span.trace_id for record in backend.log_records)
    assert all(record.span_id is not None for record in backend.log_records)


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

    assert set(fake_meter.histograms) == {"agentgrep.otel.cpu_loops"}
    assert set(fake_meter.counters) == {"agentgrep.otel.event.count"}
    assert fake_meter.histograms["agentgrep.otel.cpu_loops"].points == [
        (42, {"agentgrep_surface": "otel"}),
    ]
    assert fake_meter.counters["agentgrep.otel.event.count"].points == [
        (1, {"agentgrep_surface": "otel"}),
    ]


def test_otel_log_record_sanitizes_absolute_paths() -> None:
    """Exported OTel logs should not carry absolute local source paths."""
    from agentgrep import _telemetry_otel

    record = logging.LogRecord(
        name="agentgrep.test",
        level=logging.INFO,
        pathname="/home/d/work/python/agentgrep/src/agentgrep/example.py",
        lineno=12,
        msg="message",
        args=(),
        exc_info=None,
    )

    sanitized = _telemetry_otel._sanitized_log_record(record)

    assert sanitized.pathname == "example.py"
    assert sanitized.filename == "example.py"
    assert record.pathname == "/home/d/work/python/agentgrep/src/agentgrep/example.py"
