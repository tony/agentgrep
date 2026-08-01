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
"""

from __future__ import annotations

import typing as t

from textual.actions import SkipAction

from agentgrep.ui import _runtime

__all__ = ["CopySelectionGuard"]


class CopySelectionGuard:
    """Screen mixin that refuses to copy a selection resolving to no text.

    Mix in ahead of the Textual screen base so this override wins:
    ``class Foo(CopySelectionGuard, ModalScreen[None])``.
    """

    @_runtime.pump_only
    def action_copy_text(self) -> None:
        """Copy the screen selection, skipping when it yields no text.

        Raising :class:`~textual.actions.SkipAction` leaves the keypress
        unhandled, so it falls through to whatever else is bound to the same
        key -- which is what makes one chord able to both copy a live selection
        and quit when there is none.
        """
        screen = t.cast("t.Any", self)
        if not screen.get_selected_text():
            raise SkipAction
        t.cast("t.Any", super()).action_copy_text()
