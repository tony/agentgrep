"""Functional tests for the legacy Textual status and rendering surface."""

from __future__ import annotations

import importlib
import json
import pathlib
import time
import typing as t

import pytest

from agentgrep.ui._source_diagnostics import (
    SourceScanFinished,
    SourceScanStarted,
    UiProgressSnapshot,
)
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


def test_scroll_percent_returns_full_when_nothing_scrolls() -> None:
    """A pane that fits its viewport reports ``100%`` (tig convention)."""
    from agentgrep.ui.format import scroll_percent

    assert scroll_percent(0.0, 0.0) == 100


def test_scroll_percent_clamps_to_bounds() -> None:
    """Scroll percent is clamped to ``[0, 100]`` even for nonsense inputs."""
    from agentgrep.ui.format import scroll_percent

    assert scroll_percent(0.0, 100.0) == 0
    assert scroll_percent(50.0, 100.0) == 50
    assert scroll_percent(100.0, 100.0) == 100
    # Overshoot past max — clamped to 100.
    assert scroll_percent(500.0, 100.0) == 100
    # Negative scroll — clamped to 0.
    assert scroll_percent(-10.0, 100.0) == 0


@pytest.mark.slow
async def test_results_status_right_shows_position_or_count(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The results rule combines item position/count with list scroll percent.

    Before a cursor exists the bare match count renders; the denominator
    carries the count afterwards, so the two never appear together. Both
    numeric fields keep a stable width while their values advance.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        # No streaming results yet — empty right slot regardless of args.
        assert app.screen._format_results_right(cursor=None, visible=None, percent=100) == ""
        # Seed streaming totals so the match count segment renders.
        app.screen.all_records.extend(_seed_records(agentgrep, tmp_path, 10))
        # No cursor yet — bare match count.
        assert (
            app.screen._format_results_right(cursor=None, visible=10, percent=100)
            == "10 matches  100%"
        )
        # A local filter owns this rule, so its visible count wins over the
        # larger unfiltered search total.
        assert (
            app.screen._format_results_right(cursor=None, visible=4, percent=100)
            == "4 matches  100%"
        )
        assert (
            app.screen._format_results_right(cursor=None, visible=0, percent=100)
            == "0 matches  100%"
        )
        # Cursor at row 0 of all 10 — position plus list scroll percentage.
        assert app.screen._format_results_right(cursor=0, visible=10, percent=0) == " 1/10    0%"


@pytest.mark.slow
async def test_detail_statusline_shows_path_and_scroll_percent(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``show_detail`` populates the detail status line with path + scroll %."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("[red]x[/red]"),
        text="hello",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        updates: list[str] = []
        real_update = app.screen._detail_statusline.update

        def spy(content: t.Any = "", *args: t.Any, **kwargs: t.Any) -> None:
            updates.append(str(content))
            real_update(content, *args, **kwargs)

        monkeypatch.setattr(app.screen._detail_statusline, "update", spy)
        app.screen.show_detail(record)
        await pilot.pause()
        # Latest update should carry both the path's basename and a trailing ``%``.
        rendered = updates[-1] if updates else ""
        assert "[red]x[/red]" in rendered
        assert rendered.rstrip().endswith("%")
        assert "[red]x[/red]" in str(app.screen._detail_statusline.render())


@pytest.mark.slow
async def test_results_scroll_changed_updates_status_right(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app handler updates the results rule when cursor or scroll changes."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 40)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        updates: list[str] = []
        real_set = app.screen._results_header.set_right

        def spy(text: str) -> None:
            updates.append(text)
            real_set(text)

        monkeypatch.setattr(app.screen._results_header, "set_right", spy)
        # Pre-seed streaming records so the match count is non-zero.
        app.screen.all_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        # Explicitly land focus and move cursor to row 0 — the reactive
        # ``highlighted`` watcher fires on change, so set it directly.
        app.screen._results.focus()
        await pilot.pause()
        app.screen._results.highlighted = 0
        await pilot.pause()
        # The ``highlighted`` watcher posts the top position and percentage.
        assert any(u.strip().startswith("1/40") and u.endswith("0%") for u in updates), updates

        await pilot.press("G")
        await pilot.pause()

        assert app.screen._results_header._right == "40/40  100%"


@pytest.mark.slow
async def test_filter_completion_refreshes_unchanged_cursor_denominator(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Narrowing a filter refreshes ``1/N`` even when row 1 stays selected."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 10)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._results.highlighted = 0
        app.screen._refresh_results_status_right()
        await pilot.pause()
        assert "1/10" in app.screen._results_header._right

        app.screen.on_filter_completed(_filter_completed(app, records[:5]))
        await pilot.pause()

        assert app.screen._results.highlighted == 0
        assert "1/5" in app.screen._results_header._right


@pytest.mark.slow
async def test_stale_results_scroll_message_cannot_repaint_reset_rule(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued pre-reset scroll snapshot is only a live-state invalidation."""
    from agentgrep.ui.widgets import ResultsScrollChanged

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._results.highlighted = 0
        app.screen._refresh_results_status_right()
        await pilot.pause()
        assert app.screen._results_header._right

        app.screen._reset_search_chrome()
        await pilot.pause()
        assert app.screen._results_header._right == ""
        app.screen.on_results_scroll_changed(
            ResultsScrollChanged(cursor=0, total=5, percent=0),
        )

        assert app.screen._results_header._right == ""


class RightSlotCase(t.NamedTuple):
    """One position/scroll scenario for the results-status right slot."""

    test_id: str
    cursor: int | None
    visible: int
    percent: int
    expected: str


RIGHT_SLOT_CASES: tuple[RightSlotCase, ...] = (
    RightSlotCase(
        test_id="first-of-five-at-top",
        cursor=0,
        visible=5,
        percent=0,
        expected="1/5    0%",
    ),
    RightSlotCase(
        test_id="first-of-forty-pads-numerator",
        cursor=0,
        visible=40,
        percent=9,
        expected=" 1/40    9%",
    ),
    RightSlotCase(
        test_id="tenth-of-forty-keeps-width",
        cursor=9,
        visible=40,
        percent=10,
        expected="10/40   10%",
    ),
    RightSlotCase(
        test_id="last-of-forty-at-bottom",
        cursor=39,
        visible=40,
        percent=100,
        expected="40/40  100%",
    ),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    RIGHT_SLOT_CASES,
    ids=[case.test_id for case in RIGHT_SLOT_CASES],
)
async def test_results_status_right_has_stable_numeric_width(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: RightSlotCase,
) -> None:
    """Right slots keep fixed-width position and scroll fields."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen._set_empty_state(empty=False)
        await pilot.pause()
        assert (
            app.screen._format_results_right(
                case.cursor,
                case.visible,
                percent=case.percent,
            )
            == case.expected
        )


def _make_progress_snapshot(agentgrep: t.Any, **overrides: t.Any) -> t.Any:
    """Build a scanning-phase ``ProgressSnapshot`` with overridable fields."""
    fields: dict[str, t.Any] = {
        "query_label": "tmux",
        "phase": "scanning",
        "current": 5662,
        "total": 6748,
        "detail": "2176 records, 354 source matches",
        "matches": 2176,
        "elapsed": 32.0,
        "source_records_seen": 2176,
    }
    fields.update(overrides)
    return agentgrep.ProgressSnapshot(**fields)


@pytest.mark.slow
async def test_apply_progress_shows_indeterminate_source_heartbeat(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scanning snapshot shows source facts and heartbeat without a bar."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        # The folded header rule shows once results stream in; seed one so the
        # hybrid is past its centered-panel phase.
        app.screen.all_records.extend(_seed_records(agentgrep, tmp_path, 1))
        app.screen._set_empty_state(empty=False)
        app.screen._filter_header.begin()
        app.screen._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        rendered = app.screen._filter_header.render().plain
        assert "source 5662 of 6748" in rendered
        assert "2176 records" in rendered
        assert "▰" not in rendered
        assert "%" not in rendered


@pytest.mark.slow
async def test_header_indeterminate_before_total_shows_no_bar(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a source total the header shows no bar — the spinner carries motion."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        # Seed a result so the folded header rule (not the centered panel) is
        # the visible chrome whose payload we assert on.
        app.screen.all_records.extend(_seed_records(agentgrep, tmp_path, 1))
        app.screen._set_empty_state(empty=False)
        app.screen._filter_header.begin()
        app.screen._apply_progress(
            _make_progress_snapshot(
                agentgrep,
                phase="discovering",
                current=None,
                total=None,
                detail=None,
            ),
        )
        await pilot.pause()
        rendered = app.screen._filter_header.render().plain
        assert "Discovering" in rendered
        assert "▰" not in rendered
        assert "%" not in rendered


@pytest.mark.slow
async def test_ctrl_backslash_toggles_scanning_detail_row(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""``Ctrl-\`` does not duplicate the already-visible scan status."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        app.screen._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        detail_row = app.screen.query_one("#status-detail")
        assert not detail_row.has_class("visible")
        await pilot.press("ctrl+backslash")
        await pilot.pause()
        assert app.screen._detail_visible is True
        assert not detail_row.has_class("visible")
        assert app.screen._last_detail_text == ""
        await pilot.press("ctrl+backslash")
        await pilot.pause()
        assert app.screen._detail_visible is False
        assert not detail_row.has_class("visible")


@pytest.mark.slow
async def test_detail_row_does_not_label_planning_counts_as_sources(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planner-group counters stay distinct from active source ordinals."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        app.screen._apply_progress(
            _make_progress_snapshot(
                agentgrep,
                phase="planning",
                current=7,
                total=10,
                detail="candidate sources",
                source_records_seen=None,
            ),
        )
        await pilot.press("ctrl+backslash")
        await pilot.pause()

        assert app.screen._last_detail_text == ""
        assert not app.screen._detail_row.has_class("visible")


@pytest.mark.slow
async def test_detail_row_surfaces_only_a_thresholded_concurrent_source(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The expanded row ignores a fast tail and paints the true slow store."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        detail_row = app.screen._detail_row
        detail_row.begin()
        assert not detail_row.has_class("visible")

        updates: list[tuple[str, bool]] = []
        real_update = detail_row.update

        def spy(content: t.Any = "", *, layout: bool = True) -> None:
            updates.append((str(content), layout))
            real_update(content, layout=layout)

        monkeypatch.setattr(detail_row, "update", spy)
        now = time.monotonic()
        snapshot = _make_progress_snapshot(agentgrep, current=3, total=82)
        generation = app.screen._chrome_generation
        await app.screen._apply_streaming_event(
            generation,
            UiProgressSnapshot(
                snapshot=snapshot,
                lifecycle=SourceScanStarted(
                    source_id=3,
                    store="cursor-ide.state_vscdb",
                ),
            ),
        )
        for source_id in range(4, 83):
            fast_store = f"fast.store.{source_id}"
            await app.screen._apply_streaming_event(
                generation,
                UiProgressSnapshot(
                    snapshot=snapshot,
                    lifecycle=SourceScanStarted(
                        source_id=source_id,
                        store=fast_store,
                    ),
                ),
            )
            await app.screen._apply_streaming_event(
                generation,
                UiProgressSnapshot(
                    snapshot=snapshot,
                    lifecycle=SourceScanFinished(
                        source_id=source_id,
                        finished_at=now,
                    ),
                ),
            )

        await pilot.press("ctrl+backslash")
        await pilot.pause(0.55)
        assert detail_row.has_class("visible")
        assert app.screen._body.has_class("-searching")
        assert detail_row.display is True
        assert updates == [
            ("Slow source\ncursor-ide.state_vscdb · 500ms+", False),
        ]

        await app.screen._apply_streaming_event(
            generation,
            UiProgressSnapshot(
                snapshot=snapshot,
                lifecycle=SourceScanFinished(
                    source_id=3,
                    finished_at=time.monotonic(),
                ),
            ),
        )
        app.screen._apply_finished("complete", 40, 69.4, None)
        terminal, layout = updates[-1]
        assert terminal.startswith(
            "Search complete: 40 matches in 69.4s\nSlow source: cursor-ide.state_vscdb · ",
        )
        assert layout is False
        assert detail_row._sample_timer is None
        assert all("fast.store" not in content for content, _layout in updates)


@pytest.mark.slow
async def test_finished_source_selects_remaining_active_search_chrome(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed source yields the chrome to a remaining active source."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen._search_done = False
        generation = screen._chrome_generation

        for source_id in (1, 2):
            await screen._apply_streaming_event(
                generation,
                UiProgressSnapshot(
                    snapshot=_make_progress_snapshot(
                        agentgrep,
                        current=source_id,
                        total=2,
                        source_records_seen=0,
                    ),
                    lifecycle=SourceScanStarted(
                        source_id=source_id,
                        store=f"store.{source_id}",
                    ),
                ),
            )

        await screen._apply_streaming_event(
            generation,
            _make_progress_snapshot(
                agentgrep,
                current=1,
                total=2,
                source_records_seen=128,
            ),
        )
        await screen._apply_streaming_event(
            generation,
            UiProgressSnapshot(
                snapshot=_make_progress_snapshot(
                    agentgrep,
                    current=1,
                    total=2,
                    source_records_seen=128,
                ),
                lifecycle=SourceScanFinished(
                    source_id=1,
                    finished_at=time.monotonic(),
                ),
            ),
        )

        assert screen._last_snapshot.current == 2
        assert screen._filter_header._current == 2
        screen._apply_finished("interrupted", 0, 0.5, None)
        assert "source 1 of 2" not in screen._last_detail_text
        assert "while scanning source 2 of 2" in screen._last_detail_text


@pytest.mark.slow
async def test_detail_row_visibility_sticky_across_search_reset(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new search keeps the detail row visible but wipes its stale content."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        app.screen._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        await pilot.press("ctrl+backslash")
        await pilot.pause()
        assert app.screen._detail_visible is True
        app.screen._reset_search_chrome()
        await pilot.pause()
        assert app.screen._detail_visible is True
        assert app.screen._last_detail_text == ""
        assert not app.screen._detail_row.has_class("visible")


@pytest.mark.slow
async def test_finish_complete_freezes_header_to_done_text(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finishing freezes the header to ``Done`` and stops the timer."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        # Results present → the folded header rule (not the centered panel) is
        # the chrome that freezes and carries the outcome.
        app.screen.all_records.extend(_seed_records(agentgrep, tmp_path, 1))
        app.screen._set_empty_state(empty=False)
        app.screen._filter_header.begin()
        app.screen._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        app.screen._apply_finished("complete", 100, 12.3, None)
        await pilot.pause()
        header = app.screen._filter_header
        assert header._outcome == "complete"
        assert header.auto_refresh is None  # the spinner timer stopped
        rendered = header.render().plain
        assert "Done" in rendered
        assert "%" not in rendered
        assert "▰" not in rendered
        assert "▱" not in rendered
        assert "✓" not in rendered
        # The data summary lands in the toggleable detail row.
        assert app.screen._last_detail_text == "Search complete: 100 matches in 12.3s"


class FinishOutcomeCase(t.NamedTuple):
    """One post-search outcome scenario for the filter header."""

    test_id: str
    size: tuple[int, int]
    outcome: str
    glyph: str  # the frozen marker stored on the widget
    marker: str
    seed_scanning: bool


FINISH_OUTCOME_CASES: tuple[FinishOutcomeCase, ...] = (
    FinishOutcomeCase(
        test_id="complete-wide-done-no-bar",
        size=(160, 24),
        outcome="complete",
        glyph="✓",
        marker="Done",
        seed_scanning=True,
    ),
    FinishOutcomeCase(
        test_id="complete-narrow-done-no-bar",
        size=(40, 24),
        outcome="complete",
        glyph="✓",
        marker="Done",
        seed_scanning=True,
    ),
    FinishOutcomeCase(
        test_id="interrupted-wide-stopped-no-bar",
        size=(160, 24),
        outcome="interrupted",
        glyph="■",
        marker="■",
        seed_scanning=True,
    ),
    FinishOutcomeCase(
        # Interrupted before the first scanning snapshot: explicit stopped text,
        # no fabricated fraction or bar.
        test_id="interrupted-no-scan-square-no-bar",
        size=(160, 24),
        outcome="interrupted",
        glyph="■",
        marker="■",
        seed_scanning=False,
    ),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    FINISH_OUTCOME_CASES,
    ids=[case.test_id for case in FINISH_OUTCOME_CASES],
)
async def test_finish_outcome_freezes_header_glyph(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: FinishOutcomeCase,
) -> None:
    """The frozen filter header carries every outcome as bounded text."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=case.size) as pilot:
        await pilot.pause()
        # Reveal + lay out the chrome so the header has a real width before the
        # narrow/wide payload is computed.
        app.screen._set_empty_state(empty=False)
        await pilot.pause()
        app.screen._search_done = False
        app.screen.all_records.extend(_seed_records(agentgrep, tmp_path, 5))
        app.screen._filter_header.begin()
        if case.seed_scanning:
            app.screen._apply_progress(_make_progress_snapshot(agentgrep))
            await pilot.pause()
        app.screen._apply_finished(case.outcome, 100, 12.3, None)
        await pilot.pause()
        header = app.screen._filter_header
        assert header._outcome == case.outcome
        assert header._final_glyph == case.glyph
        rendered = header.render().plain
        assert case.marker in rendered
        assert "▰" not in rendered
        assert "▱" not in rendered
        assert "%" not in rendered
        if case.outcome == "interrupted":
            assert "Stopped" in rendered
            assert "%" not in rendered


@pytest.mark.slow
async def test_detail_row_shows_summary_after_finish(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Toggling the detail row after a finished search shows the data summary."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        app.screen._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        app.screen._apply_finished("interrupted", 2976, 2.1, None)
        await pilot.pause()
        await pilot.press("ctrl+backslash")
        await pilot.pause()
        assert app.screen._detail_visible is True
        assert app.screen._last_detail_text == (
            "Stopped at 2976 matches while scanning source 5662 of 6748 in 2.1s"
        )


@pytest.mark.slow
async def test_interrupted_planning_summary_omits_source_counts(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopping during planning never presents group counters as sources."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        app.screen._apply_progress(
            _make_progress_snapshot(
                agentgrep,
                phase="planning",
                current=7,
                total=10,
                detail="candidate sources",
                source_records_seen=None,
            ),
        )
        app.screen._apply_finished("interrupted", 0, 0.5, None)
        await pilot.pause()

        assert app.screen._last_detail_text == "Stopped at 0 matches in 0.5s"


@pytest.mark.slow
async def test_header_snapshot_setter_does_not_repaint(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """During a search, set_snapshot stores heartbeat state without repainting.

    The 2 Hz spinner timer drives the header, so thousands of per-source
    progress events never thrash the rule with extra refreshes.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        header = app.screen._filter_header
        header.begin()  # arms the self-refresh timer (drives repaints)
        refreshes: list[None] = []
        real_refresh = header.refresh

        def spy(*args: t.Any, **kwargs: t.Any) -> t.Any:
            refreshes.append(None)
            return real_refresh(*args, **kwargs)

        monkeypatch.setattr(header, "refresh", spy)
        header.set_snapshot(_make_progress_snapshot(agentgrep, source_records_seen=128))
        header.set_snapshot(_make_progress_snapshot(agentgrep, source_records_seen=256))
        assert refreshes == []  # setters store only; the timer repaints


class StaleGenerationCase(t.NamedTuple):
    """One generation-gate scenario for ``_apply_streaming_event``."""

    test_id: str
    use_current_generation: bool
    expect_applied: bool


STALE_GENERATION_CASES: tuple[StaleGenerationCase, ...] = (
    StaleGenerationCase(
        test_id="current-generation-applies",
        use_current_generation=True,
        expect_applied=True,
    ),
    StaleGenerationCase(
        test_id="stale-generation-dropped",
        use_current_generation=False,
        expect_applied=False,
    ),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    STALE_GENERATION_CASES,
    ids=[case.test_id for case in STALE_GENERATION_CASES],
)
async def test_streaming_events_gated_by_generation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: StaleGenerationCase,
) -> None:
    """Events from a cancelled worker's generation never touch the chrome.

    A cancelled worker keeps draining its queued events after the user
    starts a new search; the un-gated form repainted the new search's
    chrome with stale "Stopped" states and old bar fills.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        stale_generation = app.screen._chrome_generation
        # A new search bumps the generation; the old reporter's events
        # still carry the previous one.
        app.screen._reset_search_chrome()
        await pilot.pause()
        generation = (
            app.screen._chrome_generation if case.use_current_generation else stale_generation
        )
        await app.screen._apply_streaming_event(generation, _make_progress_snapshot(agentgrep))
        await pilot.pause()
        assert (app.screen._last_snapshot is not None) is case.expect_applied
        assert (app.screen._filter_header._current is not None) is case.expect_applied


@pytest.mark.slow
async def test_streaming_records_batch_lands_in_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A records batch routed through the generation gate populates the list.

    Regression guard: the records handler is a coroutine — the gate must
    await it, not drop the un-awaited coroutine on the floor (which left
    the results list silently empty).
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        batch = agentgrep.StreamingRecordsBatch(records=tuple(records), total=3)
        await app.screen._apply_streaming_event(app.screen._chrome_generation, batch)
        await pilot.pause()
        assert len(app.screen.all_records) == 3
        assert len(app.screen._results._records) == 3


@pytest.mark.slow
async def test_narrow_header_keeps_source_without_bar(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below the breakpoint the header keeps source state without fake percent."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(40, 24)) as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        await pilot.pause()
        app.screen._search_done = False
        app.screen.all_records.extend(_seed_records(agentgrep, tmp_path, 5))
        app.screen._filter_header.begin()
        app.screen._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        rendered = app.screen._filter_header.render().plain
        assert "5662/6748" in rendered
        assert "▰" not in rendered
        assert "%" not in rendered


class SplitOrientationCase(t.NamedTuple):
    """One terminal-width scenario for the responsive detail split."""

    test_id: str
    size: tuple[int, int]
    expect_stacked: bool


SPLIT_ORIENTATION_CASES: tuple[SplitOrientationCase, ...] = (
    SplitOrientationCase(test_id="wide-side-by-side", size=(120, 24), expect_stacked=False),
    SplitOrientationCase(test_id="narrow-stacked", size=(80, 24), expect_stacked=True),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    SPLIT_ORIENTATION_CASES,
    ids=[case.test_id for case in SPLIT_ORIENTATION_CASES],
)
async def test_body_stacks_below_split_breakpoint(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: SplitOrientationCase,
) -> None:
    """The body flips to a stacked layout below 100 cols, side-by-side above."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=case.size) as pilot:
        await pilot.pause()
        assert app.screen._stacked is case.expect_stacked
        assert app.screen._body.has_class("-stacked") is case.expect_stacked


@pytest.mark.slow
async def test_narrow_detail_opens_on_user_selection_not_autohighlight(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stacked detail stays collapsed until a genuine cursor move (tig-style)."""
    from agentgrep.ui.widgets import ResultHighlighted

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
        # Narrow + nothing opened → detail collapsed.
        assert app.screen._stacked is True
        assert app.screen._detail_column.has_class("-collapsed")
        # The programmatic row-0 highlight must NOT open it.
        app.screen.on_result_highlighted(
            ResultHighlighted(
                record=records[0],
                index=0,
                generation=app.screen._results.generation,
                programmatic=True,
            ),
        )
        await pilot.pause()
        assert app.screen._detail_opened is False
        assert app.screen._detail_column.has_class("-collapsed")
        # A real cursor move opens it and keeps it open.
        app.screen.on_result_highlighted(
            ResultHighlighted(
                record=records[1],
                index=1,
                generation=app.screen._results.generation,
                programmatic=False,
            ),
        )
        await pilot.pause()
        assert app.screen._detail_opened is True
        assert not app.screen._detail_column.has_class("-collapsed")


@pytest.mark.slow
async def test_clicking_programmatically_highlighted_row_opens_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Click intent opens stacked detail even when the cursor value is unchanged."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._results._reactive_highlighted = 2
        app.screen.on_filter_completed(_filter_completed(app, records[:1]))
        await pilot.pause()

        assert app.screen._results.highlighted == 0
        assert app.screen._detail_column.has_class("-collapsed")

        clicked = await pilot.click(app.screen._results, offset=(4, 0))
        await pilot.pause()

        assert clicked is True
        assert app.screen._detail_opened is True
        assert not app.screen._detail_column.has_class("-collapsed")


@pytest.mark.slow
async def test_stale_result_highlight_cannot_open_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued highlight from an older model is rejected by generation."""
    from agentgrep.ui.widgets import ResultHighlighted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 2)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._detail_opened = False

        app.screen.on_result_highlighted(
            ResultHighlighted(
                record=records[0],
                index=0,
                generation=app.screen._results.generation - 1,
                programmatic=True,
            ),
        )
        await pilot.pause()

        assert app.screen._detail_opened is False
        assert app.screen._current_detail_record is not records[0]


@pytest.mark.slow
async def test_wide_detail_always_visible(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Side-by-side keeps the detail pane visible regardless of selection."""
    from agentgrep.ui.widgets import ResultHighlighted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._apply_responsive_layout()
        await pilot.pause()
        assert app.screen._stacked is False
        # Visible before any selection.
        assert app.screen._detail_opened is False
        assert not app.screen._detail_column.has_class("-collapsed")
        # ...and still visible after a genuine selection (the "regardless
        # of selection" property the docstring promises).
        app.screen.on_result_highlighted(
            ResultHighlighted(
                record=records[0],
                index=0,
                generation=app.screen._results.generation,
                programmatic=False,
            ),
        )
        await pilot.pause()
        assert not app.screen._detail_column.has_class("-collapsed")


@pytest.mark.slow
async def test_responsive_layout_classes_stay_orthogonal_to_detail_zoom(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Responsive recomputation leaves logical zoom and collapse state independent."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 2)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._detail_opened = False
        app.screen._apply_responsive_layout()

        app.screen._search_input.value = "/maximize detail"
        app.screen._search_input.focus()
        await pilot.press("enter")
        await pilot.pause()
        app.screen._apply_responsive_layout()
        await pilot.pause()

        assert app.screen._body.has_class("-zoom-detail")
        assert app.screen._body.has_class("-stacked")
        assert app.screen._detail_column.has_class("-collapsed")
        assert app.screen._detail_opened is False


@pytest.mark.slow
async def test_new_search_recollapses_narrow_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_reset_search_chrome`` re-collapses the stacked detail pane."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._detail_opened = True
        app.screen._apply_responsive_layout()
        await pilot.pause()
        assert not app.screen._detail_column.has_class("-collapsed")
        app.screen._reset_search_chrome()
        await pilot.pause()
        assert app.screen._detail_opened is False
        assert app.screen._detail_column.has_class("-collapsed")


@pytest.mark.slow
async def test_stacked_focus_routes_results_and_detail_vertically(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When stacked, ctrl+j reaches the detail below and ctrl+k returns up."""
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
        app.screen._results.focus()
        await pilot.pause()
        # Down from results opens + focuses the detail below.
        app.screen.action_focus_pane_down()
        await pilot.pause()
        assert app.screen._detail_opened is True
        assert app.focused is not None and app.focused.id == "detail-scroll"
        # Up from the detail returns to the results.
        app.screen.action_focus_pane_up()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


def test_format_compact_path_passes_short_paths_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Paths that already fit the width budget are returned unchanged."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(agentgrep.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    short = tmp_path / "a" / "b.txt"
    assert agentgrep.format_compact_path(short, max_width=80) == "~/a/b.txt"


def test_format_compact_path_middle_elides_long_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Long paths get a ``…/`` middle elide, preserving the hidden-dir root."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(agentgrep.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    long_path = tmp_path / ".codex" / "sessions" / "2024" / "02" / "14" / "uuid.jsonl"
    result = agentgrep.format_compact_path(long_path, max_width=30)
    assert result == "~/.codex/…/14/uuid.jsonl"
    assert len(result) <= 30


def test_format_compact_path_drops_root_when_tight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """When even the rooted elide doesn't fit, drop the root: ``…/parent/file``."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(agentgrep.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    long_path = tmp_path / ".codex" / "sessions" / "2024" / "02" / "14" / "verylongfilename.jsonl"
    result = agentgrep.format_compact_path(long_path, max_width=20)
    # Either tier-2 (root dropped) or tier-3 (filename only) — whichever fits.
    assert len(result) <= 20
    assert "verylongfilename" in result or "…" in result


def test_truncate_lines_passes_short_text_through() -> None:
    """Short text is returned unchanged."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "a\nb\nc"
    assert agentgrep.truncate_lines(text, max_lines=10) == text


def test_truncate_lines_appends_overflow_marker() -> None:
    """Long text is truncated and a ``+N more`` marker is appended."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "\n".join(f"line {i}" for i in range(50))
    result = agentgrep.truncate_lines(text, max_lines=5)
    assert result.startswith("line 0\nline 1\nline 2\nline 3\nline 4\n")
    assert "(+45 more lines)" in result


def test_truncate_lines_caps_single_line_by_characters() -> None:
    """A newline-free body cannot bypass the detail rendering budget."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    cap = agentgrep.DETAIL_BODY_MAX_CHARS
    text = "x" * (cap + 1000)
    result = agentgrep.truncate_lines(
        text,
        max_lines=agentgrep.DETAIL_BODY_MAX_LINES,
        max_chars=cap,
    )
    assert result.startswith("x" * cap)
    assert result.endswith("… (more content)")
    assert len(result) < len(text)


@pytest.mark.slow
async def test_show_detail_caps_single_line_at_max_chars(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``show_detail`` bounds one huge line before any Rich rendering."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    cap = agentgrep.DETAIL_BODY_MAX_CHARS
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="x" * (cap + 1000),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        assert app.screen._detail_body_text.endswith("… (more content)")
        assert len(app.screen._detail_body_text) < len(record.text)


@pytest.mark.slow
async def test_show_detail_caps_body_at_max_lines(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``show_detail`` caps the body so giant records render instantly.

    The body is now wrapped in a ``VerticalScroll`` so the cap is a generous
    sanity bound (default 1000 lines), not the visible-height. Test the cap.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    cap = agentgrep.DETAIL_BODY_MAX_LINES
    huge_body = "\n".join(f"body line {i}" for i in range(cap + 1000))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text=huge_body,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        # The compatibility helper returns the Group passed to ``update()``;
        # for this plain-text body, its body renderable is a ``Text``.
        group = _static_content(app.screen._detail)
        body_text = next(
            item
            for item in group.renderables
            if hasattr(item, "plain") and "body line" in item.plain
        )
        assert "more lines" in body_text.plain
        assert body_text.plain.count("body line") == cap


def test_format_timestamp_tig_renders_iso_with_offset_in_local_tz() -> None:
    """ISO inputs with explicit offsets are localized to the system timezone."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    result = agentgrep.format_timestamp_tig("2026-05-17T11:59:12+00:00")
    # Shape: ``YYYY-MM-DD HH:MM ±HHMM`` (22 chars)
    assert len(result) == 22
    assert result[4] == "-" and result[7] == "-"
    assert result[10] == " "
    assert result[13] == ":"
    assert result[16] == " "
    assert result[17] in {"+", "-"}


def test_format_timestamp_tig_renders_zulu_input() -> None:
    """``Z`` suffix is treated as ``+00:00`` (Python's ``fromisoformat`` requires the swap)."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    result = agentgrep.format_timestamp_tig("2026-05-17T11:59:12Z")
    assert len(result) == 22


def test_format_timestamp_tig_returns_empty_string_for_missing_input() -> None:
    """``None`` / empty inputs render as the empty string so callers can pad."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.format_timestamp_tig(None) == ""
    assert agentgrep.format_timestamp_tig("") == ""


def test_format_timestamp_tig_falls_back_to_raw_on_parse_error() -> None:
    """Unparseable inputs return the original string clipped to 22 chars."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.format_timestamp_tig("not-an-iso-timestamp") == "not-an-iso-timestamp"
    # Long unparseable input is clipped.
    long_input = "this-is-not-a-timestamp-but-it-is-too-long-anyway"
    assert agentgrep.format_timestamp_tig(long_input) == long_input[:22]


def test_find_first_match_line_returns_index_of_first_match() -> None:
    """Returns the line index of the first matching line; case-insensitive by default."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "alpha\nbeta\nFOO bar\nbaz"
    assert agentgrep.find_first_match_line(text, ("foo",)) == 2
    assert agentgrep.find_first_match_line(text, ("foo",), case_sensitive=True) is None
    assert agentgrep.find_first_match_line(text, ("FOO",), case_sensitive=True) == 2
    assert agentgrep.find_first_match_line("", ("foo",)) is None
    assert agentgrep.find_first_match_line(text, ()) is None
    # Regex mode
    assert agentgrep.find_first_match_line(text, (r"b\w+",), regex=True) == 1


def test_find_first_match_line_skips_malformed_regex() -> None:
    """Malformed regex patterns are silently skipped; valid siblings still match."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "alpha\nbeta gamma\ndelta"
    # ``[`` is unbalanced; should be ignored. ``gamma`` should still match.
    assert agentgrep.find_first_match_line(text, ("[", "gamma"), regex=True) == 1


def test_highlight_matches_styles_each_occurrence() -> None:
    """``highlight_matches`` adds a styled span for every occurrence of every term."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_text = agentgrep.highlight_matches("foo foo bar", ("foo",))
    # Two spans for two occurrences.
    assert sum(1 for span in rich_text.spans if "bold yellow" in str(span.style)) == 2


def test_highlight_matches_combines_terms() -> None:
    """Multiple terms each get their own styled spans."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_text = agentgrep.highlight_matches("alpha beta alpha gamma", ("alpha", "gamma"))
    styled = [str(span.style) for span in rich_text.spans if "bold yellow" in str(span.style)]
    assert len(styled) == 3  # 2 alpha + 1 gamma


@pytest.mark.slow
async def test_show_detail_memoizes_body_formatting(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-rendering the same record + query reuses the cached body renderable."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    json_body = '{"alpha": 1, "beta": 2, "gamma": 3}'
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "j.jsonl",
        text=json_body,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        # Replace json.loads so a real cache miss would explode loudly.
        load_calls = 0
        real_loads = json.loads

        def counting_loads(*args: t.Any, **kwargs: t.Any) -> t.Any:
            nonlocal load_calls
            load_calls += 1
            return real_loads(*args, **kwargs)

        monkeypatch.setattr(json, "loads", counting_loads)
        app.screen.show_detail(record)
        await pilot.pause()
        assert load_calls == 0, "JSON should not be re-parsed for the same record + query"


@pytest.mark.slow
async def test_reset_search_chrome_invalidates_detail_caches(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starting a new search clears any stale detail-pane caches."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "x.jsonl",
        text='{"x": 1}',
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        assert len(app.screen._detail_body_cache) >= 1
        app.screen._reset_search_chrome()
        assert len(app.screen._detail_body_cache) == 0
        assert len(app.screen._detail_scroll_positions) == 0


@pytest.mark.slow
async def test_detail_scroll_memory(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New records open at the top; revisiting a record restores its scroll."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    big = "\n".join(f"line {i}" for i in range(200))

    def _record(name: str) -> t.Any:
        return agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"{name}.jsonl",
            text=big,
        )

    rec_a, rec_b = _record("a"), _record("b")
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        # A fresh record opens at the top.
        app.screen.show_detail(rec_a)
        await pilot.pause()
        assert app.screen._detail_scroll.scroll_y == 0
        # Scroll down — the position is remembered for rec_a.
        app.screen._detail_scroll.scroll_to(y=20, animate=False)
        await pilot.pause()
        # A different, never-seen record opens at the top.
        app.screen.show_detail(rec_b)
        await pilot.pause()
        assert app.screen._detail_scroll.scroll_y == 0
        # Returning to rec_a restores its remembered scroll.
        app.screen.show_detail(rec_a)
        await pilot.pause()
        assert app.screen._detail_scroll.scroll_y > 0


def test_detect_content_format_recognizes_json() -> None:
    """``detect_content_format`` returns ``"json"`` for parseable JSON objects/arrays."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format('{"a": 1, "b": 2}') == "json"
    assert agentgrep.detect_content_format("[1, 2, 3]") == "json"
    # Whitespace + pretty-printed JSON.
    assert agentgrep.detect_content_format('  {\n  "x": 1\n}') == "json"


def test_detect_content_format_falls_back_to_text_for_malformed_json() -> None:
    """A leading ``{`` that doesn't parse falls through to ``"text"``, not ``"json"``."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format('{"missing": ') == "text"
    assert agentgrep.detect_content_format("{not even json}") == "text"


def test_detect_content_format_falls_back_for_excessive_json_depth() -> None:
    """A deeply nested JSON-looking body cannot overflow format detection."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    nested = "[" * 50000 + "0" + "]" * 1000
    assert agentgrep.detect_content_format(nested) == "text"


def test_detect_content_format_recognizes_markdown() -> None:
    """ATX headings and fenced code blocks at line-start trip markdown mode."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format("# Heading\n\nbody") == "markdown"
    assert agentgrep.detect_content_format("intro\n\n## Subhead\n\nrest") == "markdown"
    assert agentgrep.detect_content_format("intro\n\n```python\nprint(1)\n```") == "markdown"


def test_detect_content_format_leans_false_negative_for_weak_markdown() -> None:
    """Bullet-style or inline-bold chat content is intentionally NOT classified as markdown."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    # A chat message starting with "- " should keep its match highlight.
    assert agentgrep.detect_content_format("- not really markdown") == "text"
    # Inline **bold** alone isn't enough either.
    assert agentgrep.detect_content_format("plain message with **emphasis** inline") == "text"


def test_detect_content_format_handles_empty_and_plain_text() -> None:
    """Empty body and plain chat prose both return ``"text"``."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format("") == "text"
    assert agentgrep.detect_content_format("just a plain prompt") == "text"
    assert agentgrep.detect_content_format("multi\nline\nplain\nbody") == "text"


@pytest.mark.slow
async def test_show_detail_renders_json_with_syntax(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON record body produces a ``Syntax`` renderable in the detail Group."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_syntax = importlib.import_module("rich.syntax")
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "json.jsonl",
        text='{"alpha": 1, "beta": "two"}',
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        rendered = _static_content(app.screen._detail)
        renderables = list(rendered.renderables)
        assert any(isinstance(item, rich_syntax.Syntax) for item in renderables)


@pytest.mark.slow
async def test_light_theme_selects_light_syntax_for_detail_renderers(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON, Markdown code, and JSON find share the light Rich syntax theme."""
    from agentgrep.ui import theme as ui_theme
    from agentgrep.ui.layouts import hud

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    json_record = _ui_record(agentgrep, tmp_path / "json.jsonl", '{"alpha": 1}', "json")
    markdown_record = _ui_record(
        agentgrep,
        tmp_path / "markdown.jsonl",
        "# Heading\n\n```json\n{}\n```\n",
        "markdown",
    )
    syntax_themes: list[str] = []
    markdown_themes: list[str] = []
    real_syntax = hud._RichSyntax
    real_markdown = hud._RichMarkdown

    def recording_syntax(*args: t.Any, **kwargs: t.Any) -> t.Any:
        syntax_themes.append(kwargs["theme"])
        return real_syntax(*args, **kwargs)

    def recording_markdown(*args: t.Any, **kwargs: t.Any) -> t.Any:
        markdown_themes.append(kwargs["code_theme"])
        return real_markdown(*args, **kwargs)

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.theme = ui_theme.LIGHT_THEME_NAME
        await pilot.pause()
        monkeypatch.setattr(hud, "_RichSyntax", recording_syntax)
        monkeypatch.setattr(hud, "_RichMarkdown", recording_markdown)

        app.screen.show_detail(json_record)
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.screen._detail_find_base_for(app.screen._detail_find_source)
        app.screen.show_detail(markdown_record)
        await pilot.pause()

    assert syntax_themes == ["ansi_light", "ansi_light"]
    assert markdown_themes == ["ansi_light"]


@pytest.mark.slow
async def test_show_detail_renders_markdown_with_markdown(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A markdown body produces a ``Markdown`` renderable in the detail Group."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_markdown = importlib.import_module("rich.markdown")
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "md.jsonl",
        text="# Heading\n\nbody paragraph\n",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        rendered = _static_content(app.screen._detail)
        renderables = list(rendered.renderables)
        assert any(isinstance(item, rich_markdown.Markdown) for item in renderables)


@pytest.mark.slow
async def test_show_detail_keeps_text_highlighting_for_plain_body(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain bodies still get bounded literal spans for search matches."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_text_module = importlib.import_module("rich.text")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agentgrep, "run_search_query", lambda *args, **kwargs: [])
    query = agentgrep.SearchQuery(
        terms=("libtmux",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    control = agentgrep.SearchControl()
    app = agentgrep.build_streaming_ui_app(home, query, control=control)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "plain.jsonl",
        text="plain prose mentioning libtmux exactly once",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        rendered = _static_content(app.screen._detail)
        renderables = list(rendered.renderables)
        # Two Text instances: the header and the body. The body is the one
        # carrying the highlight spans (header is bold labels only).
        text_bodies = [
            item
            for item in renderables
            if isinstance(item, rich_text_module.Text) and "libtmux" in item.plain
        ]
        assert text_bodies, "expected the body Text containing 'libtmux'"
        styled = [str(span.style) for span in text_bodies[0].spans]
        # Search matches carry the theme's gold foreground token, bold.
        search_hex = app.theme_variables["ag-match-search"]
        assert any("bold" in style and search_hex in style for style in styled)


@pytest.mark.slow
async def test_show_detail_includes_record_origin_without_io(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TUI detail header surfaces origin fields already on the record."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_text_module = importlib.import_module("rich.text")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(agentgrep, "run_search_query", lambda *args, **kwargs: [])
    query = agentgrep.SearchQuery(
        terms=("origin",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    control = agentgrep.SearchControl()
    app = agentgrep.build_streaming_ui_app(home, query, control=control)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=home / ".codex" / "sessions" / "rollout.jsonl",
        text="plain origin detail",
        origin=agentgrep.RecordOrigin(
            cwd=str(home / "work" / "agentgrep"),
            branch="project-context",
        ),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        rendered = _static_content(app.screen._detail)
        header = next(
            item
            for item in rendered.renderables
            if isinstance(item, rich_text_module.Text) and "Agent:" in item.plain
        )

    assert "Cwd: ~/work/agentgrep/" in header.plain
    assert "Branch: project-context" in header.plain
