"""Ambient current-line indicator in the detail pane (no visual select).

Before this, ``DetailScroll`` tracked only a scroll offset during ordinary
``j``/``k`` navigation -- the ``(row, col)`` cursor existed but was painted
solely inside tmux-style visual select (``v``). Outside visual mode there was
no persistent signal of "where" the reader's position was. These tests pin
the ambient replacement: a focus-scoped current-line band, a byproduct of
scroll position, with no new keybindings or independent cursor state.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui.app import build_streaming_ui_app
from agentgrep.ui.layouts._hud_detail_interaction import _DETAIL_CURSOR_LINE_STYLE

pytestmark = pytest.mark.tui

_LINE_COUNT = 200
_BODY = "\n".join(f"line {index}" for index in range(_LINE_COUNT))


def _make_record(text: str) -> SearchRecord:
    """Build a plain-text prompt record long enough to require scrolling."""
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


def _ambient_spans(body_widget: t.Any) -> list[t.Any]:
    """Return the ambient cursor-line spans in the detail body's rendered content."""
    visual = body_widget.visual
    return [span for span in visual.spans if span.style == _DETAIL_CURSOR_LINE_STYLE]


async def test_ambient_cursor_line_tracks_focus_and_scroll(
    tmp_path: pathlib.Path,
) -> None:
    """``j`` navigation (no ``v``) shows exactly one focus-scoped span that moves."""
    record = _make_record(_BODY)
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 30)) as pilot:
        layout = app.screen
        await pilot.pause()
        layout.all_records = [record]
        layout.filtered_records = [record]
        layout.show_detail(record)
        await app.workers.wait_for_complete()
        await pilot.pause()

        body_widget = layout._detail_body
        assert layout._detail_scroll.has_focus is False
        assert _ambient_spans(body_widget) == []

        layout._detail_scroll.focus()
        await pilot.pause()
        assert layout._detail_scroll.has_focus is True
        first_spans = _ambient_spans(body_widget)
        assert len(first_spans) == 1

        await pilot.press("j", "j", "j", "j", "j", "j", "j", "j")
        await pilot.pause()
        assert layout._detail_scroll.scroll_y > 0
        moved_spans = _ambient_spans(body_widget)
        assert len(moved_spans) == 1
        assert moved_spans != first_spans

        layout._focus_widget_by_id("results")
        await pilot.pause()
        assert layout._detail_scroll.has_focus is False
        assert _ambient_spans(body_widget) == []


async def test_ambient_cursor_line_survives_raw_toggle_and_find(
    tmp_path: pathlib.Path,
) -> None:
    """``alt+r`` and opening/closing find leave the indicator sane, never stuck."""
    record = _make_record(_BODY)
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 30)) as pilot:
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
        assert len(_ambient_spans(body_widget)) == 1

        # Raw/rendered toggle: exactly one span before and after, never
        # duplicated by re-painting on top of a stale overlay.
        await pilot.press("alt+r")
        await pilot.pause()
        assert len(_ambient_spans(body_widget)) == 1
        await pilot.press("alt+r")
        await pilot.pause()
        assert len(_ambient_spans(body_widget)) == 1

        # Opening find moves focus off the scroll -- the ambient line hides
        # rather than fighting the find-match highlight painted on the body.
        await pilot.press("slash")
        await pilot.pause()
        assert layout._detail_scroll.has_focus is False
        assert _ambient_spans(body_widget) == []

        # Closing find returns focus to the scroll -- the indicator comes
        # back, still exactly one span.
        await pilot.press("escape")
        await pilot.pause()
        assert layout._detail_scroll.has_focus is True
        assert len(_ambient_spans(body_widget)) == 1


async def test_ambient_cursor_line_does_not_hide_an_active_filter_highlight(
    tmp_path: pathlib.Path,
) -> None:
    """The band applies underneath an existing filter highlight, not over it.

    ``apply_filter_highlight`` also tints the background, the same channel
    the ambient band uses. The band must be stylized with ``stylize_before``
    (span list prepended, so a later-applied span wins the shared channel) --
    plain ``stylize`` would append it last and silently paint over an active
    filter match on the same line.
    """
    record = _make_record("line 0 needle\n" + _BODY)
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 30)) as pilot:
        layout = app.screen
        await pilot.pause()
        layout._filter_terms = ("needle",)
        layout.show_detail(record)
        await app.workers.wait_for_complete()
        await pilot.pause()

        layout._detail_scroll.focus()
        await pilot.pause()

        body_widget = layout._detail_body
        visual = body_widget.visual
        ambient_index = next(
            index
            for index, span in enumerate(visual.spans)
            if span.style == _DETAIL_CURSOR_LINE_STYLE
        )
        filter_index = next(
            index
            for index, span in enumerate(visual.spans)
            if span.style != _DETAIL_CURSOR_LINE_STYLE and span.start < 13
        )
        assert ambient_index < filter_index
