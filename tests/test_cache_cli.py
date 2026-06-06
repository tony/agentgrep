"""Tests for DB cache CLI controls."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import typing as t

import pytest

import agentgrep
import agentgrep.cli.render as render
from agentgrep.db import DbStatus, DbSyncProgress, SyncResult


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
    expected_force: bool


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
        test_id="default-skip-current",
        argv=("db", "sync"),
        expected_force=False,
    ),
    DbSyncModeFlagCase(
        test_id="force-resync",
        argv=("db", "sync", "--force"),
        expected_force=True,
    ),
)


COMMAND_GROUP_HELP_CASES: tuple[CommandGroupHelpCase, ...] = (
    CommandGroupHelpCase(
        test_id="db",
        argv=("db",),
        expected_usage="usage: agentgrep db",
        expected_examples_heading="db examples:",
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
            },
            {
                "sources_synced": 3,
                "records_indexed": 4,
                "records_removed": 1,
                "sources_skipped": 0,
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
        ),
        expected_contains=(
            "DB status",
            "/tmp/agentgrep.sqlite",
            "2 sources",
            "3 records",
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
        ),
        expected_contains=(
            "DB sync",
            "2 sources",
            "3 records indexed",
            "1 record removed",
            "4 sources skipped",
        ),
        expected_not_contains=("SyncResult(", "{", "'sources_synced'"),
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
    assert parsed.force is case.expected_force


@pytest.mark.parametrize(
    "case",
    COMMAND_GROUP_HELP_CASES,
    ids=[case.test_id for case in COMMAND_GROUP_HELP_CASES],
)
def test_command_groups_without_actions_print_help_directory(
    case: CommandGroupHelpCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DB command groups act as help directories."""
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
    """DB helpers honor JSON and NDJSON."""
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

        force: bool | None = None
        closed: bool = False

        def close(self) -> None:
            """Record that the command closed its runtime."""
            self.closed = True

        def sync_sources(
            self,
            sources: t.Iterable[agentgrep.SourceHandle],
            *,
            control: agentgrep.SearchControl | None = None,
            progress: DbSyncProgress | None = None,
            force: bool = False,
        ) -> SyncResult:
            self.force = force
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
    }
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


def test_command_group_actions_keep_default_behavior() -> None:
    """No-arg directory help does not remove existing action defaults."""
    db = agentgrep.parse_args(("db", "sync"))

    assert isinstance(db, agentgrep.DbArgs)
    assert db.action == "sync"


def test_grep_cache_require_unsupported_query_exits_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cache-required grep reports unsupported regex queries as CLI errors."""
    from agentgrep.db import DbQueryUnsupported

    class UnsupportedDb:
        """DB stub that rejects the query shape."""

        def search_records(
            self,
            _query: agentgrep.SearchQuery,
        ) -> list[agentgrep.SearchRecord]:
            msg = "query requires live scanner"
            raise DbQueryUnsupported(msg)

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
        max_count=None,
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
