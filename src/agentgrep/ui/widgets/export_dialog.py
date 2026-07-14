"""Staged, no-clobber export confirmation for one selected record."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import datetime
import functools
import os
import pathlib
import typing as t

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.worker import NoActiveWorker, get_current_worker

from agentgrep.ui import _runtime
from agentgrep.ui._export_preferences import (
    ExportPreferences,
    ExportPreferencesError,
    default_export_directory,
    render_export_filename,
    resolve_export_directory,
)
from agentgrep.ui.widgets.directory_popup import ExportDirectoryPicker

__all__ = ["ExportDialog", "ExportDraft", "ExportIntent"]

_VALIDATION_WORKER_GROUP = "export-dialog-validation"
_DIRECTORY_ERROR = "Export directory is invalid"
_DIRECTORY_UNAVAILABLE_ERROR = "Export directory is unavailable"
_DIRECTORY_ACCESS_ERROR = "Export directory is not writable"
_DESTINATION_EXISTS_ERROR = "Export destination already exists"

ExportPhase = t.Literal["edit", "validating", "review", "saving"]


@dataclasses.dataclass(frozen=True, slots=True)
class ExportDraft:
    """One retained edit-stage snapshot."""

    directory: str
    filename_template: str
    timestamp: datetime.datetime


@dataclasses.dataclass(frozen=True, slots=True)
class ExportIntent:
    """One exact reviewed destination and the preferences that produced it."""

    destination: pathlib.Path
    preferences: ExportPreferences


@dataclasses.dataclass(frozen=True, slots=True)
class _ValidationResult:
    """One typed, path-free validator result."""

    intent: ExportIntent | None = None
    error: str | None = None


def _active_worker_cancelled() -> bool:
    """Return whether the calling Textual worker has been cancelled."""
    try:
        return get_current_worker().is_cancelled
    except NoActiveWorker:
        return False


def _validate_export_draft(
    draft: ExportDraft,
    *,
    title: str,
    fallback_title: str,
    home: pathlib.Path,
) -> _ValidationResult:
    """Validate one immutable draft away from the Textual pump."""
    from agentgrep.record_export import ExportError, _ensure_private_directory

    try:
        filename = render_export_filename(
            draft.filename_template,
            title,
            fallback_title,
            draft.timestamp,
        )
        directory = resolve_export_directory(draft.directory, home)
    except ExportPreferencesError:
        return _ValidationResult(error=_DIRECTORY_ERROR)

    if directory == default_export_directory(home):
        try:
            _ensure_private_directory(directory)
        except ExportError:
            return _ValidationResult(error=_DIRECTORY_UNAVAILABLE_ERROR)

    try:
        if directory.is_symlink() or not directory.is_dir():
            return _ValidationResult(error=_DIRECTORY_UNAVAILABLE_ERROR)
        if not os.access(directory, os.W_OK | os.X_OK):
            return _ValidationResult(error=_DIRECTORY_ACCESS_ERROR)
        destination = directory / filename
        if os.path.lexists(destination):
            return _ValidationResult(error=_DESTINATION_EXISTS_ERROR)
    except OSError, RuntimeError, ValueError:
        return _ValidationResult(error=_DIRECTORY_UNAVAILABLE_ERROR)

    return _ValidationResult(
        intent=ExportIntent(
            destination=destination,
            preferences=ExportPreferences(
                directory=draft.directory,
                filename_template=draft.filename_template,
            ),
        ),
    )


class ExportDialog(ModalScreen[None]):
    """Edit, validate, review, and retain one selected-record export."""

    BINDINGS: t.ClassVar[list[Binding]] = [
        Binding("escape", "escape", "Back / Cancel", priority=True, show=False),
        Binding("ctrl+c", "cancel", "Cancel", priority=True, show=False),
        Binding("n", "review_no", "No", show=False),
        Binding("y", "review_save", "Save", show=False),
    ]

    DEFAULT_CSS = """
    ExportDialog {
        align: center middle;
    }
    #export-dialog {
        width: 100%;
        max-width: 72;
        height: 100%;
        max-height: 18;
        padding: 0 2;
    }
    #export-edit, #export-review {
        width: 100%;
        height: 1fr;
    }
    .export-label {
        width: 100%;
        height: 1;
    }
    #export-directory, #export-template {
        width: 100%;
        height: 3;
    }
    #export-preview, #export-review-directory, #export-review-filename {
        width: 100%;
        height: auto;
        max-height: 3;
        text-wrap: wrap;
    }
    #export-error, #export-edit-footer, #export-review-status {
        width: 100%;
        height: 1;
    }
    #export-review {
        display: none;
    }
    #export-confirm {
        width: 100%;
        height: 2;
    }
    """

    def __init__(
        self,
        title: str,
        fallback_title: str,
        home: pathlib.Path,
        preferences: ExportPreferences,
        on_confirm: cabc.Callable[[ExportIntent], bool],
        timestamp: datetime.datetime | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._fallback_title = fallback_title
        self._home = home
        self._on_confirm = on_confirm
        self._timestamp = timestamp or datetime.datetime.now().astimezone()
        self._initial_preferences = preferences
        self._phase: ExportPhase = "edit"
        self._validation_generation = 0
        self._intent: ExportIntent | None = None
        self._edit_focus = "template"

    @property
    def phase(self) -> ExportPhase:
        """Return the dialog's current interaction phase."""
        return self._phase

    @_runtime.pump_only
    def compose(self) -> ComposeResult:
        """Compose one quiet edit/review flow with literal output surfaces."""
        with Vertical(id="export-dialog"):
            with VerticalScroll(id="export-edit"):
                yield Static("Directory", classes="export-label")
                yield ExportDirectoryPicker(
                    value=self._initial_preferences.directory,
                    home=self._home,
                    id="export-directory",
                )
                yield Static("Template", classes="export-label")
                yield Input(
                    value=self._initial_preferences.filename_template,
                    placeholder="Filename template",
                    id="export-template",
                )
                yield Static("Exact filename", classes="export-label")
                yield Static("", id="export-preview", markup=False)
                yield Static("", id="export-error", markup=False)
                yield Static(
                    "Tab to move · Enter to review · Ctrl-C to cancel",
                    id="export-edit-footer",
                    markup=False,
                )
            with VerticalScroll(id="export-review"):
                yield Static("Directory", classes="export-label")
                yield Static("", id="export-review-directory", markup=False)
                yield Static("Filename", classes="export-label")
                yield Static("", id="export-review-filename", markup=False)
                yield OptionList("No", "Save", id="export-confirm", markup=False, compact=True)
                yield Static("", id="export-review-status", markup=False)

    @_runtime.pump_only
    def on_mount(self) -> None:
        """Render the frozen preview and focus the directory editor."""
        self._refresh_preview()
        self.query_one("#export-directory", ExportDirectoryPicker).focus_input()

    @_runtime.pump_only
    def on_unmount(self) -> None:
        """Invalidate and cancel validator work before teardown."""
        self._validation_generation += 1
        self.workers.cancel_group(self, _VALIDATION_WORKER_GROUP)

    @_runtime.pump_only
    def on_input_changed(self, event: Input.Changed) -> None:
        """Refresh the pure filename preview after template edits."""
        if event.input.id == "export-template" and self._phase == "edit":
            self._refresh_preview()

    @_runtime.pump_only
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Advance directory to template, then validate the submitted draft."""
        directory_input = self.query_one("#export-directory", ExportDirectoryPicker).query_one(
            Input,
        )
        if event.input is directory_input and self._phase == "edit":
            event.stop()
            self.query_one("#export-template", Input).focus()
            return
        if event.input.id == "export-template" and self._phase == "edit":
            event.stop()
            self._start_validation()

    @_runtime.pump_only
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle the two literal review rows while review is active."""
        if event.option_list.id != "export-confirm" or self._phase != "review":
            return
        if event.option_index == 0:
            self._show_edit()
        elif event.option_index == 1:
            self._confirm()

    @_runtime.pump_only
    def action_escape(self) -> None:
        """Return from review or cancel before a durable save begins."""
        if self._phase == "saving":
            return
        if self._phase == "review":
            self._show_edit()
            return
        self.dismiss(None)

    @_runtime.pump_only
    def action_cancel(self) -> None:
        """Dismiss unless a durable save is already active."""
        if self._phase != "saving":
            self.dismiss(None)

    @_runtime.pump_only
    def action_review_no(self) -> None:
        """Return to the retained draft only while reviewing."""
        if self._phase == "review":
            self._show_edit()

    @_runtime.pump_only
    def action_review_save(self) -> None:
        """Delegate the reviewed intent only while reviewing."""
        if self._phase == "review":
            self._confirm()

    @_runtime.pump_only
    def export_failed(self, message: str) -> None:
        """Restore editing with retained values after an asynchronous failure."""
        if self.is_mounted and self._phase == "saving":
            self._show_edit(message)

    @_runtime.pump_only
    def export_succeeded(self) -> None:
        """Dismiss after the asynchronous writer reports success."""
        if self.is_mounted and self._phase == "saving":
            self.dismiss(None)

    @_runtime.pump_only
    def _refresh_preview(self) -> bool:
        """Compile only the frozen, Textual-free filename preview."""
        template = self.query_one("#export-template", Input).value
        try:
            filename = render_export_filename(
                template,
                self._title,
                self._fallback_title,
                self._timestamp,
            )
        except ExportPreferencesError as error:
            self.query_one("#export-preview", Static).update(Content(""))
            self._update_error(str(error))
            return False
        self.query_one("#export-preview", Static).update(Content(filename))
        self._update_error("")
        return True

    @_runtime.pump_only
    def _update_error(self, message: str) -> None:
        """Update inline feedback and expose it in a compact scrolling edit stage."""
        error = self.query_one("#export-error", Static)
        error.update(Content(message))
        if message:
            error.scroll_visible(animate=False, immediate=True)

    @_runtime.pump_only
    def _start_validation(self) -> None:
        """Snapshot the draft and launch one exclusive validator worker."""
        if not self._refresh_preview():
            return
        template = self.query_one("#export-template", Input)
        picker = self.query_one("#export-directory", ExportDirectoryPicker)
        self._edit_focus = "template" if template.has_focus else "directory"
        draft = ExportDraft(
            directory=picker.value,
            filename_template=template.value,
            timestamp=self._timestamp,
        )
        self._phase = "validating"
        picker.disabled = True
        template.disabled = True
        self.query_one("#export-edit-footer", Static).update(Content("Validating…"))
        self._validation_generation += 1
        generation = self._validation_generation
        emit = _runtime.make_gated_emitter(
            self.app.call_from_thread,
            self._apply_validation,
            generation,
        )
        self.run_worker(
            functools.partial(
                self._validate_in_thread,
                draft,
                self._title,
                self._fallback_title,
                self._home,
                emit,
            ),
            name=_VALIDATION_WORKER_GROUP,
            group=_VALIDATION_WORKER_GROUP,
            description="validate export destination",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    @_runtime.offload
    def _validate_in_thread(
        self,
        draft: ExportDraft,
        title: str,
        fallback_title: str,
        home: pathlib.Path,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Validate one immutable snapshot away from the pump."""
        result = _validate_export_draft(
            draft,
            title=title,
            fallback_title=fallback_title,
            home=home,
        )
        if not _active_worker_cancelled():
            emit(result)

    @_runtime.pump_only
    def _apply_validation(self, generation: int, event: object) -> None:
        """Apply only the current typed validator result."""
        if (
            generation != self._validation_generation
            or not self.is_mounted
            or self._phase != "validating"
            or not isinstance(event, _ValidationResult)
        ):
            return
        if event.intent is None:
            self._show_edit(event.error or _DIRECTORY_UNAVAILABLE_ERROR)
            return
        self._intent = event.intent
        self._show_review(event.intent)

    @_runtime.pump_only
    def _show_edit(self, error: str | None = None) -> None:
        """Restore the retained edit stage and its prior focus."""
        self._phase = "edit"
        self._intent = None
        edit = self.query_one("#export-edit", VerticalScroll)
        review = self.query_one("#export-review", VerticalScroll)
        edit.display = True
        review.display = False
        picker = self.query_one("#export-directory", ExportDirectoryPicker)
        template = self.query_one("#export-template", Input)
        picker.disabled = False
        template.disabled = False
        self.query_one("#export-edit-footer", Static).update(
            Content("Tab to move · Enter to review · Ctrl-C to cancel"),
        )
        self._refresh_preview()
        if error is not None:
            self._update_error(error)
        if self._edit_focus == "directory":
            picker.focus_input()
        else:
            template.focus()

    @_runtime.pump_only
    def _show_review(self, intent: ExportIntent) -> None:
        """Show the literal directory and exact basename with No selected."""
        self._phase = "review"
        self.query_one("#export-edit", VerticalScroll).display = False
        self.query_one("#export-review", VerticalScroll).display = True
        self.query_one("#export-review-directory", Static).update(
            Content(intent.preferences.directory),
        )
        self.query_one("#export-review-filename", Static).update(
            Content(intent.destination.name),
        )
        status = self.query_one("#export-review-status", Static)
        status.update(Content(""))
        confirm = self.query_one("#export-confirm", OptionList)
        confirm.disabled = False
        confirm.highlighted = 0
        confirm.focus()

    @_runtime.pump_only
    def _confirm(self) -> None:
        """Delegate once and retain the modal while the writer is active."""
        intent = self._intent
        if self._phase != "review" or intent is None:
            return
        if not self._on_confirm(intent):
            return
        self._phase = "saving"
        confirm = self.query_one("#export-confirm", OptionList)
        confirm.highlighted = 1
        confirm.disabled = True
        self.query_one("#export-review-status", Static).update(Content("Saving…"))
