"""The two ways a copy can silently return the wrong thing.

Both failures here are quiet -- no traceback, no toast, just a clipboard that
does not hold what the user selected -- which is why they are worth a mounted
app. The toast wording and the tmux caveat are pure functions covered by
doctests in :mod:`agentgrep.ui._clipboard`, so they are not re-tested here.
"""

from __future__ import annotations

import json
import pathlib
import typing as t

import pytest
from textual.actions import SkipAction
from textual.geometry import Offset
from textual.selection import Selection

from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui.app import build_streaming_ui_app

pytestmark = [pytest.mark.tui, pytest.mark.slow]


def _make_record(text: str) -> SearchRecord:
    """Build a small prompt record (built inline, no worker)."""
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


async def test_unselectable_body_never_clobbers_clipboard(tmp_path: pathlib.Path) -> None:
    """A JSON body paints as Rich ``Syntax``; copying it must leave the clipboard alone.

    ``Widget.get_selection`` returns ``None`` for a non-``Text`` visual, so
    ``Screen.get_selected_text`` yields ``""`` rather than ``None`` and stock
    ``action_copy_text`` would copy the empty string over whatever the user
    had. Seeding a sentinel proves the guard preserves it.

    The action is invoked directly rather than through a chord: an empty
    selection is *meant* to fall through to the layout's quit, so a keypress
    would exercise the routing instead of the guard.
    """
    record = _make_record(json.dumps({"alpha": "one", "beta": "two"}, indent=2))
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        layout = await _present(app, pilot, [record], record)

        body_widget = layout._detail_body
        selection = Selection(Offset(0, 0), Offset(4, 0))
        app.screen.selections = {body_widget: selection}
        assert body_widget.get_selection(selection) is None
        assert app.screen.get_selected_text() == ""

        app._clipboard = "PRESET"
        with pytest.raises(SkipAction):
            app.screen.action_copy_text()
        await pilot.pause()

        assert app.clipboard == "PRESET"


async def test_screen_selection_uses_shared_clipboard_sender(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native screen copy reports the shared OSC-52 delivery notice."""
    monkeypatch.delenv("TMUX", raising=False)
    record = _make_record("selected body\n")
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        layout = await _present(app, pilot, [record], record)

        selection = Selection(Offset(0, 0), Offset(8, 0))
        app.screen.selections = {layout._detail_body: selection}
        assert app.screen.get_selected_text() == "selected"
        app._notifications.clear()

        app.screen.action_copy_text()
        await pilot.pause()

        assert app.clipboard == "selected"
        assert [str(notification.message) for notification in app._notifications] == [
            "sent selection to the clipboard (8 chars, OSC 52)"
        ]
        assert not app.screen.selections


async def test_record_switch_drops_a_stale_selection(tmp_path: pathlib.Path) -> None:
    """Selecting in one record and switching to another clears the highlight.

    ``#detail-body`` is one reused ``Static``, so a selection stores offsets,
    not content: left alone they re-aim at the incoming record and a copy
    returns text the user never selected.
    """
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

        app.screen.selections = {layout._detail_body: Selection(Offset(0, 0), Offset(5, 0))}

        layout.show_detail(record)
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.screen.selections
