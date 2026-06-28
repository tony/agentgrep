"""Pilot tests for the grep-log layout (ADR 0013, the layout axis).

``GrepLogLayout`` shares the engine seam and normalized records with the HUD but
composes a single append-only log and presents records as lines. These tests
mount it (pushed onto the shell) and drive its streaming/present hooks directly,
mirroring the HUD's ``_apply_records_batch`` tests.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

from agentgrep.progress import StreamingRecordsBatch, StreamingSearchFinished
from agentgrep.records import SearchRecord
from tests.test_agentgrep import _build_empty_ui_app


def _record(tmp_path: pathlib.Path, idx: int, text: str) -> SearchRecord:
    """Build a minimal record for the log."""
    return SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / f"r{idx}.jsonl",
        text=text,
    )


async def _mount_greplog(app: t.Any, pilot: t.Any) -> t.Any:
    """Push a grep-log layout (search workflow) onto the running shell."""
    from agentgrep.ui.layouts.greplog import GrepLogLayout
    from agentgrep.ui.workflows.search import SearchWorkflow

    layout = GrepLogLayout(app._ctx, SearchWorkflow())
    await app.push_screen(layout)
    await pilot.pause()
    return layout


async def test_greplog_streams_records_into_the_log(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A streamed batch extends the buffer and appends one log line per record."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [_record(tmp_path, i, f"row {i}") for i in range(3)]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        assert layout.query_one("#greplog") is not None
        await layout._apply_event(
            layout._generation,
            StreamingRecordsBatch(records=tuple(records), total=3),
        )
        await pilot.pause()
        assert layout._records == records
        assert len(layout.query_one("#greplog").lines) == 3


async def test_greplog_finished_sets_status_line(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished grep freezes the status line with the match count."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        layout._apply_finished("complete", 5, 1.2, None)
        await pilot.pause()
        assert "5" in str(layout.query_one("#greplog-status").render())


async def test_greplog_filter_renders_only_matches(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browse-style filter re-renders the log to the matching subset (NB-4)."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        _record(tmp_path, 0, "needle here"),
        _record(tmp_path, 1, "haystack only"),
        _record(tmp_path, 2, "needle again"),
    ]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        await layout._apply_event(
            layout._generation,
            StreamingRecordsBatch(records=tuple(records), total=3),
        )
        await pilot.pause()
        assert len(layout.query_one("#greplog").lines) == 3
        matching = tuple(r for r in records if "needle" in r.text)
        await layout._apply_log_filter(matching)
        await pilot.pause()
        assert len(layout.query_one("#greplog").lines) == 2


async def test_greplog_stale_generation_is_dropped(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch from a superseded generation never reaches the log (NB-10)."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [_record(tmp_path, 0, "row")]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        await layout._apply_event(
            layout._generation - 1,  # a stale generation
            StreamingRecordsBatch(records=tuple(records), total=1),
        )
        await pilot.pause()
        assert layout._records == []
        assert len(layout.query_one("#greplog").lines) == 0


class ResetStaleEventCase(t.NamedTuple):
    """A stale worker event that arrives after reset."""

    test_id: str
    event_kind: t.Literal["records", "finished"]


RESET_STALE_EVENT_CASES = (
    ResetStaleEventCase("records-batch", "records"),
    ResetStaleEventCase("finished-event", "finished"),
)


@pytest.mark.parametrize("case", RESET_STALE_EVENT_CASES, ids=lambda case: case.test_id)
async def test_greplog_reset_drops_stale_search_events(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: ResetStaleEventCase,
) -> None:
    """Search events from before reset must not repaint the cleared log."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [_record(tmp_path, 0, "old row")]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        old_generation = layout._generation
        layout.reset_view()
        if case.event_kind == "records":
            await layout._apply_event(
                old_generation,
                StreamingRecordsBatch(records=tuple(records), total=1),
            )
        else:
            await layout._apply_event(
                old_generation,
                StreamingSearchFinished(outcome="complete", total=9, elapsed=0.1),
            )
        await pilot.pause()
        assert layout._records == []
        assert len(layout.query_one("#greplog").lines) == 0
        assert str(layout.query_one("#greplog-status").render()) == ""
