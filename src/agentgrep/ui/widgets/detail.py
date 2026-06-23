"""The detail-pane scroll widget.

``DetailScroll`` is a ``VerticalScroll`` subclass with vim-style scroll
bindings. Imported from inside the app factory (and the tests), never eagerly.
"""

from __future__ import annotations

import typing as t

from textual.containers import VerticalScroll

from agentgrep.ui.format import scroll_percent
from agentgrep.ui.widgets.messages import DetailScrollChanged

__all__ = ["DetailScroll"]


class DetailScroll(VerticalScroll, can_focus=True):
    """``VerticalScroll`` subclass for the right-side detail pane.

    Adds vim-style bindings: ``h`` / left-arrow releases focus back to the
    results list, and ``j`` / ``k`` mirror the stock ``down`` / ``up``
    scroll bindings so navigation stays consistent with
    :class:`SearchResultsList`. ``can_focus=True`` is set via the
    class-keyword form — Textual reads it during ``__init_subclass__``,
    so the plain class-attribute form silently fails to enroll the widget
    in the focus chain.
    """

    BINDINGS: t.ClassVar[list[tuple[str, str, str]]] = [
        ("k", "scroll_up", "Up"),
        ("j", "scroll_down", "Down"),
        ("h", "focus_results", "Results"),
        ("left", "focus_results", ""),
        ("g", "scroll_home", "Top"),
        ("G", "scroll_end", "Bottom"),
        ("ctrl+d", "scroll_half_down", "½ Down"),
        ("ctrl+u", "scroll_half_up", "½ Up"),
        ("ctrl+f", "page_down", "Pg Down"),
        ("ctrl+b", "page_up", "Pg Up"),
    ]

    def action_focus_results(self) -> None:
        """Move focus leftward back to the results list (vim-style ``h``)."""
        results = self.app.query_one("#results")
        t.cast("t.Any", results).focus()

    def action_scroll_up(self) -> None:
        """Release focus to the filter input when already scrolled to the top.

        Mirrors :meth:`SearchResultsList.action_cursor_up` — when the
        widget has nothing left to give in that direction, hand focus off
        to the neighbor instead of swallowing the keystroke. Catches both
        ``k`` (our binding) and ``up`` (inherited from
        ``ScrollableContainer``).
        """
        scroll_y = t.cast("float", getattr(self, "scroll_y", 0))
        if scroll_y <= 0:
            self.app.query_one("#filter").focus()
        else:
            super().action_scroll_up()

    def action_scroll_half_down(self) -> None:
        """Scroll down by half the visible viewport (vim ``Ctrl-D``)."""
        half = max(1, self.size.height // 2)
        self.scroll_relative(y=half, animate=True)

    def action_scroll_half_up(self) -> None:
        """Scroll up by half the visible viewport (vim ``Ctrl-U``)."""
        half = max(1, self.size.height // 2)
        self.scroll_relative(y=-half, animate=True)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Re-render the detail status line on scroll."""
        base = getattr(super(), "watch_scroll_y", None)
        if callable(base):
            base(old_value, new_value)
        self.post_message(
            DetailScrollChanged(
                percent=scroll_percent(
                    float(new_value or 0),
                    float(getattr(self, "max_scroll_y", 0) or 0),
                ),
            ),
        )
