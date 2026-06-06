"""Tests for the persistent DB index layer."""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep.db import DbRuntime, DbStatus, SyncResult


class CachedSearchCase(t.NamedTuple):
    """Named case for cache-backed search runtime behavior."""

    test_id: str
    cache_mode: agentgrep.CacheMode
    expected_texts: tuple[str, ...]


class DuplicateRecordCase(t.NamedTuple):
    """Named case for duplicate source record identity behavior."""

    test_id: str
    texts: tuple[str, ...]
    expected_records: int


class StopAfterFirstSourceProgress:
    """Progress stub that requests early exit after one source transaction."""

    def __init__(self, control: agentgrep.SearchControl) -> None:
        self._control = control
        self.started_total: int | None = None
        self.finished_sources: list[str] = []
        self.finished_cleanly = False
        self.early_result: SyncResult | None = None

    def start(self, total_sources: int) -> None:
        """Capture the planned source count."""
        self.started_total = total_sources

    def source_started(
        self,
        index: int,
        total: int,
        source: agentgrep.SourceHandle,
        result: SyncResult,
    ) -> None:
        """Accept source-start events."""
        _ = (index, total, source, result)

    def source_finished(
        self,
        index: int,
        total: int,
        source: agentgrep.SourceHandle,
        records_indexed: int,
        records_removed: int,
        result: SyncResult,
    ) -> None:
        """Request early exit once the first source is fully committed."""
        _ = (index, total, records_indexed, records_removed, result)
        self.finished_sources.append(source.path.name)
        self._control.request_answer_now()

    def finish(self, result: SyncResult) -> None:
        """Record an unexpected clean finish."""
        _ = result
        self.finished_cleanly = True

    def exiting_early(self, result: SyncResult) -> None:
        """Capture the partial result returned after early exit."""
        self.early_result = result


CACHED_SEARCH_CASES: tuple[CachedSearchCase, ...] = (
    CachedSearchCase(
        test_id="require-uses-index",
        cache_mode="require",
        expected_texts=("Run ruff check before committing.",),
    ),
    CachedSearchCase(
        test_id="off-bypasses-index",
        cache_mode="off",
        expected_texts=("live scanner ruff result",),
    ),
)


DUPLICATE_RECORD_CASES: tuple[DuplicateRecordCase, ...] = (
    DuplicateRecordCase(
        test_id="identical-native-identity",
        texts=(
            "Run ruff check before committing.",
            "Run ruff check before committing.",
        ),
        expected_records=2,
    ),
)


def _source(
    path: pathlib.Path,
    *,
    source_kind: agentgrep.SourceKind = "jsonl",
    adapter_id: str = "codex.sessions_jsonl.v1",
) -> agentgrep.SourceHandle:
    """Build a synthetic source handle for db tests."""
    return agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id=adapter_id,
        path=path,
        path_kind="session_file",
        source_kind=source_kind,
        search_root=path.parent,
        mtime_ns=path.stat().st_mtime_ns if path.exists() else 0,
    )


def _record(
    source: agentgrep.SourceHandle,
    text: str,
    *,
    timestamp: str = "2026-06-05T12:00:00Z",
    session_id: str = "session-a",
) -> agentgrep.SearchRecord:
    """Build a synthetic normalized search record."""
    return agentgrep.SearchRecord(
        kind="prompt",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        timestamp=timestamp,
        session_id=session_id,
    )


def _query(term: str = "ruff") -> agentgrep.SearchQuery:
    """Build a cache-supported text search query."""
    return agentgrep.SearchQuery(
        terms=(term,),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )


def test_db_runtime_syncs_records_and_serves_fts_results(
    tmp_path: pathlib.Path,
) -> None:
    """A synced db stores normalized records and returns SearchRecord objects."""
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")

    result = runtime.sync_records(
        (
            (
                source,
                (
                    _record(source, "Run ruff check before committing."),
                    _record(source, "Run pytest for the focused suite."),
                ),
            ),
        ),
    )

    assert result.sources_synced == 1
    assert result.records_indexed == 2
    status = runtime.status()
    assert isinstance(status, DbStatus)
    assert status.sources == 1
    assert status.records == 2
    assert [record.text for record in runtime.search_records(_query("ruff"))] == [
        "Run ruff check before committing.",
    ]


def test_db_runtime_sync_skips_unchanged_sources_without_reading_records(
    tmp_path: pathlib.Path,
) -> None:
    """Fresh source_state lets repeated sync avoid opening the record stream."""
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )

    def records_that_should_not_be_read() -> t.Iterator[agentgrep.SearchRecord]:
        msg = "unchanged source records should not be consumed"
        raise AssertionError(msg)
        yield _record(source, "unreachable")

    result = runtime.sync_records(((source, records_that_should_not_be_read()),))

    assert result == SyncResult(
        sources_synced=0,
        records_indexed=0,
        records_removed=0,
        sources_skipped=1,
    )
    assert runtime.status().records == 1


def test_db_runtime_sync_force_resyncs_unchanged_sources(
    tmp_path: pathlib.Path,
) -> None:
    """Forced sync ignores source_state when the caller wants a full refresh."""
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )

    result = runtime.sync_records(
        ((source, (_record(source, "Run pytest before committing."),)),),
        force=True,
    )

    assert result.sources_synced == 1
    assert result.sources_skipped == 0
    assert result.records_indexed == 1
    assert result.records_removed == 1
    assert [record.text for record in runtime.search_records(_query("pytest"))] == [
        "Run pytest before committing.",
    ]


def test_db_store_source_id_delete_uses_index(
    tmp_path: pathlib.Path,
) -> None:
    """Source replacement must not scan all records for each source delete."""
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")

    rows = runtime.store.connection.execute(
        "EXPLAIN QUERY PLAN SELECT rowid FROM records WHERE source_id = ?",
        ("source-a",),
    ).fetchall()
    plan = " ".join(str(row["detail"]) for row in rows)

    assert "idx_records_source_id" in plan
    assert "SCAN records" not in plan


@pytest.mark.parametrize(
    "case",
    DUPLICATE_RECORD_CASES,
    ids=[case.test_id for case in DUPLICATE_RECORD_CASES],
)
def test_db_runtime_preserves_duplicate_source_records(
    case: DuplicateRecordCase,
    tmp_path: pathlib.Path,
) -> None:
    """A source can contain repeated native records without breaking sync."""
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")

    result = runtime.sync_records(
        (
            (
                source,
                tuple(_record(source, text) for text in case.texts),
            ),
        ),
    )
    rows = runtime.store.iter_record_rows()

    assert result.records_indexed == case.expected_records
    assert runtime.status().records == case.expected_records
    assert len({row.record_id for row in rows}) == case.expected_records
    assert [record.text for record in runtime.search_records(_query("ruff"))] == list(case.texts)


def test_db_runtime_sync_can_exit_early_between_sources(
    tmp_path: pathlib.Path,
) -> None:
    """The DB sync control stops before the next source without partial writes."""
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    first_path.write_text("ruff", encoding="utf-8")
    second_path.write_text("pytest", encoding="utf-8")
    first = _source(first_path)
    second = _source(second_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    control = agentgrep.SearchControl()
    progress = StopAfterFirstSourceProgress(control)

    result = runtime.sync_records(
        (
            (first, (_record(first, "Run ruff check before committing."),)),
            (second, (_record(second, "Run pytest before committing."),)),
        ),
        control=control,
        progress=progress,
    )

    assert result == SyncResult(
        sources_synced=1,
        records_indexed=1,
        records_removed=0,
    )
    assert progress.started_total == 2
    assert progress.finished_sources == ["first.jsonl"]
    assert progress.finished_cleanly is False
    assert progress.early_result == result
    assert runtime.status().sources == 1
    assert [record.text for record in runtime.search_records(_query("pytest"))] == []


@pytest.mark.parametrize(
    "case",
    CACHED_SEARCH_CASES,
    ids=[case.test_id for case in CACHED_SEARCH_CASES],
)
def test_run_search_query_respects_cache_mode(
    case: CachedSearchCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search runtime cache mode selects indexed data or the live scanner explicitly."""
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    db = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = db.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )
    runtime = agentgrep.SearchRuntime(
        source_scan_cache=None,
        db=db,
        cache_mode=case.cache_mode,
    )

    def discover_sources_for_search(
        _home: pathlib.Path,
        _query: agentgrep.SearchQuery,
        _backends: agentgrep.BackendSelection,
        *,
        version_detail: agentgrep.DiscoveryVersionDetail,
    ) -> list[agentgrep.SourceHandle]:
        _ = version_detail
        return [source]

    def iter_source_records(_source: agentgrep.SourceHandle) -> tuple[agentgrep.SearchRecord, ...]:
        return (_record(source, "live scanner ruff result"),)

    monkeypatch.setattr(agentgrep, "discover_sources_for_search", discover_sources_for_search)
    monkeypatch.setattr(agentgrep, "iter_source_records", iter_source_records)

    records = agentgrep.run_search_query(tmp_path, _query("ruff"), runtime=runtime)

    assert [record.text for record in records] == list(case.expected_texts)


def test_resync_keeps_fts_index_consistent(tmp_path: pathlib.Path) -> None:
    """Re-syncing a source removes old FTS rows with their stored values.

    External-content FTS5 deletes that pass placeholder values leave the
    old tokens mapped to a rowid SQLite later reuses, so the raw index
    keeps answering for text that no longer exists. The result-level
    post-filter masks that from ``search_records``, so this asserts on
    the index itself.
    """
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("{}", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )

    _ = runtime.sync_records(
        ((source, (_record(source, "Run pytest for the focused suite."),)),),
        force=True,
    )

    stale = runtime.store.connection.execute(
        "SELECT rowid FROM record_text_fts WHERE record_text_fts MATCH ?",
        ('"ruff"',),
    ).fetchall()
    assert stale == []
    fresh = runtime.store.connection.execute(
        "SELECT rowid FROM record_text_fts WHERE record_text_fts MATCH ?",
        ('"pytest"',),
    ).fetchall()
    assert len(fresh) == 1
    assert [record.text for record in runtime.search_records(_query("pytest"))] == [
        "Run pytest for the focused suite.",
    ]


class WalFreshnessCase(t.NamedTuple):
    """Named case for WAL-sidecar freshness behavior during DB sync."""

    test_id: str
    source_kind: agentgrep.SourceKind
    touch_wal: bool
    expected_synced: int
    expected_skipped: int


WAL_FRESHNESS_CASES: tuple[WalFreshnessCase, ...] = (
    WalFreshnessCase(
        test_id="sqlite-wal-write-resyncs",
        source_kind="sqlite",
        touch_wal=True,
        expected_synced=1,
        expected_skipped=0,
    ),
    WalFreshnessCase(
        test_id="sqlite-unchanged-skips",
        source_kind="sqlite",
        touch_wal=False,
        expected_synced=0,
        expected_skipped=1,
    ),
    WalFreshnessCase(
        test_id="jsonl-ignores-wal-sidecar",
        source_kind="jsonl",
        touch_wal=True,
        expected_synced=0,
        expected_skipped=1,
    ),
)


@pytest.mark.parametrize(
    "case",
    WAL_FRESHNESS_CASES,
    ids=[case.test_id for case in WAL_FRESHNESS_CASES],
)
def test_sync_freshness_tracks_wal_sidecars(
    case: WalFreshnessCase,
    tmp_path: pathlib.Path,
) -> None:
    """WAL-only writes invalidate freshness for sqlite sources only."""
    source_path = tmp_path / "store.db"
    source_path.write_text("primary", encoding="utf-8")
    source = _source(source_path, source_kind=case.source_kind)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )
    if case.touch_wal:
        (tmp_path / "store.db-wal").write_text("wal frame", encoding="utf-8")

    result = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )

    assert result.sources_synced == case.expected_synced
    assert result.sources_skipped == case.expected_skipped


def test_cross_file_adapters_always_resync(tmp_path: pathlib.Path) -> None:
    """Adapters that expand sibling files never satisfy the freshness skip."""
    source_path = tmp_path / "history.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path, adapter_id="claude.history_jsonl.v1")
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )

    result = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )

    assert result.sources_synced == 1
    assert result.sources_skipped == 0


class SubstringParityCase(t.NamedTuple):
    """Named case for cached-search substring parity with the live engine."""

    test_id: str
    term: str
    expected_texts: tuple[str, ...]


SUBSTRING_PARITY_CASES: tuple[SubstringParityCase, ...] = (
    SubstringParityCase(
        test_id="token-prefix",
        term="ruf",
        expected_texts=("Run ruff check.", "the scruffy dog"),
    ),
    SubstringParityCase(
        test_id="mid-token",
        term="uff",
        expected_texts=("Run ruff check.", "the scruffy dog"),
    ),
    SubstringParityCase(
        test_id="token-and-substring",
        term="ruff",
        expected_texts=("Run ruff check.", "the scruffy dog"),
    ),
    SubstringParityCase(
        test_id="uppercase-query",
        term="RUFF",
        expected_texts=("Run ruff check.", "the scruffy dog"),
    ),
    SubstringParityCase(
        test_id="short-term-scan",
        term="ty",
        expected_texts=("run ty check",),
    ),
    SubstringParityCase(
        test_id="non-ascii-term-scan",
        term="café",
        expected_texts=("visit the café notes",),
    ),
    SubstringParityCase(
        test_id="genuine-zero",
        term="zsh-plugin",
        expected_texts=(),
    ),
)


@pytest.mark.parametrize(
    "case",
    SUBSTRING_PARITY_CASES,
    ids=[case.test_id for case in SUBSTRING_PARITY_CASES],
)
def test_cached_search_matches_live_substring_semantics(
    case: SubstringParityCase,
    tmp_path: pathlib.Path,
) -> None:
    """Cached term search returns exactly what live substring matching would."""
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    texts = (
        "Run ruff check.",
        "the scruffy dog",
        "run ty check",
        "visit the café notes",
        "unrelated pytest line",
    )
    _ = runtime.sync_records(
        (
            (
                source,
                tuple(
                    _record(source, text, session_id=f"session-{index}")
                    for index, text in enumerate(texts)
                ),
            ),
        ),
    )

    found = sorted(record.text for record in runtime.search_records(_query(case.term)))

    assert found == sorted(case.expected_texts)


def test_schema_version_mismatch_rebuilds_cache(tmp_path: pathlib.Path) -> None:
    """A cache written by a different schema version is rebuilt on open."""
    db_path = tmp_path / "agentgrep.sqlite"
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(db_path)
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )
    with runtime.store.connection:
        _ = runtime.store.connection.execute(
            "UPDATE meta SET value = '999' WHERE key = 'schema_version'",
        )
    runtime.store.close()

    reopened = DbRuntime.open(db_path)

    assert reopened.status().records == 0
    assert reopened.status().sources == 0
