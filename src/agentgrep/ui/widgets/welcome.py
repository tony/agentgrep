"""Interactive widgets for the explorer's idle welcome canvas."""

from __future__ import annotations

from textual import events
from textual.widgets import Static

from agentgrep.ui import _runtime
from agentgrep.ui.widgets.messages import WelcomeQuerySelected

__all__ = ["WELCOME_QUERY_INDEX_META", "WelcomeExamples"]

WELCOME_QUERY_INDEX_META = "agentgrep_query_index"
"""Rich/Textual metadata key identifying a fixed welcome query."""


class WelcomeExamples(Static):
    """Syntax-highlighted query examples with bounded mouse selection."""

    ALLOW_SELECT = False

    @_runtime.pump_only
    def on_click(self, event: events.Click) -> None:
        """Post the integer index carried by the clicked example span."""
        index = event.style.meta.get(WELCOME_QUERY_INDEX_META)
        if type(index) is int:
            event.stop()
            self.post_message(WelcomeQuerySelected(index))
