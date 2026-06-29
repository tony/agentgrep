"""``RefinementBreadcrumb`` — the deductive narrowing path widget (ADR 0014).

A presentation-only :class:`~textual.widgets.Static` that renders the active
refinement path as ``all ▸ python ▸ level:error`` (driven by ``set_frames``). It
is the deductive workflow's chrome: the path it shows doubles as the narrowing
story and the pop target. Hidden when there are no refinements. Modeled on
:class:`~agentgrep.ui.widgets.status.PaneHeader`; the color is CSS-driven.

Imported only from inside the app factory (and the tests), never by the eager
``import agentgrep`` path (ADR 0010).
"""

from __future__ import annotations

import typing as t

from rich.text import Text
from textual.widgets import Static

if t.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["RefinementBreadcrumb"]


class RefinementBreadcrumb(Static):
    """Render the refinement path as ``all ▸ a ▸ b`` (set via :meth:`set_frames`)."""

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 -- Textual ``id`` kwarg
        super().__init__("", id=id)
        self._frames: tuple[str, ...] = ()
        self.display = False

    def set_frames(self, frames: cabc.Sequence[str]) -> None:
        """Replace the path; hide the widget entirely when ``frames`` is empty."""
        self._frames = tuple(frames)
        self.display = bool(self._frames)
        self.refresh()

    def render(self) -> Text:
        """Return ``all ▸ <frame> ▸ <frame>`` for the current path."""
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("all", style="dim")
        for frame in self._frames:
            text.append(" ▸ ")
            text.append(frame)
        return text
