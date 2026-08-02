"""The shared completion-dropdown widget.

``CompletionDropdown`` is an ``OptionList`` subclass shown over the results via
``overlay: screen``. Imported from inside the app factory (and the tests),
never eagerly.
"""

from __future__ import annotations

import contextlib

from textual import events
from textual.widgets import OptionList

from agentgrep.ui import _runtime

__all__ = ["CompletionDropdown"]


class CompletionDropdown(OptionList):
    """Floating completion picker shared by the search and filter inputs.

    A plain ``OptionList`` shown over the results via ``overlay: screen``
    and toggled with ``display`` — the same lag-free mechanism Textual's
    own ``Select`` uses, so re-population on each keystroke never mounts a
    new widget. Enter fires ``OptionList.OptionSelected`` (handled by the
    app); Escape and up-at-top return focus to ``target_input_id``.

    Modality is blur-driven, mirroring ``textual.widgets._select.SelectOverlay``
    — the reference implementation this class already claims to follow but,
    until :meth:`_on_blur` below, only borrowed the ``overlay``/``display``
    half of. Losing focus for *any* reason (a click elsewhere, Tab, another
    modal opening) dismisses this dropdown, the same way ``Select``'s own
    overlay dismisses itself; no ``ModalScreen``, no click-outside geometry
    to maintain. The owning input mirrors this from its own side (see
    ``_BoundedInput._on_blur`` in ``inputs.py``) for the case where the
    dropdown is shown but the input — not the dropdown — still holds focus.
    """

    def __init__(
        self,
        *,
        id: str | None = None,  # noqa: A002 -- forwarded to Textual's ``id`` kwarg
        target_input_id: str = "search",
    ) -> None:
        # Completion candidates are literal record terms / field names that
        # may contain Rich-markup characters (e.g. a term like ``[magenta]``
        # extracted from a record); render them as plain text so the option
        # list never tries to parse them as markup.
        super().__init__(id=id, markup=False)
        self._target_input_id = target_input_id

    @_runtime.pump_only
    def _on_blur(self, event: events.Blur) -> None:
        """Dismiss on losing focus, for any reason — see the class docstring.

        No ``super()`` call: Textual dispatches ``_on_blur`` to every
        ancestor class independently, so chaining here would double-fire
        ``Widget._on_blur``.
        """
        self.display = False

    @_runtime.pump_only
    async def on_key(self, event: events.Key) -> None:
        """Dismiss boundary keys or let the base option list handle them."""
        key = str(getattr(event, "key", ""))
        dismiss = key in {"escape", "ctrl+c"} or (
            key == "up" and int(getattr(self, "highlighted", 0) or 0) == 0
        )
        if dismiss:
            event.stop()
            event.prevent_default()
            self.display = False
            with contextlib.suppress(Exception):
                self.app.query_one(f"#{self._target_input_id}").focus()
            return
