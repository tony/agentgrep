"""Conversation-turn value objects, bubble widget, and renderer (ADR 0014).

A *turn* is one entry in the chat transcript. Its data is a frozen, slotted
value object (:class:`QueryTurn` / :class:`ResultTurn` / :class:`SystemTurn`);
the *rendering* lives on a separate, non-slotted :class:`TurnRenderer` keyed by
type via :func:`functools.singledispatchmethod`. The split is deliberate: a
``frozen=True, slots=True`` value object has no ``__dict__``, so derived state
(the resolved theme hexes, here) cannot live on it — it lives on the renderer.

:class:`MessageTurn` is the thin :class:`~textual.widgets.Static` bubble that
carries its value object, so a layout can open detail on a focused result turn.

Renders stay bounded (ADR 0011 NB-1): a result turn shows the agent, the kind,
the record's first line, and a compact path — never ``Syntax`` / ``Markdown`` of
a full body, which the detail modal builds off the pump instead.

Imported only from inside the app factory (and the tests), never by the eager
``import agentgrep`` path (ADR 0010).
"""

from __future__ import annotations

import dataclasses
import enum
import functools
import typing as t

from rich.text import Text
from textual.widgets import Static

from agentgrep._text import format_compact_path
from agentgrep.ui import theme as ui_theme

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from rich.console import RenderableType

    from agentgrep.records import SearchRecord

__all__ = [
    "ChatTurnKind",
    "MessageTurn",
    "QueryTurn",
    "ResultTurn",
    "SystemTurn",
    "Turn",
    "TurnRenderer",
]


class ChatTurnKind(enum.StrEnum):
    """Stable turn-kind ids, used for the bubble's CSS class and styling."""

    QUERY = "query"
    RESULT = "result"
    SYSTEM = "system"


@dataclasses.dataclass(frozen=True, slots=True)
class Turn:
    """Base value object for one transcript turn.

    Parameters
    ----------
    generation : int
        The search generation the turn belongs to, so a layout can drop turns
        from a superseded search (NB-10).
    """

    generation: int


@dataclasses.dataclass(frozen=True, slots=True)
class QueryTurn(Turn):
    """A user query turn: the literal text typed and its narrowing depth."""

    text: str
    depth: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class ResultTurn(Turn):
    """A single matched record rendered as an assistant turn."""

    record: SearchRecord


@dataclasses.dataclass(frozen=True, slots=True)
class SystemTurn(Turn):
    """A system note: a result count, an outcome, or widen/clear feedback."""

    text: str
    tone: t.Literal["info", "error", "muted"] = "info"


def turn_kind(turn: Turn) -> ChatTurnKind:
    """Return the :class:`ChatTurnKind` for ``turn`` (drives the CSS class)."""
    if isinstance(turn, QueryTurn):
        return ChatTurnKind.QUERY
    if isinstance(turn, ResultTurn):
        return ChatTurnKind.RESULT
    return ChatTurnKind.SYSTEM


class MessageTurn(Static, can_focus=True):
    """One transcript bubble; carries its :class:`Turn` so a layout can act on focus.

    Parameters
    ----------
    turn : Turn
        The value object this bubble renders; reachable as :attr:`turn` so the
        layout can open detail on a focused :class:`ResultTurn`.
    renderable : RenderableType
        The pre-built (bounded) renderable from :class:`TurnRenderer`.
    id : str, optional
        Textual widget id.
    """

    def __init__(
        self,
        turn: Turn,
        renderable: RenderableType,
        *,
        id: str | None = None,  # noqa: A002 -- forwarded to Textual's ``id`` kwarg
    ) -> None:
        super().__init__(renderable, id=id, classes=f"turn turn-{turn_kind(turn).value}")
        self.turn = turn


class TurnRenderer:
    """Render a :class:`Turn` value object to a bounded Rich renderable.

    Non-slotted on purpose: a ``frozen=True, slots=True`` :class:`Turn` has no
    ``__dict__``, so derived state (the resolved theme hexes) lives here, not on
    the value object. Dispatch is by turn type via
    :func:`functools.singledispatchmethod`, so a new turn type registers a render
    branch without editing the others.

    Parameters
    ----------
    theme_variables : collections.abc.Mapping[str, str]
        The app's resolved theme-variable map (``App.theme_variables``); read
        once so a render never touches the app.
    """

    def __init__(self, theme_variables: cabc.Mapping[str, str]) -> None:
        self._theme_variables = theme_variables
        self._accent = ui_theme.resolve(theme_variables, "accent")
        self._muted = ui_theme.resolve(theme_variables, "ag-muted")
        self._dim = ui_theme.resolve(theme_variables, "ag-dim")
        self._error = ui_theme.resolve(theme_variables, "error")

    @functools.singledispatchmethod
    def render(self, turn: Turn) -> RenderableType:
        """Render ``turn`` (fallback for an unknown turn type)."""
        return Text(str(turn))

    @render.register(QueryTurn)
    def _render_query(self, turn: QueryTurn) -> RenderableType:
        """Render a user query as ``you ▸ <text>``, indented by narrowing depth."""
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("  " * turn.depth)
        text.append("you ", style=f"{self._accent} bold".strip() or None)
        text.append("▸ ", style=self._accent or None)
        text.append(turn.text)
        return text

    @render.register(ResultTurn)
    def _render_result(self, turn: ResultTurn) -> RenderableType:
        """Render a matched record as a bounded one-line bubble (NB-1)."""
        record = turn.record
        agent_color = ui_theme.resolve(
            self._theme_variables,
            ui_theme.AGENT_TOKEN_BY_NAME.get(record.agent or ""),
        )
        kind_color = ui_theme.resolve(
            self._theme_variables,
            ui_theme.KIND_TOKEN_BY_NAME.get(record.kind or ""),
        )
        # Bounded slice first (NB-1): never materialize a full multi-MB body into
        # a line list on the pump — read at most a couple hundred chars, then the
        # first line of that.
        raw = record.title or record.text or ""
        first = raw[:200].split("\n", 1)[0][:120]
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("  · ", style=self._muted or None)
        text.append(f"{(record.agent or '')[:8]:<8} ", style=agent_color or None)
        text.append(f"{(record.kind or '')[:7]:<7} ", style=kind_color or None)
        text.append(first)
        path = format_compact_path(record.path, max_width=40)
        if path:
            text.append(f"  {path}", style=self._muted or None)
        return text

    @render.register(SystemTurn)
    def _render_system(self, turn: SystemTurn) -> RenderableType:
        """Render a system note as ``▸▸ <text>`` in a tone-appropriate hue."""
        tone_hex = {"error": self._error, "muted": self._dim}.get(turn.tone, self._muted)
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("▸▸ ", style=self._muted or None)
        text.append(turn.text, style=tone_hex or None)
        return text
