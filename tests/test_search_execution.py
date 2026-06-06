"""Tests for search physical-plan execution."""

from __future__ import annotations

import collections.abc as cabc
import pathlib
import threading
import typing as t

import pytest

import agentgrep
import agentgrep._engine.execution as execution
from agentgrep._engine.execution import (
    ExecutionDriverConfig,
    ExecutionRecordEmitted,
    ExecutionSourceFinished,
    ExecutionSourceStarted,
    FrontierExecutionDriver,
    InlineExecutionDriver,
    SourceScanBatch,
    SourceScanResult,
)
from agentgrep._engine.planning import (
    LimitPolicy,
    PhysicalSearchPlan,
    SourceStrategy,
    SourceTask,
    build_logical_search_plan,
)
from agentgrep._engine.profiling import EngineProfiler, use_engine_profiler


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
) -> agentgrep.SourceHandle:
    """Build a synthetic source handle for execution tests."""
    return agentgrep.SourceHandle(
        agent=agent,
        store=store,
        adapter_id=adapter_id,
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
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

    monkeypatch.setattr(agentgrep, "iter_source_records", iter_records)

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
    original_loads = agentgrep.json.loads

    def loads_with_capture(payload: str) -> object:
        decoded_inputs.append(payload)
        return t.cast("object", original_loads(payload))

    monkeypatch.setattr(agentgrep.json, "loads", loads_with_capture)

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

    monkeypatch.setattr(agentgrep, "matches_text", fail_matches_text)
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
    original_loads = agentgrep.json.loads

    def loads_with_capture(payload: str) -> object:
        decoded_inputs.append(payload)
        return t.cast("object", original_loads(payload))

    monkeypatch.setattr(agentgrep.json, "loads", loads_with_capture)

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

    monkeypatch.setattr(execution, "iter_source_task_records", iter_records)

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

    monkeypatch.setattr(execution, "iter_source_task_records", iter_records)
    task = _plan(query, source, strategy="jsonl_bounded_reverse_scan").tasks[0]

    result = execution.scan_source_task(
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

    monkeypatch.setattr(execution, "iter_source_task_records", iter_records)
    task = _plan(query, source, strategy="jsonl_bounded_reverse_scan").tasks[0]

    batches = tuple(
        execution.iter_source_task_batches(
            query,
            task,
            index=1,
            total=1,
            control=agentgrep.SearchControl(),
        ),
    )
    result = execution.scan_source_task(
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

    monkeypatch.setattr(execution, "iter_source_task_records", iter_records)
    task = _plan(query, source, strategy="jsonl_bounded_reverse_scan").tasks[0]

    batches = tuple(
        execution.iter_source_task_batches(
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


def test_limit_policy_records_source_order_frontier_satisfaction() -> None:
    """Limit policy makes the current source-order stop rule explicit."""
    query = _query(limit=1)
    source = _source(pathlib.Path("/tmp/session.jsonl"))
    frontier = execution._FrontierState(query)

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

    def scan_source_task(
        _query: agentgrep.SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        control: agentgrep.SearchControl,
        progress: agentgrep.SearchProgress | None = None,
    ) -> SourceScanResult:
        nonlocal active, max_active
        assert progress is None
        assert not control.answer_now_requested()
        with lock:
            active += 1
            max_active = max(max_active, active)
        barrier.wait(timeout=2.0)
        with lock:
            active -= 1
        return SourceScanResult(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=(_record(task.source, "bliss", f"2026-01-0{index}T00:00:00Z"),),
            records_seen=1,
            matches_seen=1,
            duration_seconds=0.01,
        )

    monkeypatch.setattr(execution, "scan_source_task", scan_source_task)

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

    monkeypatch.setattr(execution, "iter_source_task_records", iter_records)
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
    original_loads = agentgrep.json.loads

    def loads_with_capture(payload: str) -> object:
        decoded_inputs.append(payload)
        return t.cast("object", original_loads(payload))

    monkeypatch.setattr(agentgrep.json, "loads", loads_with_capture)

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
