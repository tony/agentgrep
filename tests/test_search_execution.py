"""Tests for search physical-plan execution."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import json
import os
import pathlib
import queue
import threading
import time
import typing as t

import pytest

import agentgrep
import agentgrep._engine.execution as execution
import agentgrep._engine.matching as matching
import agentgrep._engine.orchestration as _rm_orch
import agentgrep._engine.scanning as _rm_scanning
import agentgrep._engine.scanning as scanning
import agentgrep._engine.scheduling as scheduling
import agentgrep.origin as origin
from agentgrep._engine.execution import (
    ExecutionDriverConfig,
    ExecutionRecordEmitted,
    ExecutionSourceFinished,
    ExecutionSourceStarted,
    FrontierExecutionDriver,
    InlineExecutionDriver,
    SourceScanBatch,
)
from agentgrep._engine.planning import (
    LimitPolicy,
    PhysicalSearchPlan,
    SourceStrategy,
    SourceTask,
    build_logical_search_plan,
    build_source_authority_plan,
)
from agentgrep._engine.profiling import EngineProfiler, use_engine_profiler


class BatchSchedulerCase(t.NamedTuple):
    """Named case for incremental scheduler behavior."""

    test_id: str
    max_workers: int
    expected_emitted: tuple[str, ...]
    expected_skipped: int


class SourceScanCacheCase(t.NamedTuple):
    """Named case for runtime-owned source scan caching behavior."""

    test_id: str
    runtime: agentgrep.SearchRuntime | None
    mutate_source_between_scans: bool
    expected_batch_reads: int
    expected_hits: int
    expected_misses: int


def _query(
    *,
    limit: int | None = None,
    match_surface: agentgrep.SearchMatchSurface = "haystack",
) -> agentgrep.SearchQuery:
    """Build a simple prompt query for execution tests."""
    return agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=limit,
        dedupe=True,
        match_surface=match_surface,
    )


def _source(
    path: pathlib.Path,
    *,
    agent: agentgrep.AgentName = "codex",
    store: str = "codex.sessions",
    adapter_id: str = "codex.sessions_jsonl.v1",
    path_kind: agentgrep.PathKind = "session_file",
    source_kind: agentgrep.SourceKind = "jsonl",
) -> agentgrep.SourceHandle:
    """Build a synthetic source handle for execution tests."""
    return agentgrep.SourceHandle(
        agent=agent,
        store=store,
        adapter_id=adapter_id,
        path=path,
        path_kind=path_kind,
        source_kind=source_kind,
        search_root=None,
        mtime_ns=0,
    )


def _record(
    source: agentgrep.SourceHandle,
    text: str,
    timestamp: str,
) -> agentgrep.SearchRecord:
    """Build a synthetic search record for execution tests."""
    return agentgrep.SearchRecord(
        kind="prompt",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        timestamp=timestamp,
        session_id=source.path.stem,
    )


def _plan(
    query: agentgrep.SearchQuery,
    source: agentgrep.SourceHandle,
    *,
    strategy: SourceStrategy = "direct_full_scan",
) -> PhysicalSearchPlan:
    """Build a one-source physical plan for execution tests."""
    return PhysicalSearchPlan(
        logical=build_logical_search_plan(query),
        tasks=(
            SourceTask(
                source=source,
                strategy=strategy,
                record_order=(
                    "newest_first"
                    if strategy
                    in {
                        "jsonl_bounded_reverse_scan",
                        "jsonl_bounded_reverse_raw_text_prefilter",
                        "jsonl_bounded_reverse_haystack_raw_text_prefilter",
                    }
                    else "unknown"
                ),
                limit_behavior=(
                    "bounded_source"
                    if strategy
                    in {
                        "jsonl_bounded_reverse_scan",
                        "jsonl_bounded_reverse_raw_text_prefilter",
                        "jsonl_bounded_reverse_haystack_raw_text_prefilter",
                    }
                    else "drain_source"
                ),
                can_stream_records=True,
                restore_order_key=(0, str(source.path)),
            ),
        ),
        decisions=(),
        source_authority=build_source_authority_plan((source,)),
    )


def _multi_plan(
    query: agentgrep.SearchQuery,
    sources: tuple[agentgrep.SourceHandle, ...],
    *,
    strategy: SourceStrategy = "jsonl_bounded_reverse_scan",
) -> PhysicalSearchPlan:
    """Build a multi-source physical plan for scheduler tests."""
    return PhysicalSearchPlan(
        logical=build_logical_search_plan(query),
        tasks=tuple(
            SourceTask(
                source=source,
                strategy=strategy,
                record_order=(
                    "newest_first"
                    if strategy
                    in {
                        "jsonl_bounded_reverse_scan",
                        "jsonl_bounded_reverse_raw_text_prefilter",
                        "jsonl_bounded_reverse_haystack_raw_text_prefilter",
                    }
                    else "unknown"
                ),
                limit_behavior=(
                    "bounded_source"
                    if strategy
                    in {
                        "jsonl_bounded_reverse_scan",
                        "jsonl_bounded_reverse_raw_text_prefilter",
                        "jsonl_bounded_reverse_haystack_raw_text_prefilter",
                    }
                    else "drain_source"
                ),
                can_stream_records=True,
                restore_order_key=(-source.mtime_ns, str(source.path)),
            )
            for source in sources
        ),
        decisions=(),
        source_authority=build_source_authority_plan(sources),
    )


def test_inline_execution_driver_emits_source_and_record_events_in_order(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inline driver owns the source loop and per-source result ordering."""
    query = _query()
    source = _source(tmp_path / "session.jsonl")
    older = _record(source, "older bliss", "2026-01-01T00:00:00Z")
    newer = _record(source, "newer bliss", "2026-01-02T00:00:00Z")

    def iter_records(_source: agentgrep.SourceHandle) -> list[agentgrep.SearchRecord]:
        return [older, newer]

    monkeypatch.setattr(_rm_scanning, "iter_source_records", iter_records)

    events = list(InlineExecutionDriver().iter_search_plan(query, _plan(query, source)))

    assert isinstance(events[0], ExecutionSourceStarted)
    assert [event.record for event in events if isinstance(event, ExecutionRecordEmitted)] == [
        newer,
        older,
    ]
    finished = [event for event in events if isinstance(event, ExecutionSourceFinished)]
    assert len(finished) == 1
    assert finished[0].records_seen == 2
    assert finished[0].matches_seen == 2


def test_jsonl_raw_text_prefilter_skips_nonmatching_lines_before_json_decode(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw-prefiltered JSONL tasks avoid decoding lines that cannot match."""
    query = _query(match_surface="text")
    source = _source(tmp_path / "session.jsonl")
    source.path.write_text(
        "\n".join(
            (
                '{"type":"response_item","payload":{"role":"user","content":"skip me"}}',
                '{"type":"response_item","payload":{"role":"user","content":"bliss wins"}}',
            ),
        ),
        encoding="utf-8",
    )
    decoded_inputs: list[str] = []
    original_loads = agentgrep._loads

    def loads_with_capture(payload: str) -> object:
        decoded_inputs.append(payload)
        return original_loads(payload)

    monkeypatch.setattr(agentgrep.readers, "_loads", loads_with_capture)

    events = list(
        InlineExecutionDriver().iter_search_plan(
            query,
            _plan(query, source, strategy="jsonl_raw_text_prefilter"),
        ),
    )

    assert [event.record.text for event in events if isinstance(event, ExecutionRecordEmitted)] == [
        "bliss wins",
    ]
    assert not any("skip me" in payload for payload in decoded_inputs)
    assert any("bliss wins" in payload for payload in decoded_inputs)


def test_jsonl_raw_text_prefilter_keeps_escaped_candidate_lines(
    tmp_path: pathlib.Path,
) -> None:
    """Escaped JSON strings stay on the decode path to preserve semantics."""
    query = _query(match_surface="text")
    source = _source(tmp_path / "session.jsonl")
    source.path.write_text(
        '{"type":"response_item","payload":{"role":"user","content":"\\u0062liss escaped"}}\n',
        encoding="utf-8",
    )

    events = list(
        InlineExecutionDriver().iter_search_plan(
            query,
            _plan(query, source, strategy="jsonl_raw_text_prefilter"),
        ),
    )

    assert [event.record.text for event in events if isinstance(event, ExecutionRecordEmitted)] == [
        "bliss escaped",
    ]


class RawTextSkipCase(t.NamedTuple):
    """One raw-line prefilter case."""

    test_id: str
    terms: tuple[str, ...]
    any_term: bool
    case_sensitive: bool
    raw_line: str
    expected_skip: bool


RAW_TEXT_SKIP_CASES: tuple[RawTextSkipCase, ...] = (
    RawTextSkipCase(
        test_id="casefold-match",
        terms=("bliss",),
        any_term=False,
        case_sensitive=False,
        raw_line='{"content":"BLISS"}',
        expected_skip=False,
    ),
    RawTextSkipCase(
        test_id="case-sensitive-miss",
        terms=("bliss",),
        any_term=False,
        case_sensitive=True,
        raw_line='{"content":"BLISS"}',
        expected_skip=True,
    ),
    RawTextSkipCase(
        test_id="all-terms-match",
        terms=("bliss", "tmux"),
        any_term=False,
        case_sensitive=False,
        raw_line='{"content":"tmux bliss"}',
        expected_skip=False,
    ),
    RawTextSkipCase(
        test_id="all-terms-miss",
        terms=("bliss", "tmux"),
        any_term=False,
        case_sensitive=False,
        raw_line='{"content":"tmux only"}',
        expected_skip=True,
    ),
    RawTextSkipCase(
        test_id="any-term-match",
        terms=("bliss", "tmux"),
        any_term=True,
        case_sensitive=False,
        raw_line='{"content":"tmux only"}',
        expected_skip=False,
    ),
    RawTextSkipCase(
        test_id="escaped-line-kept",
        terms=("bliss",),
        any_term=False,
        case_sensitive=False,
        raw_line='{"content":"\\u0062liss"}',
        expected_skip=False,
    ),
    RawTextSkipCase(
        test_id="escaped-newline-miss",
        terms=("bliss",),
        any_term=False,
        case_sensitive=False,
        raw_line='{"content":"other\\nline"}',
        expected_skip=True,
    ),
    RawTextSkipCase(
        test_id="escaped-newline-match",
        terms=("\n",),
        any_term=False,
        case_sensitive=True,
        raw_line='{"content":"other\\nline"}',
        expected_skip=False,
    ),
    RawTextSkipCase(
        test_id="solidus-escaped-path-kept",
        terms=("/path/to",),
        any_term=False,
        case_sensitive=True,
        raw_line='{"content":"\\/path\\/to"}',
        expected_skip=False,
    ),
    RawTextSkipCase(
        test_id="solidus-unescaped-path-kept",
        terms=("/path/to",),
        any_term=False,
        case_sensitive=True,
        raw_line='{"content":"/path/to"}',
        expected_skip=False,
    ),
    RawTextSkipCase(
        test_id="solidus-term-genuine-miss-skipped",
        terms=("/abc",),
        any_term=False,
        case_sensitive=True,
        raw_line='{"content":"xyz"}',
        expected_skip=True,
    ),
    RawTextSkipCase(
        test_id="solidus-casefold-escaped-path-kept",
        terms=("/Path/Here",),
        any_term=False,
        case_sensitive=False,
        raw_line='{"content":"\\/path\\/here"}',
        expected_skip=False,
    ),
    RawTextSkipCase(
        test_id="solidus-mixed-escaping-kept-conservatively",
        terms=("/a/b",),
        any_term=False,
        case_sensitive=True,
        raw_line='{"content":"/a\\/b"}',
        expected_skip=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    RAW_TEXT_SKIP_CASES,
    ids=[c.test_id for c in RAW_TEXT_SKIP_CASES],
)
def test_raw_text_skip_line_precomputes_literal_query(case: RawTextSkipCase) -> None:
    """Literal raw-line prefiltering preserves match semantics."""
    query = agentgrep.SearchQuery(
        terms=case.terms,
        scope="conversations",
        any_term=case.any_term,
        regex=False,
        case_sensitive=case.case_sensitive,
        agents=("codex",),
        limit=500,
        dedupe=True,
        match_surface="text",
    )

    skip_line = execution.raw_text_skip_line_for_query(query)

    assert skip_line(case.raw_line) is case.expected_skip


def test_raw_text_skip_line_does_not_rebuild_matches_per_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Literal raw-line prefiltering avoids the generic matcher hot path."""
    query = _query(limit=1, match_surface="text")

    def fail_matches_text(_text: str, _query: agentgrep.SearchQuery) -> bool:
        pytest.fail("literal raw-line skip should not call matches_text")

    monkeypatch.setattr(_rm_orch, "matches_text", fail_matches_text)
    skip_line = execution.raw_text_skip_line_for_query(query)

    assert skip_line('{"content":"bliss"}') is False
    assert skip_line('{"content":"miss"}') is True


class HaystackRawTextSkipCase(t.NamedTuple):
    """One source-aware haystack raw-line prefilter case."""

    test_id: str
    terms: tuple[str, ...]
    any_term: bool
    source_path: str
    raw_line: str
    expected_skip: bool


HAYSTACK_RAW_TEXT_SKIP_CASES: tuple[HaystackRawTextSkipCase, ...] = (
    HaystackRawTextSkipCase(
        test_id="any-term-source-path-match-keeps-line",
        terms=("project-tmux", "bliss"),
        any_term=True,
        source_path="/tmp/project-tmux/session.jsonl",
        raw_line='{"content":"unrelated"}',
        expected_skip=False,
    ),
    HaystackRawTextSkipCase(
        test_id="all-terms-source-path-match-removes-static-term",
        terms=("project-tmux", "bliss"),
        any_term=False,
        source_path="/tmp/project-tmux/session.jsonl",
        raw_line='{"content":"bliss"}',
        expected_skip=False,
    ),
    HaystackRawTextSkipCase(
        test_id="all-terms-source-path-match-requires-remaining-term",
        terms=("project-tmux", "bliss"),
        any_term=False,
        source_path="/tmp/project-tmux/session.jsonl",
        raw_line='{"content":"unrelated"}',
        expected_skip=True,
    ),
    HaystackRawTextSkipCase(
        test_id="all-terms-all-source-path-matches-keep-line",
        terms=("project-tmux", "session.jsonl"),
        any_term=False,
        source_path="/tmp/project-tmux/session.jsonl",
        raw_line='{"content":"unrelated"}',
        expected_skip=False,
    ),
    HaystackRawTextSkipCase(
        test_id="unicode-escape-kept",
        terms=("bliss",),
        any_term=False,
        source_path="/tmp/session.jsonl",
        raw_line='{"content":"\\u0062liss"}',
        expected_skip=False,
    ),
    HaystackRawTextSkipCase(
        test_id="solidus-escaped-content-term-kept",
        terms=("/usr/local",),
        any_term=False,
        source_path="/tmp/session.jsonl",
        raw_line='{"content":"\\/usr\\/local"}',
        expected_skip=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    HAYSTACK_RAW_TEXT_SKIP_CASES,
    ids=[c.test_id for c in HAYSTACK_RAW_TEXT_SKIP_CASES],
)
def test_haystack_raw_text_skip_line_accounts_for_source_path(
    case: HaystackRawTextSkipCase,
) -> None:
    """Haystack raw-line prefiltering preserves source-path matches."""
    query = agentgrep.SearchQuery(
        terms=case.terms,
        scope="conversations",
        any_term=case.any_term,
        regex=False,
        case_sensitive=False,
        agents=("claude",),
        limit=500,
        dedupe=True,
        match_surface="haystack",
    )
    source = _source(
        pathlib.Path(case.source_path),
        agent="claude",
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
    )

    skip_line = execution.raw_text_skip_line_for_haystack_query(query, source)

    assert skip_line(case.raw_line) is case.expected_skip


def test_bounded_haystack_raw_prefilter_keeps_source_path_matches(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Haystack raw prefiltering keeps rows matched only by source metadata."""
    query = agentgrep.SearchQuery(
        terms=("project-tmux",),
        scope="conversations",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("claude",),
        limit=1,
        dedupe=True,
        match_surface="haystack",
    )
    source = _source(
        tmp_path / "project-tmux" / "session.jsonl",
        agent="claude",
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
    )
    source.path.parent.mkdir()
    source.path.write_text(
        '{"type":"response_item","payload":{"role":"user","content":"metadata only"}}\n',
        encoding="utf-8",
    )
    decoded_inputs: list[str] = []
    original_loads = agentgrep._loads

    def loads_with_capture(payload: str) -> object:
        decoded_inputs.append(payload)
        return original_loads(payload)

    monkeypatch.setattr(agentgrep.readers, "_loads", loads_with_capture)

    events = list(
        InlineExecutionDriver().iter_search_plan(
            query,
            _plan(
                query,
                source,
                strategy="jsonl_bounded_reverse_haystack_raw_text_prefilter",
            ),
        ),
    )

    assert [event.record.text for event in events if isinstance(event, ExecutionRecordEmitted)] == [
        "metadata only",
    ]
    assert len(decoded_inputs) == 1


class BoundedExecutionCase(t.NamedTuple):
    """One bounded source strategy and its expected early-stop behavior."""

    test_id: str
    strategy: SourceStrategy


BOUNDED_EXECUTION_CASES: tuple[BoundedExecutionCase, ...] = (
    BoundedExecutionCase(
        test_id="reverse-scan",
        strategy="jsonl_bounded_reverse_scan",
    ),
    BoundedExecutionCase(
        test_id="reverse-raw-prefilter",
        strategy="jsonl_bounded_reverse_raw_text_prefilter",
    ),
)


@pytest.mark.parametrize(
    "case",
    BOUNDED_EXECUTION_CASES,
    ids=[c.test_id for c in BOUNDED_EXECUTION_CASES],
)
def test_bounded_source_execution_stops_after_unique_limit(
    case: BoundedExecutionCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded source execution can stop without draining older records."""
    query = _query(limit=2)
    source = _source(tmp_path / "session.jsonl")
    records = (
        _record(source, "newest bliss", "2026-01-04T00:00:00Z"),
        _record(source, "newest bliss", "2026-01-03T00:00:00Z"),
        _record(source, "second bliss", "2026-01-02T00:00:00Z"),
        _record(source, "extra bliss", "2026-01-01T00:00:00Z"),
    )
    consumed_texts: list[str] = []

    def iter_records(
        task: SourceTask,
        _query: agentgrep.SearchQuery,
    ) -> cabc.Iterator[agentgrep.SearchRecord]:
        assert task.strategy == case.strategy
        for record in records:
            consumed_texts.append(record.text)
            yield record

    monkeypatch.setattr(scanning, "iter_source_task_records", iter_records)

    events = list(
        InlineExecutionDriver().iter_search_plan(
            query,
            _plan(query, source, strategy=case.strategy),
        ),
    )

    assert [event.record.text for event in events if isinstance(event, ExecutionRecordEmitted)] == [
        "newest bliss",
        "second bliss",
    ]
    assert consumed_texts == ["newest bliss", "newest bliss", "second bliss"]
    finished = [event for event in events if isinstance(event, ExecutionSourceFinished)]
    assert len(finished) == 1
    assert finished[0].records_seen == 3
    assert finished[0].matches_seen == 3


def test_bounded_reverse_raw_prefilter_reads_newest_matching_jsonl_first(
    tmp_path: pathlib.Path,
) -> None:
    """Reverse raw prefiltering can satisfy a limit from the newest JSONL match."""
    query = _query(limit=1, match_surface="text")
    source = _source(tmp_path / "session.jsonl")
    source.path.write_text(
        "\n".join(
            (
                '{"timestamp":"2026-01-01T00:00:00Z","type":"response_item",'
                '"payload":{"role":"user","content":"old bliss"}}',
                '{"timestamp":"2026-01-02T00:00:00Z","type":"response_item",'
                '"payload":{"role":"user","content":"newer bliss"}}',
                '{"timestamp":"2026-01-03T00:00:00Z","type":"response_item",'
                '"payload":{"role":"user","content":"latest miss"}}',
            ),
        ),
        encoding="utf-8",
    )

    events = list(
        InlineExecutionDriver().iter_search_plan(
            query,
            _plan(
                query,
                source,
                strategy="jsonl_bounded_reverse_raw_text_prefilter",
            ),
        ),
    )

    assert [event.record.text for event in events if isinstance(event, ExecutionRecordEmitted)] == [
        "newer bliss",
    ]
    finished = [event for event in events if isinstance(event, ExecutionSourceFinished)]
    assert len(finished) == 1
    assert finished[0].records_seen == 1
    assert finished[0].matches_seen == 1


def test_scan_source_task_returns_bounded_candidates_without_global_state(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source scan returns local candidates without owning global dedupe."""
    query = _query(limit=2)
    source = _source(tmp_path / "session.jsonl")
    records = (
        _record(source, "newest bliss", "2026-01-04T00:00:00Z"),
        _record(source, "newest bliss", "2026-01-03T00:00:00Z"),
        _record(source, "second bliss", "2026-01-02T00:00:00Z"),
        _record(source, "extra bliss", "2026-01-01T00:00:00Z"),
    )
    consumed_texts: list[str] = []

    def iter_records(
        _task: SourceTask,
        _query: agentgrep.SearchQuery,
    ) -> cabc.Iterator[agentgrep.SearchRecord]:
        for record in records:
            consumed_texts.append(record.text)
            yield record

    monkeypatch.setattr(scanning, "iter_source_task_records", iter_records)
    task = _plan(query, source, strategy="jsonl_bounded_reverse_scan").tasks[0]

    result = scanning.scan_source_task(
        query,
        task,
        index=1,
        total=1,
        control=agentgrep.SearchControl(),
    )

    assert [record.text for record in result.records] == [
        "newest bliss",
        "newest bliss",
        "second bliss",
    ]
    assert consumed_texts == ["newest bliss", "newest bliss", "second bliss"]
    assert result.records_seen == 3
    assert result.matches_seen == 3


def test_scan_source_task_collects_the_same_records_as_source_batches(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compatibility scan result is just a collector over source batches."""
    query = _query(limit=2)
    source = _source(tmp_path / "session.jsonl")
    records = (
        _record(source, "newest bliss", "2026-01-04T00:00:00Z"),
        _record(source, "newest bliss", "2026-01-03T00:00:00Z"),
        _record(source, "second bliss", "2026-01-02T00:00:00Z"),
        _record(source, "extra bliss", "2026-01-01T00:00:00Z"),
    )

    def iter_records(
        _task: SourceTask,
        _query: agentgrep.SearchQuery,
    ) -> cabc.Iterator[agentgrep.SearchRecord]:
        yield from records

    monkeypatch.setattr(scanning, "iter_source_task_records", iter_records)
    task = _plan(query, source, strategy="jsonl_bounded_reverse_scan").tasks[0]

    batches = tuple(
        scanning.iter_source_task_batches(
            query,
            task,
            index=1,
            total=1,
            control=agentgrep.SearchControl(),
        ),
    )
    result = scanning.scan_source_task(
        query,
        task,
        index=1,
        total=1,
        control=agentgrep.SearchControl(),
    )

    assert all(isinstance(batch, SourceScanBatch) for batch in batches)
    assert [record.text for batch in batches for record in batch.records] == [
        record.text for record in result.records
    ]
    assert batches[-1].is_final is True
    assert batches[-1].records_seen == result.records_seen
    assert batches[-1].matches_seen == result.matches_seen


@pytest.mark.parametrize(
    "case",
    (
        SourceScanCacheCase(
            test_id="no-runtime-cache-reads-twice",
            runtime=None,
            mutate_source_between_scans=False,
            expected_batch_reads=2,
            expected_hits=0,
            expected_misses=0,
        ),
        SourceScanCacheCase(
            test_id="runtime-cache-reuses-second-scan",
            runtime=agentgrep.SearchRuntime.with_source_scan_cache(),
            mutate_source_between_scans=False,
            expected_batch_reads=1,
            expected_hits=1,
            expected_misses=1,
        ),
        SourceScanCacheCase(
            test_id="runtime-cache-invalidates-on-source-change",
            runtime=agentgrep.SearchRuntime.with_source_scan_cache(),
            mutate_source_between_scans=True,
            expected_batch_reads=2,
            expected_hits=0,
            expected_misses=2,
        ),
    ),
    ids=lambda case: case.test_id,
)
def test_scan_source_task_uses_runtime_source_scan_cache(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: SourceScanCacheCase,
) -> None:
    """Repeated source scans can reuse a runtime cache until source metadata changes."""
    query = _query(limit=2)
    source = _source(tmp_path / "session.jsonl")
    source.path.write_text("first\n", encoding="utf-8")
    task = _plan(query, source, strategy="jsonl_bounded_reverse_scan").tasks[0]
    batch_reads = 0

    def iter_batches(
        _query: agentgrep.SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        control: agentgrep.SearchControl,
        progress: agentgrep.SearchProgress | None = None,
        batch_size: int = 32,
    ) -> cabc.Iterator[SourceScanBatch]:
        nonlocal batch_reads
        assert progress is None
        assert batch_size == 32
        assert not control.answer_now_requested()
        batch_reads += 1
        yield SourceScanBatch(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=(_record(task.source, "newest bliss", "2026-01-02T00:00:00Z"),),
            records_seen=1,
            matches_seen=1,
            duration_seconds=0.0,
            is_final=True,
        )

    monkeypatch.setattr(scanning, "iter_source_task_batches", iter_batches)

    first = scanning.scan_source_task(
        query,
        task,
        index=1,
        total=1,
        control=agentgrep.SearchControl(),
        runtime=case.runtime,
    )
    if case.mutate_source_between_scans:
        source.path.write_text("changed source size\n", encoding="utf-8")
    second = scanning.scan_source_task(
        query,
        task,
        index=2,
        total=2,
        control=agentgrep.SearchControl(),
        runtime=case.runtime,
    )

    assert first.records == second.records
    assert first.cache_hit is False
    assert second.cache_hit is (case.runtime is not None and not case.mutate_source_between_scans)
    assert second.index == 2
    assert second.total == 2
    assert batch_reads == case.expected_batch_reads
    if case.runtime is not None and case.runtime.source_scan_cache is not None:
        stats = case.runtime.source_scan_cache.stats()
        assert stats.hits == case.expected_hits
        assert stats.misses == case.expected_misses


def test_source_scan_cache_separates_origin_filters(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cached records remain scoped to the origin filter that produced them."""
    query = _query(limit=2)
    source = _source(tmp_path / "session.jsonl")
    source.path.write_text("first\n", encoding="utf-8")
    task = _plan(query, source, strategy="jsonl_bounded_reverse_scan").tasks[0]
    batch_reads = 0

    def iter_batches(
        _query: agentgrep.SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        control: agentgrep.SearchControl,
        progress: agentgrep.SearchProgress | None = None,
        batch_size: int = 32,
    ) -> cabc.Iterator[SourceScanBatch]:
        nonlocal batch_reads
        _ = progress, batch_size
        assert not control.answer_now_requested()
        batch_reads += 1
        yield SourceScanBatch(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=(_record(task.source, "newest bliss", "2026-01-02T00:00:00Z"),),
            records_seen=1,
            matches_seen=1,
            duration_seconds=0.0,
            is_final=True,
        )

    monkeypatch.setattr(scanning, "iter_source_task_batches", iter_batches)
    runtime = agentgrep.SearchRuntime.with_source_scan_cache()
    first_query = dataclasses.replace(
        query,
        origin_filter=agentgrep.RecordOrigin(cwd="/workspace/one"),
    )
    second_query = dataclasses.replace(
        query,
        origin_filter=agentgrep.RecordOrigin(cwd="/workspace/two"),
    )

    first = scanning.scan_source_task(
        first_query,
        task,
        index=1,
        total=2,
        control=agentgrep.SearchControl(),
        runtime=runtime,
    )
    second = scanning.scan_source_task(
        second_query,
        task,
        index=2,
        total=2,
        control=agentgrep.SearchControl(),
        runtime=runtime,
    )

    assert first.cache_hit is False
    assert second.cache_hit is False
    assert batch_reads == 2
    assert runtime.source_scan_cache is not None
    stats = runtime.source_scan_cache.stats()
    assert stats.hits == 0
    assert stats.misses == 2


def test_compiled_record_matcher_reuses_origin_filter_matcher(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit origin filters are compiled once for a reusable record matcher."""
    source = _source(tmp_path / "session.jsonl")
    query = dataclasses.replace(
        _query(),
        origin_filter=agentgrep.RecordOrigin(cwd="/workspace/project"),
    )
    records = (
        _record(source, "first bliss", "2026-01-01T00:00:00Z"),
        _record(source, "second bliss", "2026-01-02T00:00:00Z"),
    )
    records = tuple(
        dataclasses.replace(
            record,
            origin=agentgrep.RecordOrigin(cwd=f"/workspace/project/{index}"),
        )
        for index, record in enumerate(records)
    )
    original_from_origin = origin.OriginMatcher.from_origin
    compiled_filters = 0

    def traced_from_origin(
        cls: type[origin.OriginMatcher],
        origin_filter: agentgrep.RecordOrigin | None,
    ) -> origin.OriginMatcher:
        nonlocal compiled_filters
        _ = cls
        compiled_filters += 1
        return original_from_origin(origin_filter)

    monkeypatch.setattr(origin.OriginMatcher, "from_origin", classmethod(traced_from_origin))

    matcher = matching.compile_record_matcher(query)

    assert [matcher.matches(record) for record in records] == [True, True]
    assert compiled_filters == 1


def test_scan_source_task_exempts_cross_file_adapters(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapters reading sibling files never populate the source scan cache."""
    query = _query(limit=2)
    source = _source(
        tmp_path / "history.jsonl",
        agent="claude",
        store="claude.history",
        adapter_id="claude.history_jsonl.v1",
    )
    source.path.write_text("first\n", encoding="utf-8")
    task = _plan(query, source, strategy="direct_full_scan").tasks[0]
    batch_reads = 0

    def iter_batches(
        _query: agentgrep.SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        control: agentgrep.SearchControl,
        progress: agentgrep.SearchProgress | None = None,
        batch_size: int = 32,
    ) -> cabc.Iterator[SourceScanBatch]:
        _ = control, progress, batch_size
        nonlocal batch_reads
        batch_reads += 1
        yield SourceScanBatch(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=(_record(task.source, "history bliss", "2026-01-02T00:00:00Z"),),
            records_seen=1,
            matches_seen=1,
            duration_seconds=0.0,
            is_final=True,
        )

    monkeypatch.setattr(scanning, "iter_source_task_batches", iter_batches)
    runtime = agentgrep.SearchRuntime.with_source_scan_cache()

    for index in (1, 2):
        scanning.scan_source_task(
            query,
            task,
            index=index,
            total=2,
            control=agentgrep.SearchControl(),
            runtime=runtime,
        )

    assert batch_reads == 2
    assert runtime.source_scan_cache is not None
    stats = runtime.source_scan_cache.stats()
    assert stats.stores == 0
    assert stats.hits == 0


class WalCacheCase(t.NamedTuple):
    """One SQLite WAL sidecar shape and its expected cache behavior."""

    test_id: str
    wal_before_first_scan: bool
    mutate_wal_between_scans: bool
    expected_second_hit: bool


WAL_CACHE_CASES: tuple[WalCacheCase, ...] = (
    WalCacheCase(
        test_id="wal-appears-invalidates",
        wal_before_first_scan=False,
        mutate_wal_between_scans=True,
        expected_second_hit=False,
    ),
    WalCacheCase(
        test_id="wal-grows-invalidates",
        wal_before_first_scan=True,
        mutate_wal_between_scans=True,
        expected_second_hit=False,
    ),
    WalCacheCase(
        test_id="wal-stable-still-hits",
        wal_before_first_scan=True,
        mutate_wal_between_scans=False,
        expected_second_hit=True,
    ),
    WalCacheCase(
        test_id="no-wal-still-hits",
        wal_before_first_scan=False,
        mutate_wal_between_scans=False,
        expected_second_hit=True,
    ),
)


@pytest.mark.parametrize(
    "case",
    WAL_CACHE_CASES,
    ids=[c.test_id for c in WAL_CACHE_CASES],
)
def test_source_scan_cache_fingerprints_sqlite_wal_sidecars(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: WalCacheCase,
) -> None:
    """WAL sidecar changes invalidate cached SQLite scans.

    Regression guard: WAL-mode commits land in the -wal sidecar while the
    main database file's size and mtime stay frozen until a checkpoint, so
    a fingerprint covering only the primary file kept serving pre-commit
    records to long-lived runtimes.
    """
    query = _query(limit=5)
    db_path = tmp_path / "state.vscdb"
    db_path.write_text("main-db", encoding="utf-8")
    wal_path = tmp_path / "state.vscdb-wal"
    if case.wal_before_first_scan:
        wal_path.write_text("wal-frame-a", encoding="utf-8")
    source = _source(
        db_path,
        agent="cursor-ide",
        store="cursor-ide.workspace_state",
        adapter_id="cursor_ide.state_vscdb_modern.v1",
        path_kind="sqlite_db",
        source_kind="sqlite",
    )
    task = _plan(query, source, strategy="direct_full_scan").tasks[0]
    scan_calls = 0

    def iter_batches(
        _query: agentgrep.SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        control: agentgrep.SearchControl,
        progress: agentgrep.SearchProgress | None = None,
        batch_size: int = 32,
    ) -> cabc.Iterator[SourceScanBatch]:
        _ = control, progress, batch_size
        nonlocal scan_calls
        scan_calls += 1
        yield SourceScanBatch(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=(_record(task.source, "sqlite bliss", "2026-01-02T00:00:00Z"),),
            records_seen=1,
            matches_seen=1,
            duration_seconds=0.0,
            is_final=True,
        )

    monkeypatch.setattr(scanning, "iter_source_task_batches", iter_batches)
    runtime = agentgrep.SearchRuntime.with_source_scan_cache()
    main_stat = db_path.stat()

    first = scanning.scan_source_task(
        query,
        task,
        index=1,
        total=1,
        control=agentgrep.SearchControl(),
        runtime=runtime,
    )
    if case.mutate_wal_between_scans:
        with wal_path.open("a", encoding="utf-8") as handle:
            handle.write("wal-frame-b")
        os.utime(db_path, ns=(main_stat.st_atime_ns, main_stat.st_mtime_ns))
    second = scanning.scan_source_task(
        query,
        task,
        index=1,
        total=1,
        control=agentgrep.SearchControl(),
        runtime=runtime,
    )

    assert first.cache_hit is False
    assert second.cache_hit is case.expected_second_hit
    assert scan_calls == (1 if case.expected_second_hit else 2)


def test_source_scan_cache_never_serves_stale_paste_expansions(
    tmp_path: pathlib.Path,
) -> None:
    """Paste-cache changes are visible even when history.jsonl is untouched.

    Regression guard: the cache fingerprint stats only the primary file,
    so a cached Claude history scan kept serving the pre-expansion text
    after a referenced paste-cache file appeared.
    """
    paste_hash = "0123456789abcdef"
    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        json.dumps(
            {
                "display": "Review [Pasted text #1]",
                "pastedContents": {
                    "1": {"id": 1, "type": "text", "contentHash": paste_hash},
                },
                "timestamp": 1_700_000_000_000,
                "project": "/synthetic/project",
                "sessionId": "session-1",
            },
        )
        + "\n",
        encoding="utf-8",
    )
    source = _source(
        history_path,
        agent="claude",
        store="claude.history",
        adapter_id="claude.history_jsonl.v1",
    )
    query = agentgrep.SearchQuery(
        terms=("review",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("claude",),
        limit=None,
    )
    task = _plan(query, source, strategy="direct_full_scan").tasks[0]
    runtime = agentgrep.SearchRuntime.with_source_scan_cache()
    stat_before = history_path.stat()

    first = scanning.scan_source_task(
        query,
        task,
        index=1,
        total=1,
        control=agentgrep.SearchControl(),
        runtime=runtime,
    )
    paste_dir = tmp_path / "paste-cache"
    paste_dir.mkdir()
    (paste_dir / f"{paste_hash}.txt").write_text(
        "expanded serenity content",
        encoding="utf-8",
    )
    os.utime(history_path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    second = scanning.scan_source_task(
        query,
        task,
        index=1,
        total=1,
        control=agentgrep.SearchControl(),
        runtime=runtime,
    )

    assert "[Pasted text #1]" in first.records[0].text
    assert "expanded serenity content" in second.records[0].text


def test_source_scan_cache_evicts_least_recently_used_entry(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded runtime cache evicts the least-recently used source scan."""
    query = _query(limit=2)
    cache = agentgrep.SourceScanCache(max_entries=1)
    runtime = agentgrep.SearchRuntime(source_scan_cache=cache)
    sources = [
        _source(tmp_path / "one.jsonl"),
        _source(tmp_path / "two.jsonl"),
    ]
    for source in sources:
        source.path.write_text("source\n", encoding="utf-8")
    tasks = [
        _plan(query, source, strategy="jsonl_bounded_reverse_scan").tasks[0] for source in sources
    ]
    batch_reads = 0

    def iter_batches(
        _query: agentgrep.SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        control: agentgrep.SearchControl,
        progress: agentgrep.SearchProgress | None = None,
        batch_size: int = 32,
    ) -> cabc.Iterator[SourceScanBatch]:
        nonlocal batch_reads
        assert progress is None
        assert batch_size == 32
        assert not control.answer_now_requested()
        batch_reads += 1
        yield SourceScanBatch(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=(_record(task.source, "newest bliss", "2026-01-02T00:00:00Z"),),
            records_seen=1,
            matches_seen=1,
            duration_seconds=0.0,
            is_final=True,
        )

    monkeypatch.setattr(scanning, "iter_source_task_batches", iter_batches)

    for index, task in enumerate((*tasks, tasks[0]), start=1):
        _ = scanning.scan_source_task(
            query,
            task,
            index=index,
            total=3,
            control=agentgrep.SearchControl(),
            runtime=runtime,
        )

    stats = cache.stats()
    assert batch_reads == 3
    assert stats.entries == 1
    assert stats.hits == 0
    assert stats.misses == 3
    assert stats.evictions == 2


def test_source_batches_can_yield_partial_results_before_source_finishes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch scans expose records to the scheduler before the source is drained."""
    query = _query(limit=3)
    source = _source(tmp_path / "session.jsonl")
    records = (
        _record(source, "first bliss", "2026-01-03T00:00:00Z"),
        _record(source, "second bliss", "2026-01-02T00:00:00Z"),
        _record(source, "third bliss", "2026-01-01T00:00:00Z"),
    )

    def iter_records(
        _task: SourceTask,
        _query: agentgrep.SearchQuery,
    ) -> cabc.Iterator[agentgrep.SearchRecord]:
        yield from records

    monkeypatch.setattr(scanning, "iter_source_task_records", iter_records)
    task = _plan(query, source, strategy="jsonl_bounded_reverse_scan").tasks[0]

    batches = tuple(
        scanning.iter_source_task_batches(
            query,
            task,
            index=1,
            total=1,
            control=agentgrep.SearchControl(),
            batch_size=1,
        ),
    )

    assert [len(batch.records) for batch in batches] == [1, 1, 1]
    assert [batch.records_seen for batch in batches] == [1, 2, 3]
    assert batches[-1].is_final is True


@pytest.mark.parametrize(
    "case",
    (
        BatchSchedulerCase(
            test_id="single-worker-skips-after-first-batch",
            max_workers=1,
            expected_emitted=("newest bliss",),
            expected_skipped=1,
        ),
    ),
    ids=lambda case: case.test_id,
)
def test_frontier_driver_merges_source_batches_before_source_finishes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: BatchSchedulerCase,
) -> None:
    """The scheduler can satisfy the frontier from a partial source batch."""
    query = _query(limit=1)
    newest = _source(tmp_path / "newest.jsonl")
    older = _source(tmp_path / "older.jsonl")
    newest.mtime_ns = 2
    older.mtime_ns = 1
    scanned_paths: list[pathlib.Path] = []

    def iter_batches(
        _query: agentgrep.SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        control: agentgrep.SearchControl,
        progress: agentgrep.SearchProgress | None = None,
        batch_size: int = 32,
    ) -> cabc.Iterator[SourceScanBatch]:
        assert progress is None
        assert batch_size == 32
        scanned_paths.append(task.source.path)
        if task.source == older:
            pytest.fail("older source should be skipped after newest partial batch")
        yield SourceScanBatch(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=(_record(task.source, "newest bliss", "2026-01-02T00:00:00Z"),),
            records_seen=1,
            matches_seen=1,
            duration_seconds=0.01,
            is_final=False,
        )
        assert not control.answer_now_requested()
        yield SourceScanBatch(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=(),
            records_seen=2,
            matches_seen=1,
            duration_seconds=0.02,
            is_final=True,
        )

    monkeypatch.setattr(scanning, "iter_source_task_batches", iter_batches)
    profiler = EngineProfiler()

    with use_engine_profiler(profiler):
        events = list(
            scheduling.FrontierExecutionDriver(
                ExecutionDriverConfig(
                    max_workers=case.max_workers,
                    use_source_batches=True,
                ),
            ).iter_search_plan(
                query,
                _multi_plan(query, (newest, older)),
            ),
        )

    assert scanned_paths == [newest.path]
    assert (
        tuple(event.record.text for event in events if isinstance(event, ExecutionRecordEmitted))
        == case.expected_emitted
    )
    scheduler_sample = next(
        sample
        for sample in profiler.snapshot().samples
        if sample.name == "search.collect.scheduler"
    )
    assert scheduler_sample.attributes["agentgrep_skipped_source_count"] == case.expected_skipped
    assert scheduler_sample.attributes["agentgrep_batch_count"] == 2
    assert scheduler_sample.attributes["agentgrep_queued_batch_count"] == 2


def test_frontier_batch_path_raises_deferred_error_without_deadlock(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed source scan surfaces its error instead of hanging the drain loop.

    Regression guard: a failed worker never sends a completion item, so the
    scheduler must drop the failed task from its running set or the drain
    loop polls an empty queue forever. The consumer runs on a daemon thread
    with a join timeout so a regression fails fast instead of hanging the
    suite.
    """
    query = _query(limit=5)
    failing = _source(tmp_path / "failing.jsonl")
    healthy = _source(tmp_path / "healthy.jsonl")
    failing.mtime_ns = 2
    healthy.mtime_ns = 1

    def iter_batches(
        _query: agentgrep.SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        control: agentgrep.SearchControl,
        progress: agentgrep.SearchProgress | None = None,
        batch_size: int = 32,
    ) -> cabc.Iterator[SourceScanBatch]:
        _ = control, progress, batch_size
        if task.source == failing:
            msg = "boom"
            raise RuntimeError(msg)
        yield SourceScanBatch(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=(_record(task.source, "healthy bliss", "2026-01-02T00:00:00Z"),),
            records_seen=1,
            matches_seen=1,
            duration_seconds=0.01,
            is_final=True,
        )

    monkeypatch.setattr(scanning, "iter_source_task_batches", iter_batches)
    driver = scheduling.FrontierExecutionDriver(
        ExecutionDriverConfig(max_workers=2, use_source_batches=True),
    )
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            list(
                driver.iter_search_plan(
                    query,
                    _multi_plan(query, (failing, healthy)),
                ),
            )
        except RuntimeError as exc:
            errors.append(exc)

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    thread.join(timeout=10.0)

    assert not thread.is_alive(), "frontier batch scheduler deadlocked on a failed source"
    assert [str(error) for error in errors] == ["boom"]


def test_frontier_batch_path_releases_cancelled_tasks(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard-cancelled queued tasks leave the running set with a finished event.

    Regression guard: a queued future whose ``cancel()`` succeeds never runs
    its worker, so it never posts a completion item. Without releasing it,
    the drain loop polls an empty queue forever and the started/finished
    event pairing breaks. Both worker threads are held on an event so the
    third task stays queued (cancellable) when cancellation fires.
    """
    query = _query(limit=5)
    newest = _source(tmp_path / "newest.jsonl")
    middle = _source(tmp_path / "middle.jsonl")
    oldest = _source(tmp_path / "oldest.jsonl")
    newest.mtime_ns = 3
    middle.mtime_ns = 2
    oldest.mtime_ns = 1
    scanned_indexes: list[int] = []

    def iter_batches(
        _query: agentgrep.SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        control: agentgrep.SearchControl,
        progress: agentgrep.SearchProgress | None = None,
        batch_size: int = 32,
    ) -> cabc.Iterator[SourceScanBatch]:
        _ = control, progress, batch_size
        scanned_indexes.append(index)
        yield SourceScanBatch(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=(
                _record(task.source, f"{task.source.path.stem} bliss", "2026-01-02T00:00:00Z"),
            ),
            records_seen=1,
            matches_seen=1,
            duration_seconds=0.0,
            is_final=True,
        )

    monkeypatch.setattr(scanning, "iter_source_task_batches", iter_batches)

    hold_workers = threading.Event()
    original_scan_to_queue = scheduling._scan_source_task_to_queue

    def holding_scan_to_queue(
        query: agentgrep.SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        control: agentgrep.SearchControl,
        batch_queue: queue.Queue[t.Any],
        progress: agentgrep.SearchProgress | None = None,
    ) -> None:
        original_scan_to_queue(
            query,
            task,
            index=index,
            total=total,
            control=control,
            batch_queue=batch_queue,
            progress=progress,
        )
        if index in (1, 2):
            hold_workers.wait(timeout=10.0)

    monkeypatch.setattr(scheduling, "_scan_source_task_to_queue", holding_scan_to_queue)

    control = agentgrep.SearchControl()
    driver = scheduling.FrontierExecutionDriver(
        ExecutionDriverConfig(max_workers=2, use_source_batches=True),
    )
    plan = _multi_plan(query, (newest, middle, oldest))
    events: list[scheduling.SearchExecutionEvent] = []
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            # extend() appends incrementally as the generator yields, so the
            # main thread can poll `events` for the third Started event.
            events.extend(driver.iter_search_plan(query, plan, control=control))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            started_indexes = {
                event.index for event in tuple(events) if isinstance(event, ExecutionSourceStarted)
            }
            if 3 in started_indexes:
                break
            time.sleep(0.01)
        else:
            pytest.fail("third source task was never submitted")
        control.request_answer_now()
        time.sleep(0.3)
    finally:
        hold_workers.set()
    thread.join(timeout=10.0)

    assert not thread.is_alive(), "batch scheduler hung on a cancelled queued task"
    assert errors == []
    assert 3 not in scanned_indexes, "cancelled task ran; choreography lost the race"
    started = {e.index for e in events if isinstance(e, ExecutionSourceStarted)}
    finished = {e.index for e in events if isinstance(e, ExecutionSourceFinished)}
    assert started == finished == {1, 2, 3}


def test_whole_sources_cancellation_pairs_source_events(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Early answer-now exits still emit finished events for started sources.

    Regression guard: the whole-source scheduler cancelled remaining futures
    and broke out of its loop, leaving in-flight sources with a started
    event but no finished event.
    """
    query = _query(limit=5)
    newest = _source(tmp_path / "newest.jsonl")
    older = _source(tmp_path / "older.jsonl")
    newest.mtime_ns = 2
    older.mtime_ns = 1

    def iter_batches(
        _query: agentgrep.SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        control: agentgrep.SearchControl,
        progress: agentgrep.SearchProgress | None = None,
        batch_size: int = 32,
    ) -> cabc.Iterator[SourceScanBatch]:
        _ = progress, batch_size
        yield SourceScanBatch(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=(
                _record(task.source, f"{task.source.path.stem} bliss", "2026-01-02T00:00:00Z"),
            ),
            records_seen=1,
            matches_seen=1,
            duration_seconds=0.0,
            is_final=True,
        )
        if task.source == newest:
            control.request_answer_now()

    monkeypatch.setattr(scanning, "iter_source_task_batches", iter_batches)
    control = agentgrep.SearchControl()
    driver = scheduling.FrontierExecutionDriver(ExecutionDriverConfig(max_workers=1))

    events = list(
        driver.iter_search_plan(
            query,
            _multi_plan(query, (newest, older)),
            control=control,
        ),
    )

    started = {e.index for e in events if isinstance(e, ExecutionSourceStarted)}
    finished = {e.index for e in events if isinstance(e, ExecutionSourceFinished)}
    assert started
    assert started == finished


def test_whole_sources_cancellation_preserves_latest_source_progress(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation never reports fewer records than the latest heartbeat."""
    query = _query(limit=1)
    source = _source(tmp_path / "session.jsonl")
    control = agentgrep.SearchControl()
    release_worker = threading.Event()

    def iter_records(
        task: SourceTask,
        _query: agentgrep.SearchQuery,
    ) -> cabc.Iterator[agentgrep.SearchRecord]:
        for index in range(agentgrep._SOURCE_PROGRESS_RECORD_INTERVAL):
            yield _record(
                task.source,
                f"other {index}",
                "2026-01-01T00:00:00Z",
            )
        assert release_worker.wait(timeout=5.0), "owner never forwarded the heartbeat"
        yield _record(task.source, "other final", "2026-01-01T00:00:00Z")

    class CancellingProgress(agentgrep.NoopSearchProgress):
        def __init__(self) -> None:
            self.events: list[tuple[str, int, int]] = []

        def source_progress(
            self,
            index: int,
            total: int,
            source: agentgrep.SourceHandle,
            records: int,
            matches: int,
        ) -> None:
            _ = index, total, source
            self.events.append(("progress", records, matches))
            control.request_answer_now()
            release_worker.set()

        def source_finished(
            self,
            index: int,
            total: int,
            source: agentgrep.SourceHandle,
            records: int,
            matches: int,
        ) -> None:
            _ = index, total, source
            self.events.append(("finished", records, matches))

    monkeypatch.setattr(scanning, "iter_source_task_records", iter_records)
    progress = CancellingProgress()
    driver = scheduling.FrontierExecutionDriver(ExecutionDriverConfig(max_workers=1))

    events = list(
        driver.iter_search_plan(
            query,
            _multi_plan(query, (source,)),
            progress=progress,
            control=control,
        ),
    )

    expected_counters = (agentgrep._SOURCE_PROGRESS_RECORD_INTERVAL, 0)
    assert progress.events == [
        ("progress", *expected_counters),
        ("finished", *expected_counters),
    ]
    assert [
        (event.records_seen, event.matches_seen)
        for event in events
        if isinstance(event, ExecutionSourceFinished)
    ] == [expected_counters]


class BatchCacheCase(t.NamedTuple):
    """One batch-scheduling worker shape exercising the source scan cache."""

    test_id: str
    max_workers: int


BATCH_CACHE_CASES: tuple[BatchCacheCase, ...] = (
    BatchCacheCase(test_id="single-worker-batch-path", max_workers=1),
    BatchCacheCase(test_id="multi-worker-batch-path", max_workers=2),
)


@pytest.mark.parametrize(
    "case",
    BATCH_CACHE_CASES,
    ids=[c.test_id for c in BATCH_CACHE_CASES],
)
def test_frontier_batch_path_reuses_runtime_source_scan_cache(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: BatchCacheCase,
) -> None:
    """Batch scheduling consults the runtime source scan cache like whole-source scans."""
    query = _query(limit=5)
    newest = _source(tmp_path / "newest.jsonl")
    older = _source(tmp_path / "older.jsonl")
    newest.mtime_ns = 2
    older.mtime_ns = 1
    newest.path.write_text("newest\n", encoding="utf-8")
    older.path.write_text("older\n", encoding="utf-8")
    scan_calls = 0

    def iter_batches(
        _query: agentgrep.SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        control: agentgrep.SearchControl,
        progress: agentgrep.SearchProgress | None = None,
        batch_size: int = 32,
    ) -> cabc.Iterator[SourceScanBatch]:
        _ = control, progress, batch_size
        nonlocal scan_calls
        scan_calls += 1
        yield SourceScanBatch(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=(
                _record(task.source, f"{task.source.path.stem} bliss", "2026-01-02T00:00:00Z"),
            ),
            records_seen=1,
            matches_seen=1,
            duration_seconds=0.0,
            is_final=True,
        )

    monkeypatch.setattr(scanning, "iter_source_task_batches", iter_batches)
    runtime = agentgrep.SearchRuntime.with_source_scan_cache()
    driver = scheduling.FrontierExecutionDriver(
        ExecutionDriverConfig(max_workers=case.max_workers, use_source_batches=True),
    )
    plan = _multi_plan(query, (newest, older))

    def emitted_texts() -> list[str]:
        return [
            event.record.text
            for event in driver.iter_search_plan(query, plan, runtime=runtime)
            if isinstance(event, ExecutionRecordEmitted)
        ]

    first = emitted_texts()
    second = emitted_texts()

    assert sorted(first) == sorted(second) == ["newest bliss", "older bliss"]
    assert scan_calls == 2, "second run should be served entirely from the cache"
    assert runtime.source_scan_cache is not None
    stats = runtime.source_scan_cache.stats()
    assert stats.stores == 2
    assert stats.hits == 2


def test_frontier_batch_path_skips_cache_store_on_cancellation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled batch scans are partial and must not populate the cache."""
    query = _query(limit=5)
    source = _source(tmp_path / "session.jsonl")
    source.path.write_text("first\n", encoding="utf-8")

    def iter_batches(
        _query: agentgrep.SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        control: agentgrep.SearchControl,
        progress: agentgrep.SearchProgress | None = None,
        batch_size: int = 32,
    ) -> cabc.Iterator[SourceScanBatch]:
        _ = progress, batch_size
        yield SourceScanBatch(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=(_record(task.source, "partial bliss", "2026-01-02T00:00:00Z"),),
            records_seen=1,
            matches_seen=1,
            duration_seconds=0.0,
            is_final=False,
        )
        control.request_answer_now()

    monkeypatch.setattr(scanning, "iter_source_task_batches", iter_batches)
    runtime = agentgrep.SearchRuntime.with_source_scan_cache()
    driver = scheduling.FrontierExecutionDriver(
        ExecutionDriverConfig(max_workers=1, use_source_batches=True),
    )

    events = list(
        driver.iter_search_plan(
            query,
            _multi_plan(query, (source,)),
            runtime=runtime,
        ),
    )

    assert any(isinstance(event, ExecutionRecordEmitted) for event in events)
    assert runtime.source_scan_cache is not None
    assert runtime.source_scan_cache.stats().stores == 0


def test_select_execution_driver_uses_frontier_for_bounded_haystack_search(
    tmp_path: pathlib.Path,
) -> None:
    """Bounded haystack searches use the frontier scheduler path."""
    query = _query(limit=2)
    plan = _multi_plan(
        query,
        (
            _source(tmp_path / "a.jsonl"),
            _source(tmp_path / "b.jsonl"),
        ),
    )

    driver = execution.select_execution_driver(query, plan)

    assert isinstance(driver, FrontierExecutionDriver)


@pytest.mark.parametrize(
    "config",
    (
        pytest.param(None, id="whole-source-worker"),
        pytest.param(
            ExecutionDriverConfig(max_workers=2),
            id="concurrent-whole-source-workers",
        ),
        pytest.param(
            ExecutionDriverConfig(max_workers=1, use_source_batches=True),
            id="single-owner-thread-batches",
        ),
        pytest.param(
            ExecutionDriverConfig(max_workers=2, use_source_batches=True),
            id="concurrent-worker-batches",
        ),
    ),
)
def test_selected_frontier_driver_forwards_source_progress_on_owner_thread(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    config: ExecutionDriverConfig | None,
) -> None:
    """Limited frontier scans serialize parsed-record heartbeats on their owner."""
    query = _query(limit=1)
    sources = (
        _source(tmp_path / "newest.jsonl"),
        _source(tmp_path / "older.jsonl"),
    )
    sources[0].mtime_ns = 2
    sources[1].mtime_ns = 1
    plan = _multi_plan(query, sources)
    owner_thread_id = threading.get_ident()

    def iter_records(
        task: SourceTask,
        _query: agentgrep.SearchQuery,
    ) -> cabc.Iterator[agentgrep.SearchRecord]:
        for index in range(agentgrep._SOURCE_PROGRESS_RECORD_INTERVAL + 1):
            yield _record(
                task.source,
                f"other {index}",
                "2026-01-01T00:00:00Z",
            )

    class CapturingProgress(agentgrep.NoopSearchProgress):
        def __init__(self) -> None:
            self.events: list[tuple[int, int, int, int, int]] = []

        def source_progress(
            self,
            index: int,
            total: int,
            source: agentgrep.SourceHandle,
            records: int,
            matches: int,
        ) -> None:
            _ = source
            self.events.append(
                (index, total, records, matches, threading.get_ident()),
            )

    monkeypatch.setattr(scanning, "iter_source_task_records", iter_records)
    progress = CapturingProgress()
    driver = execution.select_execution_driver(query, plan, config=config)

    assert isinstance(driver, FrontierExecutionDriver)

    _ = list(driver.iter_search_plan(query, plan, progress=progress))

    assert sorted(progress.events) == [
        (1, 2, agentgrep._SOURCE_PROGRESS_RECORD_INTERVAL, 0, owner_thread_id),
        (2, 2, agentgrep._SOURCE_PROGRESS_RECORD_INTERVAL, 0, owner_thread_id),
    ]


def test_select_execution_driver_keeps_bounded_text_search_inline_by_default(
    tmp_path: pathlib.Path,
) -> None:
    """Bounded text searches avoid scheduler overhead without configured concurrency."""
    query = _query(limit=2, match_surface="text")
    plan = _multi_plan(
        query,
        (
            _source(tmp_path / "a.jsonl"),
            _source(tmp_path / "b.jsonl"),
        ),
    )

    driver = execution.select_execution_driver(query, plan)

    assert isinstance(driver, InlineExecutionDriver)


def test_select_execution_driver_can_schedule_bounded_text_search_with_workers(
    tmp_path: pathlib.Path,
) -> None:
    """Bounded raw-text JSONL searches can opt into source-level scheduling."""
    query = _query(limit=2, match_surface="text")
    plan = _multi_plan(
        query,
        (
            _source(tmp_path / "a.jsonl"),
            _source(tmp_path / "b.jsonl"),
        ),
    )

    driver = execution.select_execution_driver(
        query,
        plan,
        config=ExecutionDriverConfig(max_workers=2),
    )

    assert isinstance(driver, FrontierExecutionDriver)


class CodexAuthorityDriverCase(t.NamedTuple):
    """One limit shape and its authority-aware execution driver."""

    test_id: str
    limit: int | None
    expected_driver: type[InlineExecutionDriver] | type[FrontierExecutionDriver]


CODEX_AUTHORITY_DRIVER_CASES: tuple[CodexAuthorityDriverCase, ...] = (
    CodexAuthorityDriverCase(
        test_id="unlimited-keeps-streaming-inline",
        limit=None,
        expected_driver=InlineExecutionDriver,
    ),
    CodexAuthorityDriverCase(
        test_id="finite-uses-resolving-frontier",
        limit=2,
        expected_driver=FrontierExecutionDriver,
    ),
)


@pytest.mark.parametrize(
    CodexAuthorityDriverCase._fields,
    [pytest.param(*case, id=case.test_id) for case in CODEX_AUTHORITY_DRIVER_CASES],
)
def test_select_execution_driver_limits_codex_authority_buffering(
    test_id: str,
    limit: int | None,
    expected_driver: type[InlineExecutionDriver] | type[FrontierExecutionDriver],
    tmp_path: pathlib.Path,
) -> None:
    """Only finite authority plans need the fully buffered frontier."""
    _ = test_id
    query = _query(limit=limit)
    plan = _multi_plan(
        query,
        (
            _source(tmp_path / "rollout.jsonl"),
            _source(
                tmp_path / "state_5.sqlite",
                store="codex.state_db",
                adapter_id="codex.state_sqlite.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
            ),
        ),
    )

    driver = execution.select_execution_driver(query, plan)

    assert isinstance(driver, expected_driver)


def test_limit_policy_records_source_order_frontier_satisfaction() -> None:
    """Limit policy makes the current source-order stop rule explicit."""
    query = _query(limit=1)
    source = _source(pathlib.Path("/tmp/session.jsonl"))
    frontier = scheduling._FrontierState(query)

    frontier.add_records((_record(source, "bliss", "2026-01-01T00:00:00Z"),))

    assert LimitPolicy().can_skip_remaining(query=query, frontier=frontier) is True


def test_frontier_execution_driver_scans_sources_concurrently(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frontier driver runs independent source tasks concurrently."""
    query = _query(limit=2)
    source_a = _source(tmp_path / "a.jsonl")
    source_b = _source(tmp_path / "b.jsonl")
    plan = _multi_plan(query, (source_a, source_b))
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = 0
    max_active = 0

    def iter_batches(
        _query: agentgrep.SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        control: agentgrep.SearchControl,
        progress: agentgrep.SearchProgress | None = None,
        batch_size: int = 32,
    ) -> cabc.Iterator[SourceScanBatch]:
        nonlocal active, max_active
        assert progress is None
        assert batch_size == 32
        assert not control.answer_now_requested()
        with lock:
            active += 1
            max_active = max(max_active, active)
        barrier.wait(timeout=2.0)
        yield SourceScanBatch(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=(_record(task.source, "bliss", f"2026-01-0{index}T00:00:00Z"),),
            records_seen=1,
            matches_seen=1,
            duration_seconds=0.01,
            is_final=True,
        )
        with lock:
            active -= 1

    monkeypatch.setattr(scanning, "iter_source_task_batches", iter_batches)

    events = list(
        FrontierExecutionDriver(ExecutionDriverConfig(max_workers=2)).iter_search_plan(
            query,
            plan,
        ),
    )

    assert max_active == 2
    assert len([event for event in events if isinstance(event, ExecutionRecordEmitted)]) == 2


def test_frontier_execution_driver_profiles_scheduler_decisions(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduler profiling reports submitted and skipped source counts."""
    query = _query(limit=1)
    newest = _source(tmp_path / "newest.jsonl")
    older = _source(tmp_path / "older.jsonl")
    newest.mtime_ns = 2
    older.mtime_ns = 1
    scanned_paths: list[pathlib.Path] = []

    def iter_records(
        task: SourceTask,
        _query: agentgrep.SearchQuery,
    ) -> cabc.Iterator[agentgrep.SearchRecord]:
        scanned_paths.append(task.source.path)
        if task.source == newest:
            yield _record(newest, "newest bliss", "2026-01-02T00:00:00Z")
        else:
            pytest.fail("frontier should skip the older source after limit is satisfied")

    monkeypatch.setattr(scanning, "iter_source_task_records", iter_records)
    profiler = EngineProfiler()

    with use_engine_profiler(profiler):
        events = list(
            FrontierExecutionDriver(ExecutionDriverConfig(max_workers=1)).iter_search_plan(
                query,
                _multi_plan(query, (newest, older)),
            ),
        )

    assert scanned_paths == [newest.path]
    assert [event.record.text for event in events if isinstance(event, ExecutionRecordEmitted)] == [
        "newest bliss",
    ]
    scheduler_samples = [
        sample
        for sample in profiler.snapshot().samples
        if sample.name == "search.collect.scheduler"
    ]
    assert len(scheduler_samples) == 1
    assert scheduler_samples[0].attributes["agentgrep_submitted_source_count"] == 1
    assert scheduler_samples[0].attributes["agentgrep_skipped_source_count"] == 1
    assert scheduler_samples[0].attributes["agentgrep_batch_count"] == 1
    assert scheduler_samples[0].attributes["agentgrep_cancelled_source_count"] == 0


def test_bounded_codex_history_jsonl_does_not_prefetch_older_matches(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded Codex history scans stream JSONL instead of materializing it."""
    query = _query(limit=1, match_surface="text")
    source = _source(
        tmp_path / "history.jsonl",
        store="codex.history",
        adapter_id="codex.history_jsonl.v1",
    )
    source.path.write_text(
        "\n".join(
            (
                '{"timestamp":"2026-01-01T00:00:00Z","text":"old bliss"}',
                '{"timestamp":"2026-01-02T00:00:00Z","text":"newer bliss"}',
                '{"timestamp":"2026-01-03T00:00:00Z","text":"latest miss"}',
            ),
        ),
        encoding="utf-8",
    )
    decoded_inputs: list[str] = []
    original_loads = agentgrep._loads

    def loads_with_capture(payload: str) -> object:
        decoded_inputs.append(payload)
        return original_loads(payload)

    monkeypatch.setattr(agentgrep.readers, "_loads", loads_with_capture)

    events = list(
        InlineExecutionDriver().iter_search_plan(
            query,
            _plan(
                query,
                source,
                strategy="jsonl_bounded_reverse_raw_text_prefilter",
            ),
        ),
    )

    assert [event.record.text for event in events if isinstance(event, ExecutionRecordEmitted)] == [
        "newer bliss",
    ]
    assert not any("old bliss" in payload for payload in decoded_inputs)
    finished = [event for event in events if isinstance(event, ExecutionSourceFinished)]
    assert len(finished) == 1
    assert finished[0].records_seen == 1
