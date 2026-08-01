"""How long a detail-pane selection outlives what it points at.

``#detail-body`` is one reused ``Static``: every record repaints the same
widget. A native selection therefore stores offsets, not content, and without
help those offsets survive a record switch and silently re-aim at the incoming
body. The two directions matter equally -- a switch must drop the selection, a
repaint of the *same* record (resize, theme change) must keep it.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest
from textual.geometry import Offset
from textual.selection import Selection

from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui.app import build_streaming_ui_app

pytestmark = pytest.mark.tui


def _make_record(text: str) -> SearchRecord:
    """Build a small plain-text prompt record (built inline, no worker)."""
    return SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions",
        path=pathlib.Path("/home/user/.codex/sessions/2026/07/22/demo.jsonl"),
        text=text,
    )


def _empty_query() -> SearchQuery:
    """Build an idle search query (no terms)."""
    return SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )


async def _present(
    app: t.Any, pilot: t.Any, records: list[SearchRecord], show: SearchRecord
) -> t.Any:
    """Load ``records`` into the layout and present ``show`` in the detail pane."""
    layout = app.screen
    layout.all_records = records
    layout.filtered_records = records
    layout.show_detail(show)
    await app.workers.wait_for_complete()
    await pilot.pause()
    return layout


async def test_record_switch_drops_a_stale_selection(tmp_path: pathlib.Path) -> None:
    """Selecting in one record and switching to another clears the highlight."""
    first = _make_record("first record body\nsecond line\n")
    second = _make_record("a completely different record\nwith other text\n")
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        layout = await _present(app, pilot, [first, second], first)

        app.screen.selections = {layout._detail_body: Selection(Offset(0, 0), Offset(5, 0))}
        assert app.screen.selections

        layout.show_detail(second)
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert not app.screen.selections


async def test_same_record_repaint_keeps_the_selection(tmp_path: pathlib.Path) -> None:
    """A repaint of the record already shown leaves a held selection alone.

    ``show_detail`` is re-entered for a resize and a theme change with the same
    record, so an unconditional clear would destroy a selection mid-drag.
    """
    record = _make_record("first record body\nsecond line\n")
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        layout = await _present(app, pilot, [record], record)

        selection = Selection(Offset(0, 0), Offset(5, 0))
        app.screen.selections = {layout._detail_body: selection}

        layout.show_detail(record)
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.screen.selections
