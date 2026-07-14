"""Bounded directory completion for the export dialog."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import functools
import itertools
import os
import pathlib
import typing as t

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.timer import Timer
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option
from textual.worker import NoActiveWorker, get_current_worker

from agentgrep.ui import _runtime

__all__ = [
    "DIRECTORY_CANDIDATE_LIMIT",
    "DIRECTORY_COMPLETION_DEBOUNCE",
    "DIRECTORY_SCAN_LIMIT",
    "DirectoryCandidate",
    "DirectoryCompletionPopup",
    "ExportDirectoryPicker",
]

DIRECTORY_COMPLETION_DEBOUNCE = 0.15
DIRECTORY_CANDIDATE_LIMIT = 6
DIRECTORY_SCAN_LIMIT = 256
_DIRECTORY_WORKER_GROUP = "export-directory-completion"
_TRUNCATION_LABEL = "… more entries"


@dataclasses.dataclass(frozen=True, slots=True)
class DirectoryCandidate:
    """One literal completion value and its compact display label."""

    value: str
    label: str


@dataclasses.dataclass(frozen=True, slots=True)
class _DirectoryCandidates:
    """One bounded directory-enumeration result."""

    values: tuple[DirectoryCandidate, ...]
    truncated: bool


def _active_worker_cancelled() -> bool:
    """Return whether the calling Textual worker has been cancelled."""
    try:
        return get_current_worker().is_cancelled
    except NoActiveWorker:
        return False


def _split_directory_prefix(value: str) -> tuple[pathlib.Path, str, str]:
    """Return scan parent, typed parent prefix, and partial basename."""
    if value.endswith(os.sep):
        display_parent = value
        prefix = ""
    else:
        _parent, prefix = os.path.split(value)
        display_parent = value[: -len(prefix)] if prefix else value
    scan_parent = pathlib.Path(display_parent or ".").expanduser()
    return scan_parent, display_parent, prefix


def _enumerate_directory_candidates(
    value: str,
    *,
    candidate_limit: int,
    scan_limit: int,
) -> _DirectoryCandidates:
    """Return a bounded page of matching, non-symlink child directories.

    The iterator pulls one row beyond ``scan_limit`` only to determine whether
    the result is truncated. That sentinel row is never probed or returned.

    Parameters
    ----------
    value : str
        Literal directory-input value, possibly ending in a partial basename.
    candidate_limit : int
        Maximum candidates returned to the UI.
    scan_limit : int
        Maximum raw directory entries inspected.

    Returns
    -------
    _DirectoryCandidates
        Bounded literal candidates and whether more raw entries exist.
    """
    bounded_candidate_limit = max(candidate_limit, 0)
    bounded_scan_limit = max(scan_limit, 0)
    if not value or not bounded_candidate_limit or not bounded_scan_limit:
        return _DirectoryCandidates((), False)
    scan_parent, display_parent, prefix = _split_directory_prefix(value)
    matches: list[DirectoryCandidate] = []
    truncated = False
    try:
        with os.scandir(scan_parent) as entries:
            for index, entry in enumerate(
                itertools.islice(entries, bounded_scan_limit + 1),
            ):
                if _active_worker_cancelled():
                    return _DirectoryCandidates((), False)
                if index == bounded_scan_limit:
                    truncated = True
                    break
                if not entry.name.startswith(prefix):
                    continue
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if not is_directory:
                    continue
                matches.append(
                    DirectoryCandidate(
                        value=f"{display_parent}{entry.name}{os.sep}",
                        label=entry.name,
                    ),
                )
    except OSError, RuntimeError, ValueError:
        return _DirectoryCandidates((), False)
    matches.sort(key=lambda candidate: candidate.label.casefold())
    return _DirectoryCandidates(tuple(matches[:bounded_candidate_limit]), truncated)


class DirectoryCompletionPopup(OptionList, can_focus=False):
    """Non-focusable literal directory completion rows."""

    def __init__(self) -> None:
        super().__init__(markup=False, compact=True)


class _DirectoryPathInput(Input):
    """Private path editor that delegates completion gestures to its picker."""

    BINDINGS: t.ClassVar[list[Binding]] = [
        Binding("up", "directory_up", "Previous directory", show=False),
        Binding("down", "directory_down", "Next directory", show=False),
        Binding("tab", "directory_tab", "Accept directory / next field", show=False),
    ]

    def __init__(self, owner: ExportDirectoryPicker, *, value: str) -> None:
        self._owner = owner
        super().__init__(value=value, placeholder="Export directory")

    @_runtime.pump_only
    def on_focus(self) -> None:
        """Refresh completion after focus returns to the field."""
        self._owner._schedule_enumeration()

    @_runtime.pump_only
    def on_blur(self) -> None:
        """Invalidate completion before focus reaches the next field."""
        self._owner._invalidate_completion()

    @_runtime.pump_only
    def action_directory_up(self) -> None:
        """Move to the previous visible completion."""
        self._owner._move_highlight(-1)

    @_runtime.pump_only
    def action_directory_down(self) -> None:
        """Move to the next visible completion."""
        self._owner._move_highlight(1)

    @_runtime.pump_only
    def action_cursor_right(self, select: bool = False) -> None:
        """Accept at the end or retain native cursor movement elsewhere."""
        if not select and self.cursor_at_end and self._owner._accept_highlighted():
            return
        super().action_cursor_right(select)

    @_runtime.pump_only
    def action_directory_tab(self) -> None:
        """Accept an open completion or resume normal focus traversal."""
        if self._owner._accept_highlighted():
            return
        self.app.action_focus_next()


class ExportDirectoryPicker(Vertical):
    """Own an export-directory input and its bounded completion popup."""

    DEFAULT_CSS = """
    ExportDirectoryPicker {
        height: 3;
        width: 1fr;
    }
    ExportDirectoryPicker > Input {
        height: 3;
        width: 100%;
    }
    ExportDirectoryPicker > DirectoryCompletionPopup {
        overlay: screen;
        constrain: inside inside;
        display: none;
        width: 100%;
        max-width: 100%;
        height: auto;
        max-height: 7;
        border: none;
        padding: 0;
    }
    """

    def __init__(self, value: str, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._input = _DirectoryPathInput(self, value=value)
        self._popup = DirectoryCompletionPopup()
        self._candidate_generation = 0
        self._candidate_values: tuple[DirectoryCandidate, ...] = ()
        self._debounce_timer: Timer | None = None
        self._pending_value = value

    @property
    def value(self) -> str:
        """Return the literal directory field value."""
        return self._input.value

    @value.setter
    def value(self, value: str) -> None:
        self._input.value = value

    @_runtime.pump_only
    def compose(self) -> ComposeResult:
        """Compose the private field and overlay exactly once."""
        yield self._input
        yield self._popup

    @_runtime.pump_only
    def focus_input(self) -> None:
        """Focus the picker-owned directory field."""
        self._input.focus()

    @_runtime.pump_only
    def on_input_changed(self, event: Input.Changed) -> None:
        """Debounce completion for the latest literal input value."""
        if event.input is self._input:
            self._schedule_enumeration()

    @_runtime.pump_only
    def on_unmount(self) -> None:
        """Cancel all completion work before the picker leaves the DOM."""
        self._invalidate_completion()

    @_runtime.pump_only
    def _schedule_enumeration(self) -> None:
        """Invalidate current chrome and arm one named inactivity timer."""
        self._invalidate_completion()
        self._pending_value = self._input.value
        if not self._pending_value or not self._input.has_focus:
            return
        self._debounce_timer = self.set_timer(
            DIRECTORY_COMPLETION_DEBOUNCE,
            self._debounce_elapsed,
        )

    @_runtime.pump_only
    def _debounce_elapsed(self) -> None:
        """Launch one worker for the value captured after inactivity."""
        self._debounce_timer = None
        value = self._pending_value
        generation = self._candidate_generation
        if not value or not self.is_mounted or not self._input.has_focus:
            return
        emit = _runtime.make_gated_emitter(
            self.app.call_from_thread,
            self._apply_candidates,
            generation,
        )
        self.run_worker(
            functools.partial(self._enumerate_in_thread, value, emit),
            name=_DIRECTORY_WORKER_GROUP,
            group=_DIRECTORY_WORKER_GROUP,
            description="enumerate export directories",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    @_runtime.offload
    def _enumerate_in_thread(
        self,
        value: str,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Enumerate one immutable snapshot away from the pump."""
        event = _enumerate_directory_candidates(
            value,
            candidate_limit=DIRECTORY_CANDIDATE_LIMIT,
            scan_limit=DIRECTORY_SCAN_LIMIT,
        )
        if not _active_worker_cancelled():
            emit(event)

    @_runtime.pump_only
    def _apply_candidates(self, generation: int, event: object) -> None:
        """Apply only a current focused picker's bounded worker result."""
        if (
            generation != self._candidate_generation
            or not self.is_mounted
            or not self._input.has_focus
            or not isinstance(event, _DirectoryCandidates)
        ):
            return
        self._candidate_values = event.values
        options: list[Option] = [Option(candidate.label) for candidate in event.values]
        if event.truncated:
            options.append(Option(_TRUNCATION_LABEL, disabled=True))
        self._popup.set_options(options)
        self._popup.highlighted = 0 if event.values else None
        self._popup.display = bool(options)

    @_runtime.pump_only
    def _invalidate_completion(self) -> None:
        """Stop pending work, advance generation, and clear completion chrome."""
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
            self._debounce_timer = None
        self._candidate_generation += 1
        self.workers.cancel_group(self, _DIRECTORY_WORKER_GROUP)
        self._candidate_values = ()
        self._popup.clear_options()
        self._popup.display = False

    @_runtime.pump_only
    def _move_highlight(self, step: int) -> None:
        """Move through selectable completion rows with wraparound."""
        if not self._popup.display or not self._candidate_values:
            return
        if step < 0:
            self._popup.action_cursor_up()
        else:
            self._popup.action_cursor_down()

    @_runtime.pump_only
    def _accept_highlighted(self) -> bool:
        """Replace the field with the highlighted selectable value."""
        if not self._popup.display or self._popup.highlighted is None:
            return False
        index = self._popup.highlighted
        if not 0 <= index < len(self._candidate_values):
            return False
        candidate = self._candidate_values[index]
        self._popup.display = False
        self._popup.clear_options()
        self._candidate_values = ()
        self._input.value = candidate.value
        self._input.cursor_position = len(candidate.value)
        return True
