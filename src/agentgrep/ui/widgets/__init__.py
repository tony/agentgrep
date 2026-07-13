"""Textual widgets and messages for the agentgrep explorer.

These widgets and message types subclass Textual classes directly, so each
module imports Textual at the top. The package is only imported from inside
``build_streaming_ui_app`` (and the tests), never by the eager ``import
agentgrep`` path, so ADR 0010's optional-dependency rule holds; each widget is
independently unit-testable and guardable (ADR 0011).
"""

from __future__ import annotations

import logging

from agentgrep.ui.widgets.detail import DetailScroll
from agentgrep.ui.widgets.dropdown import CompletionDropdown
from agentgrep.ui.widgets.history import HistoryRecall
from agentgrep.ui.widgets.inputs import DetailFindInput, FilterInput, SearchInput
from agentgrep.ui.widgets.messages import (
    DetailFindRequested,
    DetailScrollChanged,
    FilterCompleted,
    FilterRequested,
    ResultsScrollChanged,
    SearchRequested,
)
from agentgrep.ui.widgets.results import SearchResultsList
from agentgrep.ui.widgets.status import (
    FilterHeader,
    MeterWidget,
    PaneHeader,
    ResultsHeader,
    SearchingPanel,
    SpinnerWidget,
)

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "CompletionDropdown",
    "DetailFindInput",
    "DetailFindRequested",
    "DetailScroll",
    "DetailScrollChanged",
    "FilterCompleted",
    "FilterHeader",
    "FilterInput",
    "FilterRequested",
    "HistoryRecall",
    "MeterWidget",
    "PaneHeader",
    "ResultsHeader",
    "ResultsScrollChanged",
    "SearchInput",
    "SearchRequested",
    "SearchResultsList",
    "SearchingPanel",
    "SpinnerWidget",
]
