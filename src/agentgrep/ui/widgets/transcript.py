"""``ConversationLog`` — an append-only transcript of turn bubbles (ADR 0014).

A :class:`~textual.containers.VerticalScroll` using Textual's ``layout: stream``
(the purpose-built container for a long, scrolling list of widgets like an LLM
chat). Turns are appended with :meth:`~textual.widget.Widget.mount_all` and the
log is **never** recomposed: Textual's ``recompose()`` removes and remounts every
child with no keyed diff (a long transcript would thrash), so finished turns are
mounted once and frozen — the ink ``<Static>`` discipline restated as the ADR
0011 law. A turn budget unmounts the oldest bubbles so an unbounded session can
not grow without limit.

Imported only from inside the app factory (and the tests), never by the eager
``import agentgrep`` path (ADR 0010).
"""

from __future__ import annotations

import typing as t

from textual.containers import VerticalScroll

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from textual.widget import AwaitMount

    from agentgrep.ui.widgets.turns import MessageTurn

__all__ = ["ConversationLog"]


class ConversationLog(VerticalScroll, can_focus=False):
    """Append-only stream of :class:`MessageTurn` bubbles; never recomposed."""

    DEFAULT_CSS = """
    ConversationLog { layout: stream; height: 1fr; scrollbar-size: 0 0; background: transparent; }
    """

    #: Maximum mounted turns before the oldest are trimmed. Mounting a widget is
    #: heavier than ``RichLog.write``, so the budget keeps the child count bounded.
    MAX_TURNS: t.ClassVar[int] = 2000

    def mount_turns(self, turns: cabc.Sequence[MessageTurn]) -> AwaitMount:
        """Append ``turns``, trimming the oldest past the budget, and scroll to end.

        Parameters
        ----------
        turns : collections.abc.Sequence[MessageTurn]
            The new bubbles to append (already built off the pump).

        Returns
        -------
        AwaitMount
            The mount awaitable, so a caller can await the children existing.
        """
        excess = len(self.children) + len(turns) - self.MAX_TURNS
        if excess > 0:
            for old in list(self.children)[:excess]:
                old.remove()
        result = self.mount_all(turns)
        self.scroll_end(animate=False)
        return result

    def clear_turns(self) -> None:
        """Remove every mounted turn (a new conversation or a reset)."""
        self.remove_children()
