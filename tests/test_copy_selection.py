"""Screen-selection copy guard for the explorer's screens.

Textual binds a copy chord on every :class:`~textual.screen.Screen`, and its
``action_copy_text`` skips only when nothing at all is selected. A drag over a
body whose visual cannot be extracted from still resolves to ``""``, which
stock Textual would happily write over the user's clipboard. These tests pin
that an empty resolution leaves the clipboard untouched.
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

pytestmark = pytest.mark.tui


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


async def test_unselectable_body_never_clobbers_clipboard(tmp_path: pathlib.Path) -> None:
    """A JSON body paints as Rich ``Syntax``; copying it must leave the clipboard alone.

    ``Widget.get_selection`` returns ``None`` for a non-``Text`` visual, so
    ``Screen.get_selected_text`` yields ``""`` rather than ``None`` and stock
    ``action_copy_text`` would copy the empty string over whatever the user
    had. Seeding a sentinel proves the guard preserves it, which is strictly
    stronger than asserting the clipboard stayed empty.

    The action is invoked directly rather than through a chord: both layouts
    rebind ``ctrl+c`` to their own quit, so a key press never reaches
    ``screen.copy_text`` and would pass with or without the guard.
    """
    record = _make_record(json.dumps({"alpha": "one", "beta": "two"}, indent=2))
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        await pilot.pause()
        layout.all_records = [record]
        layout.filtered_records = [record]
        layout.show_detail(record)
        await app.workers.wait_for_complete()
        await pilot.pause()

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
