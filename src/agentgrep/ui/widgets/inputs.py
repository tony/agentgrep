"""The search and filter input widgets.

These are ``Input`` subclasses. The dual-purpose arrow handling and the debounced
filter/find dispatch live here. Imported only from inside
``build_streaming_ui_app`` (and the tests), never eagerly, so ``import
agentgrep`` stays free of Textual (ADR 0010).
"""

from __future__ import annotations

import collections
import contextlib
import typing as t

from rich.highlighter import Highlighter
from textual import events
from textual.binding import Binding, BindingType
from textual.suggester import Suggester
from textual.timer import Timer
from textual.widgets import Input

from agentgrep.progress import FilterRequestedPayload, SearchRequestedPayload
from agentgrep.ui import _runtime
from agentgrep.ui._history import QUERY_TEXT_MAX_CHARS
from agentgrep.ui.widgets.messages import (
    DetailFindRequested,
    FilterRequested,
    SearchRequested,
)

__all__ = ["INPUT_MAX_LENGTH", "DetailFindInput", "FilterInput", "SearchInput"]

INPUT_MAX_LENGTH = QUERY_TEXT_MAX_CHARS
"""Maximum text processed by an interactive input on the message pump."""

_HIDDEN_EDITING_ALIASES = (
    Binding("shift+backspace", "delete_left", "Delete character left", show=False),
    Binding("shift+delete", "delete_right", "Delete character right", show=False),
)


def _consume_key(event: events.Key) -> None:
    """Stop bubbling and suppress Textual's base-class key action."""
    event.stop()
    event.prevent_default()


def _staged_ctrl_c(widget: Input, event: events.Key) -> bool:
    """Route a ctrl+c keypress through the app's staged-exit handler.

    Returns ``True`` (and stops the event) when the key was ctrl+c so the
    caller returns without falling through to the app quit binding. The
    staging itself — clear the input text first, then confirm-exit (or close
    the find bar) on an empty box — lives on the app
    (:meth:`_handle_input_ctrl_c`) so the rule is written once.
    """
    if str(getattr(event, "key", "")) != "ctrl+c":
        return False
    _consume_key(event)
    t.cast("t.Any", widget.screen)._handle_input_ctrl_c(widget)
    return True


def _disarm_confirm_exit(widget: Input) -> None:
    """Clear any pending confirm-exit when the user presses a non-ctrl+c key."""
    t.cast("t.Any", widget.screen)._disarm_confirm_exit()


class _BoundedInput(Input):
    """Input whose reactive value invariant also covers programmatic writes."""

    @_runtime.pump_only
    def validate_value(self, value: str) -> str:
        """Clamp every reactive assignment to the interactive text budget."""
        return value[:INPUT_MAX_LENGTH]


class FilterInput(_BoundedInput):
    """``Input`` subclass with debounced filter + cursor-or-focus arrows.

    The base ``Input.Changed`` event still fires immediately on each
    keystroke so the cursor, selection, and validation feedback stay
    instant. The expensive filter operation is deferred onto a
    :class:`FilterRequested` message which is only posted after 150 ms of
    typing inactivity, letting a worker run the actual filter without
    blocking the input itself.

    Up / down arrows are dual-purpose: when there's text in the input
    they jump the cursor to the start / end; when the input is empty (or
    the cursor is already at the relevant edge) they release focus to
    the previous / next widget so the user can navigate into the results
    table without reaching for Tab.
    """

    _DEBOUNCE_SECONDS: t.ClassVar[float] = 0.15

    BINDINGS: t.ClassVar[list[BindingType]] = [
        *_HIDDEN_EDITING_ALIASES,
        ("down", "release_down", "Results"),
    ]

    def __init__(
        self,
        *,
        placeholder: str = "",
        id: str | None = None,  # noqa: A002 -- forwarded to Textual's ``id`` kwarg
        suggester: Suggester | None = None,
        highlighter: Highlighter | None = None,
    ) -> None:
        super().__init__(
            placeholder=placeholder,
            id=id,
            max_length=INPUT_MAX_LENGTH,
            suggester=suggester,
            highlighter=highlighter,
        )
        self._debounce_timer: Timer | None = None

    @_runtime.pump_only
    def on_input_changed(self, event: Input.Changed) -> None:
        """Arm a debounced ``FilterRequested`` after a public change event."""
        value = event.value
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
        self._debounce_timer = self.set_timer(
            self._DEBOUNCE_SECONDS,
            lambda: self.post_message(
                FilterRequested(payload=FilterRequestedPayload(text=value)),
            ),
        )

    @_runtime.pump_only
    async def on_key(self, event: events.Key) -> None:
        """Down/up route between cursor-jump and focus-release per spec."""
        key = str(getattr(event, "key", ""))
        cursor = int(getattr(self, "cursor_position", 0))
        value = str(getattr(self, "value", ""))
        dropdown = t.cast("t.Any", getattr(self.screen, "_filter_dropdown", None))
        dropdown_open = dropdown is not None and bool(dropdown.display)
        if dropdown_open and key in {"escape", "ctrl+c"}:
            # Dismiss the dropdown, keep editing — don't quit.
            _consume_key(event)
            dropdown.display = False
            return
        if _staged_ctrl_c(self, event):
            return
        # Any other key cancels a pending "press ctrl-c again to exit".
        _disarm_confirm_exit(self)
        if dropdown_open and key == "enter":
            dropdown.display = False
        if key == "down":
            if dropdown_open and dropdown.option_count:
                # An open completion picker captures Down: jump into it.
                _consume_key(event)
                dropdown.focus()
                dropdown.highlighted = 0
                return
            if value and cursor < len(value):
                self.cursor_position = len(value)
                _consume_key(event)
                return
            # Empty or at end — release focus to next widget (DataTable)
            _consume_key(event)
            self.app.action_focus_next()
            return
        if key == "up":
            if value and cursor > 0:
                self.cursor_position = 0
                _consume_key(event)
                return
            # Empty or at start — release focus up to the top search bar
            # so plain ``up`` navigates filter → search without reaching
            # for Ctrl-K. Mirrors the symmetric ``down`` → results path.
            _consume_key(event)
            with contextlib.suppress(Exception):
                self.app.query_one("#search").focus()
            return
        if key == "right" and not value:
            # Empty filter → release focus rightward to the detail pane.
            # When the filter has text, fall through so the cursor can
            # walk through it character-by-character. Route through the
            # app's ``_focus_detail`` so a collapsed stacked pane is
            # revealed before focus lands.
            _consume_key(event)
            with contextlib.suppress(Exception):
                t.cast("t.Any", self.screen)._focus_detail()
            return

    @_runtime.pump_only
    def action_release_down(self) -> None:
        """Footer-binding fallback (``on_key`` handles the real release)."""
        self.app.action_focus_next()


class DetailFindInput(_BoundedInput):
    """``Input`` for find-in-detail, docked at the bottom of the detail pane.

    Separate from the search and filter inputs: typing posts a debounced
    :class:`DetailFindRequested`; ``enter`` / ``down`` step to the next match,
    ``up`` to the previous; ``escape`` and ``ctrl+c`` close the find and cancel
    it. The close keys are intercepted here (``event.stop()``) before the app's
    ``stop_search`` / ``smart_quit`` bindings fire, mirroring the dropdown
    dismissal in :class:`FilterInput` — so closing the find never quits the app.
    """

    _DEBOUNCE_SECONDS: t.ClassVar[float] = 0.12
    BINDINGS: t.ClassVar[list[BindingType]] = [*_HIDDEN_EDITING_ALIASES]

    def __init__(
        self,
        *,
        placeholder: str = "",
        id: str | None = None,  # noqa: A002 -- forwarded to Textual's ``id`` kwarg
    ) -> None:
        super().__init__(
            placeholder=placeholder,
            id=id,
            max_length=INPUT_MAX_LENGTH,
        )
        self._debounce_timer: Timer | None = None
        self._suppressed_change_values: collections.deque[str] = collections.deque()

    def load_query(self, value: str) -> None:
        """Set the value without posting a :class:`DetailFindRequested`.

        Used when restoring a record's remembered find query so the restore
        doesn't re-run the find (and reset the match cursor) via the debounce.
        """
        bounded = value[:INPUT_MAX_LENGTH]
        if bounded != self.value:
            self._suppressed_change_values.append(bounded)
            self.value = bounded
        self.cancel_pending_request()

    def cancel_pending_request(self) -> None:
        """Cancel the pending debounced find request, if any."""
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
            self._debounce_timer = None

    @_runtime.pump_only
    def on_input_changed(self, event: Input.Changed) -> None:
        """Arm a debounced find request after a public change event."""
        value = event.value
        self.cancel_pending_request()
        if self._suppressed_change_values and value == self._suppressed_change_values[0]:
            self._suppressed_change_values.popleft()
            return
        self._debounce_timer = self.set_timer(
            self._DEBOUNCE_SECONDS,
            lambda: self.post_message(DetailFindRequested(text=value)),
        )

    @_runtime.pump_only
    async def on_key(self, event: events.Key) -> None:
        """``esc`` closes; ``ctrl+c`` clears then closes; ``enter``/``down``/``up`` step."""
        key = str(getattr(event, "key", ""))
        app = t.cast("t.Any", self.screen)
        if key == "escape":
            _consume_key(event)
            app._close_detail_find()
            return
        # Staged ctrl+c: clear the query first; an empty box closes the bar
        # (the find's "exit" is closing, not quitting — see _handle_input_ctrl_c).
        if _staged_ctrl_c(self, event):
            return
        if key in {"enter", "down", "up"}:
            _consume_key(event)
            self.cancel_pending_request()
            value = str(getattr(self, "value", "") or "")
            if value != app._detail_find_query:
                app._run_detail_find(value, reset_cursor=True)
            app._detail_find_step(1 if key in {"enter", "down"} else -1)
            return


class SearchInput(_BoundedInput):
    """``Input`` subclass that fires :class:`SearchRequested` on Enter.

    Keystrokes update the input text immediately so the cursor stays
    instant, but no backend search runs until the user presses
    Enter. This makes the search explicit (no surprise dispatches
    while typing) and gives the cancel-existing-search logic a
    clean trigger to hang off of — every Enter cancels the prior
    worker before spawning a fresh one.
    """

    BINDINGS: t.ClassVar[list[BindingType]] = [
        *_HIDDEN_EDITING_ALIASES,
        ("down", "release_down", "Filter"),
    ]

    def __init__(
        self,
        *,
        value: str = "",
        placeholder: str = "",
        id: str | None = None,  # noqa: A002 -- forwarded to Textual's ``id`` kwarg
        suggester: Suggester | None = None,
        highlighter: Highlighter | None = None,
        label: str | None = None,
    ) -> None:
        super().__init__(
            value=value[:INPUT_MAX_LENGTH],
            placeholder=placeholder,
            id=id,
            max_length=INPUT_MAX_LENGTH,
            suggester=suggester,
            highlighter=highlighter,
        )
        self._label = label

    def load_query(self, value: str) -> None:
        """Load a bounded query and place the cursor at its end."""
        self.value = value[:INPUT_MAX_LENGTH]
        self.cursor_position = len(self.value)

    @_runtime.pump_only
    def on_mount(self) -> None:
        """Paint ``label`` into the top rule as the pi label-in-the-rule.

        pi embeds a right-aligned context label in an editor's top border
        (its scroll indicator / footer model name). Textual renders a
        widget's ``border_title`` on whatever border edge exists, so with
        the top/bottom-rule-only input styling this lands the label inside
        the top rule with no corners — the ``── search ─`` look. The label
        text is a neutral field identifier by default; reassign
        ``border_title`` at runtime to surface live state (scope, agent,
        mode) instead. Alignment and color live in ``styles.tcss``.
        """
        if self._label is not None:
            self.border_title = self._label

    @_runtime.pump_only
    def on_input_submitted(self, event: object) -> None:
        """Enter pressed — dispatch a :class:`SearchRequested` for the current value."""
        stop = getattr(event, "stop", None)
        if callable(stop):
            stop()
        value = str(getattr(self, "value", ""))
        self.post_message(
            SearchRequested(payload=SearchRequestedPayload(text=value)),
        )

    @_runtime.pump_only
    async def on_key(self, event: events.Key) -> None:
        """``down`` releases focus to the filter; ``up`` is a no-op (top widget)."""
        key = str(getattr(event, "key", ""))
        cursor = int(getattr(self, "cursor_position", 0))
        value = str(getattr(self, "value", ""))
        dropdown = t.cast("t.Any", getattr(self.screen, "_enum_dropdown", None))
        dropdown_open = dropdown is not None and bool(dropdown.display)
        if dropdown_open and key in {"escape", "ctrl+c"}:
            # Dismiss the dropdown, keep editing — don't quit or stop search.
            _consume_key(event)
            dropdown.display = False
            return
        if _staged_ctrl_c(self, event):
            return
        # Any other key cancels a pending "press ctrl-c again to exit".
        _disarm_confirm_exit(self)
        if dropdown_open and key == "enter":
            app = t.cast("t.Any", self.screen)
            if getattr(app, "_command_matches", ()):
                # Command menu: run the highlighted command rather than submit
                # the partial "/c" (which would flash an unknown-command error).
                _consume_key(event)
                app._run_command_at(int(getattr(dropdown, "highlighted", 0) or 0))
                return
            # Keyword completion: close the picker; the normal submit proceeds.
            dropdown.display = False
        if key == "down":
            if dropdown_open and dropdown.option_count:
                # An open enum picker captures Down: jump into it.
                _consume_key(event)
                dropdown.focus()
                dropdown.highlighted = 0
                return
            if value and cursor < len(value):
                self.cursor_position = len(value)
                _consume_key(event)
                return
            _consume_key(event)
            self.app.action_focus_next()
            return
        if key == "up":
            if value and cursor > 0:
                self.cursor_position = 0
                _consume_key(event)
                return
            _consume_key(event)
            return

    @_runtime.pump_only
    def action_release_down(self) -> None:
        """Footer-binding fallback (``on_key`` handles the real release)."""
        self.app.action_focus_next()
