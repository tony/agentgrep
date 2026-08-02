"""Interactive widgets for the explorer's idle welcome canvas."""

from __future__ import annotations

import typing as t

from rich.text import Text
from textual import events
from textual.binding import Binding, BindingType
from textual.content import Content
from textual.reactive import reactive
from textual.style import Style
from textual.widgets import Static

from agentgrep.ui import _result_status, _runtime, theme as ui_theme
from agentgrep.ui.highlighter import QueryHighlighter
from agentgrep.ui.widgets.messages import DepthOfferSelected, WelcomeQuerySelected

if t.TYPE_CHECKING:
    from agentgrep.results import NextAction

__all__ = [
    "DEPTH_OFFER_ACTION_META",
    "WELCOME_QUERY_INDEX_META",
    "DepthOffer",
    "WelcomeExamples",
]

WELCOME_QUERY_INDEX_META = "agentgrep_query_index"
"""Rich/Textual metadata key identifying a fixed welcome query."""

DEPTH_OFFER_ACTION_META = "agentgrep_depth_action_id"
"""Rich/Textual metadata key carrying one engine-authored ``action_id``."""

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

#: Style of the row under the depth panel's keyboard cursor. Painted only while
#: the panel holds focus, so a mouse user never sees a cursor they cannot move.
_DEPTH_OFFER_CURSOR_STYLE = "bold $accent"


def _welcome_wordmark(offset: int = 0) -> Content:
    """Build one frame of the theme-aware welcome wordmark."""
    return Content.assemble(
        "Welcome to ",
        *(
            (
                character,
                (
                    "bold $ag-brand-shine-"
                    f"{_WELCOME_BRAND_SHINE[(index + offset) % len(_WELCOME_BRAND_SHINE)]}"
                ),
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


def depth_offer_content(
    actions: tuple[NextAction, ...],
    *,
    highlighted: int | None = None,
    lead_style: str,
) -> Content:
    """Build the pre-run depth panel from engine-authored escalations.

    The engine owns both the vocabulary and the eligibility of every rung, so
    this renders whatever :func:`~agentgrep.results.offered_depth_actions`
    returns and adds no depth concept of its own. Selectable rows carry their
    ``action_id`` as span metadata; the lead line never claims coverage.

    Parameters
    ----------
    actions : tuple[NextAction, ...]
        Escalations offered for the request the primary input would submit.
    highlighted : int | None
        Index of the selectable row carrying the keyboard cursor, or ``None``
        to paint no cursor.
    lead_style : str
        Resolved Rich style for the lead sentence — the caller supplies a
        theme-calibrated color (e.g. ``$ag-muted``) rather than the plain
        ``"dim"`` SGR attribute, whose contrast against the canvas varies
        by terminal and theme.

    Returns
    -------
    Content
        A lead line plus one metadata-tagged row per selectable escalation.
        Empty when the engine offers nothing.
    """
    lead = _result_status.format_depth_offer_lead(actions)
    if not lead:
        return Content("")
    rows = _result_status.format_depth_offer_rows(actions)
    body = Text()
    body.append(lead)
    lead_end = len(body)
    click_ranges: list[tuple[int, int, str]] = []
    for action_id, row in rows:
        body.append("\n")
        start = len(body)
        body.append(f"▸ {row}")
        click_ranges.append((start, len(body), action_id))
    # Stylized after conversion, like the cursor/action-id spans below, so
    # the resolved token survives as a plain string rather than being
    # eagerly parsed into an opaque Style by Content.from_rich_text.
    content = Content.from_rich_text(body).stylize(lead_style, 0, lead_end)
    for index, (start, end, action_id) in enumerate(click_ranges):
        content = content.stylize(
            Style.from_meta({DEPTH_OFFER_ACTION_META: action_id}),
            start,
            end,
        )
        if index == highlighted:
            content = content.stylize(_DEPTH_OFFER_CURSOR_STYLE, start, end)
    return content


class DepthOffer(Static, can_focus=True):
    """Selectable engine-authored depth choices on the idle welcome canvas.

    Closes the pre-run gap in the effort ladder: without this the TUI could
    only escalate a run that had already finished, so a session that opened
    cold had no way to reach ``targeted`` at all. Selecting a row posts the
    engine's ``action_id``; the layout applies the engine's own request patch
    to whatever query the primary input currently holds.

    Both pointers reach it: a click selects the row under the cursor, and the
    panel joins the tab chain whenever it has a selectable rung, where up/down
    move the cursor and enter or space chooses it.

    ``FOCUS_ON_CLICK`` is off on purpose. Textual's default focuses whatever
    focusable widget a click lands on — including a click on the dim lead
    line or the block's own padding, not just an actionable row — and
    nothing in this app blurs a widget just because the mouse moved
    elsewhere afterward (Textual's focus model is click/Tab-driven, not
    hover-driven). Left on, that traps a mouse user behind the keyboard
    cursor this class paints only while focused, with no click-driven way
    out. Row selection is untouched: :meth:`on_click` posts
    ``DepthOfferSelected`` straight from hit-tested span metadata,
    independent of focus, so a mouse click still selects instantly. Tab
    reachability is untouched too, since Tab walks the focus chain rather
    than going through ``focus_on_click``.
    """

    ALLOW_SELECT = False
    FOCUS_ON_CLICK = False

    BINDINGS: t.ClassVar[list[BindingType]] = [
        Binding("up,k", "cursor_up", "Up", show=False),
        Binding("down,j", "cursor_down", "Down", show=False),
        Binding("enter,space", "select_offer", "Choose depth"),
    ]

    highlighted: reactive[int] = reactive(0, init=False)
    """Index of the selectable row carrying the keyboard cursor."""

    #: Engine-authored escalations currently painted, and their selectable rows.
    #: Both are replaced wholesale by :meth:`show_offer`.
    _offered: tuple[NextAction, ...] = ()
    _rows: tuple[tuple[str, str], ...] = ()

    @_runtime.pump_only
    def show_offer(self, actions: tuple[NextAction, ...]) -> None:
        """Paint one engine-authored offer and rewind the keyboard cursor.

        Parameters
        ----------
        actions : tuple[NextAction, ...]
            Escalations offered for the request the primary input would submit.
        """
        self._offered = actions
        self._rows = _result_status.format_depth_offer_rows(actions)
        # An offer with nothing selectable — the top rung, or an explicit scope
        # awaiting confirmation — must not become a dead stop in the tab chain.
        self.can_focus = bool(self._rows)
        self.set_reactive(DepthOffer.highlighted, 0)
        self._repaint_offer()

    def _repaint_offer(self) -> None:
        """Repaint the panel, showing the cursor only while it holds focus.

        Bounded string work over at most two engine-authored rows, so it is
        safe on the pump (ADR 0011 NB-5).
        """
        theme_vars = t.cast("t.Any", self.app).theme_variables
        self.update(
            depth_offer_content(
                self._offered,
                highlighted=self.highlighted if self.has_focus else None,
                lead_style=ui_theme.resolve(theme_vars, "ag-muted"),
            ),
        )

    @_runtime.pump_only
    def watch_highlighted(self) -> None:
        """Repaint after the keyboard cursor moves to another rung."""
        self._repaint_offer()

    @_runtime.pump_only
    def watch_has_focus(self, _has_focus: bool) -> None:
        """Reveal the keyboard cursor while the panel holds focus, hide it after.

        ``MessagePump`` walks the MRO subclass-first, so this class's own
        ``Focus``/``Blur`` handlers would run before ``Widget._on_focus``
        assigns ``has_focus`` — a repaint from there reads the state the panel
        is leaving, not the one it is entering. The reactive watcher runs after
        the assignment, so it is the only hook whose paint matches the flag
        :meth:`_repaint_offer` reads.

        Parameters
        ----------
        _has_focus : bool
            Whether the panel now holds focus. Named to match Textual's own
            watcher, whose CSS-state update this forwards to.
        """
        super().watch_has_focus(_has_focus)
        self._repaint_offer()

    @_runtime.pump_only
    def action_cursor_down(self) -> None:
        """Move the keyboard cursor down one rung, clamped at the last row."""
        if self._rows:
            self.highlighted = min(len(self._rows) - 1, self.highlighted + 1)

    @_runtime.pump_only
    def action_cursor_up(self) -> None:
        """Move the cursor up, or release focus when already at row 0.

        Matches :meth:`SearchResultsList.action_cursor_up` — without this,
        up/down wrapped within the two rows forever, the only escape being
        an unhinted Tab.
        """
        if self.highlighted == 0:
            self.app.action_focus_previous()
        else:
            self.highlighted -= 1

    @_runtime.pump_only
    def action_select_offer(self) -> None:
        """Post the engine ``action_id`` under the keyboard cursor."""
        if not 0 <= self.highlighted < len(self._rows):
            return
        self.post_message(DepthOfferSelected(self._rows[self.highlighted][0]))

    @_runtime.pump_only
    def on_click(self, event: events.Click) -> None:
        """Post the engine ``action_id`` carried by the clicked row span."""
        action_id = event.style.meta.get(DEPTH_OFFER_ACTION_META)
        if type(action_id) is str:
            event.stop()
            self.post_message(DepthOfferSelected(action_id))
