"""Interactive widgets for the explorer's idle welcome canvas."""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.content import Content
from textual.reactive import reactive
from textual.style import Style
from textual.widgets import Static

from agentgrep.ui import _runtime
from agentgrep.ui.highlighter import QueryHighlighter
from agentgrep.ui.widgets.messages import WelcomeQuerySelected

__all__ = ["WELCOME_QUERY_INDEX_META", "WelcomeExamples"]

WELCOME_QUERY_INDEX_META = "agentgrep_query_index"
"""Rich/Textual metadata key identifying a fixed welcome query."""

_WELCOME_QUERIES = (
    "agent:claude",
    "scope:all model:gpt*",
    "role:user",
    "timestamp:>2026-01-01",
    '"exact phrase"',
)
_WELCOME_QUERY_ROWS = ((0, 1, 2), (3, 4))
_WELCOME_BRAND_SHINE = (1, 2, 3, 4, 5, 4, 3, 2, 1)
_WELCOME_SHINE_INTERVAL = 0.08


def _welcome_wordmark(offset: int = 0) -> Content:
    """Build one frame of the theme-aware welcome wordmark."""
    return Content.assemble(
        "Welcome to ",
        *(
            (
                character,
                "bold $ag-brand-shine-"
                f"{_WELCOME_BRAND_SHINE[(index + offset) % len(_WELCOME_BRAND_SHINE)]}",
            )
            for index, character in enumerate("agentgrep")
        ),
    )


class _WelcomeWordmark(Static):
    """Fixed-size welcome wordmark with a paint-only shine frame."""

    shine_offset: reactive[int] = reactive(0, layout=False, repaint=True)

    @_runtime.pump_only
    def render(self) -> Content:
        """Render the current theme-token frame without changing geometry."""
        return _welcome_wordmark(self.shine_offset)


def _welcome_query_examples(highlighter: QueryHighlighter | None = None) -> Content:
    """Build syntax-colored examples with bounded click metadata."""
    examples = Text()
    click_ranges: list[tuple[int, int, int]] = []
    active_highlighter = highlighter or QueryHighlighter()
    for row_number, row in enumerate(_WELCOME_QUERY_ROWS):
        if row_number:
            examples.append("\n")
        for column, index in enumerate(row):
            if column:
                examples.append("   ")
            query = _WELCOME_QUERIES[index]
            hint = Text(query)
            active_highlighter.highlight(hint)
            start = len(examples)
            examples.append_text(hint)
            click_ranges.append((start, len(examples), index))

    content = Content.from_rich_text(examples)
    for start, end, index in click_ranges:
        content = content.stylize(
            Style.from_meta({WELCOME_QUERY_INDEX_META: index}),
            start,
            end,
        )
    return content


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
