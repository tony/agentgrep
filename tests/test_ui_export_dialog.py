"""Pilot contracts for the staged TUI export dialog."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import datetime
import os
import pathlib
import stat
import threading
import time
import typing as t

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.pilot import Pilot
from textual.widgets import Input, OptionList, Static

import agentgrep.ui.widgets as widgets
from agentgrep.ui import _runtime
from agentgrep.ui._export_preferences import ExportPreferences, default_export_directory
from agentgrep.ui.widgets import ExportDialog, ExportDraft, ExportIntent
from agentgrep.ui.widgets.directory_popup import ExportDirectoryPicker

_TIMESTAMP = datetime.datetime(2026, 7, 14, 9, 8, 7)


class _ExportDialogHost(App[None]):
    """Minimal host that pushes one export dialog and captures dismissal."""

    def __init__(
        self,
        home: pathlib.Path,
        on_confirm: cabc.Callable[[ExportIntent], bool],
        *,
        directory: str | None = None,
        template: str = "{date} {time} - {title}.md",
        title: str = "Machine [Readable] Title",
        timestamp: datetime.datetime = _TIMESTAMP,
    ) -> None:
        super().__init__()
        self._dialog = ExportDialog(
            title=title,
            fallback_title="record",
            home=home,
            preferences=ExportPreferences(
                directory=directory or str(home),
                filename_template=template,
            ),
            on_confirm=on_confirm,
            timestamp=timestamp,
        )
        self.dismissed: object = _UNSET

    @_runtime.pump_only
    def on_mount(self) -> None:
        """Bind the pump guard and open the dialog."""
        _runtime.bind_pump_thread()
        self.push_screen(self._dialog, self._capture)

    @_runtime.pump_only
    def on_unmount(self) -> None:
        """Release the process-wide pump binding."""
        _runtime.unbind_pump_thread()

    @_runtime.pump_only
    def _capture(self, value: None) -> None:
        """Capture the modal dismissal callback."""
        self.dismissed = value


_UNSET = object()


async def _wait_for(
    pilot: Pilot[None],
    predicate: cabc.Callable[[], bool],
    *,
    timeout: float = 3.0,
) -> None:
    """Yield to workers and the pump until ``predicate`` succeeds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.01)
    pytest.fail("timed out waiting for export-dialog state")


def _dialog(app: _ExportDialogHost) -> ExportDialog:
    """Return the mounted export dialog."""
    return t.cast("ExportDialog", app.screen)


def _text(app: _ExportDialogHost, selector: str) -> str:
    """Return the literal plain text last assigned to a ``Static``."""
    static = app.screen.query_one(selector, Static)
    content = getattr(static, "_Static__content", "")
    return getattr(content, "plain", str(content))


async def _open_review(app: _ExportDialogHost, pilot: Pilot[None]) -> None:
    """Submit the default draft and wait for its review stage."""
    await pilot.press("tab", "enter")
    await _wait_for(pilot, lambda: _dialog(app).phase == "review")


def test_export_dialog_interfaces_are_available_and_immutable(
    tmp_path: pathlib.Path,
) -> None:
    """The package exports the modal and its immutable boundary values."""
    assert widgets.ExportDialog is ExportDialog
    assert widgets.ExportDraft is ExportDraft
    assert widgets.ExportIntent is ExportIntent

    draft = ExportDraft(str(tmp_path), "{title}.md", _TIMESTAMP)
    intent = ExportIntent(tmp_path / "record.md", ExportPreferences(str(tmp_path)))
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.cast("t.Any", draft).directory = "changed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.cast("t.Any", intent).destination = tmp_path / "changed.md"


def test_review_letters_are_non_priority_bindings() -> None:
    """Focused editors receive ``n`` and ``y`` before review shortcuts."""
    bindings = {binding.key: binding for binding in ExportDialog.BINDINGS}

    assert bindings["n"].priority is False
    assert bindings["y"].priority is False
    assert bindings["ctrl+c"].priority is True


async def test_preview_is_frozen_literal_and_uses_no_filesystem(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Template edits compile only the title and frozen opening timestamp."""
    app = _ExportDialogHost(tmp_path, lambda _intent: True)
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.pause()
        assert _text(app, "#export-preview") == ("2026-07-14 09-08-07 - machine-readable-title.md")

        unexpected_message = "preview reached the filesystem"

        def unexpected_filesystem(*_args: object, **_kwargs: object) -> t.NoReturn:
            raise AssertionError(unexpected_message)

        monkeypatch.setattr(pathlib.Path, "stat", unexpected_filesystem)
        monkeypatch.setattr(pathlib.Path, "exists", unexpected_filesystem)
        monkeypatch.setattr(pathlib.Path, "is_dir", unexpected_filesystem)
        monkeypatch.setattr(pathlib.Path, "is_symlink", unexpected_filesystem)
        monkeypatch.setattr(os, "access", unexpected_filesystem)
        template = app.screen.query_one("#export-template", Input)
        template.value = "{time}-{title}.md"
        await pilot.pause()
        assert _text(app, "#export-preview") == "09-08-07-machine-readable-title.md"
        template.value = "{date}-{time}-{title}.md"
        await pilot.pause()
        assert _text(app, "#export-preview") == ("2026-07-14-09-08-07-machine-readable-title.md")


async def test_enter_moves_directory_to_template(tmp_path: pathlib.Path) -> None:
    """Enter in the directory field advances to the filename editor."""
    app = _ExportDialogHost(tmp_path, lambda _intent: True)
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.pause()
        picker = app.screen.query_one("#export-directory", ExportDirectoryPicker)
        assert picker.query_one(Input).has_focus

        await pilot.press("enter")

        assert app.screen.query_one("#export-template", Input).has_focus
        assert _dialog(app).phase == "edit"


async def test_directory_input_receives_n_and_y(tmp_path: pathlib.Path) -> None:
    """Review shortcut letters remain ordinary text in the directory editor."""
    app = _ExportDialogHost(tmp_path, lambda _intent: True)
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.pause()
        picker = app.screen.query_one("#export-directory", ExportDirectoryPicker)
        picker.value = ""

        await pilot.press("n", "y")

        assert picker.value == "ny"
        assert _dialog(app).phase == "edit"


async def test_template_input_receives_n_and_y(tmp_path: pathlib.Path) -> None:
    """Review shortcut letters remain ordinary text in the template editor."""
    app = _ExportDialogHost(tmp_path, lambda _intent: True)
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.press("tab")
        template = app.screen.query_one("#export-template", Input)
        template.value = ""

        await pilot.press("n", "y")

        assert template.value == "ny"
        assert _dialog(app).phase == "edit"


async def test_invalid_template_stays_edit_with_path_free_error(
    tmp_path: pathlib.Path,
) -> None:
    """An unsafe template never starts validation or exposes a path."""
    seen: list[ExportIntent] = []
    app = _ExportDialogHost(tmp_path, lambda intent: seen.append(intent) or True)
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.press("tab")
        template = app.screen.query_one("#export-template", Input)
        template.value = "{unknown}.md"
        await pilot.press("enter")
        await pilot.pause()

        assert _dialog(app).phase == "edit"
        assert _text(app, "#export-error") == "Export filename is invalid"
        assert str(tmp_path) not in _text(app, "#export-error")
        assert template.has_focus
        assert seen == []


async def test_validation_runs_off_pump(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directory authority checks execute only in the validator worker."""
    access_threads: list[int] = []
    original_access = os.access

    def observed_access(path: os.PathLike[str], mode: int) -> bool:
        access_threads.append(threading.get_ident())
        return original_access(path, mode)

    monkeypatch.setattr(os, "access", observed_access)
    pump_thread = threading.get_ident()
    app = _ExportDialogHost(tmp_path, lambda _intent: True)
    async with app.run_test(size=(60, 16)) as pilot:
        await _open_review(app, pilot)

        assert access_threads
        assert all(thread_id != pump_thread for thread_id in access_threads)


async def test_first_use_default_directory_is_created_privately(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean session can review the app-owned default without pre-creating it."""
    home = tmp_path / "home"
    data_home = tmp_path / "data"
    data_home.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    directory = default_export_directory(home)
    app = _ExportDialogHost(
        home,
        lambda _intent: True,
        directory=str(directory),
    )
    async with app.run_test(size=(60, 16)) as pilot:
        assert not directory.exists()
        await pilot.press("tab", "enter")
        await _wait_for(pilot, lambda: _dialog(app).phase != "validating")

        assert _dialog(app).phase == "review"
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


async def test_home_default_is_reviewed_as_tilde(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clean fallback default never exposes the absolute session home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    directory = default_export_directory(home)
    app = _ExportDialogHost(
        home,
        lambda _intent: True,
        directory=str(directory),
    )
    async with app.run_test(size=(60, 16)) as pilot:
        picker = app.screen.query_one("#export-directory", ExportDirectoryPicker)
        assert picker.value == "~/.local/share/agentgrep/exports"

        await _open_review(app, pilot)

        assert _text(app, "#export-review-directory") == "~/.local/share/agentgrep/exports"
        assert str(home) not in _text(app, "#export-review-directory")


async def test_directory_outside_home_remains_literal(tmp_path: pathlib.Path) -> None:
    """A selected directory outside the session home keeps its exact draft text."""
    home = tmp_path / "home"
    directory = tmp_path / "outside"
    home.mkdir()
    directory.mkdir()
    app = _ExportDialogHost(
        home,
        lambda _intent: True,
        directory=str(directory),
    )
    async with app.run_test(size=(60, 16)) as pilot:
        picker = app.screen.query_one("#export-directory", ExportDirectoryPicker)
        assert picker.value == str(directory)

        await _open_review(app, pilot)

        assert _text(app, "#export-review-directory") == str(directory)


async def test_submitted_absolute_home_directory_is_compacted(
    tmp_path: pathlib.Path,
) -> None:
    """A newly entered absolute home draft compacts before review."""
    home = tmp_path / "home"
    directory = home / "Exports"
    directory.mkdir(parents=True)
    app = _ExportDialogHost(home, lambda _intent: True)
    async with app.run_test(size=(60, 16)) as pilot:
        picker = app.screen.query_one("#export-directory", ExportDirectoryPicker)
        picker.value = str(directory)
        await pilot.press("enter", "enter")
        await _wait_for(pilot, lambda: _dialog(app).phase == "review")

        assert picker.value == "~/Exports"
        assert _text(app, "#export-review-directory") == "~/Exports"


async def test_missing_arbitrary_directory_is_not_created(tmp_path: pathlib.Path) -> None:
    """Validation never creates a missing user-entered directory tree."""
    directory = tmp_path / "missing" / "arbitrary"
    app = _ExportDialogHost(
        tmp_path / "home",
        lambda _intent: True,
        directory=str(directory),
    )
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.press("tab", "enter")
        await _wait_for(pilot, lambda: _dialog(app).phase != "validating")

        assert _dialog(app).phase == "edit"
        assert _text(app, "#export-error") == "Export directory is unavailable"
        assert not directory.exists()


async def test_default_directory_creation_rejects_symlinked_app_path(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default-directory exception cannot traverse an app-path symlink."""
    home = tmp_path / "home"
    data_home = tmp_path / "data"
    outside = tmp_path / "outside"
    data_home.mkdir()
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    (data_home / "agentgrep").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    directory = default_export_directory(home)
    app = _ExportDialogHost(
        home,
        lambda _intent: True,
        directory=str(directory),
    )
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.press("tab", "enter")
        await _wait_for(pilot, lambda: _dialog(app).phase != "validating")

        assert _dialog(app).phase == "edit"
        assert _text(app, "#export-error") == "Export directory is unavailable"
        assert sentinel.read_text(encoding="utf-8") == "keep"
        assert {entry.name for entry in outside.iterdir()} == {"keep.txt"}


async def test_existing_exact_destination_prevents_review(tmp_path: pathlib.Path) -> None:
    """Validation refuses the exact previewed basename instead of clobbering it."""
    destination = tmp_path / "2026-07-14 09-08-07 - machine-readable-title.md"
    destination.write_text("already here", encoding="utf-8")
    app = _ExportDialogHost(tmp_path, lambda _intent: True)
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.press("tab", "enter")
        await _wait_for(pilot, lambda: _dialog(app).phase == "edit")

        assert _text(app, "#export-error") == "Export destination already exists"
        assert str(tmp_path) not in _text(app, "#export-error")
        assert app.screen.query_one("#export-template", Input).has_focus


async def test_review_shows_directory_and_filename_literally(tmp_path: pathlib.Path) -> None:
    """Review renders user-controlled brackets as text, never as markup."""
    directory = tmp_path / "exports-[literal]"
    directory.mkdir()
    app = _ExportDialogHost(
        tmp_path,
        lambda _intent: True,
        directory=str(directory),
        title="Title [literal]",
    )
    async with app.run_test(size=(60, 16)) as pilot:
        await _open_review(app, pilot)

        assert _text(app, "#export-review-directory") == "~/exports-[literal]"
        assert _text(app, "#export-review-filename") == ("2026-07-14 09-08-07 - title-literal.md")
        confirm = app.screen.query_one("#export-confirm", OptionList)
        assert confirm._markup is False
        assert confirm.highlighted == 0


async def test_no_returns_to_editor_without_losing_values(
    tmp_path: pathlib.Path,
) -> None:
    """The default No row restores the editor with its prior draft."""
    seen: list[ExportIntent] = []
    app = _ExportDialogHost(tmp_path, lambda intent: seen.append(intent) or True)
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.press("tab", "enter")
        await _wait_for(pilot, lambda: _dialog(app).phase == "review")
        review = app.screen.query_one("#export-confirm", OptionList)
        assert review.highlighted == 0
        await pilot.press("enter")
        assert app.screen.query_one("#export-directory", ExportDirectoryPicker).value
        assert app.screen.query_one("#export-template", Input).value
        assert app.screen.query_one("#export-template", Input).has_focus
        assert _dialog(app).phase == "edit"
        assert seen == []


async def test_repeated_enter_on_default_no_cannot_save(tmp_path: pathlib.Path) -> None:
    """Repeated Enter alternates review and edit without selecting Save."""
    seen: list[ExportIntent] = []
    app = _ExportDialogHost(tmp_path, lambda intent: seen.append(intent) or True)
    async with app.run_test(size=(60, 16)) as pilot:
        await _open_review(app, pilot)
        await pilot.press("enter", "enter")
        await _wait_for(pilot, lambda: _dialog(app).phase == "review")
        await pilot.press("enter")

        assert _dialog(app).phase == "edit"
        assert seen == []


@pytest.mark.parametrize("key", ["n", "escape"])
async def test_no_shortcuts_return_to_edit(tmp_path: pathlib.Path, key: str) -> None:
    """The explicit No gestures preserve the draft and prior focus."""
    seen: list[ExportIntent] = []
    app = _ExportDialogHost(tmp_path, lambda intent: seen.append(intent) or True)
    async with app.run_test(size=(60, 16)) as pilot:
        await _open_review(app, pilot)
        await pilot.press(key)

        assert _dialog(app).phase == "edit"
        assert app.screen.query_one("#export-template", Input).has_focus
        assert seen == []


async def test_y_invokes_once_and_enters_saving(tmp_path: pathlib.Path) -> None:
    """Save delegates once and disables every further write gesture."""
    seen: list[ExportIntent] = []
    app = _ExportDialogHost(tmp_path, lambda intent: seen.append(intent) or True)
    async with app.run_test(size=(60, 16)) as pilot:
        await _open_review(app, pilot)
        await pilot.press("y", "y", "enter")

        assert _dialog(app).phase == "saving"
        confirm = app.screen.query_one("#export-confirm", OptionList)
        assert confirm.highlighted == 1
        assert confirm.disabled is True
        assert len(seen) == 1
        assert seen[0] == ExportIntent(
            destination=(tmp_path / "2026-07-14 09-08-07 - machine-readable-title.md"),
            preferences=ExportPreferences(
                directory="~",
                filename_template="{date} {time} - {title}.md",
            ),
        )


@pytest.mark.parametrize("key", ["escape", "ctrl+c"])
async def test_saving_ignores_cancel_keys(tmp_path: pathlib.Path, key: str) -> None:
    """A delegated durable write keeps its modal until worker completion."""
    app = _ExportDialogHost(tmp_path, lambda _intent: True)
    async with app.run_test(size=(60, 16)) as pilot:
        await _open_review(app, pilot)
        await pilot.press("y")
        dialog = _dialog(app)
        assert dialog.phase == "saving"

        await pilot.press(key)
        await pilot.pause()

        assert app.screen is dialog
        assert dialog.phase == "saving"


@pytest.mark.slow
async def test_ctrl_c_dismisses_from_review(tmp_path: pathlib.Path) -> None:
    """Ctrl-C cancels the reviewed draft while no durable worker is active."""
    app = _ExportDialogHost(tmp_path, lambda _intent: True)
    async with app.run_test(size=(60, 16)) as pilot:
        await _open_review(app, pilot)

        await pilot.press("ctrl+c")
        await _wait_for(pilot, lambda: app.dismissed is None)

        assert not app.query(ExportDialog)


@pytest.mark.parametrize("focused", ["directory", "template"])
@pytest.mark.slow
async def test_ctrl_c_clears_focused_edit_before_dismissal(
    tmp_path: pathlib.Path,
    focused: str,
) -> None:
    """Ctrl-C clears a focused edit once, then cancels its empty draft."""
    app = _ExportDialogHost(tmp_path, lambda _intent: True)
    async with app.run_test(size=(60, 16)) as pilot:
        dialog = _dialog(app)
        directory = dialog.query_one("#export-directory", ExportDirectoryPicker).query_one(Input)
        template = dialog.query_one("#export-template", Input)
        field, other = (directory, template) if focused == "directory" else (template, directory)
        other_value = other.value
        field.focus()
        await pilot.pause()

        await pilot.press("ctrl+c")
        await pilot.pause()

        assert app.screen is dialog
        assert dialog.phase == "edit"
        assert field.value == ""
        assert field.has_focus
        assert other.value == other_value

        await pilot.press("ctrl+c")
        await _wait_for(pilot, lambda: app.dismissed is None)

        assert not app.query(ExportDialog)


async def test_escape_dismisses_from_edit(tmp_path: pathlib.Path) -> None:
    """Escape cancels the dialog outside the review back-step."""
    app = _ExportDialogHost(tmp_path, lambda _intent: True)
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.press("escape")
        await _wait_for(pilot, lambda: app.dismissed is None)

        assert not app.query(ExportDialog)


async def test_export_failed_restores_edit_with_values(tmp_path: pathlib.Path) -> None:
    """An asynchronous write error returns to the retained draft."""
    app = _ExportDialogHost(tmp_path, lambda _intent: True)
    async with app.run_test(size=(60, 16)) as pilot:
        await _open_review(app, pilot)
        directory = app.screen.query_one("#export-directory", ExportDirectoryPicker).value
        template = app.screen.query_one("#export-template", Input).value
        await pilot.press("y")
        _dialog(app).export_failed("Export [failed]")
        await pilot.pause()

        assert _dialog(app).phase == "edit"
        assert app.screen.query_one("#export-directory", ExportDirectoryPicker).value == directory
        assert app.screen.query_one("#export-template", Input).value == template
        assert _text(app, "#export-error") == "Export [failed]"
        assert app.screen.query_one("#export-template", Input).has_focus


async def test_export_succeeded_dismisses(tmp_path: pathlib.Path) -> None:
    """An asynchronous write success closes the retained saving modal."""
    app = _ExportDialogHost(tmp_path, lambda _intent: True)
    async with app.run_test(size=(60, 16)) as pilot:
        await _open_review(app, pilot)
        await pilot.press("y")
        _dialog(app).export_succeeded()
        await _wait_for(pilot, lambda: app.dismissed is None)

        assert not app.query(ExportDialog)


async def test_dialog_fits_compact_terminal_without_horizontal_scroll(
    tmp_path: pathlib.Path,
) -> None:
    """The single modal stays inside a 60 by 16 terminal in both stages."""
    app = _ExportDialogHost(tmp_path, lambda _intent: True)
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.pause()
        dialog_body = app.screen.query_one("#export-dialog")
        edit = app.screen.query_one("#export-edit", VerticalScroll)
        assert dialog_body.region.width <= 60
        assert dialog_body.region.height <= 16
        assert edit.show_vertical_scrollbar is False
        await _open_review(app, pilot)
        review = app.screen.query_one("#export-review", VerticalScroll)
        assert dialog_body.region.width <= 60
        assert dialog_body.region.height <= 16
        assert review.show_vertical_scrollbar is False


@pytest.mark.parametrize("size", [(40, 12), (30, 10)])
async def test_invalid_template_error_is_visible_in_small_terminal(
    size: tuple[int, int],
    tmp_path: pathlib.Path,
) -> None:
    """Inline edit feedback remains inside a narrow tmux viewport."""
    app = _ExportDialogHost(tmp_path, lambda _intent: True)
    async with app.run_test(size=size) as pilot:
        await pilot.press("tab")
        template = app.screen.query_one("#export-template", Input)
        template.value = "{unknown}.md"
        await pilot.press("enter")
        await pilot.pause()
        error = app.screen.query_one("#export-error", Static)

        assert _text(app, "#export-error") == "Export filename is invalid"
        assert error.region.y >= 0
        assert error.region.bottom <= size[1]
        assert template.has_focus


@pytest.mark.parametrize("size", [(40, 12), (30, 10)])
async def test_review_and_edit_are_reachable_in_small_terminal(
    size: tuple[int, int],
    tmp_path: pathlib.Path,
) -> None:
    """The confirmation and retained editor remain keyboard-reachable when compact."""
    app = _ExportDialogHost(tmp_path, lambda _intent: True)
    async with app.run_test(size=size) as pilot:
        await _open_review(app, pilot)
        confirm = app.screen.query_one("#export-confirm", OptionList)

        assert confirm.has_focus
        assert confirm.region.y >= 0
        assert confirm.region.bottom <= size[1]

        await pilot.press("n")
        template = app.screen.query_one("#export-template", Input)
        assert _dialog(app).phase == "edit"
        assert template.has_focus
        assert template.region.y >= 0
        assert template.region.bottom <= size[1]
