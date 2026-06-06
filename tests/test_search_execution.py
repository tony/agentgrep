"""Tests for search physical-plan execution."""

from __future__ import annotations

import collections.abc as cabc
import pathlib
import typing as t

import pytest

import agentgrep
import agentgrep._engine.execution as execution
from agentgrep._engine.execution import (
    ExecutionRecordEmitted,
    ExecutionSourceFinished,
    ExecutionSourceStarted,
    InlineExecutionDriver,
)
from agentgrep._engine.planning import (
    PhysicalSearchPlan,
    SourceStrategy,
    SourceTask,
    build_logical_search_plan,
)


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
    store: str = "codex.sessions",
    adapter_id: str = "codex.sessions_jsonl.v1",
) -> agentgrep.SourceHandle:
    """Build a synthetic source handle for execution tests."""
    return agentgrep.SourceHandle(
        agent="codex",
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
                    }
                    else "unknown"
                ),
                limit_behavior=(
                    "bounded_source"
                    if strategy
                    in {
                        "jsonl_bounded_reverse_scan",
                        "jsonl_bounded_reverse_raw_text_prefilter",
                    }
                    else "drain_source"
                ),
                can_stream_records=True,
                restore_order_key=(0, str(source.path)),
            ),
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
