"""The detail-pane scroll widget.

``DetailScroll`` is a ``VerticalScroll`` subclass with vim-style scroll
bindings. Imported from inside the app factory (and the tests), never eagerly.
"""

from __future__ import annotations

import typing as t

from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll

from agentgrep.ui.format import scroll_percent
from agentgrep.ui.widgets.messages import DetailFocusRequested, DetailScrollChanged

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

    # Remappable scroll/focus/find/toggle/copy bindings carry ``id=``s so a
    # user keymap file can rebind their keys by id. The authored keys bind the
    # arrow, vim (hjkl), and emacs (ctrl+n/p) motions all at once. ``ctrl+b`` ->
    # ``page_up`` stays id-less. The detail pane consumes no typed text, so the
    # bare-letter copy chords (``y`` / ``Y``) are safe.
    BINDINGS: t.ClassVar[list[BindingType]] = [
        Binding("up,k,ctrl+p", "scroll_up", "Up", id="detail.scroll_up"),
        Binding("down,j,ctrl+n", "scroll_down", "Down", id="detail.scroll_down"),
        Binding("left,h", "focus_results", "Results", id="detail.focus_results"),
        Binding("home,g", "scroll_home", "Top", id="detail.scroll_home"),
        Binding("end,G", "scroll_end", "Bottom", id="detail.scroll_end"),
        Binding("ctrl+d", "scroll_half_down", "½ Down", id="detail.scroll_half_down"),
        Binding("ctrl+u", "scroll_half_up", "½ Up", id="detail.scroll_half_up"),
        Binding("slash,ctrl+f", "open_find", "Find", id="detail.open_find"),
        ("ctrl+b", "page_up", "Pg Up"),
        # Raw <-> rendered toggle: ``alt+r`` (codex precedent) with ``ctrl+e``
        # as a fallback for terminals that mangle ``alt``.
        Binding("alt+r,ctrl+e", "toggle_raw", "Raw", id="detail.toggle_raw"),
        Binding("y", "copy_source", "Copy src", id="detail.copy_source"),
        Binding("Y", "copy_rendered", "Copy rendered", id="detail.copy_rendered"),
    ]

    def action_open_find(self) -> None:
        """Open the find-in-detail bar (``/`` or ``ctrl+f``); no-op without a record."""
        t.cast("t.Any", self.screen).action_open_detail_find()

    def action_toggle_raw(self) -> None:
        """Toggle the detail pane between rendered and raw source (``alt+r``)."""
        t.cast("t.Any", self.screen).action_toggle_detail_raw()

    def action_copy_source(self) -> None:
        """Copy the raw record source to the clipboard (``y``)."""
        t.cast("t.Any", self.screen).action_copy_detail_source()

    def action_copy_rendered(self) -> None:
        """Copy the flattened rendered text to the clipboard (``Y``)."""
        t.cast("t.Any", self.screen).action_copy_detail_rendered()

    def action_focus_results(self) -> None:
        """Move focus leftward back to the results list (vim-style ``h``)."""
        self.post_message(DetailFocusRequested("results"))

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
            self.post_message(DetailFocusRequested("filter"))
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
