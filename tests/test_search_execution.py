"""Tests for search physical-plan execution."""

from __future__ import annotations

import pathlib

import pytest

import agentgrep
from agentgrep._engine.execution import (
    ExecutionRecordEmitted,
    ExecutionSourceFinished,
    ExecutionSourceStarted,
    InlineExecutionDriver,
)
from agentgrep._engine.planning import (
    PhysicalSearchPlan,
    SourceTask,
    build_logical_search_plan,
)


def _query(*, limit: int | None = None) -> agentgrep.SearchQuery:
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
    )


def _source(path: pathlib.Path) -> agentgrep.SourceHandle:
    """Build a synthetic source handle for execution tests."""
    return agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
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


def _plan(query: agentgrep.SearchQuery, source: agentgrep.SourceHandle) -> PhysicalSearchPlan:
    """Build a one-source physical plan for execution tests."""
    return PhysicalSearchPlan(
        logical=build_logical_search_plan(query),
        tasks=(
            SourceTask(
                source=source,
                strategy="direct_full_scan",
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
