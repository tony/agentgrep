"""Functional tests for the legacy Textual detail and streaming surface."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
import threading
import time
import typing as t

import pytest

import agentgrep as _agentgrep_module
from tests._agentgrep_tui_support import (
    _build_empty_ui_app,
    _filter_completed,
    _seed_records,
    _set_result_records,
    _static_content,
    _ui_record,
    load_agentgrep_module,
)

pytestmark = pytest.mark.tui


def _detail_find_record(agentgrep: t.Any, path: pathlib.Path) -> t.Any:
    """Build a record whose body has several 'needle' matches across lines."""
    body = "\n".join(
        f"line {i} has a needle here" if i % 3 == 0 else f"line {i} is plain" for i in range(30)
    )
    return _ui_record(agentgrep, path, body, "find")


async def _open_detail_with_find(app: t.Any, record: t.Any, pilot: t.Any) -> None:
    """Show ``record`` in the detail pane and reveal the find bar."""
    app.screen._set_empty_state(empty=False)
    app.screen.show_detail(record)
    await pilot.pause()
    app.screen.action_open_detail_find()
    await pilot.pause()


class DetailFindStaleRequestCase(t.NamedTuple):
    """A stale debounced find request scenario."""

    test_id: str
    live_text: str
    message_text: str
    close_first: bool


DETAIL_FIND_STALE_REQUEST_CASES = [
    DetailFindStaleRequestCase(
        test_id="closed-find-ignores-pending-request",
        live_text="needle",
        message_text="needle",
        close_first=True,
    ),
    DetailFindStaleRequestCase(
        test_id="changed-input-ignores-old-request",
        live_text="nomatch",
        message_text="needle",
        close_first=False,
    ),
]


class DetailFindStepLiveQueryCase(t.NamedTuple):
    """An immediate find navigation key scenario."""

    test_id: str
    key: str
    expected_index: int


DETAIL_FIND_STEP_LIVE_QUERY_CASES = [
    DetailFindStepLiveQueryCase(test_id="enter-steps-live-query", key="enter", expected_index=1),
    DetailFindStepLiveQueryCase(test_id="down-steps-live-query", key="down", expected_index=1),
    DetailFindStepLiveQueryCase(test_id="up-steps-live-query", key="up", expected_index=9),
]


class DetailFindPendingRenderCase(t.NamedTuple):
    """A detail-find query while the selected large record is still rendering."""

    test_id: str
    query: str
    expected_matches: int


DETAIL_FIND_PENDING_RENDER_CASES = [
    DetailFindPendingRenderCase(
        test_id="does-not-search-old-source",
        query="oldneedle",
        expected_matches=0,
    ),
    DetailFindPendingRenderCase(
        test_id="searches-new-body-fallback",
        query="newneedle",
        expected_matches=1,
    ),
]


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    DETAIL_FIND_STALE_REQUEST_CASES,
    ids=[case.test_id for case in DETAIL_FIND_STALE_REQUEST_CASES],
)
async def test_detail_find_ignores_stale_debounce_requests(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: DetailFindStaleRequestCase,
) -> None:
    """Stale debounced find requests do not repaint hidden or superseded find state."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    from agentgrep.ui.widgets.messages import DetailFindRequested

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, record, pilot)
        app.screen._detail_find_input.load_query(case.live_text)
        if case.close_first:
            app.screen._close_detail_find()
            await pilot.pause()

        app.screen.on_detail_find_requested(DetailFindRequested(case.message_text))
        await pilot.pause()

        assert app.screen._detail_find_query == ""
        assert app.screen._detail_find_matches == []
        if case.close_first:
            assert app.screen._detail_find_active is False
            assert app.screen._detail_find_input.display is False


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    DETAIL_FIND_STEP_LIVE_QUERY_CASES,
    ids=[case.test_id for case in DETAIL_FIND_STEP_LIVE_QUERY_CASES],
)
async def test_detail_find_steps_live_query_before_navigation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: DetailFindStepLiveQueryCase,
) -> None:
    """Find navigation keys search the live input before stepping matches."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    from textual import events

    from agentgrep.ui.widgets.messages import DetailFindRequested

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, record, pilot)
        app.screen._detail_find_input.value = "needle"

        await app.screen._detail_find_input.on_key(events.Key(case.key, None))
        app.screen.on_detail_find_requested(DetailFindRequested("needle"))
        await pilot.pause()

        assert app.screen._detail_find_query == "needle"
        assert len(app.screen._detail_find_matches) == 10
        assert app.screen._detail_find_current == case.expected_index


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    DETAIL_FIND_PENDING_RENDER_CASES,
    ids=[case.test_id for case in DETAIL_FIND_PENDING_RENDER_CASES],
)
async def test_detail_find_uses_new_body_while_large_render_is_pending(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: DetailFindPendingRenderCase,
) -> None:
    """Opening find before a large render finishes searches the new record body."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    old_record = _ui_record(
        agentgrep,
        tmp_path / "old.jsonl",
        "oldneedle only lives in the previous record",
        "old",
    )
    new_body = "newneedle lives here\n" + (
        "x" * (app.get_default_screen()._DETAIL_ASYNC_BODY_THRESHOLD + 1000)
    )
    new_record = _ui_record(agentgrep, tmp_path / "new.jsonl", new_body, "new")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen.show_detail(old_record)
        await pilot.pause()
        assert "oldneedle" in app.screen._detail_find_source

        scheduled_workers: list[object] = []

        def capture_worker(worker: object, **_: object) -> None:
            scheduled_workers.append(worker)

        monkeypatch.setattr(app.screen, "run_worker", capture_worker)
        app.screen.show_detail(new_record)
        assert scheduled_workers

        app.screen.action_open_detail_find()
        app.screen._detail_find_input.load_query(case.query)
        app.screen._run_detail_find(case.query, reset_cursor=True)
        await pilot.pause()

        assert len(app.screen._detail_find_matches) == case.expected_matches


@pytest.mark.slow
async def test_detail_find_searches_navigates_and_counts(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typing in the find bar matches the body, counts N/M, and steps the cursor."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, record, pilot)
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        await pilot.pause()
        assert len(app.screen._detail_find_matches) == 10
        assert app.screen._detail_find_current == 0
        assert "1/10" in str(app.screen._detail_statusline.render())
        # Next match advances the cursor and scrolls the body.
        before = app.screen._detail_scroll.scroll_y
        app.screen._detail_find_step(1)
        await pilot.pause()
        assert app.screen._detail_find_current == 1
        assert app.screen._detail_scroll.scroll_y > before
        # Wrap-around: previous from match 1 -> 0, previous again -> last (9).
        app.screen._detail_find_step(-1)
        app.screen._detail_find_step(-1)
        assert app.screen._detail_find_current == 9


@pytest.mark.slow
async def test_detail_find_step_reuses_syntax_base(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stepping find matches must not re-tokenize the JSON body each press (NB-9).

    ``_present_detail_find`` re-overlays only the find-match spans; the
    syntax+search+filter base is identical across a find session, so a cached
    base keeps the per-keystroke cost off a full-body ``Syntax`` re-highlight.
    """
    from agentgrep.ui.layouts import hud

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    body = json.dumps({"notes": [f"needle {i}" for i in range(12)]}, indent=2)
    record = _ui_record(agentgrep, tmp_path / "j.jsonl", body, "json")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        syntax_calls = 0
        real_syntax = hud._RichSyntax

        def counting_syntax(*args: t.Any, **kwargs: t.Any) -> t.Any:  # forwarding spy
            nonlocal syntax_calls
            syntax_calls += 1
            return real_syntax(*args, **kwargs)

        monkeypatch.setattr(hud, "_RichSyntax", counting_syntax)
        await _open_detail_with_find(app, record, pilot)
        app.screen._run_detail_find("needle", reset_cursor=True)
        await pilot.pause()
        assert app.screen._detail_find_matches  # the JSON body really was matched
        after_find = syntax_calls
        assert after_find >= 1  # the JSON base was tokenized at least once
        app.screen._detail_find_step(1)
        await pilot.pause()
        assert syntax_calls == after_find  # the step reused the cached base


@pytest.mark.slow
async def test_detail_find_open_reuses_presented_text_highlights(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening find reuses the long plain-text body already highlighted off-pump.

    Patch the defining module: hud calls
    ``_streaming._apply_bounded_literal_highlights`` through the module
    namespace, so patching ``ui._streaming`` intercepts every caller;
    patching a ``hud`` alias would intercept nothing.
    """
    from agentgrep.ui import _streaming

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _ui_record(
        agentgrep,
        tmp_path / "long.jsonl",
        "needle " + ("plain text " * 6000),
        "long",
    )
    highlight_calls = 0
    real_highlight = _streaming._apply_bounded_literal_highlights

    def counting_highlight(*args: t.Any, **kwargs: t.Any) -> None:
        nonlocal highlight_calls
        highlight_calls += 1
        real_highlight(*args, **kwargs)

    monkeypatch.setattr(_streaming, "_apply_bounded_literal_highlights", counting_highlight)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen.search_query = dataclasses.replace(
            app.screen.search_query,
            terms=("needle",),
        )
        app.screen._set_empty_state(empty=False)
        app.screen.show_detail(record)
        await app.workers.wait_for_complete()
        await pilot.pause()
        after_present = highlight_calls
        assert after_present >= 1

        app.screen.action_open_detail_find()
        await pilot.pause()

        assert highlight_calls == after_present


class DetailFindFilterRefreshCase(t.NamedTuple):
    """A same-record filter change while detail find stays open."""

    test_id: str
    initial_filter: str
    updated_filter: str
    find_query: str


DETAIL_FIND_FILTER_REFRESH_CASES: tuple[DetailFindFilterRefreshCase, ...] = (
    DetailFindFilterRefreshCase(
        test_id="same-record-filter-change",
        initial_filter="before",
        updated_filter="after",
        find_query="needle",
    ),
)


@pytest.mark.slow
async def test_filter_completion_refreshes_same_record_detail_highlights(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new filter repaints decoration even when the selected record is unchanged."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    body = "before needle after"
    record = _ui_record(agentgrep, tmp_path / "filter.jsonl", body, "filter")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._filter_terms = ("before",)
        app.screen.show_detail(record)
        await app.workers.wait_for_complete()
        await pilot.pause()

        app.screen._filter_terms = ("after",)
        app.screen._filter_input.value = "after"
        app.screen.on_filter_completed(
            _filter_completed(app, [record], text="after"),
        )
        await app.workers.wait_for_complete()
        await pilot.pause()

        detail_body = _static_content(app.screen._detail).renderables[1]
        spans = [(span.start, span.end, str(span.style)) for span in detail_body.spans]
        filter_bg = app.theme_variables["ag-match-filter-bg"]
        assert not any(
            start == 0 and end == len("before") and filter_bg in style
            for start, end, style in spans
        )
        after_start = body.index("after")
        assert any(
            start == after_start and end == after_start + len("after") and filter_bg in style
            for start, end, style in spans
        )


@pytest.mark.slow
async def test_empty_filter_completion_clears_detail_find_selection(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty filter cannot retain or repaint the excluded detail record."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _ui_record(agentgrep, tmp_path / "excluded.jsonl", "needle body", "excluded")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, record, pilot)
        app.screen._detail_opened = True
        app.screen._apply_responsive_layout()
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        app.screen._search_done = True
        app.screen._filter_input.value = "absent"
        await pilot.pause()

        app.screen.on_filter_completed(
            _filter_completed(app, [], text="absent"),
        )
        await pilot.pause()

        assert str(app.screen._detail.render()) == "No results."
        assert app.screen._current_detail_record is None
        assert app.screen._detail_find_active is False
        assert app.screen._detail_find_input.display is False
        assert app.screen._detail_find_matches == []
        assert str(app.screen._detail_statusline.render()) == ""
        assert getattr(app.focused, "id", None) == "filter"
        assert app.screen._detail_opened is False
        assert app.screen._detail_column.has_class("-collapsed")

        app.screen._detail_find_step(1)

        assert str(app.screen._detail.render()) == "No results."


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    DETAIL_FIND_FILTER_REFRESH_CASES,
    ids=[case.test_id for case in DETAIL_FIND_FILTER_REFRESH_CASES],
)
async def test_detail_find_base_refreshes_filter_highlights_when_filter_changes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: DetailFindFilterRefreshCase,
) -> None:
    """A same-record filter change refreshes the cached find-highlight base."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    body = f"{case.initial_filter} {case.find_query} {case.updated_filter}"
    record = _ui_record(agentgrep, tmp_path / "filter.jsonl", body, "filter")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, record, pilot)
        app.screen._filter_terms = (case.initial_filter,)
        app.screen._detail_find_input.load_query(case.find_query)
        app.screen._run_detail_find(case.find_query, reset_cursor=True)
        await pilot.pause()

        app.screen._filter_terms = (case.updated_filter,)
        app.screen._filter_input.value = case.updated_filter
        app.screen.on_filter_completed(
            _filter_completed(
                app,
                [record],
                text=case.updated_filter,
            ),
        )
        app.screen._detail_find_step(1)
        await pilot.pause()

        detail_body = _static_content(app.screen._detail).renderables[1]
        spans = [(span.start, span.end, str(span.style)) for span in detail_body.spans]
        filter_bg = app.theme_variables["ag-match-filter-bg"]
        initial_start = body.index(case.initial_filter)
        updated_start = body.index(case.updated_filter)
        assert not any(
            start == initial_start
            and end == initial_start + len(case.initial_filter)
            and filter_bg in style
            for start, end, style in spans
        )
        assert any(
            start == updated_start
            and end == updated_start + len(case.updated_filter)
            and filter_bg in style
            for start, end, style in spans
        )


@pytest.mark.slow
async def test_new_search_clears_results_render_cache(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh search releases rendered rows so a reused record id can't go stale.

    The row cache is keyed by ``id(record)`` (like cached_haystack); when a new
    search empties ``all_records`` the rows must be released with them.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        _ui_record(agentgrep, tmp_path / f"r{i}.jsonl", f"row {i}", f"s{i}") for i in range(6)
    ]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        _set_result_records(app.screen._results, records)
        assert app.screen._results._render_cache == {}  # model replacement stays lazy
        app.screen._results._render_record(records[0])  # one requested row populates the LRU
        assert app.screen._results._render_cache  # non-empty
        app.screen._reset_search_chrome()  # a fresh search releases the old records
        assert app.screen._results._render_cache == {}  # cache released with them


@pytest.mark.slow
async def test_detail_find_only_opens_with_a_record(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The find bar stays hidden when no detail record is loaded (gated)."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert app.screen._current_detail_record is None
        app.screen.action_open_detail_find()
        await pilot.pause()
        assert app.screen._detail_find_input.display is False
        assert app.screen._detail_find_active is False


@pytest.mark.slow
async def test_detail_find_escape_closes_without_quitting(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Esc closes the find bar and refocuses the detail body without exiting."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, record, pilot)
        assert app.screen._detail_find_input.display is True
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen._detail_find_input.display is False
        assert app.screen._detail_find_active is False
        assert getattr(app.focused, "id", None) == "detail-scroll"
        assert app.is_running  # esc closed find, did not quit the app


@pytest.mark.slow
async def test_detail_find_memory_restores_per_record(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing find saves the query+cursor per record; revisiting restores them."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    rec_a = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    rec_b = _ui_record(agentgrep, tmp_path / "b.jsonl", "no matches at all\n" * 8, "b")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, rec_a, pilot)
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        app.screen._detail_find_step(1)  # land on match index 1
        await pilot.pause()
        app.screen._close_detail_find()
        await pilot.pause()
        assert app.screen._detail_find_state[id(rec_a)][:2] == ("needle", 1)
        # Visit another record, come back, reopen -> the query + cursor restore.
        app.screen.show_detail(rec_b)
        await pilot.pause()
        app.screen.show_detail(rec_a)
        await pilot.pause()
        app.screen.action_open_detail_find()
        await pilot.pause()
        assert app.screen._detail_find_input.value == "needle"
        assert app.screen._detail_find_current == 1
        assert len(app.screen._detail_find_matches) == 10


@pytest.mark.slow
async def test_detail_find_resets_on_record_switch_while_open(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching records with the find bar open closes it (no stale matches/count).

    Regression: leaving the bar open across a record switch otherwise applied
    the old record's match offsets to the new body and showed a stale N/M. The
    outgoing record's find is saved, so a revisit restores it.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    rec_a = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    rec_b = _ui_record(agentgrep, tmp_path / "b.jsonl", "no matches at all\n" * 8, "b")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, rec_a, pilot)
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        app.screen._detail_find_step(1)
        await pilot.pause()
        # Switch to B WITHOUT closing find first (the bug path).
        app.screen.show_detail(rec_b)
        await pilot.pause()
        assert app.screen._detail_find_active is False
        assert app.screen._detail_find_input.display is False
        assert app.screen._detail_find_matches == []
        # A's find survived in per-record memory for a later revisit.
        assert app.screen._detail_find_state[id(rec_a)][:2] == ("needle", 1)


@pytest.mark.slow
async def test_detail_find_survives_theme_switch(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A theme switch re-renders the same record but keeps the find active+highlighted."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    from agentgrep.ui import theme as ui_theme

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    rec_a = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, rec_a, pilot)
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        await pilot.pause()
        app.theme = ui_theme.LIGHT_THEME_NAME  # same record re-render
        await pilot.pause()
        # Find stays active with valid matches (not closed by the re-render),
        # and _present_detail re-overlays the highlights via _present_detail_find.
        assert app.screen._detail_find_active is True
        assert app.screen._detail_find_input.display is True
        assert len(app.screen._detail_find_matches) == 10


@pytest.mark.slow
async def test_theme_switch_refreshes_the_searching_panel(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A theme switch re-bakes the SearchingPanel's hex spans, like the header."""
    from agentgrep.ui import theme as ui_theme

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        calls: list[int] = []
        monkeypatch.setattr(app.screen._searching_panel, "refresh_theme", lambda: calls.append(1))
        app.theme = ui_theme.LIGHT_THEME_NAME
        await pilot.pause()
        assert calls == [1]


@pytest.mark.slow
async def test_input_ctrl_c_clears_then_arms_confirm_exit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C in the search input clears the text first, then arms confirm-exit."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        search.focus()
        search.value = "hello"
        await pilot.pause()
        await pilot.press("ctrl+c")  # text present -> clear, no exit, no arm
        await pilot.pause()
        assert search.value == ""
        assert app.is_running
        assert app.screen._confirm_exit_pending is False
        await pilot.press("ctrl+c")  # empty box -> arm confirm-exit (gutter shown)
        await pilot.pause()
        assert app.screen._confirm_exit_pending is True
        assert app.is_running
        assert app.screen._ctrlc_gutter.has_class("-shown")
        await pilot.press("x")  # any other key disarms
        await pilot.pause()
        assert app.screen._confirm_exit_pending is False
        assert app.screen._ctrlc_gutter.has_class("-shown") is False


@pytest.mark.slow
async def test_input_second_ctrl_c_on_empty_exits(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second ctrl-c on an empty input within the window exits the app."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        search.focus()
        await pilot.press("ctrl+c")  # arm
        await pilot.pause()
        assert app.screen._confirm_exit_pending is True
        await pilot.press("ctrl+c")  # exit
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.slow
@pytest.mark.parametrize("input_id", ["search", "filter"])
async def test_empty_input_ctrl_c_cancels_active_search(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    input_id: str,
) -> None:
    """An empty focused input cancels active work before arming exit."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        target = app.screen.query_one(f"#{input_id}")
        target.focus()
        app.screen._search_done = False

        await pilot.press("ctrl+c")
        await pilot.pause()

        assert app.screen.control.answer_now_requested() is True
        assert app.screen._confirm_exit_pending is False
        assert app.is_running


@pytest.mark.slow
async def test_ctrl_c_on_detail_pane_arms_confirm_exit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C with a non-input pane focused arms confirm-exit, like the inputs.

    Regression: the staged "press ctrl-c again to exit" gutter only fired from a
    focused input; on the detail scroll (a non-input widget) the first ctrl-c
    quit outright with no warning. ``action_smart_quit`` now routes through the
    same arm-then-confirm flow.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        app.screen.show_detail(record)
        await pilot.pause()
        app.screen._detail_scroll.focus()
        await pilot.pause()
        assert getattr(app.focused, "id", None) == "detail-scroll"
        assert app.screen._has_active_actions() is False
        await pilot.press("ctrl+c")  # non-input focus -> arm, do not quit
        await pilot.pause()
        assert app.is_running
        assert app.screen._confirm_exit_pending is True
        assert app.screen._ctrlc_gutter.has_class("-shown")
        await pilot.press("ctrl+c")  # second press within the window -> exit
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.slow
async def test_find_input_ctrl_c_clears_then_closes_bar(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C in the find input clears the query, then closes the bar (never quits)."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, record, pilot)
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        app.screen._detail_find_input.focus()
        await pilot.pause()
        await pilot.press("ctrl+c")  # query present -> clear, bar stays open
        await pilot.pause()
        assert app.screen._detail_find_input.value == ""
        assert app.screen._detail_find_active is True
        assert app.is_running
        await pilot.press("ctrl+c")  # empty -> close the bar (not quit)
        await pilot.pause()
        assert app.screen._detail_find_active is False
        assert app.screen._detail_find_input.display is False
        assert app.is_running


@pytest.mark.slow
async def test_detail_find_scrolls_wrapped_match_into_view(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scroll-to-match brings a match on a wrapped line into the viewport.

    A logical newline count would land the match far above the viewport when
    long lines wrap; the wrap-aware row computation puts it on screen.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    body = "\n".join(
        ["x" * 220 for _ in range(6)] + ["a needle to find"] + ["y" * 220 for _ in range(6)],
    )
    record = _ui_record(agentgrep, tmp_path / "wrap.jsonl", body, "wrap")
    async with app.run_test(size=(140, 24)) as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        app.screen.show_detail(record)
        await pilot.pause()
        app.screen.action_open_detail_find()
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        await pilot.pause()
        scroll = app.screen._detail_scroll
        # The match's true visual row (read off the rendered wrap cache) lies in
        # the scrolled viewport; a logical-line count would land it off-screen.
        app.screen._detail._render_content()
        rows = [
            i
            for i, strip in enumerate(app.screen._detail._render_cache.lines)
            if "needle" in strip.text
        ]
        assert rows, "match should be in the rendered output"
        viewport = range(int(scroll.scroll_y), int(scroll.scroll_y) + scroll.size.height)
        assert any(row in viewport for row in rows)


@pytest.mark.slow
async def test_detail_find_keeps_json_syntax_colors(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Find on a JSON body keeps syntax token colors and layers the find highlight."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    body = '{"role": "user", "needle": "a", "items": [{"needle": "b"}], "x": "no"}'
    record = _ui_record(agentgrep, tmp_path / "j.jsonl", body, "j")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        app.screen.show_detail(record)
        await pilot.pause()
        # The find source is the pretty-printed (multiline) JSON, so offsets and
        # matches line up with what is displayed.
        assert "\n" in app.screen._detail_find_source
        app.screen.action_open_detail_find()
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        await pilot.pause()
        assert len(app.screen._detail_find_matches) == 2
        body_text = _static_content(app.screen._detail).renderables[1]
        styles = {str(span.style) for span in body_text.spans}
        assert any("on " in s for s in styles)  # find-match background spans
        assert any(s and "on " not in s and s != "none" for s in styles)  # JSON token colors


@pytest.mark.slow
async def test_ctrl_j_from_filter_focuses_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-J`` while the filter input has focus moves focus to the results list."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("ctrl+j")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


@pytest.mark.slow
async def test_ctrl_l_from_results_focuses_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-L`` from the results list moves focus rightward to the detail pane."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"


@pytest.mark.slow
async def test_ctrl_h_from_detail_focuses_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-H`` from the detail pane moves focus leftward to the results list."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        app.screen._detail_scroll.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        await pilot.press("ctrl+h")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


@pytest.mark.slow
async def test_ctrl_k_from_results_focuses_filter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-K`` from the results list moves focus up to the filter input."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


@pytest.mark.slow
async def test_ctrl_k_from_detail_focuses_filter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-K`` from the detail pane jumps focus all the way back to the filter."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        app.screen._detail_scroll.focus()
        await pilot.pause()
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


@pytest.mark.slow
async def test_backspace_from_detail_focuses_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backspace aliases ``Ctrl-H`` in many terminals — should focus results from detail."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        app.screen._detail_scroll.focus()
        await pilot.pause()
        await pilot.press("backspace")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


@pytest.mark.slow
async def test_backspace_in_filter_still_deletes_a_character(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backspace alias must NOT steal backspace from the filter input."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("a")
        await pilot.press("b")
        await pilot.press("c")
        await pilot.pause()
        assert app.screen._filter_input.value == "abc"
        await pilot.press("backspace")
        await pilot.pause()
        # Backspace deleted the last character; focus stayed on filter.
        assert app.screen._filter_input.value == "ab"
        assert app.focused.id == "filter"


@pytest.mark.slow
async def test_ctrl_h_from_filter_is_a_noop(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-H`` on the filter does nothing (no pane to the left)."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("ctrl+h")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


@pytest.mark.slow
async def test_up_on_empty_filter_releases_focus_to_search(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain ``up`` on an empty filter input lifts focus to the top search bar."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "search"


@pytest.mark.slow
async def test_up_on_filter_with_cursor_at_start_releases_focus_to_search(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``up`` on a non-empty filter whose cursor is at position 0 still escapes upward."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        # Type something, then move cursor back to start.
        app.screen._filter_input.value = "abc"
        app.screen._filter_input.cursor_position = 0
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "search"


class FocusDetailRevealCase(t.NamedTuple):
    """One width scenario for ``right``/``l`` focusing the detail pane."""

    test_id: str
    size: tuple[int, int]
    expect_opened: bool


FOCUS_DETAIL_REVEAL_CASES: tuple[FocusDetailRevealCase, ...] = (
    FocusDetailRevealCase(
        test_id="wide-records-explicit-focus", size=(120, 24), expect_opened=True
    ),
    FocusDetailRevealCase(test_id="narrow-opens-on-focus", size=(80, 24), expect_opened=True),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    FOCUS_DETAIL_REVEAL_CASES,
    ids=[case.test_id for case in FOCUS_DETAIL_REVEAL_CASES],
)
async def test_right_on_empty_filter_focuses_and_opens_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: FocusDetailRevealCase,
) -> None:
    """``right`` on an empty filter focuses the detail — opening it when stacked.

    On a narrow terminal the detail starts collapsed (``display: none``);
    focusing it must reveal it first, not move focus into a hidden pane.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=case.size) as pilot:
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        assert app.screen._filter_input.value == ""
        await pilot.press("right")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        assert not app.screen._detail_column.has_class("-collapsed")
        # Explicit detail focus records the user's reader intent even when
        # wide mode already has the pane visible.
        assert app.screen._detail_opened is case.expect_opened


class DetailFocusResizeCase(t.NamedTuple):
    """One explicit detail-focus route before a wide-to-narrow resize."""

    test_id: str
    key: str


DETAIL_FOCUS_RESIZE_CASES: tuple[DetailFocusResizeCase, ...] = (
    DetailFocusResizeCase(test_id="l-from-results", key="l"),
    DetailFocusResizeCase(test_id="right-from-results", key="right"),
    DetailFocusResizeCase(test_id="ctrl-l-from-results", key="ctrl+l"),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    DETAIL_FOCUS_RESIZE_CASES,
    ids=[case.test_id for case in DETAIL_FOCUS_RESIZE_CASES],
)
async def test_explicit_wide_detail_focus_survives_narrow_resize(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: DetailFocusResizeCase,
) -> None:
    """Explicit reader focus in wide mode remains visible after stacking."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._apply_responsive_layout()
        app.screen._results.focus()
        await pilot.pause()

        await pilot.press(case.key)
        await pilot.pause()
        assert app.screen._stacked is False
        assert app.focused is not None and app.focused.id == "detail-scroll"
        assert app.screen._detail_opened is True

        await pilot.resize_terminal(80, 24)
        await pilot.pause(0.1)
        assert app.screen._stacked is True
        assert app.screen._detail_opened is True
        assert not app.screen._detail_column.has_class("-collapsed")
        assert app.focused is not None and app.focused.id == "detail-scroll"


@pytest.mark.slow
async def test_l_from_results_opens_stacked_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing ``l`` in the results list opens + focuses the stacked detail."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._apply_responsive_layout()
        await pilot.pause()
        assert app.screen._detail_column.has_class("-collapsed")
        app.screen._results.focus()
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        assert not app.screen._detail_column.has_class("-collapsed")
        assert app.screen._detail_opened is True


class FocusDetailRenderCase(t.NamedTuple):
    """One explicit-detail focus scenario and the record it should render."""

    test_id: str
    highlighted: int | None
    expected_index: int


FOCUS_DETAIL_RENDER_CASES: tuple[FocusDetailRenderCase, ...] = (
    FocusDetailRenderCase(
        test_id="no-highlight-falls-back-to-first-record",
        highlighted=None,
        expected_index=0,
    ),
    FocusDetailRenderCase(
        test_id="highlighted-record-wins",
        highlighted=2,
        expected_index=2,
    ),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    FOCUS_DETAIL_RENDER_CASES,
    ids=[case.test_id for case in FOCUS_DETAIL_RENDER_CASES],
)
async def test_focus_detail_renders_record_when_opening_stacked_streaming_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: FocusDetailRenderCase,
) -> None:
    """Opening a stacked streaming result renders a readable detail body."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"prefix\nVISIBLEPROBE record {idx}\nsuffix",
        )
        for idx in range(3)
    ]
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        app.screen._results.append_records(records)
        if case.highlighted is not None:
            # Seed Textual's reactive storage directly so this case can
            # model a highlighted row without dispatching the same genuine
            # cursor-move event that normally opens the stacked detail.
            app.screen._results._reactive_highlighted = case.highlighted
            app.screen._current_detail_record = records[0]
            app.screen._detail_opened = False
        app.screen._apply_responsive_layout()
        await pilot.pause()
        assert app.screen._detail_column.has_class("-collapsed")
        app.screen._results.focus()
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        expected = records[case.expected_index]
        assert app.focused is not None and app.focused.id == "detail-scroll"
        assert app.screen._current_detail_record is expected
        assert not app.screen._detail_column.has_class("-collapsed")
        # Records open at the top now (per-record scroll memory), so in the
        # short stacked viewport the matched body line sits below the metadata
        # header — scroll down to bring it into view before asserting it renders.
        app.screen._detail_scroll.scroll_end(animate=False)
        await pilot.pause()
        screenshot = app.export_screenshot(simplify=True)
        assert "VISIBLEPROBE" in screenshot
        assert f"record&#160;{case.expected_index}" in screenshot


@pytest.mark.slow
async def test_detail_focus_membership_uses_ids_maintained_at_all_mutation_seams(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current-detail visibility is O(1) after reset, append, and replace."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    first, second = _seed_records(agentgrep, tmp_path, 2)
    iteration_error = "current-detail membership scanned the result list"

    class NoIdentityIteration(list[t.Any]):
        def __iter__(self) -> t.NoReturn:
            raise AssertionError(iteration_error)

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._results.append_records((first,))
        app.screen._reset_search_chrome()
        assert app.screen._results.contains_record(first) is False

        await app.screen._apply_records_batch((first, second), total=2)
        assert app.screen._results.contains_record(first) is True
        assert app.screen._results.contains_record(second) is True

        app.screen.on_filter_completed(_filter_completed(app, [second]))
        assert app.screen._results.contains_record(first) is False
        assert app.screen._results.contains_record(second) is True

        app.screen._results._reactive_highlighted = None
        app.screen._current_detail_record = second
        app.screen.filtered_records = NoIdentityIteration((second,))
        assert app.screen._record_for_detail_focus() is second


class AutohighlightQueueCase(t.NamedTuple):
    """One filter-result scenario for queued programmatic highlights."""

    test_id: str
    record_count: int
    matching_count: int
    initial_highlighted: int | None
    expect_programmatic: int


AUTOHIGHLIGHT_QUEUE_CASES: tuple[AutohighlightQueueCase, ...] = (
    AutohighlightQueueCase(
        test_id="streamed-results-without-highlight",
        record_count=3,
        matching_count=3,
        initial_highlighted=None,
        expect_programmatic=0,
    ),
    AutohighlightQueueCase(
        test_id="empty-leaves-it-disarmed",
        record_count=3,
        matching_count=0,
        initial_highlighted=None,
        expect_programmatic=0,
    ),
    AutohighlightQueueCase(
        test_id="single-clamp-highlight",
        record_count=3,
        matching_count=2,
        initial_highlighted=2,
        expect_programmatic=1,
    ),
    AutohighlightQueueCase(
        test_id="far-clamp-is-one-programmatic-move",
        record_count=10,
        matching_count=5,
        initial_highlighted=9,
        expect_programmatic=1,
    ),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    AUTOHIGHLIGHT_QUEUE_CASES,
    ids=[case.test_id for case in AUTOHIGHLIGHT_QUEUE_CASES],
)
async def test_filter_completion_marks_only_model_highlights_programmatic(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: AutohighlightQueueCase,
) -> None:
    """Only an existing cursor emits a programmatic model-change highlight."""
    from agentgrep.ui.widgets import ResultHighlighted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, case.record_count)
    messages: dict[int, ResultHighlighted] = {}

    def capture(message: object) -> None:
        if isinstance(message, ResultHighlighted) and message.programmatic:
            messages[id(message)] = message

    async with app.run_test(size=(80, 24), message_hook=capture) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        app.screen._results.append_records(records)
        if case.initial_highlighted is not None:
            app.screen._results._reactive_highlighted = case.initial_highlighted
        app.screen.on_filter_completed(
            _filter_completed(
                app,
                records[: case.matching_count],
            ),
        )
        await pilot.pause()
        assert len(messages) == case.expect_programmatic


class FilterUserMoveCase(t.NamedTuple):
    """One filter path and the first genuine cursor move after it."""

    test_id: str
    record_count: int
    matching_count: int
    initial_highlighted: int | None
    first_user_key: str


FILTER_USER_MOVE_CASES: tuple[FilterUserMoveCase, ...] = (
    FilterUserMoveCase(
        test_id="streamed-results-without-highlight",
        record_count=3,
        matching_count=3,
        initial_highlighted=None,
        first_user_key="j",
    ),
    FilterUserMoveCase(
        test_id="narrowing-keeps-highlight-index",
        record_count=3,
        matching_count=2,
        initial_highlighted=0,
        first_user_key="j",
    ),
    FilterUserMoveCase(
        test_id="single-clamp-highlight-is-programmatic",
        record_count=3,
        matching_count=2,
        initial_highlighted=2,
        first_user_key="k",
    ),
    FilterUserMoveCase(
        test_id="multi-clamp-highlights-are-programmatic",
        record_count=10,
        matching_count=5,
        initial_highlighted=9,
        first_user_key="k",
    ),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    FILTER_USER_MOVE_CASES,
    ids=[case.test_id for case in FILTER_USER_MOVE_CASES],
)
async def test_filter_completion_does_not_swallow_first_real_cursor_move(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: FilterUserMoveCase,
) -> None:
    """Only queued programmatic highlights may keep stacked detail collapsed."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, case.record_count)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        app.screen._results.append_records(records)
        if case.initial_highlighted is not None:
            app.screen._results._reactive_highlighted = case.initial_highlighted
        app.screen._detail_opened = False
        app.screen._apply_responsive_layout()
        app.screen._results.focus()
        await pilot.pause()

        app.screen.on_filter_completed(
            _filter_completed(
                app,
                records[: case.matching_count],
            ),
        )
        await pilot.pause()
        await pilot.pause()
        assert app.screen._detail_opened is False
        assert app.screen._detail_column.has_class("-collapsed")

        await pilot.press(case.first_user_key)
        await pilot.pause()
        assert app.screen._detail_opened is True
        assert not app.screen._detail_column.has_class("-collapsed")


@pytest.mark.slow
async def test_filter_completion_keeps_detail_on_unchanged_cursor_index(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing the record under a stable cursor also replaces its detail."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._results.highlighted = 1
        await pilot.pause()

        matching = [records[2], records[4]]
        app.screen.on_filter_completed(_filter_completed(app, matching))
        await pilot.pause()

        assert app.screen._results.highlighted == 1
        assert app.screen._current_detail_record is records[4]


@pytest.mark.slow
async def test_filter_completion_adopts_worker_model_without_iteration(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pump adopts worker-prepared lists and identity indexes in O(1)."""
    from agentgrep.ui.widgets import FilterCompleted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    iteration_error = "prepared filter records were scanned on the pump"

    class PreparedRecords(list[t.Any]):
        def __iter__(self) -> t.NoReturn:
            raise AssertionError(iteration_error)

    prepared = PreparedRecords(records)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen.on_filter_completed(
            FilterCompleted(
                text="",
                records=prepared,
                record_ids={id(record) for record in records},
                generation=app.screen._filter_generation,
                records_generation=app.screen._records_generation,
            ),
        )

        assert app.screen.filtered_records is prepared
        assert app.screen._results.uses_records(prepared)
        assert app.screen._results.contains_record(records[2])


@pytest.mark.slow
async def test_filter_completion_drops_superseded_filter_generation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older same-text filter worker cannot replace the current model."""
    from agentgrep.ui.widgets import FilterCompleted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    first, second = _seed_records(agentgrep, tmp_path, 2)
    async with app.run_test(size=(120, 24)):
        app.screen.filtered_records = [first]
        _set_result_records(app.screen._results, [first])
        completion = FilterCompleted(
            text="",
            records=[second],
            record_ids={id(second)},
            generation=app.screen._filter_generation - 1,
            records_generation=app.screen._records_generation,
        )

        app.screen.on_filter_completed(completion)

        assert app.screen.filtered_records == [first]
        assert app.screen._results.contains_record(first)
        assert not app.screen._results.contains_record(second)


@pytest.mark.slow
async def test_filter_completion_retries_after_streamed_records(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker snapshot cannot replace records streamed after it started."""
    from agentgrep.ui.widgets import FilterCompleted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    first, second = _seed_records(agentgrep, tmp_path, 2)
    async with app.run_test(size=(120, 24)):
        await app.screen._apply_records_batch((first,), total=1)
        stale_revision = app.screen._records_generation
        completion = FilterCompleted(
            text="",
            records=[first],
            record_ids={id(first)},
            generation=app.screen._filter_generation,
            records_generation=stale_revision,
        )
        await app.screen._apply_records_batch((second,), total=2)
        retries: list[str] = []
        monkeypatch.setattr(app.screen, "filter_loaded", retries.append)

        app.screen.on_filter_completed(completion)

        assert app.screen.filtered_records == [first, second]
        assert app.screen._results.contains_record(second)
        assert retries == [""]


@pytest.mark.slow
async def test_filter_retry_supersedes_inflight_stream_projection(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry cannot duplicate chunks left in a superseded batch projection."""
    from agentgrep.ui import _runtime
    from agentgrep.ui.widgets import FilterCompleted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    entered_yield = asyncio.Event()
    release_yield = asyncio.Event()

    async def pause_between_chunks() -> None:
        entered_yield.set()
        await release_yield.wait()

    monkeypatch.setattr(_runtime, "_sleep_zero", pause_between_chunks)
    async with app.run_test(size=(120, 24)) as pilot:
        records = _seed_records(
            agentgrep,
            tmp_path,
            app.screen._APPLY_CHUNK_SIZE * 2 + 2,
        )
        stale_completion = FilterCompleted(
            text="",
            records=[],
            record_ids=set(),
            generation=app.screen._filter_generation,
            records_generation=app.screen._records_generation,
        )
        apply_task = asyncio.create_task(
            app.screen._apply_records_batch(records, total=len(records)),
        )
        await asyncio.wait_for(entered_yield.wait(), timeout=2)

        app.screen.on_filter_completed(stale_completion)
        async with asyncio.timeout(2):
            while app.screen._results.option_count != len(records):
                await pilot.pause()

        release_yield.set()
        await apply_task

        assert len(app.screen.filtered_records) == len(records)
        assert app.screen._results.option_count == len(records)
        assert len({id(record) for record in app.screen.filtered_records}) == len(records)


@pytest.mark.slow
async def test_right_on_non_empty_filter_moves_cursor(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``right`` on a non-empty filter walks the cursor — does not release focus."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        app.screen._filter_input.value = "abc"
        app.screen._filter_input.cursor_position = 0
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        # Focus stays on the filter; cursor advances by one.
        assert app.focused is not None and app.focused.id == "filter"
        assert app.screen._filter_input.cursor_position == 1


@pytest.mark.slow
async def test_search_results_list_append_under_load(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Appending 1000 records to the results list completes within a generous bound.

    Smoke test against accidental O(N²) regressions in the virtual model update.
    The row renderables themselves remain lazy and are covered separately.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(1000)
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        start = time.monotonic()
        app.screen._results.append_records(records)
        elapsed = time.monotonic() - start
        await pilot.pause()
        assert len(app.screen._results._records) == 1000
        assert elapsed < 2.0, f"append_records(1000) took {elapsed:.3f}s; expected < 2.0s"


@pytest.mark.slow
async def test_set_records_narrowing_preserves_order(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A narrowing filter swaps the model without eagerly rebuilding rows."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(10)
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._results.append_records(records)
        await pilot.pause()
        rendered = 0
        original_build = app.screen._results._build_row

        def counting_build(record: t.Any) -> t.Any:
            nonlocal rendered
            rendered += 1
            return original_build(record)

        monkeypatch.setattr(app.screen._results, "_build_row", counting_build)
        _set_result_records(app.screen._results, records[:7])
        assert rendered == 0
        await pilot.pause()
        assert len(app.screen._results._records) == 7
        assert [id(r) for r in app.screen._results._records] == [id(r) for r in records[:7]]


@pytest.mark.slow
async def test_set_records_widening_preserves_order(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Widening publishes the complete requested order without Option materialization."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(5)
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._results.append_records(records[:3])
        await pilot.pause()
        _set_result_records(app.screen._results, records)
        await pilot.pause()
        assert len(app.screen._results._records) == 5
        assert [id(r) for r in app.screen._results._records] == [id(r) for r in records]


@pytest.mark.slow
async def test_apply_records_batch_yields_between_chunks(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Applying a large batch yields to the event loop every chunk_size records."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    chunk = app.get_default_screen()._APPLY_CHUNK_SIZE
    # Three chunks worth — should yield twice (between chunk 0/1 and 1/2).
    record_count = chunk * 3
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(record_count)
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        sleep_calls = 0
        real_sleep = asyncio.sleep

        async def counting_sleep(delay: float) -> None:
            nonlocal sleep_calls
            if delay == 0:
                sleep_calls += 1
            await real_sleep(delay)

        monkeypatch.setattr(asyncio, "sleep", counting_sleep)
        await app.screen._apply_records_batch(records, record_count)
        assert sleep_calls >= 2, (
            f"expected >= 2 yields for {record_count} records in chunks of {chunk}, "
            f"got {sleep_calls}"
        )
        assert len(app.screen._results._records) == record_count


@pytest.mark.slow
async def test_apply_records_batch_filters_off_pump_in_bounded_chunks(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming filter projection stays off-pump, bounded, and ordered."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 401)
    pump_thread = threading.get_ident()
    match_threads: list[int] = []
    worker_chunks: list[int] = []
    repr_error = "worker description rendered matcher data"

    class EvenMatcher:
        def __repr__(self) -> t.NoReturn:
            raise AssertionError(repr_error)

        def matches(self, record: t.Any) -> bool:
            match_threads.append(threading.get_ident())
            return int(record.path.stem.removeprefix("r")) % 2 == 0

    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen._filter_matcher = EvenMatcher()
        app.screen._filter_generation += 1
        original_run_worker = app.screen.run_worker

        def capture_worker(work: t.Any, **kwargs: t.Any) -> t.Any:
            if kwargs.get("group") == "stream-filter":
                assert kwargs.get("description") == "match streamed records"
                worker_chunks.append(len(work.args[-1]))
            return original_run_worker(work, **kwargs)

        monkeypatch.setattr(app.screen, "run_worker", capture_worker)
        await app.screen._apply_records_batch(records, total=len(records))

        expected = records[::2]
        assert worker_chunks == [200, 200, 1]
        assert match_threads
        assert all(thread_id != pump_thread for thread_id in match_threads)
        assert app.screen.filtered_records == expected
        assert app.screen._results._records == expected


def test_stream_filter_chunks_bound_body_work(
    tmp_path: pathlib.Path,
) -> None:
    """Worker slices also cap projected body characters, not only rows."""
    from agentgrep.ui._streaming import (
        _STREAM_FILTER_MAX_TEXT_CHARS,
        _stream_filter_chunks,
    )

    records = tuple(
        _agentgrep_module.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{index}",
            text=text,
        )
        for index, text in enumerate(
            ("x" * _STREAM_FILTER_MAX_TEXT_CHARS, "y", ""),
        )
    )

    chunks = tuple(
        _stream_filter_chunks(
            records,
            max_records=200,
            max_chars=_STREAM_FILTER_MAX_TEXT_CHARS,
        ),
    )

    assert tuple(map(len, chunks)) == (1, 2)


@pytest.mark.slow
async def test_apply_records_batch_drops_stale_worker_projection(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filter change cannot publish a worker slice from the old matcher."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _seed_records(agentgrep, tmp_path, 1)[0]
    worker_started = threading.Event()
    release_worker = threading.Event()

    class BlockingMatcher:
        def matches(self, _record: t.Any) -> bool:
            worker_started.set()
            assert release_worker.wait(timeout=2)
            return True

    async with app.run_test(size=(120, 24)):
        app.screen._filter_matcher = BlockingMatcher()
        app.screen._filter_generation += 1
        apply_task = asyncio.create_task(
            app.screen._apply_records_batch((record,), total=1),
        )
        assert await asyncio.to_thread(worker_started.wait, 2)

        app.screen._filter_generation += 1
        app.screen._filter_matcher = None
        release_worker.set()
        await apply_task

        assert app.screen.all_records == [record]
        assert app.screen.filtered_records == []
        assert app.screen._results.option_count == 0


@pytest.mark.slow
async def test_stream_filter_worker_does_not_hold_message_dispatch(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked filter slice cannot hold keystrokes behind its pump callback."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _seed_records(agentgrep, tmp_path, 1)[0]
    worker_started = threading.Event()
    release_worker = threading.Event()

    class BlockingMatcher:
        def matches(self, _record: t.Any) -> bool:
            worker_started.set()
            assert release_worker.wait(timeout=2)
            return True

    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen._filter_matcher = BlockingMatcher()
        app.screen._filter_generation += 1
        batch = agentgrep.StreamingRecordsBatch(records=(record,), total=1)
        apply_task = asyncio.create_task(
            asyncio.to_thread(
                app.call_from_thread,
                app.screen._apply_streaming_event,
                app.screen._chrome_generation,
                batch,
            ),
        )
        assert await asyncio.to_thread(worker_started.wait, 2)

        try:
            await asyncio.wait_for(pilot.press("a"), timeout=1)
            assert app.screen._search_input.value == "a"
        finally:
            release_worker.set()
            await apply_task


@pytest.mark.slow
async def test_set_records_majority_removal_clamps_cursor_once(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large narrowing clamps the global cursor with one programmatic move."""
    from agentgrep.ui.widgets import ResultHighlighted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    messages: list[ResultHighlighted] = []

    def capture(message: object) -> None:
        if isinstance(message, ResultHighlighted):
            messages.append(message)

    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(10)
    ]
    async with app.run_test(message_hook=capture) as pilot:
        await pilot.pause()
        app.screen._results.append_records(records)
        app.screen._results._reactive_highlighted = 9
        await pilot.pause()
        messages.clear()
        result = _set_result_records(app.screen._results, records[:2])
        await pilot.pause()
        assert result is None
        generation = app.screen._results.generation
        current = {
            id(message): (message.index, message.programmatic)
            for message in messages
            if message.generation == generation
        }
        assert list(current.values()) == [(1, True)]
        assert app.screen._results.highlighted == 1
        assert len(app.screen._results._records) == 2
