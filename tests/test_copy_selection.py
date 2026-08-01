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
from agentgrep.ui._clipboard import TMUX_CLIPBOARD_HINT
from agentgrep.ui.app import build_streaming_ui_app
from agentgrep.ui.widgets.history import HistoryRecall

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


async def test_screen_selection_beats_focused_input_clearing(
    tmp_path: pathlib.Path,
) -> None:
    """Ctrl-C copies a fresh body selection without clearing the focused query."""
    record = _make_record("fresh selection\n")
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        layout = await _present(app, pilot, [record], record)
        search = app.query_one("#search")
        search.value = "query"
        search.focus()
        selection = Selection(Offset(0, 0), Offset(5, 0))
        app.screen.selections = {layout._detail_body: selection}
        assert app.screen.get_selected_text() == "fresh"
        app._clipboard = "PRESET"

        await pilot.press("ctrl+c")
        await pilot.pause()

        assert search.value == "query"
        assert app.clipboard == "fresh"
        assert not app.screen.selections


async def test_focused_input_supports_every_copy_chord(tmp_path: pathlib.Path) -> None:
    """Each advertised copy chord copies a focused input selection."""
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        search = app.query_one("#search")
        search.value = "before needle after"
        search.focus()

        for key in ("ctrl+c", "super+c", "ctrl+shift+c", "shift+super+c"):
            search.selection = type(search.selection)(7, 13)
            app._clipboard = f"PRESET-{key}"

            await pilot.press(key)
            await pilot.pause()

            assert app.clipboard == "needle", key
            assert search.value == "before needle after", key


async def test_history_filter_supports_every_copy_chord(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each advertised copy chord copies a HistoryRecall filter selection."""
    monkeypatch.delenv("TMUX", raising=False)
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(HistoryRecall(()))
        await pilot.pause()
        await app.workers.wait_for_complete()
        field = app.screen.query_one("#history-filter")
        field.value = "before needle after"
        field.focus()

        for key in ("ctrl+c", "super+c", "ctrl+shift+c", "shift+super+c"):
            field.selection = type(field.selection)(7, 13)
            app._clipboard = f"PRESET-{key}"
            app._notifications.clear()

            await pilot.press(key)
            await pilot.pause()

            assert app.clipboard == "needle", key
            assert field.value == "before needle after", key
            assert [str(item.message) for item in app._notifications] == [
                "sent selection to the clipboard (6 chars, OSC 52)"
            ], key


async def test_tmux_clipboard_hint_is_once_per_app_session(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crossing into a modal does not repeat the session's tmux caveat."""
    monkeypatch.setenv("TMUX", "/tmp/tmux/default,1,0")
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    messages: list[str] = []
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, **_kwargs: messages.append(str(message)),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.screen.send_to_clipboard("first", label="selection")
        app.push_screen(HistoryRecall(()))
        await pilot.pause()
        await app.workers.wait_for_complete()
        app.screen.send_to_clipboard("second", label="selection")

        assert messages.count(TMUX_CLIPBOARD_HINT) == 1


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


async def test_same_record_width_change_drops_the_selection(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A width-baked rebuild drops offsets from the old coordinate space."""
    record = _make_record(
        "one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen\n"
    )
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        layout = await _present(app, pilot, [record], record)
        before = layout._presented_detail_cache_key
        assert before is not None
        app.screen.selections = {layout._detail_body: Selection(Offset(0, 0), Offset(5, 0))}
        assert app.screen.selections

        monkeypatch.setattr(layout, "_detail_render_width", lambda: before[-1] + 20)
        layout._after_resize()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert layout._presented_detail_cache_key != before
        assert not app.screen.selections


async def test_greplog_mouse_selection_copies_without_quitting(
    tmp_path: pathlib.Path,
) -> None:
    """The grep log exposes dragged text and clears offsets before pruning."""
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(
            tmp_path,
            _empty_query(),
            control=SearchControl(),
            layout="greplog",
        ),
    )
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        layout = app.screen
        log = layout._log
        log.write("alpha one\nbeta two")
        await pilot.pause()

        assert await pilot.mouse_down(log, offset=(0, 0))
        assert await pilot.hover(log, offset=(4, 0))
        assert await pilot.mouse_up(log, offset=(4, 0))
        await pilot.pause()
        assert app.screen.get_selected_text() == "alpha"

        app._clipboard = "PRESET"
        await pilot.press("ctrl+c")
        await pilot.pause()

        assert app.clipboard == "alpha"
        assert app.is_running
        assert not app.screen.selections

        log.max_lines = 2
        log.clear()
        layout._write_chunk([_make_record("first"), _make_record("second")])
        app.screen.selections = {log: Selection(Offset(0, 0), Offset(5, 0))}
        assert app.screen.get_selected_text()

        layout._write_chunk([_make_record("third")])

        assert not app.screen.selections


async def test_greplog_mouse_selection_tracks_rendered_cells(
    tmp_path: pathlib.Path,
) -> None:
    """Copy the rendered glyph under display-cell mouse coordinates."""
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(
            tmp_path,
            _empty_query(),
            control=SearchControl(),
            layout="greplog",
        ),
    )
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        log = app.screen._log

        for text, start, end, expected in (
            ("a界b", (1, 0), (2, 0), "界"),
            ("a\tb", (8, 0), (9, 0), "b"),
        ):
            app.screen.clear_selection()
            log.clear()
            log.write(text)
            await pilot.pause()

            assert await pilot.mouse_down(log, offset=start)
            assert await pilot.hover(log, offset=end)
            assert await pilot.mouse_up(log, offset=end)
            await pilot.pause()

            assert app.screen.get_selected_text() == expected


async def test_greplog_drag_keeps_anchor_while_scrolling(
    tmp_path: pathlib.Path,
) -> None:
    """Scrolling during a drag keeps its original content-row anchor."""
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(
            tmp_path,
            _empty_query(),
            control=SearchControl(),
            layout="greplog",
        ),
    )
    async with app.run_test(size=(60, 15)) as pilot:
        await pilot.pause()
        log = app.screen._log
        log.auto_scroll = False
        log.write("\n".join(f"L{index:02d}" for index in range(50)))
        await pilot.pause()
        log.scroll_to(y=10, animate=False)
        await pilot.pause()

        assert await pilot.mouse_down(log, offset=(0, 0))
        log.scroll_to(y=15, animate=False)
        await pilot.pause()
        assert await pilot.hover(log, offset=(2, 2))
        assert await pilot.mouse_up(log, offset=(2, 2))
        await pilot.pause()

        assert app.screen.get_selected_text() == "\n".join(
            f"L{index:02d}" for index in range(10, 18)
        )
