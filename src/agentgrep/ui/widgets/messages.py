"""Frontend message types for the Textual explorer.

These subclass Textual's ``Message`` directly. The module imports Textual at the
top, but is only imported from inside ``build_streaming_ui_app`` (and the tests),
never by the eager ``import agentgrep`` path, so the optional-dependency rule
holds (ADR 0010).

``FilterRequested`` / ``SearchRequested`` / ``FilterCompleted`` cross the message
bus at typing speed (debounced); ``ResultsScrollChanged`` / ``DetailScrollChanged``
carry pre-shaped status snapshots so the widgets never reach into the app
directly.
"""

from __future__ import annotations

import typing as t

from textual.message import Message

from agentgrep.progress import (
    FilterCompletedPayload,
    FilterRequestedPayload,
    SearchRequestedPayload,
)

__all__ = [
    "DetailFindRequested",
    "DetailFocusRequested",
    "DetailScrollChanged",
    "FilterCompleted",
    "FilterRequested",
    "ResultsScrollChanged",
    "SearchRequested",
]


class DetailFindRequested(Message):
    """Debounced find-in-detail text-changed event from :class:`DetailFindInput`."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class DetailFocusRequested(Message):
    """Request that the layout reveal and focus a detail-pane neighbor."""

    def __init__(self, target: t.Literal["filter", "results"]) -> None:
        super().__init__()
        self.target = target


class FilterRequested(Message):
    """Debounced filter-text-changed event from :class:`FilterInput`."""

    def __init__(self, payload: FilterRequestedPayload) -> None:
        super().__init__()
        self.payload = payload


class FilterCompleted(Message):
    """Worker-completed filter result posted back to the main thread."""

    def __init__(self, payload: FilterCompletedPayload) -> None:
        super().__init__()
        self.payload = payload


class SearchRequested(Message):
    """Debounced search-text-changed event from :class:`SearchInput`."""

    def __init__(self, payload: SearchRequestedPayload) -> None:
        super().__init__()
        self.payload = payload


class ResultsScrollChanged(Message):
    """Posted by :class:`SearchResultsList` when scroll or cursor moves.

    The app handler renders the right side of the results status line from this
    snapshot — cursor position out of total, plus the scroll percent. Pre-shaped
    here so the widget never reaches into the app directly.
    """

    def __init__(self, cursor: int | None, total: int, percent: int) -> None:
        super().__init__()
        self.cursor = cursor
        self.total = total
        self.percent = percent


class DetailScrollChanged(Message):
    """Posted by :class:`DetailScroll` when the detail-pane scrolls."""

    def __init__(self, percent: int) -> None:
        super().__init__()
        self.percent = percent
