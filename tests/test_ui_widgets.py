"""Isolation tests for the extracted widgets.

The payoff of moving the widgets out of the ``build_streaming_ui_app`` closure
into ``ui/widgets/*.py`` modules: each widget is a plain class that can be
imported and exercised without booting the whole app. These tests construct the
widgets directly (no ``run_test`` Pilot) and assert their pure behavior.
"""

from __future__ import annotations

import dataclasses
import pathlib
import typing as t

import pytest
from rich.cells import cell_len
from rich.style import Style
from textual.app import App, ComposeResult
from textual.scroll_view import ScrollView
from textual.widgets import Input, OptionList, Static

from agentgrep.progress import FilterRequestedPayload, ProgressSnapshot
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui._history import HistoryEntry
from agentgrep.ui.format import phase_label
from agentgrep.ui.widgets import (
    CompletionDropdown,
    DetailFindInput,
    DetailFindRequested,
    DetailScroll,
    FilterCompleted,
    FilterHeader,
    FilterInput,
    FilterRequested,
    MeterWidget,
    PaneHeader,
    ResultHighlighted,
    ResultsHeader,
    ResultsScrollChanged,
    SearchingPanel,
    SearchInput,
    SearchResultsList,
    SpinnerWidget,
)
from agentgrep.ui.widgets.history import _ROW_TEXT_MAX_CHARS, HistoryRecall
from agentgrep.ui.widgets.inputs import INPUT_MAX_LENGTH

pytestmark = pytest.mark.tui


def _make_record(text: str = "bliss") -> SearchRecord:
    """Build a minimal valid prompt record for widget tests."""
    return SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex-cli",
        adapter_id="codex",
        path=pathlib.Path("s1.jsonl"),
        text=text,
        role="user",
        session_id="s1",
    )


def _make_query(*terms: str) -> SearchQuery:
    """Build a minimal valid prompts-scope query (empty terms = browse)."""
    return SearchQuery(
        terms=tuple(terms),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )


def _set_records(results: SearchResultsList, records: t.Iterable[SearchRecord]) -> None:
    """Adopt one test-prepared result model."""
    prepared = list(records)
    results.set_records(
        prepared,
        record_ids={id(record) for record in prepared},
    )


def _snapshot(
    phase: str,
    *,
    current: int | None = None,
    total: int | None = None,
    source_records_seen: int | None = None,
    matches: int = 0,
    detail: str | None = None,
    elapsed: float = 0.0,
) -> ProgressSnapshot:
    """Build a ProgressSnapshot for a status-widget render test."""
    return ProgressSnapshot(
        query_label="q",
        phase=phase,
        current=current,
        total=total,
        detail=detail,
        matches=matches,
        elapsed=elapsed,
        source_records_seen=source_records_seen,
    )


def test_spinner_is_static_subclass_that_animates() -> None:
    """The spinner is a Static subclass rendering frames from its sequence."""
    assert issubclass(SpinnerWidget, Static)
    spinner = SpinnerWidget(id="spin")
    assert spinner.render() in SpinnerWidget._SEQUENCE


def test_spinner_freeze_locks_glyph() -> None:
    """``freeze`` locks the displayed glyph (``unfreeze`` needs a live loop)."""
    spinner = SpinnerWidget(id="spin")
    spinner.freeze("✓")
    assert spinner.render() == "✓"


def test_meter_shows_bar_logic() -> None:
    """``shows_bar`` reflects fraction / narrow state without needing a layout."""
    meter = MeterWidget(id="meter")
    assert meter.shows_bar() is False  # no fraction yet
    meter.set_progress(0.5)
    assert meter.shows_bar() is True
    meter.set_narrow(narrow=True)
    assert meter.shows_bar() is False


def test_meter_freeze_complete_fills_and_marks_done() -> None:
    """Freezing 'complete' fills the bar and adds the -done class."""
    meter = MeterWidget(id="meter")
    meter.set_progress(0.3)
    meter.freeze("complete")
    assert meter.shows_bar() is True
    assert meter.has_class("-done")


def test_messages_carry_their_payloads() -> None:
    """The message classes carry their payloads / snapshot fields."""
    event = FilterRequested(payload=FilterRequestedPayload(text="bliss"))
    assert event.payload.text == "bliss"
    record = _make_record()
    completed = FilterCompleted(
        text="bliss",
        records=[record],
        record_ids={id(record)},
        generation=2,
        records_generation=3,
    )
    assert (
        completed.text,
        completed.records,
        completed.record_ids,
        completed.generation,
        completed.records_generation,
    ) == ("bliss", [record], {id(record)}, 2, 3)
    highlighted = ResultHighlighted(
        record=record,
        index=3,
        generation=7,
        programmatic=True,
    )
    assert (
        highlighted.record,
        highlighted.index,
        highlighted.generation,
        highlighted.programmatic,
    ) == (record, 3, 7, True)
    snapshot = ResultsScrollChanged(cursor=2, total=10, percent=20)
    assert (snapshot.cursor, snapshot.total, snapshot.percent) == (2, 10, 20)


def test_results_list_is_scrollview_subclass_starting_empty() -> None:
    """The results list is a line-rendered ScrollView starting empty."""
    assert issubclass(SearchResultsList, ScrollView)
    assert not issubclass(SearchResultsList, OptionList)
    results = SearchResultsList(id="results")
    assert results._records == []
    results.append_records([])  # empty batch is a no-op, no app required


def test_completion_dropdown_remembers_target_input() -> None:
    """The dropdown is an OptionList subclass bound to its input."""
    assert issubclass(CompletionDropdown, OptionList)
    dropdown = CompletionDropdown(id="enum-dropdown", target_input_id="filter")
    assert dropdown._target_input_id == "filter"


def test_pane_header_renders_label_and_rule() -> None:
    """PaneHeader renders ``─<label><rule>``: a left label embedded in a rule.

    One leading rule cell precedes the label (the left mirror of the filter
    input's trailing cap dash); the rule then fills to the widget's width.
    """
    assert issubclass(PaneHeader, Static)
    header = PaneHeader("results", id="results-header")
    # No size before mount → leading rule cell + bold label, no fill (width clamps).
    text = header.render()
    assert text.plain == "─results"
    # The label keeps its bold weight; the leading rule cell does not.
    assert any("bold" in str(span.style) for span in text.spans)


def test_inputs_are_input_subclasses() -> None:
    """The filter/search inputs are Input subclasses."""
    assert issubclass(FilterInput, Input)
    assert issubclass(SearchInput, Input)
    assert FilterInput._DEBOUNCE_SECONDS == 0.15


def test_interactive_widgets_use_public_textual_handlers() -> None:
    """Custom widgets avoid Textual's private key and value hooks."""
    for widget_type in (CompletionDropdown, DetailFindInput, FilterInput, SearchInput):
        assert "_on_key" not in widget_type.__dict__
        assert "on_key" in widget_type.__dict__
    for widget_type in (DetailFindInput, FilterInput, SearchInput):
        assert "_watch_value" not in widget_type.__dict__
        assert "on_input_changed" in widget_type.__dict__


@pytest.mark.slow
async def test_search_input_submit_hint_tracks_nonblank_value() -> None:
    """The border affordance follows initial, typed, and loaded query text."""

    class InputHarness(App[None]):
        def compose(self) -> ComposeResult:
            yield SearchInput(id="search")
            yield SearchInput(value="ready", id="initial")

    app = InputHarness()
    async with app.run_test(size=(40, 8)) as pilot:
        search = app.query_one("#search", SearchInput)
        initial = app.query_one("#initial", SearchInput)

        assert not search.border_subtitle
        assert initial.border_subtitle == "Press [bold $accent]Enter[/bold $accent] ↵"

        search.value = "agent:claude"
        await pilot.pause()
        assert search.border_subtitle == "Press [bold $accent]Enter[/bold $accent] ↵"

        search.value = "   "
        await pilot.pause()
        assert not search.border_subtitle

        search.load_query("role:user")
        await pilot.pause()
        assert search.border_subtitle == "Press [bold $accent]Enter[/bold $accent] ↵"


@pytest.mark.slow
@pytest.mark.parametrize(
    "query",
    [
        "x" * INPUT_MAX_LENGTH,
        "界" * INPUT_MAX_LENGTH,
        "e\N{COMBINING ACUTE ACCENT}" * (INPUT_MAX_LENGTH // 2),
    ],
    ids=("ascii", "wide", "combining"),
)
async def test_search_input_submit_hint_fits_supported_minimum_width(query: str) -> None:
    """The full hint fits the existing 16-column compact boundary."""

    class InputHarness(App[None]):
        CSS = """
        Input {
            border: none;
            border-top: solid red;
            border-bottom: solid red;
            border-subtitle-align: right;
            padding: 0 1;
        }
        """

        def compose(self) -> ComposeResult:
            yield SearchInput(id="search")

    app = InputHarness()
    async with app.run_test(size=(16, 4)) as pilot:
        await pilot.pause()
        search = app.query_one("#search", SearchInput)
        assert not search.border_subtitle
        search.load_query(query)
        await pilot.pause()
        assert search.border_subtitle == "Press [bold $accent]Enter[/bold $accent] ↵"
        assert search.cursor_position == len(query)
        assert 0 <= search.cursor_screen_offset.x < 16
        update = app.screen._compositor.render_full_update()
        bottom_rule = "".join(strip.text for strip in update.strips[2])
        assert bottom_rule == "──Press Enter ↵─"


@pytest.mark.slow
async def test_inputs_bound_text_processed_on_the_pump() -> None:
    """Typed, initial, and restored input text share one finite budget."""
    oversized = "x" * (INPUT_MAX_LENGTH + 1)

    class InputHarness(App[None]):
        def compose(self) -> ComposeResult:
            yield SearchInput(value=oversized, id="search")
            yield FilterInput(id="filter")
            yield DetailFindInput(id="detail-find")

    app = InputHarness()
    async with app.run_test() as pilot:
        search = app.query_one("#search", SearchInput)
        filter_input = app.query_one("#filter", FilterInput)
        detail_find = app.query_one("#detail-find", DetailFindInput)

        assert search.max_length == INPUT_MAX_LENGTH
        assert search.value == oversized[:INPUT_MAX_LENGTH]
        assert filter_input.max_length == INPUT_MAX_LENGTH
        assert detail_find.max_length == INPUT_MAX_LENGTH

        search.value = oversized
        filter_input.value = oversized
        detail_find.value = oversized
        await pilot.pause()
        assert search.value == oversized[:INPUT_MAX_LENGTH]
        assert filter_input.value == oversized[:INPUT_MAX_LENGTH]
        assert detail_find.value == oversized[:INPUT_MAX_LENGTH]

        search.load_query(oversized)
        detail_find.load_query("first")
        detail_find.load_query(oversized)
        await pilot.pause()
        assert search.value == oversized[:INPUT_MAX_LENGTH]
        assert detail_find.value == oversized[:INPUT_MAX_LENGTH]
        assert detail_find._debounce_timer is None


@pytest.mark.slow
async def test_detail_find_restore_cancels_queued_user_request() -> None:
    """A restored value cannot leave an earlier user debounce armed."""
    seen: list[str] = []

    class InputHarness(App[None]):
        def compose(self) -> ComposeResult:
            yield DetailFindInput(id="detail-find")

        def on_detail_find_requested(self, message: DetailFindRequested) -> None:
            seen.append(message.text)

    app = InputHarness()
    async with app.run_test() as pilot:
        detail_find = app.query_one("#detail-find", DetailFindInput)
        detail_find.value = "typed-before-restore"
        detail_find.load_query("restored")
        await pilot.pause(DetailFindInput._DEBOUNCE_SECONDS * 2)

        assert detail_find.value == "restored"
        assert seen == []
        assert detail_find._debounce_timer is None


@pytest.mark.slow
async def test_history_filter_bounds_seed_text() -> None:
    """History filtering cannot restore an unbounded query onto the pump."""
    oversized = "x" * (INPUT_MAX_LENGTH + 1)
    app = App[None]()

    async with app.run_test() as pilot:
        await app.push_screen(
            HistoryRecall([HistoryEntry(oversized, 1.0)], seed=oversized),
        )
        await pilot.pause()
        history_filter = app.screen.query_one("#history-filter", Input)
        assert history_filter.max_length == _ROW_TEXT_MAX_CHARS
        assert history_filter.value == oversized[:_ROW_TEXT_MAX_CHARS]


def test_format_relative_time_units() -> None:
    """``format_relative_time`` renders a compact '<n><unit> ago' label."""
    from agentgrep.ui.format import format_relative_time

    assert format_relative_time(1000, 1000) == "just now"
    assert format_relative_time(1000, 1005) == "5s ago"
    assert format_relative_time(0, 90) == "1m ago"
    assert format_relative_time(0, 3 * 3600) == "3h ago"
    assert format_relative_time(0, 86400) == "1d ago"
    assert format_relative_time(0, 14 * 86400) == "2w ago"
    assert format_relative_time(0, 400 * 86400) == "1y ago"
    # Clock skew / future timestamps clamp to "just now" rather than negatives.
    assert format_relative_time(100, 0) == "just now"


def test_phase_label_curates_engine_jargon() -> None:
    """``phase_label`` maps engine phase strings to user-facing verbs."""
    assert phase_label("scanning") == "Scanning"
    assert phase_label("planning") == "Planning"
    assert phase_label("discovering") == "Discovering"
    # 'prefiltering' is internal jargon — curated to a word a user reads.
    assert phase_label("prefiltering") == "Filtering"
    # Unknown phases title-case rather than vanish.
    assert phase_label("widgeting") == "Widgeting"
    assert phase_label("") == ""


def test_filter_header_scanning_shows_truthful_indeterminate_status() -> None:
    """An active header shows source facts and heartbeat, never a percentage.

    ``begin()`` is skipped on purpose: it arms a Textual ``auto_refresh``
    timer that needs a running event loop. This exercises the pure ``_payload``
    seam; the timer lifecycle is covered by the app-level integration test.
    """
    header = FilterHeader("filter", id="filter-header")
    header.set_snapshot(
        _snapshot("scanning", current=42, total=68, source_records_seen=128),
    )
    payload = header._payload(60).plain
    assert "Scanning" in payload
    assert "source 42 of 68" in payload
    assert "128 records" in payload
    assert "▰" not in payload
    assert "%" not in payload


def test_filter_header_heartbeat_advances_with_fixed_source() -> None:
    """The heartbeat advances while one expensive source remains active."""
    header = FilterHeader("filter", id="filter-header")
    header.set_snapshot(
        _snapshot("scanning", current=3, total=82, source_records_seen=128),
    )
    first = header._payload(60).plain
    header.set_snapshot(
        _snapshot("scanning", current=3, total=82, source_records_seen=256),
    )
    second = header._payload(60).plain

    assert "source 3 of 82" in first
    assert "source 3 of 82" in second
    assert "128 records" in first
    assert "256 records" in second
    assert first != second


def test_filter_header_narrow_keeps_source_without_fake_progress() -> None:
    """Narrow chrome keeps the source ordinal and sheds the heartbeat first."""
    header = FilterHeader("filter", id="filter-header")
    header.set_snapshot(
        _snapshot("scanning", current=3, total=82, source_records_seen=128),
    )

    payload = header._payload(18).plain

    assert cell_len(payload) <= 18
    assert "3/82" in payload
    assert "128" not in payload
    assert "▰" not in payload
    assert "%" not in payload


def test_filter_header_source_does_not_disappear_as_width_grows() -> None:
    """Growing narrow chrome never lets the phase crowd out source identity."""
    header = FilterHeader("filter", id="filter-header")
    header.set_snapshot(
        _snapshot("scanning", current=3, total=82, source_records_seen=128),
    )

    payloads = {avail: header._payload(avail).plain for avail in (10, 12, 18)}

    assert all(cell_len(payload) <= avail for avail, payload in payloads.items())
    assert all("3/82" in payload for payload in payloads.values())
    assert "Scanning" in payloads[18]


def test_filter_header_curates_prefiltering_phase() -> None:
    """The header uses the curated 'Filtering' word, never raw 'prefiltering'."""
    header = FilterHeader("filter", id="filter-header")
    header.set_snapshot(_snapshot("prefiltering"))
    payload = header._payload(60).plain
    assert "Filtering" in payload
    assert "Prefiltering" not in payload


def test_planning_counts_are_not_labeled_as_sources() -> None:
    """Candidate-plan counts never masquerade as an active source ordinal."""
    snapshot = _snapshot("planning", current=7, total=10)
    header = FilterHeader("filter", id="filter-header")
    header.set_snapshot(snapshot)
    panel = SearchingPanel(id="searching-panel")
    panel.set_snapshot(snapshot)

    header_text = header._payload(60).plain
    panel_text = panel.render().plain

    assert "Planning" in header_text
    assert "Planning" in panel_text
    assert "source" not in header_text
    assert "source" not in panel_text


def test_filter_header_idle_stays_a_plain_rule() -> None:
    """With no search active the header is still the bare ``─filter`` rule."""
    header = FilterHeader("filter", id="filter-header")
    text = header.render()
    assert text.plain.startswith("─filter")
    assert "Scanning" not in text.plain


def test_filter_header_complete_uses_text_without_meter() -> None:
    """A completed scan says ``Done`` without reviving determinate progress."""
    header = FilterHeader("filter", id="filter-header")
    header.set_snapshot(_snapshot("scanning", current=3, total=5, source_records_seen=10))
    header.freeze("complete")
    payload = header._payload(60).plain
    assert "Done" in payload
    assert "%" not in payload
    assert "▰" not in payload
    assert "▱" not in payload
    assert "✓" not in payload
    assert "Scanning" not in payload  # the verb drops once frozen


def test_filter_header_interrupted_and_error_keep_a_marker() -> None:
    """Stopped/error remain explicit with both a glyph and text."""
    stopped = FilterHeader("filter", id="filter-header")
    stopped.set_snapshot(_snapshot("scanning", current=3, total=5, source_records_seen=10))
    stopped.freeze("interrupted")
    stopped_payload = stopped._payload(60).plain
    assert "■" in stopped_payload
    assert "Stopped" in stopped_payload
    assert "%" not in stopped_payload

    errored = FilterHeader("filter", id="filter-header")
    errored.freeze("error", message="bad query")
    payload = errored._payload(60).plain
    assert "✗" in payload
    assert "bad query" in payload


def test_filter_header_early_interruption_is_explicit() -> None:
    """Stopping before the first snapshot still renders a textual outcome."""
    header = FilterHeader("filter", id="filter-header")
    header.freeze("interrupted")

    payload = header._payload(18).plain

    assert "Stopped" in payload
    assert "▰" not in payload
    assert "%" not in payload


def test_results_header_fits_whole_navigation_variants() -> None:
    """The results rule sheds scroll percent before item position."""
    header = ResultsHeader("results", id="results-header")
    header.set_right(" 1/40    9%")

    assert header._fit_right(11) == " 1/40    9%"
    assert header._fit_right(5) == "1/40"
    assert header._fit_right(3) == ""


def test_searching_panel_is_static_subclass() -> None:
    """The centered searching panel is a Static subclass."""
    assert issubclass(SearchingPanel, Static)


def test_searching_panel_renders_phase_verb_and_counts() -> None:
    """The empty-canvas panel shows source, heartbeat, and match facts."""
    panel = SearchingPanel(id="searching-panel")
    panel.set_snapshot(
        _snapshot(
            "scanning",
            current=42,
            total=68,
            source_records_seen=128,
            matches=2343,
        ),
    )
    text = panel.render().plain
    assert "Scanning" in text
    assert "source 42 of 68" in text
    assert "128 records" in text
    assert "2343 matches" in text
    assert "\n" in text


def test_searching_panel_discovering_phase_has_a_verb() -> None:
    """The no-count discovery phase still shows a phase verb, not a bare glyph."""
    panel = SearchingPanel(id="searching-panel")
    panel.set_snapshot(_snapshot("discovering"))
    assert "Discovering" in panel.render().plain


def test_searching_panel_freeze_zero_results_says_no_matches() -> None:
    """A completed search with no results freezes the panel into a 'No matches' state."""
    panel = SearchingPanel(id="searching-panel")
    panel.set_snapshot(_snapshot("scanning", current=10, total=10, matches=0))
    panel.freeze("complete", total=0, elapsed=1.2)
    assert "No matches" in panel.render().plain


# --- ADR 0012 characterization pins (Task 0) -------------------------------


def test_results_list_constructs_empty() -> None:
    """A bare results list starts empty; an empty append early-returns (pure).

    ``append_records`` reads ``self.app`` for a non-empty batch, so only the
    empty path is exercisable without a mounted app; the rendered-row path is
    pinned by ``test_results_streamed_row_is_pinned`` against the real app.
    """
    results = SearchResultsList(id="results")
    assert results._records == []
    results.append_records([])  # early-returns before touching self.app
    assert results.option_count == 0


@pytest.mark.slow
async def test_results_streamed_row_is_pinned(
    snapshot: object,
    tmp_path: pathlib.Path,
) -> None:
    """A streamed row's rendered text is pinned against the real app theme.

    ``_render_record`` resolves ``self.app.theme_variables`` for the ``ag-*``
    palette, so the row is rendered inside the real app (empty ``tmp_path``
    home → discovery finds nothing).
    """
    from agentgrep.progress import SearchControl
    from agentgrep.ui.app import build_streaming_ui_app

    # build_streaming_ui_app returns ``object`` (app.screen.py stays Textual-free);
    # Cast to Any for run_test/query_one, mirroring the shared TUI support pattern.
    app = t.cast("t.Any", build_streaming_ui_app(tmp_path, _make_query(), control=SearchControl()))
    async with app.run_test():
        results = app.screen.query_one(SearchResultsList)
        results.append_records([_make_record()])
        assert results.option_count == 1
        assert results._render_record(_make_record()).plain == snapshot


@pytest.mark.slow
async def test_set_records_defers_row_rendering_to_visible_lines(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing a large result set builds only rows requested by the viewport."""
    from agentgrep.progress import SearchControl
    from agentgrep.ui.app import build_streaming_ui_app

    app = t.cast("t.Any", build_streaming_ui_app(tmp_path, _make_query(), control=SearchControl()))
    async with app.run_test(size=(80, 24)) as pilot:
        results = app.screen.query_one(SearchResultsList)
        app.screen._set_empty_state(empty=False)
        built = 0
        original = results._build_row

        def count_build(record: SearchRecord) -> t.Any:
            nonlocal built
            built += 1
            return original(record)

        monkeypatch.setattr(results, "_build_row", count_build)
        records = [_make_record(f"row {index}") for index in range(1_000)]

        _set_records(results, records)

        assert built == 0
        await pilot.pause()
        assert 0 < built <= results.size.height


@pytest.mark.slow
async def test_results_highlight_clamps_and_posts_typed_record(
    tmp_path: pathlib.Path,
) -> None:
    """Out-of-range cursors clamp before emitting their record and index."""
    from agentgrep.progress import SearchControl
    from agentgrep.ui.app import build_streaming_ui_app

    messages: list[ResultHighlighted] = []

    def capture(message: object) -> None:
        if isinstance(message, ResultHighlighted):
            messages.append(message)

    app = t.cast("t.Any", build_streaming_ui_app(tmp_path, _make_query(), control=SearchControl()))
    async with app.run_test(size=(80, 24), message_hook=capture) as pilot:
        results = app.screen.query_one(SearchResultsList)
        records = [_make_record(f"row {index}") for index in range(3)]
        _set_records(results, records)

        results.highlighted = -7
        await pilot.pause()
        assert results.highlighted == 0
        assert (messages[-1].record, messages[-1].index) == (records[0], 0)
        assert messages[-1].generation == results.generation
        assert messages[-1].programmatic is False

        results.highlighted = 99
        await pilot.pause()
        assert results.highlighted == 2
        assert (messages[-1].record, messages[-1].index) == (records[2], 2)
        assert messages[-1].generation == results.generation
        assert messages[-1].programmatic is False


@pytest.mark.slow
async def test_results_page_keys_match_option_list_from_no_cursor(
    tmp_path: pathlib.Path,
) -> None:
    """Page keys retain Textual's first/last behavior before selection."""
    from agentgrep.progress import SearchControl
    from agentgrep.ui.app import build_streaming_ui_app

    app = t.cast("t.Any", build_streaming_ui_app(tmp_path, _make_query(), control=SearchControl()))
    async with app.run_test(size=(80, 24)) as pilot:
        results = app.screen.query_one(SearchResultsList)
        app.screen._set_empty_state(empty=False)
        records = [_make_record(f"row {index}") for index in range(50)]
        _set_records(results, records)
        await pilot.pause()

        results.action_page_down()
        assert results.highlighted == len(records) - 1

        results.highlighted = None
        results.action_page_up()
        assert results.highlighted == 0


@pytest.mark.slow
async def test_results_hover_and_click_track_scrolled_rows(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mouse hover and click map viewport rows to the scrolled model."""
    from agentgrep.progress import SearchControl
    from agentgrep.ui.app import build_streaming_ui_app

    app = t.cast("t.Any", build_streaming_ui_app(tmp_path, _make_query(), control=SearchControl()))
    async with app.run_test(size=(80, 24)) as pilot:
        results = app.screen.query_one(SearchResultsList)
        app.screen._set_empty_state(empty=False)
        _set_records(results, [_make_record(f"row {index}") for index in range(100)])
        await pilot.pause()
        results.scroll_to(y=20, animate=False, force=True, immediate=True)
        await pilot.pause()
        assert results.scroll_offset.y > 0

        row_y = 2
        index = results.scroll_offset.y + row_y
        assert await pilot.hover(results, offset=(4, row_y)) is True
        await pilot.pause()
        assert results._hovered == index
        assert results.highlighted is None

        components: list[str] = []
        get_style = results.get_component_rich_style

        def capture_component(component: str) -> Style:
            components.append(component)
            return get_style(component)

        monkeypatch.setattr(results, "get_component_rich_style", capture_component)
        results.render_line(row_y)
        assert components[-1] == "option-list--option-hover"

        results.highlighted = index
        await pilot.pause()
        results.render_line(row_y)
        assert components[-1] == "option-list--option-highlighted"

        results.highlighted = None
        assert await pilot.click(results, offset=(4, row_y)) is True
        await pilot.pause()
        assert results.highlighted == index

        assert await pilot.hover("#filter") is True
        await pilot.pause()
        assert results._hovered is None


@pytest.mark.slow
async def test_filter_rebuild_reuses_cached_row_renders(tmp_path: pathlib.Path) -> None:
    """Replacing the model reuses cached rows rather than re-rendering them.

    ``_render_record`` dominates the filter re-apply cost, and the rows were
    already rendered during streaming, so replacement must reuse them by record id
    instead of rebuilding every ``Text`` (the filter-widen pump stall, #1).
    """
    from agentgrep.progress import SearchControl
    from agentgrep.ui.app import build_streaming_ui_app

    app = t.cast("t.Any", build_streaming_ui_app(tmp_path, _make_query(), control=SearchControl()))
    async with app.run_test():
        results = app.screen.query_one(SearchResultsList)
        records = [_make_record(f"row {i}") for i in range(8)]
        _set_records(results, records)
        before = [results._render_record(record) for record in records]
        _set_records(results, records)
        after = [results._render_record(record) for record in records]
        assert results.option_count == len(records)
        assert all(a is b for a, b in zip(before, after, strict=True))


@pytest.mark.slow
async def test_results_render_cache_evicts_oldest_rows(tmp_path: pathlib.Path) -> None:
    """Rendering beyond the row-cache limit evicts the least-recently used row."""
    from agentgrep.progress import SearchControl
    from agentgrep.ui.app import build_streaming_ui_app

    app = t.cast("t.Any", build_streaming_ui_app(tmp_path, _make_query(), control=SearchControl()))
    async with app.run_test():
        results = app.screen.query_one(SearchResultsList)
        records = [_make_record(f"row {index}") for index in range(results._RENDER_CACHE_MAX + 1)]
        for record in records[:-1]:
            results._render_record(record)
        results._render_record(records[0])
        results._render_record(records[-1])

        theme_name = str(app.theme)
        assert len(results._render_cache) == results._RENDER_CACHE_MAX
        assert (theme_name, id(records[0])) in results._render_cache
        assert (theme_name, id(records[1])) not in results._render_cache
        assert (theme_name, id(records[-1])) in results._render_cache


@pytest.mark.slow
async def test_results_line_cache_reuses_final_strip(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rendering an unchanged row reuses the completed fixed-width Strip."""
    from agentgrep.progress import SearchControl
    from agentgrep.ui.app import build_streaming_ui_app

    app = t.cast("t.Any", build_streaming_ui_app(tmp_path, _make_query(), control=SearchControl()))
    async with app.run_test(size=(80, 24)) as pilot:
        results = app.screen.query_one(SearchResultsList)
        app.screen._set_empty_state(empty=False)
        _set_records(results, [_make_record()])
        await pilot.pause()
        results._strip_cache.clear()
        render_lines_calls = 0
        original = app.console.render_lines

        def count_render_lines(*args: t.Any, **kwargs: t.Any) -> t.Any:
            nonlocal render_lines_calls
            render_lines_calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(app.console, "render_lines", count_render_lines)
        first = results.render_line(0)
        second = results.render_line(0)

        assert second is first
        assert render_lines_calls == 1


@pytest.mark.slow
async def test_results_line_cache_invalidates_render_inputs(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Theme, width, effective style, and record identity key final rows."""
    from agentgrep.progress import SearchControl
    from agentgrep.ui import theme as ui_theme
    from agentgrep.ui.app import build_streaming_ui_app

    app = t.cast("t.Any", build_streaming_ui_app(tmp_path, _make_query(), control=SearchControl()))
    async with app.run_test(size=(80, 24)) as pilot:
        results = app.screen.query_one(SearchResultsList)
        app.screen._set_empty_state(empty=False)
        first_record = _make_record("same row")
        _set_records(results, [first_record])
        await pilot.pause()
        results._strip_cache.clear()

        base_style = results.get_component_rich_style("option-list--option")
        styles = [base_style]
        monkeypatch.setattr(
            results,
            "get_component_rich_style",
            lambda _component: styles[0],
        )
        base = results.render_line(0)
        assert results.render_line(0) is base

        styles[0] = base_style + Style(reverse=True)
        restyled = results.render_line(0)
        assert restyled is not base
        assert results.render_line(0) is restyled

        app.theme = (
            ui_theme.LIGHT_THEME_NAME
            if str(app.theme) == ui_theme.DARK_THEME_NAME
            else ui_theme.DARK_THEME_NAME
        )
        await pilot.pause()
        themed = results.render_line(0)
        assert themed is not restyled

        old_width = results.size.width
        await pilot.resize_terminal(120, 24)
        await pilot.pause()
        assert results.size.width != old_width
        resized = results.render_line(0)
        assert resized is not themed

        _set_records(results, [_make_record("same row")])
        replaced = results.render_line(0)
        assert replaced is not resized


@pytest.mark.slow
async def test_results_caches_verify_record_identity_on_key_collision(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An identity-key collision cannot return another record's cached row."""
    from agentgrep.progress import SearchControl
    from agentgrep.ui.app import build_streaming_ui_app
    from agentgrep.ui.widgets import results as results_module

    app = t.cast("t.Any", build_streaming_ui_app(tmp_path, _make_query(), control=SearchControl()))
    async with app.run_test(size=(80, 24)) as pilot:
        results = app.screen.query_one(SearchResultsList)
        app.screen._set_empty_state(empty=False)
        old_record = dataclasses.replace(_make_record(), title="old cached row")
        new_record = dataclasses.replace(_make_record(), title="new live row")
        monkeypatch.setattr(results_module, "id", lambda _record: 1, raising=False)

        _set_records(results, [old_record])
        await pilot.pause()
        old_strip = results.render_line(0)

        _set_records(results, [new_record])
        new_strip = results.render_line(0)

        assert new_strip is not old_strip
        assert "new live row" in new_strip.text
        assert "old cached row" not in new_strip.text


def test_detail_scroll_is_focusable_vertical_scroll() -> None:
    """DetailScroll is a focusable VerticalScroll exposing the vim scroll keys.

    The record and per-record scroll memory live on the app today, not the
    widget (ADR 0012 moves them onto the widget in a later task); this pins the
    current widget surface so that move is an observable, intentional change.
    """
    from textual.containers import VerticalScroll

    assert issubclass(DetailScroll, VerticalScroll)
    assert DetailScroll.can_focus is True
    binding_keys = {binding[0] for binding in DetailScroll.BINDINGS}
    assert {"j", "k", "h"} <= binding_keys
