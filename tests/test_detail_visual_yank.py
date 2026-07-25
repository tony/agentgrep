"""tmux copy-mode-vi visual select + yank in the detail pane.

Exercises the native-selection path through a real ``App.run_test`` pilot:
focusing the detail body, pressing ``v`` to begin a selection anchored at the
logical cursor, extending it with ``j`` / ``l`` motions, and pressing ``y`` to
yank the exact selected source substring to the clipboard. A second pass proves
``escape`` cancels visual mode and clears the native selection.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui.app import build_streaming_ui_app

pytestmark = pytest.mark.tui

_BODY = "line one\nline two\nline three\nline four\n"


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


async def test_detail_visual_select_and_yank(tmp_path: pathlib.Path) -> None:
    """``v`` + ``j``/``l`` motions + ``y`` yanks the exact selected substring."""
    record = _make_record(_BODY)
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
        layout._detail_scroll.focus()
        await pilot.pause()

        # v begins a selection anchored at the logical cursor (0, 0); j drops
        # onto line one; l x3 extends to column 3. tmux selection is inclusive
        # of the cursor cell, so the yank covers "line one\n" + "line" (chars
        # 0..3 of "line two").
        await pilot.press("v")
        await pilot.pause()
        assert layout._detail_visual_active is True
        assert body_widget in app.screen.selections

        await pilot.press("j", "l", "l", "l", "y")
        await pilot.pause()

        assert app.clipboard == "line one\nline"
        # y exits visual mode and clears the native selection.
        assert layout._detail_visual_active is False
        assert app.screen.selections == {}

        # Re-enter visual mode, then escape cancels + clears the selection.
        await pilot.press("v")
        await pilot.pause()
        assert layout._detail_visual_active is True
        assert body_widget in app.screen.selections

        await pilot.press("escape")
        await pilot.pause()
        assert layout._detail_visual_active is False
        assert app.screen.selections == {}
