"""Tests for DB cache CLI controls."""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import typing as t

import pytest

import agentgrep
import agentgrep.cli.render as render
from agentgrep.db import (
    ANSWERABLE_QUERY_FORMS,
    DbExplain,
    DbStatus,
    DbSyncProgress,
    SyncCoverage,
    SyncResult,
)
from agentgrep.insights import OmissionFinding, VariantEdge
from agentgrep.suggestions import SuggestionArtifact

if t.TYPE_CHECKING:
    import collections.abc as cabc


class CacheFlagCase(t.NamedTuple):
    """Named case for search-shaped cache flag parsing."""

    test_id: str
    argv: tuple[str, ...]
    expected_cache_mode: agentgrep.CacheMode


class StructuredOutputCase(t.NamedTuple):
    """Named case for small structured CLI payload rendering."""

    test_id: str
    payload: object
    output_mode: t.Literal["json", "ndjson"]
    expected_documents: tuple[object, ...]


class StructuredTextOutputCase(t.NamedTuple):
    """Named case for small structured CLI text rendering."""

    test_id: str
    payload: object
    expected_contains: tuple[str, ...]
    expected_not_contains: tuple[str, ...]


class CommandGroupHelpCase(t.NamedTuple):
    """Named case for command-group directory help behavior."""

    test_id: str
    argv: tuple[str, ...]
    expected_usage: str
    expected_examples_heading: str


class DbSyncProgressFlagCase(t.NamedTuple):
    """Named case for DB sync progress and color flag parsing."""

    test_id: str
    argv: tuple[str, ...]
    expected_progress_mode: agentgrep.ProgressMode
    expected_color_mode: agentgrep.ColorMode


class DbSyncModeFlagCase(t.NamedTuple):
    """Named case for DB sync cache-refresh flag parsing."""

    test_id: str
    argv: tuple[str, ...]
    expected_features_mode: t.Literal["defer", "inline"]
    expected_force: bool


class InsightsAnalyzeFlagCase(t.NamedTuple):
    """Named case for insights analyze progress flag parsing."""

    test_id: str
    argv: tuple[str, ...]
    expected_progress_mode: agentgrep.ProgressMode


CACHE_FLAG_CASES: tuple[CacheFlagCase, ...] = (
    CacheFlagCase("search-default-auto", ("search", "ruff"), "auto"),
    CacheFlagCase("search-no-cache-off", ("search", "--no-cache", "ruff"), "off"),
    CacheFlagCase("search-require", ("search", "--cache", "require", "ruff"), "require"),
    CacheFlagCase("grep-default-auto", ("grep", "ruff"), "auto"),
    CacheFlagCase("grep-no-cache-off", ("grep", "--no-cache", "ruff"), "off"),
    CacheFlagCase("grep-cache-off", ("grep", "--cache", "off", "ruff"), "off"),
)


DB_SYNC_PROGRESS_FLAG_CASES: tuple[DbSyncProgressFlagCase, ...] = (
    DbSyncProgressFlagCase(
        test_id="default-auto",
        argv=("db", "sync"),
        expected_progress_mode="auto",
        expected_color_mode="auto",
    ),
    DbSyncProgressFlagCase(
        test_id="explicit-never",
        argv=("db", "sync", "--progress", "never"),
        expected_progress_mode="never",
        expected_color_mode="auto",
    ),
    DbSyncProgressFlagCase(
        test_id="no-progress-alias",
        argv=("db", "sync", "--no-progress"),
        expected_progress_mode="never",
        expected_color_mode="auto",
    ),
    DbSyncProgressFlagCase(
        test_id="forced-color",
        argv=("--color", "always", "db", "sync", "--progress", "always"),
        expected_progress_mode="always",
        expected_color_mode="always",
    ),
)


DB_SYNC_MODE_FLAG_CASES: tuple[DbSyncModeFlagCase, ...] = (
    DbSyncModeFlagCase(
        test_id="default-defer-skip-current",
        argv=("db", "sync"),
        expected_features_mode="defer",
        expected_force=False,
    ),
    DbSyncModeFlagCase(
        test_id="inline-features",
        argv=("db", "sync", "--features", "inline"),
        expected_features_mode="inline",
        expected_force=False,
    ),
    DbSyncModeFlagCase(
        test_id="force-resync",
        argv=("db", "sync", "--force"),
        expected_features_mode="defer",
        expected_force=True,
    ),
)


INSIGHTS_ANALYZE_FLAG_CASES: tuple[InsightsAnalyzeFlagCase, ...] = (
    InsightsAnalyzeFlagCase(
        test_id="default-auto",
        argv=("insights", "analyze"),
        expected_progress_mode="auto",
    ),
    InsightsAnalyzeFlagCase(
        test_id="explicit-never",
        argv=("insights", "analyze", "--progress", "never"),
        expected_progress_mode="never",
    ),
    InsightsAnalyzeFlagCase(
        test_id="no-progress-alias",
        argv=("insights", "analyze", "--no-progress"),
        expected_progress_mode="never",
    ),
)


COMMAND_GROUP_HELP_CASES: tuple[CommandGroupHelpCase, ...] = (
    CommandGroupHelpCase(
        test_id="db",
        argv=("db",),
        expected_usage="usage: agentgrep db",
        expected_examples_heading="db examples:",
    ),
    CommandGroupHelpCase(
        test_id="insights",
        argv=("insights",),
        expected_usage="usage: agentgrep insights",
        expected_examples_heading="insights examples:",
    ),
    CommandGroupHelpCase(
        test_id="suggestions",
        argv=("suggestions",),
        expected_usage="usage: agentgrep suggestions",
        expected_examples_heading="suggestions examples:",
    ),
)


STRUCTURED_OUTPUT_CASES: tuple[StructuredOutputCase, ...] = (
    StructuredOutputCase(
        test_id="dataclass-json",
        payload=SyncResult(sources_synced=1, records_indexed=2, records_removed=0),
        output_mode="json",
        expected_documents=(
            {
                "sources_synced": 1,
                "records_indexed": 2,
                "records_removed": 0,
                "sources_skipped": 0,
                "sources_pruned": 0,
                "features_deferred": 0,
            },
        ),
    ),
    StructuredOutputCase(
        test_id="dataclass-ndjson",
        payload=SyncResult(sources_synced=1, records_indexed=2, records_removed=0),
        output_mode="ndjson",
        expected_documents=(
            {
                "sources_synced": 1,
                "records_indexed": 2,
                "records_removed": 0,
                "sources_skipped": 0,
                "sources_pruned": 0,
                "features_deferred": 0,
            },
        ),
    ),
    StructuredOutputCase(
        test_id="list-ndjson",
        payload=[
            SyncResult(sources_synced=1, records_indexed=2, records_removed=0),
            SyncResult(sources_synced=3, records_indexed=4, records_removed=1),
        ],
        output_mode="ndjson",
        expected_documents=(
            {
                "sources_synced": 1,
                "records_indexed": 2,
                "records_removed": 0,
                "sources_skipped": 0,
                "sources_pruned": 0,
                "features_deferred": 0,
            },
            {
                "sources_synced": 3,
                "records_indexed": 4,
                "records_removed": 1,
                "sources_skipped": 0,
                "sources_pruned": 0,
                "features_deferred": 0,
            },
        ),
    ),
    StructuredOutputCase(
        test_id="mapping-ndjson",
        payload={
            "results": [
                SyncResult(sources_synced=1, records_indexed=2, records_removed=0),
            ],
        },
        output_mode="ndjson",
        expected_documents=(
            {
                "results": [
                    {
                        "sources_synced": 1,
                        "records_indexed": 2,
                        "records_removed": 0,
                        "sources_skipped": 0,
                        "sources_pruned": 0,
                        "features_deferred": 0,
                    },
                ],
            },
        ),
    ),
)


STRUCTURED_TEXT_OUTPUT_CASES: tuple[StructuredTextOutputCase, ...] = (
    StructuredTextOutputCase(
        test_id="db-status",
        payload=DbStatus(
            db_path=pathlib.Path("/tmp/agentgrep.sqlite"),
            schema_version=1,
            sources=2,
            records=3,
            features=4,
            variant_edges=5,
            omission_findings=6,
            suggestions=7,
        ),
        expected_contains=(
            "DB status",
            "/tmp/agentgrep.sqlite",
            "2 sources",
            "3 records",
            "5 variant edges",
        ),
        expected_not_contains=("DbStatus(", "{", "'db_path'"),
    ),
    StructuredTextOutputCase(
        test_id="db-sync",
        payload=SyncResult(
            sources_synced=2,
            records_indexed=3,
            records_removed=1,
            sources_skipped=4,
            features_deferred=5,
        ),
        expected_contains=(
            "DB sync",
            "2 sources",
            "3 records indexed",
            "1 record removed",
            "4 sources skipped",
            "5 features deferred",
        ),
        expected_not_contains=("SyncResult(", "{", "'sources_synced'"),
    ),
    StructuredTextOutputCase(
        test_id="db-sync-with-pruned",
        payload=SyncResult(
            sources_synced=2,
            records_indexed=3,
            records_removed=4,
            sources_pruned=1,
        ),
        expected_contains=(
            "DB sync",
            "1 vanished source pruned",
        ),
        expected_not_contains=("SyncResult(",),
    ),
    StructuredTextOutputCase(
        test_id="db-explain-with-coverage",
        payload=DbExplain(
            db_path=pathlib.Path("/tmp/agentgrep.sqlite"),
            schema_version=1,
            sources=2,
            records=3,
            synced_ok=2,
            sync_errors=0,
            last_synced_at="2026-06-07T00:00:00Z",
            answerable=ANSWERABLE_QUERY_FORMS,
            coverage={"codex": ("all",), "claude": ("prompts",)},
        ),
        expected_contains=(
            "DB explain",
            "Coverage",
            "claude=prompts",
            "codex=all",
        ),
        expected_not_contains=("DbExplain(", "not recorded"),
    ),
    StructuredTextOutputCase(
        test_id="db-explain-coverage-not-recorded",
        payload=DbExplain(
            db_path=pathlib.Path("/tmp/agentgrep.sqlite"),
            schema_version=1,
            sources=0,
            records=0,
            synced_ok=0,
            sync_errors=0,
            last_synced_at=None,
            answerable=ANSWERABLE_QUERY_FORMS,
            coverage=None,
        ),
        expected_contains=("DB explain", "Coverage", "not recorded"),
        expected_not_contains=("DbExplain(",),
    ),
    StructuredTextOutputCase(
        test_id="insights-list",
        payload={
            "limit": 10,
            "variant_edges": {
                "total": 100,
                "returned": 1,
                "truncated": True,
                "items": [
                    VariantEdge(
                        edge_id="edge-1",
                        run_id="run-1",
                        left_record_id="left-record",
                        right_record_id="right-record",
                        variant_type="exact_duplicate",
                        confidence=1.0,
                        explanation="normalized prompt text is identical",
                    ),
                ],
            },
            "omission_findings": {
                "total": 2,
                "returned": 1,
                "truncated": True,
                "items": [
                    OmissionFinding(
                        finding_id="finding-1",
                        run_id="run-1",
                        target_path=pathlib.Path("AGENTS.md"),
                        representative_record_id="record-1",
                        confidence=0.82,
                        rationale="neighboring projects repeat this instruction",
                    ),
                ],
            },
        },
        expected_contains=(
            "Insights",
            "limit 10",
            "1/100 variant edges",
            "edge-1",
            "exact_duplicate",
            "1/2 omission findings",
            "finding-1",
        ),
        expected_not_contains=("VariantEdge(", "OmissionFinding(", "{", "'variant_edges'"),
    ),
    StructuredTextOutputCase(
        test_id="suggestions-list",
        payload=[
            SuggestionArtifact(
                suggestion_id="suggestion-1",
                run_id="run-1",
                target_path=pathlib.Path("AGENTS.md"),
                surface_kind="agents_md",
                title="Add missing agent instruction",
                body="Run ruff check before committing.",
                confidence=0.92,
                status="proposed",
                rationale="repeated nearby instruction",
                reload_note="Reload the agent session.",
            ),
        ],
        expected_contains=(
            "Suggestions",
            "suggestion-1",
            "AGENTS.md",
            "0.92",
            "proposed",
            "Add missing agent instruction",
        ),
        expected_not_contains=("SuggestionArtifact(", "{", "'suggestion_id'"),
    ),
)


def _source(path: pathlib.Path) -> agentgrep.SourceHandle:
    """Build a synthetic source handle for CLI tests."""
    return agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=path.parent,
        mtime_ns=0,
    )


@pytest.mark.parametrize(
    "case",
    CACHE_FLAG_CASES,
    ids=[case.test_id for case in CACHE_FLAG_CASES],
)
def test_search_shaped_commands_parse_cache_mode(case: CacheFlagCase) -> None:
    """Search and grep expose cache controls for fresh/cold benchmark runs."""
    parsed = agentgrep.parse_args(case.argv)

    assert isinstance(parsed, (agentgrep.SearchArgs, agentgrep.GrepArgs))
    assert parsed.cache_mode == case.expected_cache_mode


@pytest.mark.parametrize(
    "case",
    DB_SYNC_PROGRESS_FLAG_CASES,
    ids=[case.test_id for case in DB_SYNC_PROGRESS_FLAG_CASES],
)
def test_db_sync_parses_progress_and_color_modes(case: DbSyncProgressFlagCase) -> None:
    """DB sync exposes the same status controls as grep/search."""
    parsed = agentgrep.parse_args(case.argv)

    assert isinstance(parsed, agentgrep.DbArgs)
    assert parsed.progress_mode == case.expected_progress_mode
    assert parsed.color_mode == case.expected_color_mode


@pytest.mark.parametrize(
    "case",
    DB_SYNC_MODE_FLAG_CASES,
    ids=[case.test_id for case in DB_SYNC_MODE_FLAG_CASES],
)
def test_db_sync_parses_cache_refresh_modes(case: DbSyncModeFlagCase) -> None:
    """DB sync exposes cache-fast defaults and explicit full-refresh flags."""
    parsed = agentgrep.parse_args(case.argv)

    assert isinstance(parsed, agentgrep.DbArgs)
    assert parsed.features_mode == case.expected_features_mode
    assert parsed.force is case.expected_force


@pytest.mark.parametrize(
    "case",
    INSIGHTS_ANALYZE_FLAG_CASES,
    ids=[case.test_id for case in INSIGHTS_ANALYZE_FLAG_CASES],
)
def test_insights_analyze_parses_progress_modes(case: InsightsAnalyzeFlagCase) -> None:
    """Insights analyze exposes the same progress controls as DB sync."""
    parsed = agentgrep.parse_args(case.argv)

    assert isinstance(parsed, agentgrep.InsightsArgs)
    assert parsed.action == "analyze"
    assert parsed.progress_mode == case.expected_progress_mode


@pytest.mark.parametrize(
    "case",
    COMMAND_GROUP_HELP_CASES,
    ids=[case.test_id for case in COMMAND_GROUP_HELP_CASES],
)
def test_command_groups_without_actions_print_help_directory(
    case: CommandGroupHelpCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DB/insight command groups act as help directories."""
    parsed = agentgrep.parse_args(case.argv)

    captured = capsys.readouterr()
    assert parsed is None
    assert captured.err == ""
    assert case.expected_usage in captured.out
    assert case.expected_examples_heading in captured.out


@pytest.mark.parametrize(
    "case",
    COMMAND_GROUP_HELP_CASES,
    ids=[case.test_id for case in COMMAND_GROUP_HELP_CASES],
)
def test_command_group_main_invocation_returns_success(
    case: CommandGroupHelpCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI entry point treats directory help as a successful command."""
    exit_code = agentgrep.main(case.argv)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert case.expected_usage in captured.out


@pytest.mark.parametrize(
    "case",
    STRUCTURED_OUTPUT_CASES,
    ids=[case.test_id for case in STRUCTURED_OUTPUT_CASES],
)
def test_small_structured_commands_emit_machine_readable_output(
    case: StructuredOutputCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DB, insight, and suggestion helpers honor JSON and NDJSON."""
    render._print_json_or_text(case.payload, output_mode=case.output_mode)

    captured = capsys.readouterr()
    if case.output_mode == "json":
        assert (json.loads(captured.out),) == case.expected_documents
        return

    documents = tuple(json.loads(line) for line in captured.out.splitlines() if line.strip())
    assert documents == case.expected_documents


@pytest.mark.parametrize(
    "case",
    STRUCTURED_TEXT_OUTPUT_CASES,
    ids=[case.test_id for case in STRUCTURED_TEXT_OUTPUT_CASES],
)
def test_small_structured_commands_emit_human_text_output(
    case: StructuredTextOutputCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Small structured commands render terminal summaries by default."""
    render._print_json_or_text(case.payload, output_mode="text", color_mode="never")

    captured = capsys.readouterr()
    assert not captured.out.lstrip().startswith(("{", "["))
    for expected in case.expected_contains:
        assert expected in captured.out
    for rejected in case.expected_not_contains:
        assert rejected not in captured.out


def test_small_structured_text_output_supports_semantic_color(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Small structured command text output uses the shared ANSI palette."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    render._print_json_or_text(
        SyncResult(sources_synced=1, records_indexed=2, records_removed=0),
        output_mode="text",
        color_mode="always",
    )

    captured = capsys.readouterr()
    assert "\x1b[" in captured.out
    assert "DB sync" in captured.out


def test_db_status_command_parses_db_path() -> None:
    """DB commands have typed parser output separate from search args."""
    parsed = agentgrep.parse_args(
        ("db", "status", "--db", "/tmp/agentgrep.sqlite", "--json"),
    )

    assert isinstance(parsed, agentgrep.DbArgs)
    assert parsed.action == "status"
    assert parsed.db_path == "/tmp/agentgrep.sqlite"
    assert parsed.output_mode == "json"


def test_db_sync_forced_progress_keeps_json_stdout_clean(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Forced DB sync progress writes status to stderr, never JSON stdout."""
    source = _source(tmp_path / "session.jsonl")

    class RuntimeStub:
        """Runtime stub that exercises the sync progress protocol."""

        features_mode: t.Literal["defer", "inline"] | None = None
        force: bool | None = None
        closed: bool = False
        coverage: SyncCoverage | None = None
        prune_missing: bool | None = None

        def close(self) -> None:
            """Record that the command closed its runtime."""
            self.closed = True

        def sync_sources(
            self,
            sources: t.Iterable[agentgrep.SourceHandle],
            *,
            control: agentgrep.SearchControl | None = None,
            progress: DbSyncProgress | None = None,
            features_mode: t.Literal["defer", "inline"] = "defer",
            force: bool = False,
            coverage: SyncCoverage | None = None,
            prune_missing: bool = False,
        ) -> SyncResult:
            self.features_mode = features_mode
            self.force = force
            self.coverage = coverage
            self.prune_missing = prune_missing
            source_list = tuple(sources)
            result = SyncResult(sources_synced=0, records_indexed=0, records_removed=0)
            assert control is not None
            if progress is not None:
                progress.start(len(source_list))
            for index, item in enumerate(source_list, start=1):
                if progress is not None:
                    progress.source_started(index, len(source_list), item, result)
                result = SyncResult(
                    sources_synced=index,
                    records_indexed=index * 2,
                    records_removed=0,
                )
                if progress is not None:
                    progress.source_finished(index, len(source_list), item, 2, 0, result)
            if progress is not None:
                progress.finish(result)
            return result

    def discover_sources_for_search(
        _home: pathlib.Path,
        _query: agentgrep.SearchQuery,
        _backends: agentgrep.BackendSelection,
        *,
        version_detail: agentgrep.DiscoveryVersionDetail,
    ) -> list[agentgrep.SourceHandle]:
        _ = version_detail
        return [source]

    runtime_stub = RuntimeStub()
    monkeypatch.setattr(render, "_open_db_runtime", lambda _path: runtime_stub)
    monkeypatch.setattr(
        agentgrep,
        "select_backends",
        lambda: agentgrep.BackendSelection(None, None, None),
    )
    monkeypatch.setattr(agentgrep, "discover_sources_for_search", discover_sources_for_search)
    args = agentgrep.DbArgs(
        action="sync",
        db_path=None,
        agents=("codex",),
        scope="all",
        output_mode="json",
        color_mode="never",
        progress_mode="always",
        features_mode="inline",
        force=True,
    )

    exit_code = render.run_db_command(args)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {
        "sources_synced": 1,
        "records_indexed": 2,
        "records_removed": 0,
        "sources_skipped": 0,
        "sources_pruned": 0,
        "features_deferred": 0,
    }
    assert runtime_stub.features_mode == "inline"
    assert runtime_stub.force is True
    assert runtime_stub.closed is True
    assert "DB sync" in captured.err
    assert "Sync complete:" in captured.err


def test_db_sync_tty_progress_renders_exit_hint() -> None:
    """TTY DB sync progress mirrors search hint and color semantics."""
    buffer = _StringBuffer()
    progress = render.ConsoleDbSyncProgress(
        enabled=True,
        stream=t.cast("t.TextIO", buffer),
        tty=True,
        color_mode="always",
        refresh_interval=60.0,
        answer_now_hint=True,
    )

    progress.start(1)
    progress.source_started(
        1,
        1,
        _source(pathlib.Path("session.jsonl")),
        SyncResult(sources_synced=0, records_indexed=0, records_removed=0),
    )
    progress.exiting_early(SyncResult(sources_synced=0, records_indexed=0, records_removed=0))

    output = buffer.getvalue()
    assert "\x1b[" in output
    assert "[Press enter, exit early]" in output
    assert "Exiting early:" in output


class _StringBuffer:
    """Small text stream stub for tty progress tests."""

    def __init__(self) -> None:
        self._parts: list[str] = []

    def write(self, text: str) -> int:
        """Capture written text."""
        self._parts.append(text)
        return len(text)

    def flush(self) -> None:
        """Skip flushing for the in-memory buffer."""

    def isatty(self) -> bool:
        """Pretend to be an interactive terminal."""
        return True

    def getvalue(self) -> str:
        """Return captured text."""
        return "".join(self._parts)


class _TinyTerminalBuffer(_StringBuffer):
    """TTY stream stub with a file descriptor for terminal-size probing."""

    def fileno(self) -> int:
        """Return a harmless descriptor number for monkeypatched size probes."""
        return 1


def test_insights_progress_tiny_tty_width_uses_readable_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Insights progress remains readable when a PTY reports zero columns."""
    monkeypatch.setattr(render.os, "get_terminal_size", lambda _fd: os.terminal_size((0, 24)))
    monkeypatch.setattr(
        render.shutil,
        "get_terminal_size",
        lambda *, fallback: os.terminal_size((88, 24)),
    )
    buffer = _TinyTerminalBuffer()
    progress = render.ConsoleInsightsAnalyzeProgress(
        enabled=True,
        stream=t.cast("t.TextIO", buffer),
        tty=True,
        color_mode="never",
        refresh_interval=60.0,
    )
    result = render.InsightsAnalyzeProgressResult(
        runs_analyzed=0,
        features_refreshed=0,
        clusters=0,
        variant_edges=0,
        omission_findings=0,
    )

    progress.start(1)
    progress.step_started(1, 1, "similarity", result)
    progress.set_activity("refreshing feature cache", detail="15,084 missing feature rows")
    progress.interrupt()

    output = buffer.getvalue()
    assert "Insights analyze" in output
    assert "refreshing feature cache" in output


def test_collection_is_not_a_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``collection`` is rejected as an unknown command."""
    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(("collection",))

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "invalid choice: 'collection'" in captured.err
    assert "db" in captured.err


def test_insights_analyze_command_parses_target_path() -> None:
    """Insights commands expose deterministic analysis configuration."""
    parsed = agentgrep.parse_args(
        ("insights", "analyze", "--target", "AGENTS.md", "--kind", "omissions"),
    )

    assert isinstance(parsed, agentgrep.InsightsArgs)
    assert parsed.action == "analyze"
    assert parsed.kind == "omissions"
    assert parsed.target == "AGENTS.md"


def test_insights_list_parses_default_and_explicit_limit() -> None:
    """Insights list defaults to a bounded CLI page."""
    default = agentgrep.parse_args(("insights", "list"))
    explicit = agentgrep.parse_args(("insights", "list", "--limit", "12"))

    assert isinstance(default, agentgrep.InsightsArgs)
    assert default.action == "list"
    assert default.limit == 50
    assert isinstance(explicit, agentgrep.InsightsArgs)
    assert explicit.limit == 12


def test_insights_list_rejects_non_positive_limit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Insights list limit must stay positive."""
    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(("insights", "list", "--limit", "0"))

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--limit" in captured.err


def test_command_group_actions_keep_default_behavior() -> None:
    """No-arg directory help does not remove existing action defaults."""
    db = agentgrep.parse_args(("db", "sync"))
    insights = agentgrep.parse_args(("insights", "analyze"))
    suggestions = agentgrep.parse_args(("suggestions", "list"))

    assert isinstance(db, agentgrep.DbArgs)
    assert db.action == "sync"
    assert isinstance(insights, agentgrep.InsightsArgs)
    assert insights.action == "analyze"
    assert insights.kind == "all"
    assert isinstance(suggestions, agentgrep.SuggestionsArgs)
    assert suggestions.action == "list"


def test_suggestions_show_without_identifier_still_errors() -> None:
    """Only command groups become directories; required action operands remain strict."""
    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(("suggestions", "show"))

    assert exc_info.value.code == 2


def test_insights_analyze_omissions_requires_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Omission analysis needs an explicit target instruction surface."""
    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(("insights", "analyze", "--kind", "omissions"))

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--target" in captured.err


def test_insights_run_is_not_an_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``run`` is rejected as an unknown insights action."""
    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(("insights", "run"))

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "invalid choice: 'run'" in captured.err
    assert "analyze" in captured.err


def test_insights_analyze_forced_progress_keeps_json_stdout_clean(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Forced insights progress writes status to stderr, never JSON stdout."""

    class RuntimeStub:
        """Runtime stub with a store attribute for the insight engine."""

        store = object()

        def close(self) -> None:
            """Accept the command's close call."""

    class EngineStub:
        """Insight engine stub that exercises analyze progress."""

        def __init__(self, _store: object) -> None:
            self._store = _store

        def run_similarity(
            self,
            *,
            control: agentgrep.SearchControl | None = None,
            progress: object | None = None,
        ) -> object:
            _ = (control, progress)
            return {"kind": "similarity", "variant_edges": 2}

    import agentgrep.insights as insights_module

    monkeypatch.setattr(render, "_open_db_runtime", lambda _path: RuntimeStub())
    monkeypatch.setattr(insights_module, "InsightEngine", EngineStub)
    args = agentgrep.InsightsArgs(
        action="analyze",
        db_path=None,
        kind="similarity",
        target=None,
        output_mode="json",
        color_mode="never",
        progress_mode="always",
    )

    exit_code = render.run_insights_command(args)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {"kind": "similarity", "variant_edges": 2}
    assert "Insights analyze" in captured.err
    assert "Analyze complete:" in captured.err


def test_insights_list_uses_limited_pages_and_count_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Insights list fetches a bounded page plus SQL-backed totals."""

    class RuntimeStub:
        """Runtime stub with a store attribute for the insight engine."""

        store = object()

        def close(self) -> None:
            """Accept the command's close call."""

    class EngineStub:
        """Insight engine stub that records list limits."""

        variant_limit: int | None = None
        omission_limit: int | None = None

        def __init__(self, _store: object) -> None:
            self._store = _store

        def count_variant_edges(self) -> int:
            """Return the full persisted edge count."""
            return 100

        def count_omission_findings(self) -> int:
            """Return the full persisted omission count."""
            return 2

        def list_variant_edges(self, *, limit: int | None = None) -> list[dict[str, object]]:
            """Return one bounded edge sample."""
            type(self).variant_limit = limit
            return [{"edge_id": "edge-1"}]

        def list_omission_findings(
            self,
            *,
            limit: int | None = None,
        ) -> list[dict[str, object]]:
            """Return one bounded omission sample."""
            type(self).omission_limit = limit
            return [{"finding_id": "finding-1"}]

    import agentgrep.insights as insights_module

    monkeypatch.setattr(render, "_open_db_runtime", lambda _path: RuntimeStub())
    monkeypatch.setattr(insights_module, "InsightEngine", EngineStub)
    args = agentgrep.InsightsArgs(
        action="list",
        db_path=None,
        kind="all",
        target=None,
        output_mode="json",
        color_mode="never",
        limit=7,
    )

    exit_code = render.run_insights_command(args)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert EngineStub.variant_limit == 7
    assert EngineStub.omission_limit == 7
    assert payload["variant_edges"]["total"] == 100
    assert payload["variant_edges"]["returned"] == 1
    assert payload["variant_edges"]["truncated"] is True
    assert payload["omission_findings"]["total"] == 2


def test_insights_list_default_output_is_human_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Insights list defaults to terminal text, not Python repr or JSON."""

    class RuntimeStub:
        """Runtime stub with a store attribute for the insight engine."""

        store = object()

        def close(self) -> None:
            """Accept the command's close call."""

    class EngineStub:
        """Insight engine stub with one persisted edge sample."""

        def __init__(self, _store: object) -> None:
            self._store = _store

        def count_variant_edges(self) -> int:
            """Return the full persisted edge count."""
            return 100

        def count_omission_findings(self) -> int:
            """Return the full persisted omission count."""
            return 0

        def list_variant_edges(self, *, limit: int | None = None) -> list[VariantEdge]:
            """Return one bounded edge sample."""
            assert limit == 7
            return [
                VariantEdge(
                    edge_id="edge-1",
                    run_id="run-1",
                    left_record_id="left-record",
                    right_record_id="right-record",
                    variant_type="exact_duplicate",
                    confidence=1.0,
                    explanation="normalized prompt text is identical",
                ),
            ]

        def list_omission_findings(
            self,
            *,
            limit: int | None = None,
        ) -> list[OmissionFinding]:
            """Return no omission samples."""
            assert limit == 7
            return []

    import agentgrep.insights as insights_module

    monkeypatch.setattr(render, "_open_db_runtime", lambda _path: RuntimeStub())
    monkeypatch.setattr(insights_module, "InsightEngine", EngineStub)
    args = agentgrep.InsightsArgs(
        action="list",
        db_path=None,
        kind="all",
        target=None,
        output_mode="text",
        color_mode="never",
        limit=7,
    )

    exit_code = render.run_insights_command(args)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "Insights" in captured.out
    assert "limit 7" in captured.out
    assert "1/100 variant edges" in captured.out
    assert "edge-1" in captured.out
    assert not captured.out.lstrip().startswith(("{", "["))
    assert "VariantEdge(" not in captured.out


def test_insights_explain_uses_counts_without_listing_rows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Insights explain is a cheap summary, not a row dump."""

    class RuntimeStub:
        """Runtime stub with a store attribute for the insight engine."""

        store = object()

        def close(self) -> None:
            """Accept the command's close call."""

    class EngineStub:
        """Insight engine stub that rejects unbounded list calls."""

        def __init__(self, _store: object) -> None:
            self._store = _store

        def count_variant_edges(self) -> int:
            """Return the full persisted edge count."""
            return 100

        def count_omission_findings(self) -> int:
            """Return the full persisted omission count."""
            return 2

        def list_variant_edges(self, *, limit: int | None = None) -> list[object]:
            """Reject accidental row listing."""
            _ = limit
            msg = "explain should not list variant edges"
            raise AssertionError(msg)

        def list_omission_findings(self, *, limit: int | None = None) -> list[object]:
            """Reject accidental row listing."""
            _ = limit
            msg = "explain should not list omission findings"
            raise AssertionError(msg)

    import agentgrep.insights as insights_module

    monkeypatch.setattr(render, "_open_db_runtime", lambda _path: RuntimeStub())
    monkeypatch.setattr(insights_module, "InsightEngine", EngineStub)
    args = agentgrep.InsightsArgs(
        action="explain",
        db_path=None,
        kind="all",
        target=None,
        output_mode="json",
        color_mode="never",
    )

    exit_code = render.run_insights_command(args)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {
        "variant_edges": 100,
        "omission_findings": 2,
    }


def test_insights_analyze_can_exit_early_between_steps(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Insights analyze honors answer-now control before the next insight step."""
    target = tmp_path / "AGENTS.md"
    target.write_text("Run pytest before committing.\n", encoding="utf-8")

    class RuntimeStub:
        """Runtime stub with a store attribute for the insight engine."""

        store = object()

        def close(self) -> None:
            """Accept the command's close call."""

    class EngineStub:
        """Insight engine stub that requests early exit after similarity."""

        def __init__(self, _store: object) -> None:
            self._store = _store

        def run_similarity(
            self,
            *,
            control: agentgrep.SearchControl | None = None,
            progress: object | None = None,
        ) -> object:
            _ = progress
            assert control is not None
            control.request_answer_now()
            return {"kind": "similarity", "variant_edges": 1}

        def run_omissions(
            self,
            *,
            target_path: pathlib.Path,
            target_text: str,
            control: agentgrep.SearchControl | None = None,
            progress: object | None = None,
        ) -> object:
            _ = (target_path, target_text, control, progress)
            msg = "omissions should not run after early exit"
            raise AssertionError(msg)

    import agentgrep.insights as insights_module

    monkeypatch.setattr(render, "_open_db_runtime", lambda _path: RuntimeStub())
    monkeypatch.setattr(insights_module, "InsightEngine", EngineStub)
    args = agentgrep.InsightsArgs(
        action="analyze",
        db_path=None,
        kind="all",
        target=str(target),
        output_mode="json",
        color_mode="never",
        progress_mode="always",
    )

    exit_code = render.run_insights_command(args)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {"kind": "similarity", "variant_edges": 1}
    assert "Exiting early:" in captured.err


def test_insights_analyze_tty_progress_renders_exit_hint() -> None:
    """TTY insights progress mirrors DB sync hint and color semantics."""
    buffer = _StringBuffer()
    progress = render.ConsoleInsightsAnalyzeProgress(
        enabled=True,
        stream=t.cast("t.TextIO", buffer),
        tty=True,
        color_mode="always",
        refresh_interval=60.0,
        answer_now_hint=True,
    )
    result = render.InsightsAnalyzeProgressResult(
        runs_analyzed=0,
        features_refreshed=0,
        clusters=0,
        variant_edges=0,
        omission_findings=0,
    )

    progress.start(1)
    progress.step_started(1, 1, "similarity", result)
    progress.exiting_early(result)

    output = buffer.getvalue()
    assert "\x1b[" in output
    assert "[Press enter, exit early]" in output
    assert "Exiting early:" in output


def test_insights_analyze_progress_lines_show_activity_without_empty_results() -> None:
    """Insights progress does not show zero aggregate counters as live results."""
    colors = agentgrep.AnsiColors(enabled=False)
    snapshot = render.InsightsAnalyzeProgressSnapshot(
        phase="analyzing",
        current=1,
        total=1,
        detail="similarity",
        activity="refreshing feature cache",
        activity_detail="15,084 missing feature rows",
        result=render.InsightsAnalyzeProgressResult(
            runs_analyzed=0,
            features_refreshed=0,
            clusters=0,
            variant_edges=0,
            omission_findings=0,
        ),
        elapsed=16.6,
    )

    lines = render.format_insights_analyze_progress_lines(
        snapshot,
        colors=colors,
        answer_now_hint=True,
    )

    assert lines == (
        "Insights analyze | analyzing 1/1 steps | similarity | 16.6s | [Press enter, exit early]",
        "Doing | refreshing feature cache | 15,084 missing feature rows",
    )


def test_insights_analyze_progress_lines_show_nonzero_results() -> None:
    """Insights progress keeps aggregate counters once a step has produced output."""
    colors = agentgrep.AnsiColors(enabled=False)
    snapshot = render.InsightsAnalyzeProgressSnapshot(
        phase="analyzing",
        current=2,
        total=2,
        detail="omissions",
        activity="comparing omission candidates",
        activity_detail="100 indexed records",
        result=render.InsightsAnalyzeProgressResult(
            runs_analyzed=1,
            features_refreshed=3,
            clusters=2,
            variant_edges=4,
            omission_findings=0,
        ),
        elapsed=5.5,
    )

    lines = render.format_insights_analyze_progress_lines(snapshot, colors=colors)

    assert lines == (
        "Insights analyze | analyzing 2/2 steps | omissions | 5.5s",
        "Doing | comparing omission candidates | 100 indexed records",
        "Results | 1 run analyzed | 3 features refreshed | 2 clusters | "
        "4 variant edges | 0 omission findings",
    )


def test_insights_analyze_progress_lines_preserve_step_detail_when_results_truncate() -> None:
    """Wide result counters do not hide the current insight step."""
    colors = agentgrep.AnsiColors(enabled=False)
    snapshot = render.InsightsAnalyzeProgressSnapshot(
        phase="analyzing",
        current=1,
        total=1,
        detail="similarity",
        activity="writing similarity artifacts",
        activity_detail="658 duplicate prompt families",
        result=render.InsightsAnalyzeProgressResult(
            runs_analyzed=0,
            features_refreshed=0,
            clusters=658,
            variant_edges=5584,
            omission_findings=0,
        ),
        elapsed=16.6,
    )

    lines = render.format_insights_analyze_progress_lines(
        snapshot,
        colors=colors,
        max_width=72,
    )

    assert "similarity" in lines[0]
    assert "writing similarity artifacts" in lines[1]
    assert lines[2].endswith("…")


def test_grep_cache_require_unsupported_query_exits_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cache-required grep reports unsupported regex queries as CLI errors."""
    from agentgrep.db import DbQueryUnsupportedError

    class UnsupportedDb:
        """DB stub that rejects the query shape."""

        def search_records(
            self,
            _query: agentgrep.SearchQuery,
        ) -> list[agentgrep.SearchRecord]:
            msg = "query requires live scanner"
            raise DbQueryUnsupportedError(msg)

        def close(self) -> None:
            """Accept the search path's runtime close."""

    runtime = agentgrep.SearchRuntime(
        db=t.cast("t.Any", UnsupportedDb()),
        cache_mode="require",
    )
    monkeypatch.setattr(render, "_db_runtime_for_cli", lambda _mode: runtime)
    args = agentgrep.GrepArgs(
        patterns=("ruff",),
        agents=("codex",),
        scope="prompts",
        case_mode="smart",
        pattern_mode="regex",
        invert_match=False,
        count_only=False,
        files_with_matches=False,
        only_matching=False,
        no_dedupe=False,
        line_number=None,
        heading=None,
        limit=None,
        vimgrep=False,
        column=False,
        output_mode="text",
        color_mode="never",
        progress_mode="never",
        cache_mode="require",
    )

    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.run_grep_command(args)

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--cache require" in captured.err
    assert "Traceback" not in captured.err


def test_suggestions_render_command_parses_identifier() -> None:
    """Suggestions commands retrieve stored review artifacts by id."""
    parsed = agentgrep.parse_args(("suggestions", "render", "suggestion-1", "--json"))

    assert isinstance(parsed, agentgrep.SuggestionsArgs)
    assert parsed.action == "render"
    assert parsed.suggestion_id == "suggestion-1"
    assert parsed.output_mode == "json"


def test_db_command_closes_runtime_on_exit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every db action closes its per-call SQLite connection."""
    import agentgrep.db as agentgrep_db

    db_path = tmp_path / "agentgrep.sqlite"
    agentgrep_db.DbRuntime.open(db_path).close()
    opened: list[agentgrep_db.DbRuntime] = []
    real_open_readonly = agentgrep_db.DbRuntime.open_readonly

    def capturing_open_readonly(
        db_path: pathlib.Path | str | None = None,
    ) -> agentgrep_db.DbRuntime:
        runtime = real_open_readonly(db_path)
        opened.append(runtime)
        return runtime

    monkeypatch.setattr(agentgrep_db.DbRuntime, "open_readonly", capturing_open_readonly)
    args = agentgrep.DbArgs(
        action="status",
        db_path=str(db_path),
        agents=("codex",),
        scope="all",
        output_mode="json",
        color_mode="never",
        progress_mode="never",
    )

    exit_code = render.run_db_command(args)

    _ = capsys.readouterr()
    assert exit_code == 0
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        _ = opened[0].store.connection.execute("SELECT 1")


def test_db_status_never_writes_the_cache(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Status reads are byte-for-byte write-free, even on read-only files."""
    import hashlib

    import agentgrep.db as agentgrep_db

    db_path = tmp_path / "agentgrep.sqlite"
    agentgrep_db.DbRuntime.open(db_path).close()
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    db_path.chmod(0o444)
    args = agentgrep.DbArgs(
        action="status",
        db_path=str(db_path),
        agents=("codex",),
        scope="all",
        output_mode="json",
        color_mode="never",
        progress_mode="never",
    )

    exit_code = render.run_db_command(args)

    db_path.chmod(0o644)
    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out)["records"] == 0
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_db_status_on_missing_db_reports_zeros_without_creating(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Status on a missing cache reports zero counts and creates nothing."""
    db_path = tmp_path / "missing.sqlite"
    args = agentgrep.DbArgs(
        action="status",
        db_path=str(db_path),
        agents=("codex",),
        scope="all",
        output_mode="json",
        color_mode="never",
        progress_mode="never",
    )

    exit_code = render.run_db_command(args)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out)["sources"] == 0
    assert not db_path.exists()


def test_db_status_on_foreign_file_fails_cleanly(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Status on a non-database file errors without a traceback."""
    db_path = tmp_path / "not-a-db.sqlite"
    db_path.write_text("plain text", encoding="utf-8")
    args = agentgrep.DbArgs(
        action="status",
        db_path=str(db_path),
        agents=("codex",),
        scope="all",
        output_mode="json",
        color_mode="never",
        progress_mode="never",
    )

    exit_code = render.run_db_command(args)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "not an agentgrep database" in captured.err
    assert "Traceback" not in captured.err


def test_db_explain_reports_sync_diagnostics(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Explain returns a diagnostic payload distinct from status."""
    import agentgrep.db as agentgrep_db

    db_path = tmp_path / "agentgrep.sqlite"
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    runtime = agentgrep_db.DbRuntime.open(db_path)
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=source_path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=source_path.parent,
        mtime_ns=source_path.stat().st_mtime_ns,
    )
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text="Run ruff check before committing.",
        timestamp="2026-06-05T12:00:00Z",
        session_id="session-a",
    )
    _ = runtime.sync_records(((source, (record,)),))
    runtime.close()
    args = agentgrep.DbArgs(
        action="explain",
        db_path=str(db_path),
        agents=("codex",),
        scope="all",
        output_mode="json",
        color_mode="never",
        progress_mode="never",
    )

    exit_code = render.run_db_command(args)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["synced_ok"] == 1
    assert payload["sync_errors"] == 0
    assert payload["last_synced_at"] is not None
    assert "term AND queries" in payload["answerable"]


def test_db_explain_text_output_shows_sync_summary(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Explain text mode renders the diagnostic summary."""
    args = agentgrep.DbArgs(
        action="explain",
        db_path=str(tmp_path / "missing.sqlite"),
        agents=("codex",),
        scope="all",
        output_mode="text",
        color_mode="never",
        progress_mode="never",
    )

    exit_code = render.run_db_command(args)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "DB explain" in captured.out
    assert "0 ok" in captured.out
    assert "Answerable" in captured.out


class CacheEnvCase(t.NamedTuple):
    """Named case for AGENTGREP_CACHE resolution."""

    test_id: str
    env_value: str | None
    argv: tuple[str, ...]
    expected_cache_mode: agentgrep.CacheMode


CACHE_ENV_CASES: tuple[CacheEnvCase, ...] = (
    CacheEnvCase(
        test_id="env-off-applies",
        env_value="off",
        argv=("grep", "ruff"),
        expected_cache_mode="off",
    ),
    CacheEnvCase(
        test_id="env-require-applies",
        env_value="require",
        argv=("search", "ruff"),
        expected_cache_mode="require",
    ),
    CacheEnvCase(
        test_id="flag-overrides-env",
        env_value="off",
        argv=("grep", "--cache", "require", "ruff"),
        expected_cache_mode="require",
    ),
    CacheEnvCase(
        test_id="no-cache-flag-overrides-env",
        env_value="require",
        argv=("search", "--no-cache", "ruff"),
        expected_cache_mode="off",
    ),
    CacheEnvCase(
        test_id="unset-env-defaults-auto",
        env_value=None,
        argv=("grep", "ruff"),
        expected_cache_mode="auto",
    ),
)


@pytest.mark.parametrize(
    "case",
    CACHE_ENV_CASES,
    ids=[case.test_id for case in CACHE_ENV_CASES],
)
def test_cache_mode_resolves_flag_over_env(
    case: CacheEnvCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache mode resolution honors flag > AGENTGREP_CACHE > auto."""
    if case.env_value is None:
        monkeypatch.delenv("AGENTGREP_CACHE", raising=False)
    else:
        monkeypatch.setenv("AGENTGREP_CACHE", case.env_value)

    parsed = agentgrep.parse_args(case.argv)

    assert isinstance(parsed, (agentgrep.GrepArgs, agentgrep.SearchArgs))
    assert parsed.cache_mode == case.expected_cache_mode


def test_invalid_cache_env_value_fails_at_parse_time(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An invalid AGENTGREP_CACHE value is a clean parse-time error."""
    monkeypatch.setenv("AGENTGREP_CACHE", "never")

    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(("grep", "ruff"))

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "AGENTGREP_CACHE must be one of auto, require, off" in captured.err
    assert "Traceback" not in captured.err


def test_cached_search_never_writes_the_cache(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cache-served searches leave the db file byte-for-byte unchanged."""
    import hashlib

    import agentgrep.db as agentgrep_db

    db_path = tmp_path / "agentgrep.sqlite"
    monkeypatch.setenv("AGENTGREP_DB", str(db_path))
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    runtime = agentgrep_db.DbRuntime.open(db_path)
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=source_path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=source_path.parent,
        mtime_ns=source_path.stat().st_mtime_ns,
    )
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text="Run ruff check before committing.",
        timestamp="2026-06-05T12:00:00Z",
        session_id="session-a",
    )
    _ = runtime.sync_records(((source, (record,)),))
    runtime.close()
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()

    cli_runtime = render._db_runtime_for_cli("require")

    assert cli_runtime is not None
    assert cli_runtime.db is not None
    found = cli_runtime.db.search_records(
        agentgrep.SearchQuery(
            terms=("ruff",),
            scope="prompts",
            any_term=False,
            regex=False,
            case_sensitive=False,
            agents=("codex",),
            limit=None,
        ),
    )
    cli_runtime.db.close()
    _ = capsys.readouterr()
    assert [item.text for item in found] == ["Run ruff check before committing."]
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_cache_require_with_missing_db_exits_cleanly(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--cache require without a synced cache is a clean error, no create."""
    db_path = tmp_path / "missing.sqlite"
    monkeypatch.setenv("AGENTGREP_DB", str(db_path))

    with pytest.raises(SystemExit) as exc_info:
        _ = render._db_runtime_for_cli("require")

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "needs a synced DB" in captured.err
    assert "Traceback" not in captured.err
    assert not db_path.exists()


def test_cache_auto_with_missing_db_falls_back_live(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto mode without a cache returns no runtime and creates nothing."""
    db_path = tmp_path / "missing.sqlite"
    monkeypatch.setenv("AGENTGREP_DB", str(db_path))

    assert render._db_runtime_for_cli("auto") is None
    assert not db_path.exists()


def test_cache_require_with_foreign_file_exits_cleanly(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--cache require on a non-database file is a clean error.

    Read-only SQLite connects are lazy, so without an open-time probe
    the corruption would surface as a traceback inside the search.
    """
    db_path = tmp_path / "not-a-db.sqlite"
    db_path.write_text("plain text", encoding="utf-8")
    monkeypatch.setenv("AGENTGREP_DB", str(db_path))

    with pytest.raises(SystemExit) as exc_info:
        _ = render._db_runtime_for_cli("require")

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "not an agentgrep database" in captured.err
    assert "Traceback" not in captured.err


def test_cache_auto_with_foreign_file_falls_back_live(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto mode degrades to a live scan when the cache file is foreign."""
    db_path = tmp_path / "not-a-db.sqlite"
    db_path.write_text("plain text", encoding="utf-8")
    monkeypatch.setenv("AGENTGREP_DB", str(db_path))

    assert render._db_runtime_for_cli("auto") is None


class ClosableDbStub:
    """DB stub recording whether the search path closed it."""

    def __init__(self) -> None:
        """Start in the not-closed state."""
        self.closed = False

    def close(self) -> None:
        """Record that the search path closed this runtime."""
        self.closed = True


class SearchPathCloseCase(t.NamedTuple):
    """Named case for search-path runtime close behavior."""

    test_id: str
    path: t.Literal["eager", "iter-exhausted", "iter-early-break"]


SEARCH_PATH_CLOSE_CASES: tuple[SearchPathCloseCase, ...] = (
    SearchPathCloseCase(test_id="eager-path-closes", path="eager"),
    SearchPathCloseCase(test_id="iter-path-closes-on-exhaustion", path="iter-exhausted"),
    SearchPathCloseCase(test_id="iter-path-closes-on-early-break", path="iter-early-break"),
)


@pytest.mark.parametrize(
    "case",
    SEARCH_PATH_CLOSE_CASES,
    ids=[case.test_id for case in SEARCH_PATH_CLOSE_CASES],
)
def test_search_paths_close_their_db_runtime(
    case: SearchPathCloseCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both CLI search paths close the cache runtime they open."""
    db_stub = ClosableDbStub()
    runtime = agentgrep.SearchRuntime(db=t.cast("t.Any", db_stub), cache_mode="auto")
    monkeypatch.setattr(render, "_db_runtime_for_cli", lambda _mode: runtime)

    def fake_run_search_query(
        _home: pathlib.Path,
        _query: agentgrep.SearchQuery,
        *,
        progress: object | None = None,
        control: object | None = None,
        runtime: object | None = None,
    ) -> list[agentgrep.SearchRecord]:
        del progress, control, runtime
        return []

    def fake_iter_search_events(
        _home: pathlib.Path,
        _query: agentgrep.SearchQuery,
        *,
        control: object | None = None,
        runtime: object | None = None,
    ) -> cabc.Iterator[object]:
        del control, runtime
        yield "event-a"
        yield "event-b"

    monkeypatch.setattr(agentgrep, "run_search_query", fake_run_search_query)
    monkeypatch.setattr(agentgrep, "iter_search_events", fake_iter_search_events)
    query = agentgrep.SearchQuery(
        terms=("ruff",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    if case.path == "eager":
        _ = render._run_search_query_for_cli(
            tmp_path,
            query,
            progress=t.cast("agentgrep.SearchProgress", None),
            control=agentgrep.SearchControl(),
            cache_mode="auto",
        )
    else:
        events = render._iter_search_events_for_cli(
            tmp_path,
            query,
            control=agentgrep.SearchControl(),
            cache_mode="auto",
        )
        if case.path == "iter-exhausted":
            _ = list(events)
        else:
            _ = next(events)
            t.cast("cabc.Generator[object]", events).close()

    assert db_stub.closed is True


class CoverageRecordingStub:
    """Runtime stub capturing the coverage the sync command passes."""

    def __init__(self) -> None:
        """Start with no recorded coverage."""
        self.coverage: SyncCoverage | None = None
        self.prune_missing: bool | None = None

    def close(self) -> None:
        """Accept the command's runtime close."""

    def sync_sources(
        self,
        sources: t.Iterable[agentgrep.SourceHandle],
        *,
        control: agentgrep.SearchControl | None = None,
        progress: DbSyncProgress | None = None,
        force: bool = False,
        coverage: SyncCoverage | None = None,
        prune_missing: bool = False,
        features_mode: str = "defer",
    ) -> SyncResult:
        """Record coverage and pruning, returning zero counters."""
        del sources, control, progress, force, features_mode
        self.coverage = coverage
        self.prune_missing = prune_missing
        return SyncResult(sources_synced=0, records_indexed=0, records_removed=0)


class SyncCoverageArgsCase(t.NamedTuple):
    """Named case for the coverage the db sync command computes."""

    test_id: str
    limit_sources: int | None
    scope: agentgrep.SearchScope
    expected_complete: bool


SYNC_COVERAGE_ARGS_CASES: tuple[SyncCoverageArgsCase, ...] = (
    SyncCoverageArgsCase(
        test_id="full-sync-is-complete",
        limit_sources=None,
        scope="all",
        expected_complete=True,
    ),
    SyncCoverageArgsCase(
        test_id="capped-sync-is-incomplete",
        limit_sources=1,
        scope="all",
        expected_complete=False,
    ),
    SyncCoverageArgsCase(
        test_id="scoped-sync-keeps-its-scope",
        limit_sources=None,
        scope="prompts",
        expected_complete=True,
    ),
)


@pytest.mark.parametrize(
    "case",
    SYNC_COVERAGE_ARGS_CASES,
    ids=[case.test_id for case in SYNC_COVERAGE_ARGS_CASES],
)
def test_db_sync_passes_coverage_from_args(
    case: SyncCoverageArgsCase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The sync command derives coverage from its agent/scope/cap arguments."""
    stub = CoverageRecordingStub()
    monkeypatch.setattr(render, "_open_db_runtime", lambda _path: stub)
    monkeypatch.setattr(
        agentgrep,
        "discover_sources_for_search",
        lambda _home, _query, _backends, version_detail: [],
    )
    args = agentgrep.DbArgs(
        action="sync",
        db_path=None,
        agents=("codex",),
        scope=case.scope,
        limit_sources=case.limit_sources,
        output_mode="json",
        color_mode="never",
        progress_mode="never",
    )

    exit_code = render.run_db_command(args)
    _ = capsys.readouterr()

    assert exit_code == 0
    assert stub.coverage is not None
    assert stub.coverage.agents == ("codex",)
    assert stub.coverage.scope == case.scope
    assert stub.coverage.complete is case.expected_complete


class PruneArgsCase(t.NamedTuple):
    """Named case for the prune flag the db sync command derives."""

    test_id: str
    agents: tuple[agentgrep.AgentName, ...]
    scope: agentgrep.SearchScope
    limit_sources: int | None
    expected_prune: bool


PRUNE_ARGS_CASES: tuple[PruneArgsCase, ...] = (
    PruneArgsCase(
        test_id="full-sync-prunes",
        agents=agentgrep.AGENT_CHOICES,
        scope="all",
        limit_sources=None,
        expected_prune=True,
    ),
    PruneArgsCase(
        test_id="agent-subset-never-prunes",
        agents=("codex",),
        scope="all",
        limit_sources=None,
        expected_prune=False,
    ),
    PruneArgsCase(
        test_id="scoped-sync-never-prunes",
        agents=agentgrep.AGENT_CHOICES,
        scope="prompts",
        limit_sources=None,
        expected_prune=False,
    ),
    PruneArgsCase(
        test_id="capped-sync-never-prunes",
        agents=agentgrep.AGENT_CHOICES,
        scope="all",
        limit_sources=1,
        expected_prune=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    PRUNE_ARGS_CASES,
    ids=[case.test_id for case in PRUNE_ARGS_CASES],
)
def test_db_sync_prunes_only_on_full_syncs(
    case: PruneArgsCase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only an uncapped, full-scope, all-agents sync may prune."""
    stub = CoverageRecordingStub()
    monkeypatch.setattr(render, "_open_db_runtime", lambda _path: stub)
    monkeypatch.setattr(
        agentgrep,
        "discover_sources_for_search",
        lambda _home, _query, _backends, version_detail: [],
    )
    args = agentgrep.DbArgs(
        action="sync",
        db_path=None,
        agents=case.agents,
        scope=case.scope,
        limit_sources=case.limit_sources,
        output_mode="json",
        color_mode="never",
        progress_mode="never",
    )

    exit_code = render.run_db_command(args)
    _ = capsys.readouterr()

    assert exit_code == 0
    assert stub.prune_missing is case.expected_prune


def test_insights_command_closes_runtime_on_exit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Insights actions close their per-call SQLite connection."""
    import agentgrep.db as agentgrep_db

    opened: list[agentgrep_db.DbRuntime] = []
    real_open = agentgrep_db.DbRuntime.open

    def capturing_open(
        db_path: pathlib.Path | str | None = None,
    ) -> agentgrep_db.DbRuntime:
        runtime = real_open(db_path)
        opened.append(runtime)
        return runtime

    monkeypatch.setattr(agentgrep_db.DbRuntime, "open", capturing_open)
    args = agentgrep.InsightsArgs(
        action="explain",
        db_path=str(tmp_path / "agentgrep.sqlite"),
        kind="all",
        target=None,
        output_mode="json",
    )

    exit_code = agentgrep.run_insights_command(args)

    _ = capsys.readouterr()
    assert exit_code == 0
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        _ = opened[0].store.connection.execute("SELECT 1")
