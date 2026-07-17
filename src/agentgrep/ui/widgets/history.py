"""The Ctrl-R search-history recall modal.

``HistoryRecall`` is the app's first :class:`~textual.screen.ModalScreen`: a
two-pane recall surface — a left list of past search queries (each row a
relative-time prefix + the query, fuzzy-highlighted) and a right preview of the
selected entry — with a bottom incremental filter and a footer hint. It owns all
keys while open, dims the screen below, and ``dismiss(...)`` flows the chosen
query (or ``None`` on cancel) back to the app, which fills the search box.

The list is a small, capped, in-memory snapshot, so filtering runs synchronously
on every keystroke (no worker, no disk) — ADR 0011's no-blocking rule holds with
room to spare. The query text is rendered as a plain :class:`~textual.content.Content`
(never ``from_markup``) so a query containing ``[...]`` is shown verbatim, and the
fuzzy match offsets are stylized by hand — mirroring the completion dropdown's
``markup=False`` guard.
"""

from __future__ import annotations

import time
import typing as t

from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.fuzzy import Matcher
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from agentgrep.ui import theme as ui_theme
from agentgrep.ui.format import format_relative_time

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from agentgrep.ui._history import HistoryEntry

__all__ = ["HistoryRecall"]

_AGE_WIDTH = 9
"""Fixed width of the relative-time prefix column so query text aligns."""


class HistoryRecall(ModalScreen[t.Optional[str]]):  # noqa: UP045 -- Textual generic base needs a runtime subscript
    """Two-pane Ctrl-R recall over the persisted search-input history."""

    AUTO_FOCUS = "#history-filter"
    # The base declares this ``ClassVar[...] | None``; override with the same
    # shape (no bare ``ClassVar``, which the checker rejects in a union).
    HORIZONTAL_BREAKPOINTS: list[tuple[int, str]] | None = [(0, "-narrow"), (80, "-wide")]  # noqa: RUF012
    PREVIEW_ROWS: t.ClassVar[int] = 12

    BINDINGS: t.ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
        # priority so ctrl+c stages here (clear/close) instead of the app's
        # ctrl+c -> smart_quit firing and quitting out from under the modal.
        Binding("ctrl+c", "filter_clear_or_cancel", "Clear / Cancel", priority=True, show=False),
        Binding("up", "nav_up", show=False),
        Binding("down", "nav_down", show=False),
        Binding("pageup", "nav_page_up", show=False),
        Binding("pagedown", "nav_page_down", show=False),
        Binding("home", "nav_home", show=False),
        Binding("end", "nav_end", show=False),
    ]

    DEFAULT_CSS = """
    HistoryRecall {
        align: center middle;
    }
    #history-dialog {
        width: 90%;
        max-width: 120;
        height: 80%;
        max-height: 24;
        padding: 0 1;
    }
    #history-title { height: 1; }
    #history-body { height: 1fr; }
    #history-list { width: 2fr; height: 1fr; }
    #history-preview-scroll { width: 3fr; height: 1fr; padding: 0 1; }
    #history-filter { height: 3; }
    #history-footer { height: 1; color: $text-muted; }
    HistoryRecall.-narrow #history-preview-scroll { display: none; }
    HistoryRecall.-narrow #history-list { width: 1fr; }
    """

    def __init__(
        self,
        entries: cabc.Sequence[HistoryEntry],
        *,
        seed: str = "",
        id: str | None = None,  # noqa: A002 -- Textual ``id`` kwarg
    ) -> None:
        super().__init__(id=id)
        self._entries = list(entries)
        self._seed = seed
        self._matches: list[HistoryEntry] = []
        self._now = int(time.time())
        # Content.stylize takes a style string; reverse-video by default,
        # recolored to the accent hex once the theme resolves on mount.
        self._match_style = "reverse"

    def compose(self) -> cabc.Iterator[t.Any]:
        """Lay out the title, the list+preview body, the filter, and the footer."""
        with Vertical(id="history-dialog"):
            yield Static("Search prompts", id="history-title")
            with Horizontal(id="history-body"):
                yield OptionList(id="history-list", markup=False)
                with VerticalScroll(id="history-preview-scroll"):
                    yield Static("", id="history-preview")
            yield Input(placeholder="Search history", id="history-filter")
            yield Static(
                "↑/↓ to navigate · Enter to use · Esc to cancel",
                id="history-footer",
            )

    def on_mount(self) -> None:
        """Resolve the highlight color, seed the filter, and render the first list."""
        accent = self._resolve_accent()
        if accent:
            self._match_style = f"{accent} bold"
        if self._seed:
            # Setting the value fires ``Input.Changed`` -> ``_refilter``; only
            # refilter directly when there is no seed to drive that event.
            self.query_one("#history-filter", Input).value = self._seed
        else:
            self._refilter("")

    def _resolve_accent(self) -> str:
        """Return the theme's accent hex, or ``""`` when it cannot be resolved."""
        try:
            return ui_theme.resolve(t.cast("t.Any", self.app).theme_variables, "accent")
        except AttributeError, KeyError, TypeError:
            return ""

    # --- filtering + rendering -------------------------------------------
    def _refilter(self, query: str) -> None:
        """Rebuild the list for ``query`` (fuzzy, newest-first), repaint the preview."""
        option_list = self.query_one("#history-list", OptionList)
        option_list.clear_options()
        matcher = Matcher(query) if query else None
        if matcher is None:
            self._matches = list(self._entries)
        else:
            scored = [
                (score, entry)
                for entry in self._entries
                if (score := matcher.match(entry.text)) > 0
            ]
            # Stable sort by score keeps newest-first among equal scores.
            scored.sort(key=lambda pair: pair[0], reverse=True)
            self._matches = [entry for _score, entry in scored]
        if not self._matches:
            option_list.add_option(Option(self._empty_text(query), disabled=True))
            self.query_one("#history-preview", Static).update("")
            return
        option_list.add_options(Option(self._row(entry, matcher)) for entry in self._matches)
        option_list.highlighted = 0
        self._update_preview(0)

    def _row(self, entry: HistoryEntry, matcher: Matcher | None) -> Content:
        """Compose a list row: a dim relative-time prefix + the (highlighted) query."""
        prefix = f"{format_relative_time(entry.ts, self._now):<{_AGE_WIDTH}}"
        content = Content(entry.text)
        if matcher is not None:
            _score, offsets = matcher.fuzzy_search.match(matcher.query, entry.text)
            for offset in offsets:
                if 0 <= offset < len(entry.text) and not entry.text[offset].isspace():
                    content = content.stylize(self._match_style, offset, offset + 1)
        return Content.assemble((prefix, "dim"), content)

    def _empty_text(self, query: str) -> str:
        """Return the disabled-row text: a no-match hint, or a no-history hint."""
        return "No matching prompts" if query else "No history yet"

    def _preview_content(self, entry: HistoryEntry) -> Content:
        """Render the right preview, truncating to a row budget with '+N lines'."""
        lines = entry.text.split("\n")
        if len(lines) > self.PREVIEW_ROWS:
            shown = lines[: self.PREVIEW_ROWS - 1]
            more = len(lines) - len(shown)
            body = "\n".join(shown)
            return Content.assemble(body + "\n", (f"+{more} lines", "dim"))
        return Content(entry.text)

    def _update_preview(self, index: int | None) -> None:
        """Repaint the preview pane for the highlighted row index."""
        preview = self.query_one("#history-preview", Static)
        if index is None or not (0 <= index < len(self._matches)):
            preview.update("")
            return
        preview.update(self._preview_content(self._matches[index]))

    # --- navigation (filter keeps focus; these drive the list) -----------
    def _set_highlight(self, index: int) -> None:
        if not self._matches:
            return
        option_list = self.query_one("#history-list", OptionList)
        option_list.highlighted = max(0, min(len(self._matches) - 1, index))

    def _move(self, delta: int) -> None:
        if not self._matches:
            return
        option_list = self.query_one("#history-list", OptionList)
        current = option_list.highlighted if option_list.highlighted is not None else 0
        self._set_highlight(current + delta)

    def action_nav_up(self) -> None:
        """Move the list selection up one row."""
        self._move(-1)

    def action_nav_down(self) -> None:
        """Move the list selection down one row."""
        self._move(1)

    def action_nav_page_up(self) -> None:
        """Move the list selection up a page."""
        self._move(-10)

    def action_nav_page_down(self) -> None:
        """Move the list selection down a page."""
        self._move(10)

    def action_nav_home(self) -> None:
        """Jump the selection to the newest row."""
        self._set_highlight(0)

    def action_nav_end(self) -> None:
        """Jump the selection to the oldest row."""
        self._set_highlight(len(self._matches) - 1)

    # --- messages --------------------------------------------------------
    def on_input_changed(self, event: Input.Changed) -> None:
        """Re-filter the list as the user types in the bottom filter."""
        if event.input.id == "history-filter":
            self._refilter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the filter accepts the highlighted row."""
        if event.input.id == "history-filter":
            option_list = self.query_one("#history-list", OptionList)
            self._accept(option_list.highlighted)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Drive the preview pane from the newly highlighted row."""
        self._update_preview(event.option_index)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Accept a clicked or selected row."""
        self._accept(event.option_index)

    def _accept(self, index: int | None) -> None:
        """Dismiss with the chosen query text, or ``None`` when there is none."""
        if index is None or not (0 <= index < len(self._matches)):
            self.dismiss(None)
            return
        self.dismiss(self._matches[index].text)

    def action_cancel(self) -> None:
        """Escape cancels: dismiss with ``None`` so the search box is left as-is."""
        self.dismiss(None)

    def action_filter_clear_or_cancel(self) -> None:
        """Staged ctrl-c: clear the filter if it has text, else close the modal.

        Mirrors the app's input ctrl-c (text → clear; empty → close), but the
        modal's "exit" is closing itself. Setting ``value = ""`` re-fires
        ``Input.Changed`` → :meth:`on_input_changed` → ``_refilter("")``, so the
        full list repaints with no manual re-trigger.
        """
        filter_input = self.query_one("#history-filter", Input)
        if filter_input.value:
            filter_input.value = ""
            return
        self.dismiss(None)
