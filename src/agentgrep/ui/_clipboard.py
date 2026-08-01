"""Shared clipboard behavior for the explorer's screens.

Textual resolves a screen selection through
:meth:`~textual.screen.Screen.get_selected_text`, which returns ``None`` only
when nothing is selected at all. A drag over a widget whose visual is not
selectable -- a JSON record body renders as a Rich ``Syntax``, and
``Widget.get_selection`` yields ``None`` for it -- leaves ``Screen.selections``
populated while every widget contributes nothing, so the resolved text is
``""`` rather than ``None``. Stock ``action_copy_text`` treats that as a
successful copy: it overwrites the clipboard with an empty string and marks the
key handled, so a second binding on the same key never runs.

:class:`CopySelectionGuard` closes that hole for every screen that mixes it in.
It lives here rather than on one layout because the explorer has three screen
families -- the layouts plus the theme and history modals -- and all three
inherit Textual's copy binding.

Delivery itself is unverifiable. ``App.copy_to_clipboard`` writes one bare
OSC-52 escape and returns nothing; the sequence has no acknowledgement, and
several ordinary setups discard it in silence -- macOS Terminal (Textual's own
docstring says so), a relay that filters OSC, and tmux left on its shipped
``set-clipboard external`` default. :func:`copy_notice` therefore reports what
was *sent* and how much of it, never that it arrived.

Detecting tmux's option value would mean running ``tmux show -gv
set-clipboard``, a subprocess, which ADR 0011 NB-1 forbids anywhere the message
pump can reach. Reading ``TMUX`` from the environment is an O(1) mapping
lookup, so :func:`tmux_clipboard_hint` surfaces the caveat instead.
"""

from __future__ import annotations

import collections.abc as cabc
import os
import typing as t

from textual.actions import SkipAction

from agentgrep.ui import _runtime

__all__ = [
    "TMUX_CLIPBOARD_HINT",
    "CopySelectionGuard",
    "copy_notice",
    "tmux_clipboard_hint",
]

TMUX_CLIPBOARD_HINT = (
    "Inside tmux: OSC 52 is dropped unless your tmux.conf has `set -g set-clipboard on`."
)
"""One-time caveat shown when the explorer copies from inside a tmux pane."""


def copy_notice(text: str, *, label: str, truncated: bool = False) -> str:
    """Return the toast for a copy of ``text``.

    Reports the action taken rather than asserting delivery, and names the
    mechanism so a failed paste is diagnosable from the toast alone.

    Parameters
    ----------
    text : str
        Exact payload handed to ``App.copy_to_clipboard``.
    label : str
        What was copied, in user vocabulary (``"selection"``, ``"source"``).
    truncated : bool
        ``True`` when the payload is a bounded prefix of the record text.

    Returns
    -------
    str
        Notification body.

    Examples
    --------
    >>> copy_notice("hello", label="selection")
    'sent selection to the clipboard (5 chars, OSC 52)'
    >>> copy_notice("x" * 2048, label="source", truncated=True)
    'sent source to the clipboard (2,048 chars, truncated, OSC 52)'
    >>> copy_notice("", label="row")
    'sent row to the clipboard (0 chars, OSC 52)'
    """
    detail = f"{len(text):,} chars"
    if truncated:
        detail = f"{detail}, truncated"
    return f"sent {label} to the clipboard ({detail}, OSC 52)"


def tmux_clipboard_hint(
    environ: cabc.Mapping[str, str] | None = None,
) -> str | None:
    """Return the tmux caveat when running inside tmux, else ``None``.

    ``TMUX`` names the innermost server only and reports no nesting depth, so
    it answers "is a tmux server between us and the terminal?" and nothing
    more. That is exactly the question the caveat needs.

    Parameters
    ----------
    environ : collections.abc.Mapping[str, str] | None
        Environment mapping; defaults to :data:`os.environ`.

    Returns
    -------
    str | None
        :data:`TMUX_CLIPBOARD_HINT` inside tmux, otherwise ``None``.

    Examples
    --------
    >>> tmux_clipboard_hint({}) is None
    True
    >>> tmux_clipboard_hint({"TMUX": ""}) is None
    True
    >>> tmux_clipboard_hint({"TMUX": "/tmp/tmux-1000/default,108550,24"})
    'Inside tmux: OSC 52 is dropped unless your tmux.conf has `set -g set-clipboard on`.'
    """
    environment = os.environ if environ is None else environ
    return TMUX_CLIPBOARD_HINT if environment.get("TMUX") else None


class CopySelectionGuard:
    """Screen mixin owning how the explorer delivers text to the clipboard.

    Mix in ahead of the Textual screen base so the override wins:
    ``class Foo(CopySelectionGuard, ModalScreen[None])``.

    Carries both halves so every screen family gets the same rules: the
    empty-selection guard on :meth:`action_copy_text`, and the delivery and
    wording in :meth:`send_to_clipboard`. A screen that shadows the copy chord
    with a ``priority=True`` binding of its own still reaches the latter, which
    is the only way the once-per-session tmux caveat can be session-wide.
    """

    #: Class-level default so a mixin with no ``__init__`` still reads cleanly;
    #: the first copy shadows it with an instance attribute.
    _clipboard_hint_shown = False

    @_runtime.pump_only
    def send_to_clipboard(self, text: str, *, label: str, truncated: bool = False) -> None:
        """Copy ``text`` and report what was sent, never that it arrived.

        Every copy path funnels through here so one wording rule covers them
        all. ``App.copy_to_clipboard`` writes one bare OSC-52 escape and returns
        nothing, so success is not observable; the toast names the payload size
        and the mechanism, and the first copy of a session inside tmux also
        carries the ``set-clipboard`` caveat. Both the encode and the driver
        write are bounded and the ``TMUX`` lookup is O(1), so nothing here
        blocks the pump (ADR 0011 NB-1).

        Parameters
        ----------
        text : str
            Payload to deliver (already bounded by the caller).
        label : str
            What was copied, in user vocabulary (``"selection"``, ``"source"``).
        truncated : bool
            ``True`` when the payload is a bounded prefix of the record text.
        """
        screen = t.cast("t.Any", self)
        screen.app.copy_to_clipboard(text)
        screen.notify(copy_notice(text, label=label, truncated=truncated))
        if self._clipboard_hint_shown:
            return
        self._clipboard_hint_shown = True
        if (hint := tmux_clipboard_hint()) is not None:
            screen.notify(hint, title="Clipboard", severity="warning")

    @_runtime.pump_only
    def action_copy_text(self) -> None:
        """Copy the screen selection, skipping when it yields no text.

        Raising :class:`~textual.actions.SkipAction` leaves the keypress
        unhandled, so it falls through to whatever else is bound to the same
        key -- which is what makes one chord able to both copy a live selection
        and quit when there is none.

        Clearing the selection afterwards keeps ``ctrl+c`` a reliable abort:
        one press copies, the next reaches the layout's stop/quit staging. It
        also matches the detail pane's own ``y`` yank, which exits visual mode.
        """
        screen = t.cast("t.Any", self)
        selection = screen.get_selected_text()
        if not selection:
            raise SkipAction
        self.send_to_clipboard(selection, label="selection")
        screen.clear_selection()
