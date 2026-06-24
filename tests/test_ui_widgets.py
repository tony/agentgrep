"""Isolation tests for the extracted widgets.

The payoff of moving the widgets out of the ``build_streaming_ui_app`` closure
into ``ui/widgets/*.py`` modules: each widget is a plain class that can be
imported and exercised without booting the whole app. These tests construct the
widgets directly (no ``run_test`` Pilot) and assert their pure behavior.
"""

from __future__ import annotations

import re
import time

from textual.widgets import Input, OptionList, Static

from agentgrep.progress import FilterRequestedPayload, ProgressSnapshot
from agentgrep.ui.format import phase_label
from agentgrep.ui.widgets import (
    CompletionDropdown,
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


def _snapshot(
    phase: str,
    *,
    current: int | None = None,
    total: int | None = None,
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


def test_results_header_renders_phase_word_left_of_bar() -> None:
    """An active scanning header shows the verb + N/M count before the bar.

    ``begin()`` is skipped on purpose: it arms a Textual ``auto_refresh``
    timer that needs a running event loop. These tests exercise the pure
    ``_payload`` render seam; the timer lifecycle is covered by the app-level
    integration test.
    """
    header = ResultsHeader("results", id="results-header")
    header.set_progress(0.61, "scanning", "42/68")
    payload = header._payload(60).plain
    assert "Scanning" in payload
    assert "42/68" in payload


def test_results_header_curates_prefiltering_phase() -> None:
    """The header uses the curated 'Filtering' word, never raw 'prefiltering'."""
    header = ResultsHeader("results", id="results-header")
    header.set_progress(None, "prefiltering")
    payload = header._payload(60).plain
    assert "Filtering" in payload
    assert "Prefiltering" not in payload


def test_results_header_idle_stays_a_plain_rule() -> None:
    """With no search active the header is still the bare ``─results`` rule."""
    header = ResultsHeader("results", id="results-header")
    text = header.render()
    assert text.plain.startswith("─results")
    assert "Scanning" not in text.plain


def test_results_header_freeze_shows_outcome_word() -> None:
    """Freezing pairs the outcome glyph with the Done/Stopped/Error word."""
    for outcome, word in (("complete", "Done"), ("interrupted", "Stopped"), ("error", "Error")):
        header = ResultsHeader("results", id="results-header")
        header.freeze(outcome, message="bad query" if outcome == "error" else "", elapsed=4.1)
        assert word in header._payload(60).plain


def test_results_header_shows_elapsed_ticker() -> None:
    """The header carries a self-driven elapsed seconds token when there is room."""
    header = ResultsHeader("results", id="results-header")
    header._started_at = time.monotonic() - 3.4
    header.set_progress(0.61, "scanning", "42/68")
    assert re.search(r"\d+s", header._payload(80).plain)


def test_searching_panel_is_static_subclass() -> None:
    """The centered searching panel is a Static subclass."""
    assert issubclass(SearchingPanel, Static)


def test_searching_panel_renders_phase_verb_and_counts() -> None:
    """An active scanning panel shows the verb, the source N/M, and the match count."""
    panel = SearchingPanel(id="searching-panel")
    panel.set_snapshot(_snapshot("scanning", current=42, total=68, matches=2343))
    text = panel.render().plain
    assert "Scanning" in text
    assert "42" in text
    assert "68" in text
    assert "2343" in text


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
