"""Tests for the persistent DB index layer."""

from __future__ import annotations

import dataclasses
import logging
import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep.db import DbRuntime, DbStatus, DbSyncProgress, SyncCoverage, SyncResult


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
    agent: agentgrep.AgentName = "codex",
    store: str = "codex.sessions",
) -> agentgrep.SourceHandle:
    """Build a synthetic source handle for db tests."""
    return agentgrep.SourceHandle(
        agent=agent,
        store=store,
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
    kind: t.Literal["prompt", "history"] = "prompt",
    timestamp: str = "2026-06-05T12:00:00Z",
    session_id: str = "session-a",
    title: str | None = None,
    model: str | None = None,
    role: str | None = None,
) -> agentgrep.SearchRecord:
    """Build a synthetic normalized search record."""
    return agentgrep.SearchRecord(
        kind=kind,
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        title=title,
        model=model,
        role=role,
        timestamp=timestamp,
        session_id=session_id,
    )


def _query(
    term: str = "ruff",
    *,
    dedupe: bool = True,
    scope: agentgrep.SearchScope = "prompts",
    agents: tuple[agentgrep.AgentName, ...] = ("codex",),
) -> agentgrep.SearchQuery:
    """Build a cache-supported text search query."""
    return agentgrep.SearchQuery(
        terms=(term,),
        scope=scope,
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agents,
        limit=None,
        dedupe=dedupe,
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
    found = runtime.search_records(_query("ruff", dedupe=False))
    assert [record.text for record in found] == list(case.texts)


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
            (second, (_record(second, "Run typecheck before committing."),)),
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
    # The haystack surface includes record paths, and pytest tmp dirs
    # contain "pytest", so probe with a term absent from every path.
    assert [record.text for record in runtime.search_records(_query("typecheck"))] == []


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


class CacheDedupeCase(t.NamedTuple):
    """Named case for per-session dedup on the cached search path."""

    test_id: str
    dedupe: bool
    expected_count: int


CACHE_DEDUPE_CASES: tuple[CacheDedupeCase, ...] = (
    CacheDedupeCase(test_id="dedupe-collapses-session", dedupe=True, expected_count=1),
    CacheDedupeCase(test_id="no-dedupe-keeps-both", dedupe=False, expected_count=2),
)


@pytest.mark.parametrize(
    "case",
    CACHE_DEDUPE_CASES,
    ids=[case.test_id for case in CACHE_DEDUPE_CASES],
)
def test_cached_search_applies_per_session_dedup(
    case: CacheDedupeCase,
    tmp_path: pathlib.Path,
) -> None:
    """Cached records honor the per-session dedup decision like live scans."""
    first_path = tmp_path / "one.jsonl"
    second_path = tmp_path / "two.jsonl"
    first_path.write_text("ruff", encoding="utf-8")
    second_path.write_text("ruff", encoding="utf-8")
    first = _source(first_path)
    second = _source(second_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (first, (_record(first, "Run ruff check before committing."),)),
            (second, (_record(second, "Run ruff check before committing."),)),
        ),
    )
    search_runtime = agentgrep.SearchRuntime(cache_mode="require", db=runtime)

    handled, records = agentgrep._db_search_result(
        _query("ruff", dedupe=case.dedupe),
        search_runtime,
    )

    assert handled is True
    assert len(records) == case.expected_count


class CacheLimitDedupeCase(t.NamedTuple):
    """Named case for limit interaction with per-session dedup."""

    test_id: str
    dedupe: bool
    limit: int
    expected_texts: tuple[str, ...]


CACHE_LIMIT_DEDUPE_CASES: tuple[CacheLimitDedupeCase, ...] = (
    CacheLimitDedupeCase(
        test_id="limit-counts-unique-records",
        dedupe=True,
        limit=2,
        expected_texts=(
            "Run ruff check before committing.",
            "Run pytest before committing.",
        ),
    ),
    CacheLimitDedupeCase(
        test_id="no-dedupe-limit-keeps-duplicates",
        dedupe=False,
        limit=2,
        expected_texts=(
            "Run ruff check before committing.",
            "Run ruff check before committing.",
        ),
    ),
)


@pytest.mark.parametrize(
    "case",
    CACHE_LIMIT_DEDUPE_CASES,
    ids=[case.test_id for case in CACHE_LIMIT_DEDUPE_CASES],
)
def test_cached_search_limit_counts_unique_records(
    case: CacheLimitDedupeCase,
    tmp_path: pathlib.Path,
) -> None:
    """A result cap counts unique records, like the live driver."""
    first_path = tmp_path / "one.jsonl"
    second_path = tmp_path / "two.jsonl"
    first_path.write_text("ruff", encoding="utf-8")
    second_path.write_text("ruff", encoding="utf-8")
    first = _source(first_path)
    second = _source(second_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (
                first,
                (
                    _record(first, "Run ruff check before committing."),
                    _record(
                        first,
                        "Run pytest before committing.",
                        timestamp="2026-06-04T12:00:00Z",
                        session_id="session-b",
                    ),
                ),
            ),
            (second, (_record(second, "Run ruff check before committing."),)),
        ),
    )
    query = dataclasses.replace(
        _query("committing", dedupe=case.dedupe),
        limit=case.limit,
    )

    found = [record.text for record in runtime.search_records(query)]

    assert found == list(case.expected_texts)


class HaystackParityCase(t.NamedTuple):
    """Named case for cached haystack-field parity with the live engine."""

    test_id: str
    term: str
    match_surface: agentgrep.SearchMatchSurface
    expected_texts: tuple[str, ...]


HAYSTACK_PARITY_CASES: tuple[HaystackParityCase, ...] = (
    HaystackParityCase(
        test_id="model-only-term",
        term="opus",
        match_surface="haystack",
        expected_texts=("model record",),
    ),
    HaystackParityCase(
        test_id="role-only-term",
        term="assistant",
        match_surface="haystack",
        expected_texts=("role record",),
    ),
    HaystackParityCase(
        test_id="path-only-term",
        term="projalpha",
        match_surface="haystack",
        expected_texts=("path record",),
    ),
    HaystackParityCase(
        test_id="casefold-expanding-text",
        term="ass",
        match_surface="haystack",
        # "ass" matches the folded "strasse" expansion AND the role
        # record's "assistant" haystack — both are live-parity hits.
        expected_texts=("Besuch der Straße notieren", "role record"),
    ),
    HaystackParityCase(
        test_id="text-surface-excludes-title-match",
        term="release",
        match_surface="text",
        expected_texts=("release steps live here",),
    ),
)


@pytest.mark.parametrize(
    "case",
    HAYSTACK_PARITY_CASES,
    ids=[case.test_id for case in HAYSTACK_PARITY_CASES],
)
def test_cached_search_covers_live_haystack_fields(
    case: HaystackParityCase,
    tmp_path: pathlib.Path,
) -> None:
    """Cached candidates cover every field the live matcher searches."""
    plain_path = tmp_path / "plain" / "session.jsonl"
    proj_path = tmp_path / "projalpha" / "session.jsonl"
    plain_path.parent.mkdir()
    proj_path.parent.mkdir()
    plain_path.write_text("{}", encoding="utf-8")
    proj_path.write_text("{}", encoding="utf-8")
    plain = _source(plain_path)
    proj = _source(proj_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (
                plain,
                (
                    _record(plain, "model record", model="claude-opus-4", session_id="s1"),
                    _record(plain, "role record", role="assistant", session_id="s2"),
                    _record(plain, "Besuch der Straße notieren", session_id="s3"),
                    _record(plain, "title only", title="release notes", session_id="s4"),
                    _record(plain, "release steps live here", session_id="s5"),
                ),
            ),
            (proj, (_record(proj, "path record", session_id="s6"),)),
        ),
    )
    query = dataclasses.replace(_query(case.term), match_surface=case.match_surface)

    found = sorted(record.text for record in runtime.search_records(query))

    assert found == sorted(case.expected_texts)


class CacheDecisionSpanCase(t.NamedTuple):
    """Named case for the search.cache.decision telemetry span."""

    test_id: str
    cache_mode: agentgrep.CacheMode
    term: str
    regex: bool
    expected_spans: int
    expected_handled: bool | None
    expected_reason: str | None
    with_coverage: bool = True


CACHE_DECISION_SPAN_CASES: tuple[CacheDecisionSpanCase, ...] = (
    CacheDecisionSpanCase(
        test_id="served-from-cache",
        cache_mode="require",
        term="ruff",
        regex=False,
        expected_spans=1,
        expected_handled=True,
        expected_reason=None,
    ),
    CacheDecisionSpanCase(
        test_id="auto-empty-falls-back",
        cache_mode="auto",
        term="zsh-plugin-nowhere",
        regex=False,
        expected_spans=1,
        expected_handled=False,
        expected_reason="empty",
    ),
    CacheDecisionSpanCase(
        test_id="auto-unsupported-falls-back",
        cache_mode="auto",
        term="ruff.*check",
        regex=True,
        expected_spans=1,
        expected_handled=False,
        expected_reason="unsupported",
    ),
    CacheDecisionSpanCase(
        test_id="auto-partial-coverage-falls-back",
        cache_mode="auto",
        term="ruff",
        regex=False,
        expected_spans=1,
        expected_handled=False,
        expected_reason="partial-coverage",
        with_coverage=False,
    ),
    CacheDecisionSpanCase(
        test_id="off-emits-nothing",
        cache_mode="off",
        term="ruff",
        regex=False,
        expected_spans=0,
        expected_handled=None,
        expected_reason=None,
    ),
)


@pytest.mark.parametrize(
    "case",
    CACHE_DECISION_SPAN_CASES,
    ids=[case.test_id for case in CACHE_DECISION_SPAN_CASES],
)
def test_cache_decision_span_reports_aggregate_outcome(
    case: CacheDecisionSpanCase,
    tmp_path: pathlib.Path,
) -> None:
    """One privacy-safe span per consulted query, never per record."""
    from agentgrep._engine.profiling import EngineProfiler, use_engine_profiler

    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
        coverage=(
            SyncCoverage(agents=("codex",), scope="all", complete=True)
            if case.with_coverage
            else None
        ),
    )
    search_runtime = agentgrep.SearchRuntime(
        db=None if case.cache_mode == "off" else runtime,
        cache_mode=case.cache_mode,
    )
    query = dataclasses.replace(_query(case.term), regex=case.regex)
    profiler = EngineProfiler()

    with use_engine_profiler(profiler):
        _ = agentgrep._db_search_result(query, search_runtime)

    samples = [
        sample for sample in profiler.snapshot().samples if sample.name == "search.cache.decision"
    ]
    assert len(samples) == case.expected_spans
    if case.expected_spans:
        attributes = samples[0].attributes
        assert attributes["agentgrep_cache_mode"] == case.cache_mode
        assert attributes["agentgrep_cache_handled"] == case.expected_handled
        if case.expected_reason is None:
            assert "agentgrep_cache_fallback_reason" not in attributes
        else:
            assert attributes["agentgrep_cache_fallback_reason"] == case.expected_reason


class ScopeParityCase(t.NamedTuple):
    """Named case for cached scope filters mirroring live semantics."""

    test_id: str
    scope: agentgrep.SearchScope
    agents: tuple[agentgrep.AgentName, ...]
    expected_texts: tuple[str, ...]


SCOPE_PARITY_CASES: tuple[ScopeParityCase, ...] = (
    ScopeParityCase(
        test_id="conversations-include-user-turns",
        scope="conversations",
        agents=("codex",),
        expected_texts=(
            "alpaca assistant turn",
            "alpaca chat user turn",
        ),
    ),
    ScopeParityCase(
        test_id="prompts-use-prompt-store-for-covered-agent",
        scope="prompts",
        agents=("codex",),
        expected_texts=("alpaca prompt store entry",),
    ),
    ScopeParityCase(
        test_id="prompts-fall-back-to-chat-without-prompt-store",
        scope="prompts",
        agents=("pi",),
        expected_texts=("alpaca pi user turn",),
    ),
    ScopeParityCase(
        test_id="all-scope-returns-everything",
        scope="all",
        agents=("codex", "pi"),
        expected_texts=(
            "alpaca assistant turn",
            "alpaca chat user turn",
            "alpaca pi user turn",
            "alpaca prompt store entry",
        ),
    ),
)


@pytest.mark.parametrize(
    "case",
    SCOPE_PARITY_CASES,
    ids=[case.test_id for case in SCOPE_PARITY_CASES],
)
def test_cached_scope_filters_mirror_live_semantics(
    case: ScopeParityCase,
    tmp_path: pathlib.Path,
) -> None:
    """Cached scope filtering matches the live planner and record filters.

    Live conversations scope admits records by store role — chat
    adapters emit user turns as kind='prompt' — and live prompts scope
    serves an agent from its dedicated prompt-history store when one
    exists, falling back to chat-store user turns only when it does
    not.
    """
    chat_path = tmp_path / "codex-session.jsonl"
    chat_path.write_text("chat", encoding="utf-8")
    chat_source = _source(chat_path)
    prompt_path = tmp_path / "codex-history.jsonl"
    prompt_path.write_text("history", encoding="utf-8")
    prompt_source = _source(
        prompt_path,
        adapter_id="codex.history_jsonl.v1",
        store="codex.history",
    )
    pi_path = tmp_path / "pi-session.jsonl"
    pi_path.write_text("pi", encoding="utf-8")
    pi_source = _source(
        pi_path,
        adapter_id="pi.sessions_jsonl.v1",
        agent="pi",
        store="pi.sessions",
    )
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (
                chat_source,
                (
                    _record(chat_source, "alpaca chat user turn", session_id="chat-a"),
                    _record(
                        chat_source,
                        "alpaca assistant turn",
                        kind="history",
                        session_id="chat-a",
                    ),
                ),
            ),
            (
                prompt_source,
                (_record(prompt_source, "alpaca prompt store entry", session_id="hist-a"),),
            ),
            (
                pi_source,
                (_record(pi_source, "alpaca pi user turn", session_id="pi-a"),),
            ),
        ),
    )

    found = runtime.search_records(
        _query("alpaca", scope=case.scope, agents=case.agents),
    )
    runtime.close()

    assert sorted(record.text for record in found) == sorted(case.expected_texts)


class CoverageGateCase(t.NamedTuple):
    """Named case for the auto-mode coverage gate."""

    test_id: str
    sync_coverage: SyncCoverage | None
    cache_mode: agentgrep.CacheMode
    query_agents: tuple[agentgrep.AgentName, ...]
    query_scope: agentgrep.SearchScope
    expected_handled: bool


COVERAGE_GATE_CASES: tuple[CoverageGateCase, ...] = (
    CoverageGateCase(
        test_id="full-sync-serves-auto",
        sync_coverage=SyncCoverage(agents=("codex",), scope="all", complete=True),
        cache_mode="auto",
        query_agents=("codex",),
        query_scope="prompts",
        expected_handled=True,
    ),
    CoverageGateCase(
        test_id="agent-subset-falls-back",
        sync_coverage=SyncCoverage(agents=("codex",), scope="all", complete=True),
        cache_mode="auto",
        query_agents=("codex", "claude"),
        query_scope="prompts",
        expected_handled=False,
    ),
    CoverageGateCase(
        test_id="scoped-sync-covers-only-its-scope",
        sync_coverage=SyncCoverage(agents=("codex",), scope="prompts", complete=True),
        cache_mode="auto",
        query_agents=("codex",),
        query_scope="conversations",
        expected_handled=False,
    ),
    CoverageGateCase(
        test_id="capped-sync-claims-nothing",
        sync_coverage=SyncCoverage(agents=("codex",), scope="all", complete=False),
        cache_mode="auto",
        query_agents=("codex",),
        query_scope="prompts",
        expected_handled=False,
    ),
    CoverageGateCase(
        test_id="no-coverage-falls-back",
        sync_coverage=None,
        cache_mode="auto",
        query_agents=("codex",),
        query_scope="prompts",
        expected_handled=False,
    ),
    CoverageGateCase(
        test_id="require-serves-without-coverage",
        sync_coverage=None,
        cache_mode="require",
        query_agents=("codex",),
        query_scope="prompts",
        expected_handled=True,
    ),
)


@pytest.mark.parametrize(
    "case",
    COVERAGE_GATE_CASES,
    ids=[case.test_id for case in COVERAGE_GATE_CASES],
)
def test_auto_cache_hits_require_sync_coverage(
    case: CoverageGateCase,
    tmp_path: pathlib.Path,
) -> None:
    """Auto mode serves cache hits only when coverage spans the query.

    Coverage means the agent/scope combination completed a sync;
    require mode keeps serving regardless because the caller demanded
    the cache.
    """
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
        coverage=case.sync_coverage,
    )
    search_runtime = agentgrep.SearchRuntime(db=runtime, cache_mode=case.cache_mode)

    handled, records = agentgrep._db_search_result(
        _query("ruff", scope=case.query_scope, agents=case.query_agents),
        search_runtime,
    )
    runtime.close()

    assert handled is case.expected_handled
    assert bool(records) is case.expected_handled


def test_interrupted_sync_records_no_coverage(tmp_path: pathlib.Path) -> None:
    """An early-exited sync loop never claims coverage."""
    first_path = tmp_path / "first.jsonl"
    first_path.write_text("ruff", encoding="utf-8")
    second_path = tmp_path / "second.jsonl"
    second_path.write_text("ruff", encoding="utf-8")
    first = _source(first_path)
    second = _source(second_path)
    control = agentgrep.SearchControl()
    progress = StopAfterFirstSourceProgress(control)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")

    _ = runtime.sync_records(
        (
            (first, (_record(first, "Run ruff check before committing."),)),
            (second, (_record(second, "Run ruff again."),)),
        ),
        control=control,
        progress=t.cast("DbSyncProgress", progress),
        coverage=SyncCoverage(agents=("codex",), scope="all", complete=True),
    )

    assert runtime.store.coverage() is None
    runtime.close()


def test_merge_coverage_keeps_other_agents(tmp_path: pathlib.Path) -> None:
    """A narrowed re-sync merges into coverage instead of replacing it."""
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    runtime.store.merge_coverage(
        SyncCoverage(agents=("codex", "claude"), scope="all", complete=True),
    )
    runtime.store.merge_coverage(
        SyncCoverage(agents=("codex",), scope="prompts", complete=True),
    )

    coverage = runtime.store.coverage()
    runtime.close()

    assert coverage == {
        "claude": ("all",),
        "codex": ("all", "prompts"),
    }


class PruneCase(t.NamedTuple):
    """Named case for vanished-source pruning during sync."""

    test_id: str
    prune_missing: bool
    expected_pruned: int
    expected_texts: tuple[str, ...]


PRUNE_CASES: tuple[PruneCase, ...] = (
    PruneCase(
        test_id="full-sync-prunes-vanished-source",
        prune_missing=True,
        expected_pruned=1,
        expected_texts=("Run ruff check before committing.",),
    ),
    PruneCase(
        test_id="narrowed-sync-keeps-vanished-source",
        prune_missing=False,
        expected_pruned=0,
        expected_texts=(
            "Run ruff check before committing.",
            "Run ruff from the vanished file.",
        ),
    ),
)


@pytest.mark.parametrize(
    "case",
    PRUNE_CASES,
    ids=[case.test_id for case in PRUNE_CASES],
)
def test_resync_prunes_sources_missing_from_discovery(
    case: PruneCase,
    tmp_path: pathlib.Path,
) -> None:
    """A pruning resync drops ledger rows for vanished sources.

    A previously indexed file that is deleted or rotated never appears
    in discovery again, so without pruning its records answer cached
    searches forever. Freshness-skipped sources stay: they are part of
    the resync's batch set.
    """
    kept_path = tmp_path / "kept.jsonl"
    kept_path.write_text("ruff", encoding="utf-8")
    vanished_path = tmp_path / "vanished.jsonl"
    vanished_path.write_text("ruff", encoding="utf-8")
    kept = _source(kept_path)
    vanished = _source(vanished_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        (
            (kept, (_record(kept, "Run ruff check before committing."),)),
            (vanished, (_record(vanished, "Run ruff from the vanished file."),)),
        ),
    )
    vanished_path.unlink()

    result = runtime.sync_records(
        ((kept, (_record(kept, "unread"),)),),
        prune_missing=case.prune_missing,
    )

    assert result.sources_pruned == case.expected_pruned
    assert result.sources_skipped == 1
    found = runtime.search_records(_query("ruff"))
    assert sorted(record.text for record in found) == sorted(case.expected_texts)
    assert runtime.status().sources == 2 - case.expected_pruned
    runtime.close()


def test_early_exited_sync_never_prunes(tmp_path: pathlib.Path) -> None:
    """An early-exited loop must not prune sources it never visited."""
    first_path = tmp_path / "first.jsonl"
    first_path.write_text("ruff", encoding="utf-8")
    vanished_path = tmp_path / "vanished.jsonl"
    vanished_path.write_text("ruff", encoding="utf-8")
    first = _source(first_path)
    vanished = _source(vanished_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((vanished, (_record(vanished, "Run ruff from the vanished file."),)),),
    )
    vanished_path.unlink()
    control = agentgrep.SearchControl()
    progress = StopAfterFirstSourceProgress(control)

    second_path = tmp_path / "second.jsonl"
    second_path.write_text("ruff", encoding="utf-8")
    second = _source(second_path)
    result = runtime.sync_records(
        (
            (first, (_record(first, "Run ruff check before committing."),)),
            (second, (_record(second, "Run ruff again."),)),
        ),
        control=control,
        progress=t.cast("DbSyncProgress", progress),
        prune_missing=True,
    )

    assert result.sources_pruned == 0
    assert runtime.status().sources == 2
    runtime.close()


def test_cached_search_with_empty_agent_selection_returns_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """An empty agent selection matches live discovery: zero records.

    Guards the public library surface — CLI and MCP always normalize
    to a non-empty selection, but ``SearchQuery(agents=())`` is a
    structurally valid input and ``IN ()`` is nonstandard SQL.
    """
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "Run ruff check before committing."),)),),
    )

    found = runtime.search_records(_query("ruff", agents=()))
    runtime.close()

    assert found == []


class SqlTelemetrySearchCase(t.NamedTuple):
    """Named case for SQL statement samples emitted by cached search."""

    test_id: str
    term: str
    expected_statement: str


SQL_TELEMETRY_SEARCH_CASES: tuple[SqlTelemetrySearchCase, ...] = (
    SqlTelemetrySearchCase(
        test_id="fts-path-statement",
        term="alpaca",
        expected_statement="records.search_fts",
    ),
    SqlTelemetrySearchCase(
        test_id="scan-path-statement",
        term="zq",
        expected_statement="records.search_scan",
    ),
)


def _sql_samples(
    profiler_samples: t.Any,
) -> dict[str, t.Any]:
    """Index db.sql.statement samples by statement name."""
    return {
        str(sample.attributes["agentgrep_sql_statement"]): sample
        for sample in profiler_samples
        if sample.name == "db.sql.statement"
    }


@pytest.mark.parametrize(
    "case",
    SQL_TELEMETRY_SEARCH_CASES,
    ids=[case.test_id for case in SQL_TELEMETRY_SEARCH_CASES],
)
def test_search_emits_aggregated_sql_statement_samples(
    case: SqlTelemetrySearchCase,
    tmp_path: pathlib.Path,
) -> None:
    """Cached search emits one db.sql.statement sample per statement shape."""
    from agentgrep._engine.profiling import EngineProfiler, use_engine_profiler

    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "alpaca zq sentinel-free text"),)),),
    )
    profiler = EngineProfiler()

    with use_engine_profiler(profiler):
        _ = runtime.search_records(_query(case.term))
    runtime.close()

    samples = _sql_samples(profiler.snapshot().samples)
    assert case.expected_statement in samples
    statement = samples[case.expected_statement]
    assert statement.attributes["agentgrep_sql_count"] == 1
    assert statement.attributes["agentgrep_sql_rows"] == 1


def test_sync_aggregates_sql_statement_samples(tmp_path: pathlib.Path) -> None:
    """A multi-record sync emits one aggregated sample per statement shape.

    Per-record statements (records.insert, fts.insert) must show up as
    one sample with a count — the n+1 signal — never as one sample per
    execution.
    """
    from agentgrep._engine.profiling import EngineProfiler, use_engine_profiler

    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    records = tuple(
        _record(source, f"alpaca record {index}", session_id=f"session-{index}")
        for index in range(3)
    )
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    profiler = EngineProfiler()

    with use_engine_profiler(profiler):
        _ = runtime.sync_records(((source, records),))
    runtime.close()

    all_sql = [
        sample for sample in profiler.snapshot().samples if sample.name == "db.sql.statement"
    ]
    samples = _sql_samples(all_sql)
    assert samples["records.insert"].attributes["agentgrep_sql_count"] == 3
    assert samples["fts.insert"].attributes["agentgrep_sql_count"] == 3
    statement_names = [str(sample.attributes["agentgrep_sql_statement"]) for sample in all_sql]
    assert len(statement_names) == len(set(statement_names))


def test_sql_telemetry_is_silent_without_active_profiler(
    tmp_path: pathlib.Path,
) -> None:
    """Without an active profiler, ops run clean and leave no stats behind."""
    from agentgrep._engine.profiling import EngineProfiler, use_engine_profiler

    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "alpaca quiet"),)),),
    )
    _ = runtime.search_records(_query("alpaca"))
    assert runtime.store._sql_stats == {}

    late_profiler = EngineProfiler()
    with use_engine_profiler(late_profiler):
        pass
    runtime.close()

    assert late_profiler.snapshot().samples == ()


def test_sql_telemetry_never_captures_bound_parameters(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Statement telemetry carries placeholders only — never search terms."""
    from agentgrep._engine.profiling import EngineProfiler, use_engine_profiler

    sentinel = "zanzibar7sentinel"
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, f"prompt mentioning {sentinel} once"),)),),
    )
    profiler = EngineProfiler()

    with (
        caplog.at_level(logging.DEBUG, logger="agentgrep.db"),
        use_engine_profiler(profiler),
    ):
        found = runtime.search_records(_query(sentinel))
    runtime.close()

    assert len(found) == 1
    sql_records = [
        record for record in caplog.records if hasattr(record, "agentgrep_sql_statement")
    ]
    assert sql_records
    for record in sql_records:
        assert sentinel not in record.getMessage()
        assert sentinel not in str(record.agentgrep_sql_statement)
    for sample in profiler.snapshot().samples:
        for value in sample.attributes.values():
            assert sentinel not in str(value)


class SqlPlanCaptureCase(t.NamedTuple):
    """Named case for opt-in EXPLAIN QUERY PLAN capture."""

    test_id: str
    explain_env: str | None
    term: str
    expected_statement: str
    expects_plan: bool
    plan_mentions: str | None


SQL_PLAN_CAPTURE_CASES: tuple[SqlPlanCaptureCase, ...] = (
    SqlPlanCaptureCase(
        test_id="fts-plan-captured",
        explain_env="1",
        term="alpaca",
        expected_statement="records.search_fts",
        expects_plan=True,
        plan_mentions="VIRTUAL TABLE",
    ),
    SqlPlanCaptureCase(
        test_id="scan-plan-captured",
        explain_env="1",
        term="zq",
        expected_statement="records.search_scan",
        expects_plan=True,
        plan_mentions="SCAN",
    ),
    SqlPlanCaptureCase(
        test_id="off-by-default",
        explain_env=None,
        term="alpaca",
        expected_statement="records.search_fts",
        expects_plan=False,
        plan_mentions=None,
    ),
)


@pytest.mark.parametrize(
    "case",
    SQL_PLAN_CAPTURE_CASES,
    ids=[case.test_id for case in SQL_PLAN_CAPTURE_CASES],
)
def test_sql_plan_capture_honors_explain_lever(
    case: SqlPlanCaptureCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AGENTGREP_SQL_EXPLAIN attaches query plans to statement samples."""
    from agentgrep._engine.profiling import EngineProfiler, use_engine_profiler

    if case.explain_env is None:
        monkeypatch.delenv("AGENTGREP_SQL_EXPLAIN", raising=False)
    else:
        monkeypatch.setenv("AGENTGREP_SQL_EXPLAIN", case.explain_env)
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("ruff", encoding="utf-8")
    source = _source(source_path)
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    _ = runtime.sync_records(
        ((source, (_record(source, "alpaca zq plan capture text"),)),),
    )
    profiler = EngineProfiler()

    with use_engine_profiler(profiler):
        _ = runtime.search_records(_query(case.term))
    runtime.close()

    samples = _sql_samples(profiler.snapshot().samples)
    statement = samples[case.expected_statement]
    if case.expects_plan:
        plan = str(statement.attributes["agentgrep_sql_plan"])
        assert case.plan_mentions is not None
        assert case.plan_mentions in plan
    else:
        assert "agentgrep_sql_plan" not in statement.attributes


def test_sql_plan_captured_once_per_statement_shape(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated executions of one shape run EXPLAIN only once."""
    monkeypatch.setenv("AGENTGREP_SQL_EXPLAIN", "1")
    runtime = DbRuntime.open(tmp_path / "agentgrep.sqlite")
    store = runtime.store

    first = store._query("meta.get", "SELECT value FROM meta WHERE key = ?", ("a",))
    stats = store._sql_stats["meta.get"]
    plan_after_first = stats.plan
    second = store._query("meta.get", "SELECT value FROM meta WHERE key = ?", ("b",))
    runtime.close()

    assert first == [] and second == []
    assert plan_after_first is not None
    assert stats.plan is plan_after_first
    assert stats.count == 2
