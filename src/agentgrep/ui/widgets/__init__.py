"""Textual widgets and messages for the agentgrep explorer.

These widgets and message types subclass Textual classes directly, so each
module imports Textual at the top. The package is only imported from inside
``build_streaming_ui_app`` (and the tests), never by the eager ``import
agentgrep`` path, so ADR 0010's optional-dependency rule holds; each widget is
independently unit-testable and guardable (ADR 0011).
"""

from __future__ import annotations

import logging

from agentgrep.ui.widgets.breadcrumb import RefinementBreadcrumb
from agentgrep.ui.widgets.detail import DetailScroll
from agentgrep.ui.widgets.dropdown import CompletionDropdown
from agentgrep.ui.widgets.history import HistoryRecall
from agentgrep.ui.widgets.inputs import DetailFindInput, FilterInput, SearchInput
from agentgrep.ui.widgets.messages import (
    DetailFindRequested,
    DetailFocusRequested,
    DetailScrollChanged,
    FilterCompleted,
    FilterRequested,
    ResultHighlighted,
    ResultsScrollChanged,
    SearchRequested,
    WelcomeQuerySelected,
)
from agentgrep.ui.widgets.results import SearchResultsList
from agentgrep.ui.widgets.status import (
    FilterHeader,
    MeterWidget,
    PaneHeader,
    ResultsHeader,
    SearchingPanel,
    SlowSourceDiagnosticsRow,
    SpinnerWidget,
)
from agentgrep.ui.widgets.transcript import ConversationLog
from agentgrep.ui.widgets.turns import (
    ChatTurnKind,
    MessageTurn,
    QueryTurn,
    ResultTurn,
    SystemTurn,
    Turn,
    TurnRenderer,
)
from agentgrep.ui.widgets.welcome import WELCOME_QUERY_INDEX_META, WelcomeExamples

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "ChatTurnKind",
    "CompletionDropdown",
    "ConversationLog",
    "DetailFindInput",
    "DetailFindRequested",
    "DetailFocusRequested",
    "DetailScroll",
    "DetailScrollChanged",
    "FilterCompleted",
    "FilterHeader",
    "FilterInput",
    "FilterRequested",
    "HistoryRecall",
    "MessageTurn",
    "MeterWidget",
    "PaneHeader",
    "QueryTurn",
    "RefinementBreadcrumb",
    "ResultHighlighted",
    "ResultTurn",
    "ResultsHeader",
    "ResultsScrollChanged",
    "SearchInput",
    "SearchRequested",
    "SearchResultsList",
    "SearchingPanel",
    "SlowSourceDiagnosticsRow",
    "SpinnerWidget",
    "SystemTurn",
    "Turn",
    "TurnRenderer",
    "WELCOME_QUERY_INDEX_META",
    "WelcomeExamples",
    "WelcomeQuerySelected",
]
