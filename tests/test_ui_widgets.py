"""Isolation tests for the extracted widgets.

The payoff of moving the widgets out of the ``build_streaming_ui_app`` closure
into ``ui/widgets/*.py`` modules: each widget is a plain class that can be
imported and exercised without booting the whole app. These tests construct the
widgets directly (no ``run_test`` Pilot) and assert their pure behavior.
"""

from __future__ import annotations

import pathlib
import typing as t

from rich.cells import cell_len
from textual.widgets import Input, OptionList, Static

from agentgrep.progress import FilterRequestedPayload, ProgressSnapshot
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui.format import phase_label
from agentgrep.ui.widgets import (
    CompletionDropdown,
    DetailScroll,
    FilterInput,
    FilterRequested,
    MeterWidget,
    PaneHeader,
    ResultsHeader,
    ResultsScrollChanged,
    SearchingPanel,
    SearchInput,
    SearchResultsList,
    SpinnerWidget,
)


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
    snapshot = ResultsScrollChanged(cursor=2, total=10, percent=20)
    assert (snapshot.cursor, snapshot.total, snapshot.percent) == (2, 10, 20)


def test_results_list_is_optionlist_subclass_starting_empty() -> None:
    """The results list is an OptionList subclass starting empty."""
    assert issubclass(SearchResultsList, OptionList)
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


def test_results_header_scanning_shows_truthful_indeterminate_status() -> None:
    """An active header shows source facts and heartbeat, never a percentage.

    ``begin()`` is skipped on purpose: it arms a Textual ``auto_refresh``
    timer that needs a running event loop. This exercises the pure ``_payload``
    seam; the timer lifecycle is covered by the app-level integration test.
    """
    header = ResultsHeader("results", id="results-header")
    header.set_snapshot(
        _snapshot("scanning", current=42, total=68, source_records_seen=128),
    )
    header.set_matches("180 matches")
    payload = header._payload(60).plain
    assert "Scanning" in payload
    assert "source 42 of 68" in payload
    assert "128 records" in payload
    assert "▰" not in payload
    assert "%" not in payload
    assert "180 matches" not in payload  # match count hidden while scanning


def test_results_header_heartbeat_advances_with_fixed_source() -> None:
    """The heartbeat advances while one expensive source remains active."""
    header = ResultsHeader("results", id="results-header")
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


def test_results_header_narrow_keeps_source_without_fake_progress() -> None:
    """Narrow chrome keeps the source ordinal and sheds the heartbeat first."""
    header = ResultsHeader("results", id="results-header")
    header.set_snapshot(
        _snapshot("scanning", current=3, total=82, source_records_seen=128),
    )

    payload = header._payload(18).plain

    assert cell_len(payload) <= 18
    assert "3/82" in payload
    assert "128" not in payload
    assert "▰" not in payload
    assert "%" not in payload


def test_results_header_source_does_not_disappear_as_width_grows() -> None:
    """Growing narrow chrome never lets the phase crowd out source identity."""
    header = ResultsHeader("results", id="results-header")
    header.set_snapshot(
        _snapshot("scanning", current=3, total=82, source_records_seen=128),
    )

    payloads = {avail: header._payload(avail).plain for avail in (10, 12, 18)}

    assert all(cell_len(payload) <= avail for avail, payload in payloads.items())
    assert all("3/82" in payload for payload in payloads.values())
    assert "Scanning" in payloads[18]


def test_results_header_curates_prefiltering_phase() -> None:
    """The header uses the curated 'Filtering' word, never raw 'prefiltering'."""
    header = ResultsHeader("results", id="results-header")
    header.set_snapshot(_snapshot("prefiltering"))
    payload = header._payload(60).plain
    assert "Filtering" in payload
    assert "Prefiltering" not in payload


def test_planning_counts_are_not_labeled_as_sources() -> None:
    """Candidate-plan counts never masquerade as an active source ordinal."""
    snapshot = _snapshot("planning", current=7, total=10)
    header = ResultsHeader("results", id="results-header")
    header.set_snapshot(snapshot)
    panel = SearchingPanel(id="searching-panel")
    panel.set_snapshot(snapshot)

    header_text = header._payload(60).plain
    panel_text = panel.render().plain

    assert "Planning" in header_text
    assert "Planning" in panel_text
    assert "source" not in header_text
    assert "source" not in panel_text


def test_results_header_idle_stays_a_plain_rule() -> None:
    """With no search active the header is still the bare ``─results`` rule."""
    header = ResultsHeader("results", id="results-header")
    text = header.render()
    assert text.plain.startswith("─results")
    assert "Scanning" not in text.plain


def test_results_header_complete_drops_glyph_and_word() -> None:
    """A completed scan reads as a full 100%% bar — no ✓ glyph and no 'Done' word."""
    header = ResultsHeader("results", id="results-header")
    header.set_snapshot(_snapshot("scanning", current=3, total=5, source_records_seen=10))
    header.freeze("complete")
    payload = header._payload(60).plain
    assert "100%" in payload
    assert "▰" in payload
    assert "✓" not in payload
    assert "Done" not in payload
    assert "Scanning" not in payload  # the verb drops once frozen


def test_results_header_interrupted_and_error_keep_a_marker() -> None:
    """Stopped/error aren't self-evident from the bar, so they keep a marker."""
    stopped = ResultsHeader("results", id="results-header")
    stopped.set_snapshot(_snapshot("scanning", current=3, total=5, source_records_seen=10))
    stopped.freeze("interrupted")
    stopped_payload = stopped._payload(60).plain
    assert "■" in stopped_payload
    assert "Stopped" in stopped_payload
    assert "%" not in stopped_payload

    errored = ResultsHeader("results", id="results-header")
    errored.freeze("error", message="bad query")
    payload = errored._payload(60).plain
    assert "✗" in payload
    assert "bad query" in payload


def test_results_header_early_interruption_is_explicit() -> None:
    """Stopping before the first snapshot still renders a textual outcome."""
    header = ResultsHeader("results", id="results-header")
    header.freeze("interrupted")

    payload = header._payload(18).plain

    assert "Stopped" in payload
    assert "▰" not in payload
    assert "%" not in payload


def test_results_header_shows_match_count_only_once_finished() -> None:
    """The match/cursor count appears after the scan, never during it."""
    header = ResultsHeader("results", id="results-header")
    header.set_snapshot(_snapshot("scanning", current=3, total=5, source_records_seen=10))
    header.set_matches("3/180")
    assert "3/180" not in header._payload(60).plain  # hidden while scanning
    header.freeze("complete")
    assert "3/180" in header._payload(60).plain  # shown once finished


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


async def test_results_streamed_row_is_pinned(
    snapshot,
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
    # cast to Any for run_test/query_one, mirroring the test_agentgrep.py pattern.
    app = t.cast("t.Any", build_streaming_ui_app(tmp_path, _make_query(), control=SearchControl()))
    async with app.run_test():
        results = app.screen.query_one(SearchResultsList)
        results.append_records([_make_record()])
        assert results.option_count == 1
        assert results._render_record(_make_record()).plain == snapshot


async def test_filter_rebuild_reuses_cached_row_renders(tmp_path: pathlib.Path) -> None:
    """A full rebuild reuses cached row renders rather than re-rendering them.

    ``_render_record`` dominates the filter re-apply cost, and the rows were
    already rendered during streaming, so a rebuild must reuse them by record id
    instead of rebuilding every ``Text`` (the filter-widen pump stall, #1).
    """
    from agentgrep.progress import SearchControl
    from agentgrep.ui.app import build_streaming_ui_app

    app = t.cast("t.Any", build_streaming_ui_app(tmp_path, _make_query(), control=SearchControl()))
    async with app.run_test():
        results = app.screen.query_one(SearchResultsList)
        records = [_make_record(f"row {i}") for i in range(8)]
        results.set_records(records)  # initial render populates the row cache
        before = [results.get_option_at_index(i).prompt for i in range(results.option_count)]
        results._rebuild_options(records)  # a filter widen rebuilds the whole list
        after = [results.get_option_at_index(i).prompt for i in range(results.option_count)]
        assert results.option_count == len(records)
        assert all(a is b for a, b in zip(before, after, strict=True))


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
