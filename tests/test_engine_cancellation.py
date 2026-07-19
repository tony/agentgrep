"""Cancellation contracts for source scans."""

from __future__ import annotations

import collections.abc as cabc
import concurrent.futures
import pathlib
import threading

import pytest

import agentgrep._engine.scanning as scanning
from agentgrep._engine.planning import SourceTask
from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery, SearchRecord, SourceHandle


def _source_task() -> SourceTask:
    """Build one synthetic streaming source task."""
    source = SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("cancel-session.jsonl"),
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=0,
    )
    return SourceTask(
        source=source,
        strategy="direct_full_scan",
        record_order="unknown",
        limit_behavior="drain_source",
        can_stream_records=True,
        restore_order_key=(0, str(source.path)),
    )


def _record(source: SourceHandle, index: int) -> SearchRecord:
    """Build one matching record for the synthetic source."""
    return SearchRecord(
        kind="prompt",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=f"matching record {index}",
        session_id=source.path.stem,
    )


def test_scan_source_task_drops_record_pulled_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Answer-now drops a record pulled after cancellation was requested.

    The scanner seam is patched because this contract requires precise control
    of an in-progress ``next()`` call rather than filesystem-backed parsing.
    """
    task = _source_task()
    query = SearchQuery(
        terms=("matching",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    records = tuple(_record(task.source, index) for index in range(1, 4))
    second_next_active = threading.Event()
    release_iterator = threading.Event()

    def iter_records(
        active_task: SourceTask,
        active_query: SearchQuery,
    ) -> cabc.Iterator[SearchRecord]:
        assert active_task is task
        assert active_query is query
        yield records[0]
        second_next_active.set()
        assert release_iterator.wait(timeout=5.0), "test thread did not release iterator"
        yield from records[1:]

    monkeypatch.setattr(scanning, "iter_source_task_records", iter_records)
    control = SearchControl()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            scanning.scan_source_task,
            query,
            task,
            index=1,
            total=1,
            control=control,
        )
        try:
            assert second_next_active.wait(timeout=5.0), "worker did not request second record"
            control.request_answer_now()
        finally:
            release_iterator.set()
        result = future.result(timeout=5.0)

    assert len(result.records) == 1
    assert result.records == records[:1]
    assert result.records_seen == 1
    assert result.matches_seen == 1
