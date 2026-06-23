"""The shared completion-dropdown widget.

``CompletionDropdown`` is an ``OptionList`` subclass shown over the results via
``overlay: screen``. Imported from inside the app factory (and the tests),
never eagerly.
"""

from __future__ import annotations

import contextlib

from textual import events
from textual.widgets import OptionList

__all__ = ["CompletionDropdown"]


class CompletionDropdown(OptionList):
    """Floating completion picker shared by the search and filter inputs.

    A plain ``OptionList`` shown over the results via ``overlay: screen``
    and toggled with ``display`` — the same lag-free mechanism Textual's
    own ``Select`` uses, so re-population on each keystroke never mounts a
    new widget. Enter fires ``OptionList.OptionSelected`` (handled by the
    app); Escape and up-at-top return focus to ``target_input_id``.
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

    async def _on_key(self, event: events.Key) -> None:
        key = str(getattr(event, "key", ""))
        stop = getattr(event, "stop", None)
        dismiss = key in {"escape", "ctrl+c"} or (
            key == "up" and int(getattr(self, "highlighted", 0) or 0) == 0
        )
        if dismiss:
            if callable(stop):
                stop()
            self.display = False
            with contextlib.suppress(Exception):
                self.app.query_one(f"#{self._target_input_id}").focus()
            return
        await super()._on_key(event)
