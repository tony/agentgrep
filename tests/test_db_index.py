"""Tests for the persistent DB index layer."""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep.db import DbRuntime, DbStatus


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


def _source(path: pathlib.Path) -> agentgrep.SourceHandle:
    """Build a synthetic source handle for db tests."""
    return agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
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
