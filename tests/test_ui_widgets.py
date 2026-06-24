"""Isolation tests for the extracted widgets.

The payoff of moving the widgets out of the ``build_streaming_ui_app`` closure
into ``ui/widgets/*.py`` modules: each widget is a plain class that can be
imported and exercised without booting the whole app. These tests construct the
widgets directly (no ``run_test`` Pilot) and assert their pure behavior.
"""

from __future__ import annotations

from textual.widgets import Input, OptionList, Static

from agentgrep.progress import FilterRequestedPayload
from agentgrep.ui.widgets import (
    CompletionDropdown,
    FilterInput,
    FilterRequested,
    MeterWidget,
    PaneHeader,
    ResultsScrollChanged,
    SearchInput,
    SearchResultsList,
    SpinnerWidget,
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
