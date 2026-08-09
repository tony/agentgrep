"""Compact in-memory bookmark recall modal."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import typing as t

from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from agentgrep.bookmarks import BookmarkEntry
from agentgrep.records import SearchRecord
from agentgrep.ui import _runtime

__all__ = ["BookmarkChoice", "BookmarkRecall"]


@dataclasses.dataclass(frozen=True, slots=True)
class BookmarkChoice:
    """One saved bookmark paired with its resolved record, when available."""

    entry: BookmarkEntry
    record: SearchRecord | None


class BookmarkRecall(ModalScreen[t.Optional[BookmarkChoice]]):  # noqa: UP045
    """Single-list recall over an already-resolved in-memory snapshot."""

    AUTO_FOCUS = "#bookmark-filter"
    PREVIEW_CHARS: t.ClassVar[int] = 160
    BINDINGS: t.ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
        Binding(
            "ctrl+c",
            "filter_clear_or_cancel",
            "Clear / Cancel",
            priority=True,
            show=False,
        ),
        Binding("up", "nav_up", priority=True, show=False),
        Binding("down", "nav_down", priority=True, show=False),
        Binding("pageup", "nav_page_up", priority=True, show=False),
        Binding("pagedown", "nav_page_down", priority=True, show=False),
        Binding("home", "nav_home", priority=True, show=False),
        Binding("end", "nav_end", priority=True, show=False),
    ]

    DEFAULT_CSS = """
    BookmarkRecall { align: center middle; }
    #bookmark-dialog {
        width: 90%;
        max-width: 100;
        height: 70%;
        max-height: 20;
        padding: 0 1;
    }
    #bookmark-title, #bookmark-preview, #bookmark-footer { height: 1; }
    #bookmark-list { height: 1fr; }
    #bookmark-filter { height: 3; }
    """

    def __init__(
        self,
        choices: cabc.Sequence[BookmarkChoice],
        *,
        id: str | None = None,  # noqa: A002
    ) -> None:
        super().__init__(id=id)
        self._choices = list(choices)
        self._matches: list[BookmarkChoice] = []

    @_runtime.pump_only
    def compose(self) -> cabc.Iterator[t.Any]:
        """Lay out a title, list, one-line preview, filter, and footer."""
        with Vertical(id="bookmark-dialog"):
            yield Static("Bookmarks", id="bookmark-title")
            yield OptionList(id="bookmark-list", markup=False)
            yield Static("", id="bookmark-preview", markup=False)
            yield Input(placeholder="Filter bookmarks", id="bookmark-filter")
            yield Static(
                "↑/↓ to navigate · Enter to open · Esc to cancel",
                id="bookmark-footer",
            )

    @_runtime.pump_only
    def on_mount(self) -> None:
        """Render the initial in-memory snapshot."""
        self._refilter("")

    def _search_text(self, choice: BookmarkChoice) -> str:
        """Return bounded in-memory text used by the filter."""
        preview = self._bounded_preview(choice.record)
        return f"{choice.entry.scope} {choice.entry.target_id} {preview}"

    @classmethod
    def _bounded_preview(cls, record: SearchRecord | None) -> str:
        """Slice before newline search so a huge single line stays frame-bounded."""
        if record is None:
            return ""
        source = record.title or record.text
        return source[: cls.PREVIEW_CHARS].partition("\n")[0]

    def _refilter(self, query: str) -> None:
        """Rebuild the bounded list with case-insensitive substring matching."""
        option_list = self.query_one("#bookmark-list", OptionList)
        option_list.clear_options()
        needle = query[: self.PREVIEW_CHARS].casefold()
        self._matches = [
            choice
            for choice in self._choices
            if not needle or needle in self._search_text(choice).casefold()
        ]
        if not self._matches:
            message = "No matching bookmarks" if query else "No bookmarks yet"
            option_list.add_option(Option(message, disabled=True))
            self.query_one("#bookmark-preview", Static).update("")
            return
        for choice in self._matches:
            option_list.add_option(Option(self._row(choice)))
        option_list.highlighted = 0
        self._update_preview(0)

    @staticmethod
    def _row(choice: BookmarkChoice) -> Content:
        """Return a canonical-target row with an explicit resolution status."""
        status = "resolved" if choice.record is not None else "unresolved"
        return Content.assemble(
            (f"{choice.entry.scope:<7}", "bold"),
            choice.entry.target_id,
            (f"  {status}", "dim"),
        )

    @staticmethod
    def _preview(choice: BookmarkChoice) -> str:
        """Return one line of resolved preview or a path-free unavailable status."""
        if choice.record is None:
            return f"Unavailable · {choice.entry.target_id}"
        preview = BookmarkRecall._bounded_preview(choice.record)
        return f"Ready · {preview}"

    def _update_preview(self, index: int | None) -> None:
        """Repaint the one-line status for a highlighted match."""
        preview = self.query_one("#bookmark-preview", Static)
        if index is None or not (0 <= index < len(self._matches)):
            preview.update("")
            return
        preview.update(self._preview(self._matches[index]))

    def _set_highlight(self, index: int) -> None:
        """Clamp and set the highlighted match index."""
        if not self._matches:
            return
        option_list = self.query_one("#bookmark-list", OptionList)
        option_list.highlighted = max(0, min(len(self._matches) - 1, index))

    def _move(self, delta: int) -> None:
        """Move the highlighted match by ``delta`` rows."""
        if not self._matches:
            return
        option_list = self.query_one("#bookmark-list", OptionList)
        current = option_list.highlighted if option_list.highlighted is not None else 0
        self._set_highlight(current + delta)

    @_runtime.pump_only
    def action_nav_up(self) -> None:
        """Move the selection up one row."""
        self._move(-1)

    @_runtime.pump_only
    def action_nav_down(self) -> None:
        """Move the selection down one row."""
        self._move(1)

    @_runtime.pump_only
    def action_nav_page_up(self) -> None:
        """Move the selection up a page."""
        self._move(-10)

    @_runtime.pump_only
    def action_nav_page_down(self) -> None:
        """Move the selection down a page."""
        self._move(10)

    @_runtime.pump_only
    def action_nav_home(self) -> None:
        """Jump to the first bookmark."""
        self._set_highlight(0)

    @_runtime.pump_only
    def action_nav_end(self) -> None:
        """Jump to the last bookmark."""
        self._set_highlight(len(self._matches) - 1)

    @_runtime.pump_only
    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter the bounded snapshot while the user types."""
        if event.input.id == "bookmark-filter":
            self._refilter(event.value)

    @_runtime.pump_only
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Accept the highlighted match from the focused filter."""
        if event.input.id == "bookmark-filter":
            option_list = self.query_one("#bookmark-list", OptionList)
            self._accept(option_list.highlighted)

    @_runtime.pump_only
    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Update preview status for a newly highlighted row."""
        self._update_preview(event.option_index)

    @_runtime.pump_only
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Accept a clicked or keyboard-selected row."""
        self._accept(event.option_index)

    def _accept(self, index: int | None) -> None:
        """Dismiss with a choice, or ``None`` when no match is selectable."""
        if index is None or not (0 <= index < len(self._matches)):
            self.dismiss(None)
            return
        self.dismiss(self._matches[index])

    @_runtime.pump_only
    def action_cancel(self) -> None:
        """Dismiss without selecting a bookmark."""
        self.dismiss(None)

    @_runtime.pump_only
    def action_filter_clear_or_cancel(self) -> None:
        """Clear a non-empty filter, otherwise dismiss the modal."""
        filter_input = self.query_one("#bookmark-filter", Input)
        if filter_input.value:
            filter_input.value = ""
            return
        self.dismiss(None)
