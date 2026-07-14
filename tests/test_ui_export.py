"""Mounted export-command tests for the pi-like Textual HUD."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import pathlib
import stat
import threading
import time
import typing as t

import pytest
from textual.widgets import HelpPanel, Input, Static

import agentgrep.identity as identity
import agentgrep.record_export as record_export
from agentgrep.records import RecordPosition, SearchRecord
from agentgrep.ui import _export_preferences, _runtime
from agentgrep.ui._export_preferences import (
    ExportPreferences,
    ExportPreferencesError,
    export_preferences_path,
    load_export_preferences,
    save_export_preferences,
)
from agentgrep.ui.layouts import hud as hud_module
from agentgrep.ui.widgets import ExportDialog, FilterCompleted
from agentgrep.ui.widgets.directory_popup import ExportDirectoryPicker
from tests._agentgrep_tui_support import _build_empty_ui_app
from tests.test_agentgrep_tui import _search_requested

pytestmark = pytest.mark.tui


def _record(
    tmp_path: pathlib.Path,
    text: str,
    *,
    ordinal: int,
    session_id: str | None = "session-a",
    source_name: str | None = None,
    title: str | None = None,
) -> SearchRecord:
    """Build one normalized source record with deterministic identities."""
    return SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / (source_name or f"source-{ordinal}.jsonl"),
        text=text,
        title=title,
        role="user",
        timestamp=f"2026-07-12T12:00:{ordinal:02d}Z",
        model="gpt-test",
        session_id=session_id,
        conversation_id=session_id,
        identity_namespace="codex.session" if session_id is not None else None,
        position=RecordPosition(ordinal=ordinal, quality="source_order"),
    )


async def _load_records(
    screen: t.Any,
    records: tuple[SearchRecord, ...],
    *,
    selected: int = 0,
) -> None:
    """Mount records through the bounded result applier and select one row."""
    await screen._apply_records_batch(records, len(records))
    screen._results.highlighted = selected
    screen._current_detail_record = records[selected]


async def _wait_for(predicate: t.Callable[[], bool], *, timeout: float = 3.0) -> None:
    """Yield until a worker-observable condition is true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("timed out waiting for export worker")


def _static_text(dialog: ExportDialog, selector: str) -> str:
    """Return the literal plain text last assigned to a dialog ``Static``."""
    static = dialog.query_one(selector, Static)
    content = getattr(static, "_Static__content", "")
    return getattr(content, "plain", str(content))


async def _open_export_review(
    app: t.Any,
    pilot: t.Any,
    *,
    directory: pathlib.Path,
    template: str,
) -> tuple[ExportDialog, str]:
    """Open the selected-record dialog and advance one draft to review."""
    await pilot.press("e")
    await pilot.pause()
    assert isinstance(app.screen, ExportDialog)
    dialog = app.screen
    dialog.query_one("#export-directory", ExportDirectoryPicker).value = str(directory)
    template_input = dialog.query_one("#export-template", Input)
    template_input.value = template
    template_input.focus()
    await pilot.press("enter")
    await _wait_for(lambda: dialog.phase == "review")
    return dialog, _static_text(dialog, "#export-review-filename")


def _capture_notifications(
    screen: t.Any,
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[tuple[object, ...], dict[str, object]]]:
    """Capture HUD notifications without rendering a toast."""
    notes: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(screen, "notify", lambda *a, **k: notes.append((a, k)))
    return notes


def _defer_export_start(
    screen: t.Any,
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[t.Callable[..., t.Awaitable[None]], tuple[object, ...]]]:
    """Capture the one pump callback scheduled by an accepted export."""
    scheduled: list[tuple[t.Callable[..., t.Awaitable[None]], tuple[object, ...]]] = []

    def defer(
        callback: t.Callable[..., t.Awaitable[None]],
        *args: object,
    ) -> None:
        scheduled.append((callback, args))

    monkeypatch.setattr(screen, "call_later", defer)
    return scheduled


def _change_results(screen: t.Any, change: str, replacement: SearchRecord) -> None:
    """Apply one mounted result-view change before deferred export startup."""
    if change == "reset":
        screen._reset_search_chrome()
    elif change == "filter":
        screen._filter_input.value = "replacement"
        screen.on_filter_completed(
            FilterCompleted(
                text="replacement",
                records=[replacement],
                record_ids={id(replacement)},
                generation=screen._filter_generation,
                records_generation=screen._records_generation,
            ),
        )
    else:
        screen._start_search_worker(screen._build_search_query("replacement"))


@pytest.mark.parametrize("pane", ["_results", "_detail_scroll"], ids=("results", "detail"))
@pytest.mark.slow
async def test_export_shortcut_confirms_selected_record_and_appears_in_keys(
    pane: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain ``e`` stages either content-pane selection before writing."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = (
        _record(tmp_path, "first body", ordinal=1),
        _record(tmp_path, "selected body", ordinal=2),
    )
    export_dir = tmp_path / "data" / "agentgrep" / "exports"
    export_dir.mkdir(parents=True)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        hud = app.screen
        await _load_records(hud, records, selected=0)
        await pilot.pause()
        hud.show_detail(records[1])
        await pilot.pause()

        hud._search_input.value = "/keys"
        hud._search_input.focus()
        await pilot.press("enter")
        getattr(hud, pane).focus()
        await pilot.pause()

        binding = hud.active_bindings["e"].binding
        assert len(hud.query(HelpPanel)) == 1
        assert binding.description == "Export selected"
        assert binding.show is False
        notes = _capture_notifications(hud, monkeypatch)

        await pilot.press("e")
        await pilot.pause()

        assert isinstance(app.screen, ExportDialog)
        dialog = app.screen
        assert hud._export_dialog is dialog
        assert list(export_dir.glob("*.md")) == []
        hud._results.highlighted = 1
        hud._current_detail_record = records[0]

        await pilot.press("enter", "enter")
        await _wait_for(lambda: dialog.phase == "review")
        assert _static_text(dialog, "#export-review-directory") == str(export_dir)
        await pilot.press("y")
        await _wait_for(
            lambda: bool(list(export_dir.glob("*.md"))) or dialog.phase == "edit",
        )

        exports = list(export_dir.glob("*.md"))
        assert exports, notes
        exported = exports[0].read_text(encoding="utf-8")
        expected = "first body" if pane == "_results" else "selected body"
        unexpected = "selected body" if pane == "_results" else "first body"
        assert expected in exported
        assert unexpected not in exported


@pytest.mark.slow
async def test_export_preferences_load_before_mount_and_warn_once_path_free(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HUD construction loads once and mount reports a bounded warning."""
    config_home = tmp_path / "config"
    config_path = config_home / "agentgrep" / "tui-export.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    calls: list[pathlib.Path] = []
    real_load = _export_preferences.load_export_preferences

    def tracked_load(home: pathlib.Path) -> t.Any:
        calls.append(home)
        return real_load(home)

    notes: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(_export_preferences, "load_export_preferences", tracked_load)
    monkeypatch.setattr(
        hud_module.HudLayout,
        "notify",
        lambda _self, *args, **kwargs: notes.append((args, kwargs)),
    )

    app = _build_empty_ui_app(tmp_path, monkeypatch)

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        assert calls == [tmp_path / "home"]
        assert notes == [
            (
                ("Export preferences could not be read",),
                {"title": "Export preferences", "severity": "warning"},
            ),
        ]
        assert str(config_path) not in str(notes)


@pytest.mark.slow
async def test_confirmed_export_writes_exact_filename_then_preferences(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reviewed no-clobber artifact precedes preference persistence."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    export_dir = tmp_path / "Selected"
    export_dir.mkdir(mode=0o750)
    export_dir.chmod(0o750)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    record = _record(
        tmp_path,
        "captured body",
        ordinal=1,
        title="Reviewed [Title]",
    )
    order: list[str] = []
    write_calls: list[tuple[pathlib.Path, dict[str, object]]] = []
    real_write = record_export.write_export
    real_save = save_export_preferences

    def tracked_write(
        artifact: record_export.ExportArtifact,
        destination: str | pathlib.Path,
        **kwargs: t.Any,
    ) -> pathlib.Path:
        protected_paths = tuple(kwargs["protected_paths"])
        kwargs["protected_paths"] = protected_paths
        order.append("artifact")
        write_calls.append((pathlib.Path(destination), dict(kwargs)))
        return real_write(artifact, destination, **kwargs)

    def tracked_save(home: pathlib.Path, preferences: ExportPreferences) -> None:
        order.append("preferences")
        assert write_calls[0][0].exists()
        real_save(home, preferences)

    monkeypatch.setattr(record_export, "write_export", tracked_write)
    monkeypatch.setattr(hud_module, "save_export_preferences", tracked_save, raising=False)
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        hud = app.screen
        await _load_records(hud, (record,))
        hud._results.focus()
        notes = _capture_notifications(hud, monkeypatch)

        _dialog, filename = await _open_export_review(
            app,
            pilot,
            directory=export_dir,
            template="reviewed-{title}.md",
        )
        destination = export_dir / filename
        assert filename == "reviewed-reviewed-title.md"
        assert not destination.exists()
        assert not export_preferences_path(tmp_path / "home").exists()

        await pilot.press("y")
        await _wait_for(destination.exists)
        await _wait_for(lambda: app.screen is hud)

        assert order == ["artifact", "preferences"]
        assert write_calls == [
            (
                destination,
                {"force": False, "protected_paths": (record.path,)},
            ),
        ]
        assert stat.S_IMODE(export_dir.stat().st_mode) == 0o750
        assert load_export_preferences(tmp_path / "home").preferences == ExportPreferences(
            directory=str(export_dir),
            filename_template="reviewed-{title}.md",
        )
        assert hud._export_dialog is None
        assert len(notes) == 1
        assert filename in str(notes[0][0][0])


@pytest.mark.slow
async def test_export_failure_restores_draft_without_saving_preferences(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifact failure returns to editing and preserves stored preferences."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    original_dir = tmp_path / "Original"
    original_dir.mkdir()
    selected_dir = tmp_path / "Selected"
    selected_dir.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    original = ExportPreferences(
        directory=str(original_dir),
        filename_template="original-{title}.md",
    )
    save_export_preferences(tmp_path / "home", original)
    record = _record(tmp_path, "body", ordinal=1, title="Retained Draft")

    def fail_write(*_args: object, **_kwargs: object) -> t.NoReturn:
        message = "export could not be written"
        raise record_export.ExportWriteError(message)

    monkeypatch.setattr(record_export, "write_export", fail_write)
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        hud = app.screen
        await _load_records(hud, (record,))
        hud._results.focus()
        notes = _capture_notifications(hud, monkeypatch)
        dialog, filename = await _open_export_review(
            app,
            pilot,
            directory=selected_dir,
            template="retry-{title}.md",
        )

        await pilot.press("y")
        await _wait_for(lambda: dialog.phase == "edit")

        assert dialog.is_mounted
        assert dialog.query_one("#export-directory", ExportDirectoryPicker).value == str(
            selected_dir,
        )
        assert dialog.query_one("#export-template", Input).value == "retry-{title}.md"
        assert hud._export_dialog is dialog
        assert not (selected_dir / filename).exists()
        assert load_export_preferences(tmp_path / "home").preferences == original
        assert notes[0][1]["severity"] == "error"


@pytest.mark.slow
async def test_preference_save_failure_keeps_artifact_success_and_warns(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config failure cannot turn a completed artifact into export failure."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    export_dir = tmp_path / "Selected"
    export_dir.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    record = _record(tmp_path, "body", ordinal=1, title="Saved Artifact")

    def fail_save(_home: pathlib.Path, _preferences: ExportPreferences) -> t.NoReturn:
        message = "Export preferences could not be saved"
        raise ExportPreferencesError(message)

    monkeypatch.setattr(hud_module, "save_export_preferences", fail_save, raising=False)
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        hud = app.screen
        await _load_records(hud, (record,))
        hud._results.focus()
        notes = _capture_notifications(hud, monkeypatch)
        _dialog, filename = await _open_export_review(
            app,
            pilot,
            directory=export_dir,
            template="{title}.md",
        )
        destination = export_dir / filename

        await pilot.press("y")
        await _wait_for(destination.exists)
        await _wait_for(lambda: app.screen is hud)

        assert destination.read_text(encoding="utf-8").startswith(
            "# agentgrep record export",
        )
        assert not export_preferences_path(tmp_path / "home").exists()
        assert hud._export_dialog is None
        assert len(notes) == 2
        assert any(note[1].get("title") == "Export complete" for note in notes)
        warning = next(note for note in notes if note[1].get("severity") == "warning")
        assert warning[0][0] == "Export preferences could not be saved"
        assert str(tmp_path) not in str(warning)


@pytest.mark.slow
async def test_dialog_confirmation_cannot_launch_duplicate_write(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated saving gestures retain one non-supersedable worker."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    export_dir = tmp_path / "Selected"
    export_dir.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    record = _record(tmp_path, "body", ordinal=1, title="Only Once")
    started = threading.Event()
    release = threading.Event()
    calls = 0
    real_write = record_export.write_export

    def slow_write(
        artifact: record_export.ExportArtifact,
        destination: str | pathlib.Path,
        **kwargs: t.Any,
    ) -> pathlib.Path:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(3)
        return real_write(artifact, destination, **kwargs)

    monkeypatch.setattr(record_export, "write_export", slow_write)
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        hud = app.screen
        await _load_records(hud, (record,))
        hud._results.focus()
        dialog, filename = await _open_export_review(
            app,
            pilot,
            directory=export_dir,
            template="{title}.md",
        )

        await pilot.press("y")
        assert await asyncio.to_thread(started.wait, 2)
        await pilot.press("y", "enter")
        await pilot.pause()

        assert dialog.phase == "saving"
        assert calls == 1
        release.set()
        await _wait_for(lambda: (export_dir / filename).exists())


@pytest.mark.slow
async def test_unmount_invalidates_and_clears_retained_export_dialog(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HUD teardown drops the modal reference and invalidates completions."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "body", ordinal=1)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        hud = app.screen
        await _load_records(hud, (record,))
        hud._results.focus()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, ExportDialog)
        assert hud._export_dialog is app.screen
        generation = hud._export_generation

        hud.on_unmount()

        assert hud._export_dialog is None
        assert hud._export_generation == generation + 1
        assert hud._export_pending is False


@pytest.mark.parametrize(
    "input_attr",
    ["_search_input", "_filter_input"],
    ids=("search", "filter"),
)
@pytest.mark.slow
async def test_export_shortcut_remains_literal_in_inputs(
    input_attr: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editable inputs consume ``e`` without starting an export."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        requests: list[tuple[str, str]] = []
        monkeypatch.setattr(
            app.screen,
            "request_export",
            lambda destination, *, selection: requests.append((destination, selection)),
        )
        input_widget = getattr(app.screen, input_attr)
        input_widget.focus()

        await pilot.press("e")
        await pilot.pause()

        assert input_widget.value == "e"
        assert requests == []


@pytest.mark.slow
async def test_export_shortcut_is_inert_in_completion_dropdown(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completion focus never turns a printable choice key into export."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "selected body", ordinal=1)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (record,))
        await pilot.pause()
        requests: list[tuple[str, str]] = []
        monkeypatch.setattr(
            app.screen,
            "request_export",
            lambda destination, *, selection: requests.append((destination, selection)),
        )

        app.screen._search_input.value = "scope:"
        app.screen._search_input.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is app.screen._enum_dropdown

        await pilot.press("e")
        await pilot.pause()

        assert "e" not in app.screen.active_bindings
        assert requests == []


@pytest.mark.parametrize("pane", ["_results", "_detail_scroll"], ids=("results", "detail"))
@pytest.mark.slow
async def test_export_shortcut_requires_live_pane_selection(
    pane: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shortcut is absent when its focused pane has no live selection."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "live body", ordinal=1)
    stale = _record(tmp_path, "stale body", ordinal=2)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (record,))
        await pilot.pause()
        requests: list[tuple[str, str]] = []
        monkeypatch.setattr(
            app.screen,
            "request_export",
            lambda destination, *, selection: requests.append((destination, selection)),
        )
        if pane == "_results":
            app.screen._results.highlighted = None
        else:
            app.screen._current_detail_record = stale
        getattr(app.screen, pane).focus()
        await pilot.pause()

        assert "e" not in app.screen.active_bindings
        await pilot.press("e")
        await pilot.pause()

        assert requests == []


@pytest.mark.slow
async def test_export_commands_accept_paths_but_legacy_args_stay_searches(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only export commands consume an argument remainder."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        requests: list[tuple[str, str]] = []
        searches: list[object] = []
        monkeypatch.setattr(
            app.screen,
            "request_export",
            lambda path, *, selection: requests.append((selection, path)),
        )
        monkeypatch.setattr(app.screen, "_start_search_worker", searches.append)

        app.screen._search_input.focus()
        app.screen._search_input.value = "/export nested/result.md"
        await pilot.pause()
        assert app.screen._enum_dropdown.display is False
        await pilot.press("enter")
        app.screen.on_search_requested(_search_requested("/export-thread thread.md"))
        app.screen.on_search_requested(_search_requested("/help still a query"))
        await pilot.pause()

        assert requests == [
            ("records", "nested/result.md"),
            ("thread", "thread.md"),
        ]
        assert len(searches) == 1


@pytest.mark.slow
async def test_export_without_selection_is_a_path_free_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command on an empty result set does not launch disk work."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        notes = _capture_notifications(app.screen, monkeypatch)
        workers: list[tuple[tuple[object, ...], dict[str, object]]] = []
        monkeypatch.setattr(
            app.screen,
            "run_worker",
            lambda *a, **k: workers.append((a, k)),
        )

        app.screen.on_search_requested(_search_requested("/export"))
        await pilot.pause()

        assert workers == []
        assert len(notes) == 1
        assert notes[0][1]["severity"] == "error"
        assert "select" in str(notes[0][0][0]).lower()
        assert str(tmp_path) not in str(notes)


@pytest.mark.parametrize("explicit", [False, True], ids=("private", "explicit"))
@pytest.mark.slow
async def test_record_export_writes_markdown_and_preserves_results(
    explicit: bool,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default and explicit sinks export exactly the selected record."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    saved_preferences: list[ExportPreferences] = []
    monkeypatch.setattr(
        hud_module,
        "save_export_preferences",
        lambda _home, preferences: saved_preferences.append(preferences),
        raising=False,
    )
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = (
        _record(tmp_path, "first exact body", ordinal=1),
        _record(tmp_path, "second private body", ordinal=2),
    )
    destination = tmp_path / "chosen directory" / "selected record.md"
    if explicit:
        destination.parent.mkdir()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, records, selected=0)
        notes = _capture_notifications(app.screen, monkeypatch)
        before_all = list(app.screen.all_records)
        before_filtered = list(app.screen.filtered_records)

        command = f"/export {destination}" if explicit else "/export"
        app.screen.on_search_requested(_search_requested(command))
        if explicit:
            await _wait_for(destination.exists)
            exported = destination
        else:
            export_dir = tmp_path / "data" / "agentgrep" / "exports"
            await _wait_for(lambda: bool(list(export_dir.glob("*.md"))))
            exported = next(export_dir.glob("*.md"))
        await pilot.pause()

        text = exported.read_text(encoding="utf-8")
        assert text.startswith("# agentgrep record export")
        assert "first exact body" in text
        assert "second private body" not in text
        assert app.screen.all_records == before_all
        assert app.screen.filtered_records == before_filtered
        assert app.screen._current_detail_record is records[0]
        assert len(notes) == 1
        message = str(notes[0][0][0])
        assert exported.name in message
        assert str(exported.parent) not in message
        assert "markdown" in message
        assert "1 record" in message
        assert saved_preferences == []
        assert not export_preferences_path(tmp_path / "home").exists()


@pytest.mark.slow
async def test_thread_export_uses_only_selected_observed_thread(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed and threadless active results do not contaminate the chosen thread."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    saved_preferences: list[ExportPreferences] = []
    monkeypatch.setattr(
        hud_module,
        "save_export_preferences",
        lambda _home, preferences: saved_preferences.append(preferences),
        raising=False,
    )
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = (
        _record(tmp_path, "thread a first", ordinal=1, session_id="session-a"),
        _record(tmp_path, "thread b", ordinal=2, session_id="session-b"),
        _record(tmp_path, "thread a second", ordinal=3, session_id="session-a"),
        _record(tmp_path, "threadless", ordinal=4, session_id=None),
    )
    destination = tmp_path / "thread.md"
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, records, selected=2)
        notes = _capture_notifications(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested(f"/export-thread {destination}"))
        await _wait_for(destination.exists)
        await pilot.pause()

        text = destination.read_text(encoding="utf-8")
        assert text.startswith("# agentgrep observed thread export")
        assert "thread a first" in text
        assert "thread a second" in text
        assert "thread b" not in text
        assert "threadless" not in text
        assert "- Record count: 2" in text
        assert "- Fidelity: unordered" in text
        assert "2 records" in str(notes[0][0][0])
        assert saved_preferences == []
        assert not export_preferences_path(tmp_path / "home").exists()


@pytest.mark.slow
async def test_thread_export_without_path_uses_private_markdown_sink(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-path thread command writes a collision-safe canonical artifact."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    saved_preferences: list[ExportPreferences] = []
    monkeypatch.setattr(
        hud_module,
        "save_export_preferences",
        lambda _home, preferences: saved_preferences.append(preferences),
        raising=False,
    )
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = (
        _record(tmp_path, "first", ordinal=1),
        _record(tmp_path, "second", ordinal=2),
    )
    export_dir = tmp_path / "data" / "agentgrep" / "exports"
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, records)
        notes = _capture_notifications(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested("/export-thread"))
        await _wait_for(lambda: bool(list(export_dir.glob("*.md"))))
        exported = next(export_dir.glob("*.md"))
        await pilot.pause()

        assert exported.name.startswith("agentgrep-agt1-")
        assert exported.read_text(encoding="utf-8").startswith(
            "# agentgrep observed thread export",
        )
        assert exported.name in str(notes[0][0][0])
        assert str(export_dir) not in str(notes)
        assert saved_preferences == []
        assert not export_preferences_path(tmp_path / "home").exists()


@pytest.mark.slow
async def test_thread_export_freezes_result_count_when_accepted(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A streamed turn arriving before deferred capture is not retroactively selected."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    first = _record(tmp_path, "accepted turn", ordinal=1)
    late = _record(tmp_path, "late turn", ordinal=2)
    destination = tmp_path / "accepted-thread.md"
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (first,))
        scheduled = _defer_export_start(app.screen, monkeypatch)
        app.screen.on_search_requested(_search_requested(f"/export-thread {destination}"))
        assert len(scheduled) == 1

        app.screen.filtered_records.append(late)
        callback, args = scheduled.pop()
        await callback(*args)
        await _wait_for(destination.exists)

        text = destination.read_text(encoding="utf-8")
        assert "accepted turn" in text
        assert "late turn" not in text
        assert "- Record count: 1" in text


@pytest.mark.parametrize("change", ["reset", "filter", "new-search"])
@pytest.mark.slow
async def test_record_export_survives_result_change_before_deferred_start(
    change: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted exact record is independent of later result-view changes."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    selected = _record(tmp_path, "accepted exact record", ordinal=1)
    replacement = _record(tmp_path, "replacement record", ordinal=2, session_id="other")
    destination = tmp_path / f"record-{change}.md"
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (selected,))
        notes = _capture_notifications(app.screen, monkeypatch)
        scheduled = _defer_export_start(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested(f"/export {destination}"))
        assert len(scheduled) == 1
        _change_results(app.screen, change, replacement)
        callback, args = scheduled.pop()
        await callback(*args)
        await _wait_for(destination.exists)
        await pilot.pause()

        text = destination.read_text(encoding="utf-8")
        assert "accepted exact record" in text
        assert "replacement record" not in text
        assert not any("canceled" in str(note[0][0]).lower() for note in notes)


@pytest.mark.parametrize("change", ["reset", "filter", "new-search"])
@pytest.mark.slow
async def test_thread_export_cancels_result_change_before_deferred_start(
    change: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An observed-thread snapshot still requires its accepted result view."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    selected = _record(tmp_path, "accepted thread", ordinal=1)
    replacement = _record(tmp_path, "replacement record", ordinal=2, session_id="other")
    destination = tmp_path / f"thread-{change}.md"
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (selected,))
        notes = _capture_notifications(app.screen, monkeypatch)
        scheduled = _defer_export_start(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested(f"/export-thread {destination}"))
        assert len(scheduled) == 1
        _change_results(app.screen, change, replacement)
        callback, args = scheduled.pop()
        await callback(*args)
        await pilot.pause()

        assert not destination.exists()
        assert app.screen._export_pending is False
        assert any("changed" in str(note[0][0]).lower() for note in notes)


@pytest.mark.slow
async def test_thread_export_rejects_threadless_selection(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A null canonical thread identity is rejected without creating a file."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "threadless", ordinal=1, session_id=None)
    destination = tmp_path / "thread.md"
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (record,))
        notes = _capture_notifications(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested(f"/export-thread {destination}"))
        await _wait_for(lambda: bool(notes))

        assert not destination.exists()
        assert notes[0][1]["severity"] == "error"
        assert "thread" in str(notes[0][0][0]).lower()
        assert str(tmp_path) not in str(notes)


@pytest.mark.parametrize("unsafe", ["exists", "symlink", "source"])
@pytest.mark.slow
async def test_explicit_export_refuses_unsafe_destinations(
    unsafe: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-overwrite, no-symlink, and source-alias rules reach the TUI."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "protected body", ordinal=1)
    destination = tmp_path / "destination.md"
    if unsafe == "exists":
        destination.write_text("keep", encoding="utf-8")
    elif unsafe == "symlink":
        target = tmp_path / "target.md"
        target.write_text("keep", encoding="utf-8")
        destination.symlink_to(target)
    else:
        destination = record.path
        destination.write_text("source", encoding="utf-8")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (record,))
        notes = _capture_notifications(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested(f"/export {destination}"))
        await _wait_for(lambda: bool(notes))

        assert notes[0][1]["severity"] == "error"
        assert str(tmp_path) not in str(notes)
        if unsafe == "exists":
            assert destination.read_text(encoding="utf-8") == "keep"
        elif unsafe == "symlink":
            assert destination.is_symlink()
            assert destination.read_text(encoding="utf-8") == "keep"
        else:
            assert destination.read_text(encoding="utf-8") == "source"


@pytest.mark.slow
async def test_unexpected_writer_error_is_path_free(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An arbitrary filesystem exception cannot leak its destination text."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "body", ordinal=1)
    secret_path = tmp_path / "private-name.md"

    def fail_write(*args: object, **kwargs: object) -> pathlib.Path:
        message = f"failed at {secret_path}"
        raise OSError(message)

    monkeypatch.setattr(record_export, "write_export", fail_write)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (record,))
        notes = _capture_notifications(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested(f"/export {secret_path}"))
        await _wait_for(lambda: bool(notes))

        assert notes[0][1]["severity"] == "error"
        assert str(secret_path) not in str(notes)
        assert "could not" in str(notes[0][0][0]).lower()


@pytest.mark.slow
async def test_rapid_duplicate_export_is_blocked_not_superseded(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only one durable export may be accepted at a time."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "body", ordinal=1)
    destination = tmp_path / "result.md"
    started = threading.Event()
    release = threading.Event()
    real_render = record_export.render_export
    calls = 0

    def slow_render(
        records: cabc.Iterable[SearchRecord],
        *,
        format: record_export.ExportFormat,  # noqa: A002 - mirrors public API.
        include_bodies: bool,
        selection: record_export.ExportSelection = "records",
    ) -> record_export.ExportArtifact:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(3)
        return real_render(
            records,
            format=format,
            include_bodies=include_bodies,
            selection=selection,
        )

    monkeypatch.setattr(record_export, "render_export", slow_render)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (record,))
        notes = _capture_notifications(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested(f"/export {destination}"))
        assert await asyncio.to_thread(started.wait, 2)
        app.screen.on_search_requested(_search_requested(f"/export {destination}"))
        await pilot.pause()
        assert calls == 1
        assert any("progress" in str(note[0][0]).lower() for note in notes)

        release.set()
        await _wait_for(destination.exists)
        await pilot.pause()
        assert calls == 1


@pytest.mark.slow
async def test_record_switch_does_not_change_accepted_export(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker owns the exact selection captured when the command was accepted."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = (
        _record(tmp_path, "first selected", ordinal=1),
        _record(tmp_path, "second later", ordinal=2),
    )
    destination = tmp_path / "selected.md"
    started = threading.Event()
    release = threading.Event()
    real_render = record_export.render_export

    def slow_render(
        records: cabc.Iterable[SearchRecord],
        *,
        format: record_export.ExportFormat,  # noqa: A002 - mirrors public API.
        include_bodies: bool,
        selection: record_export.ExportSelection = "records",
    ) -> record_export.ExportArtifact:
        started.set()
        assert release.wait(3)
        return real_render(
            records,
            format=format,
            include_bodies=include_bodies,
            selection=selection,
        )

    monkeypatch.setattr(record_export, "render_export", slow_render)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, records, selected=0)

        app.screen.on_search_requested(_search_requested(f"/export {destination}"))
        assert await asyncio.to_thread(started.wait, 2)
        app.screen._results.highlighted = 1
        app.screen._current_detail_record = records[1]
        release.set()
        await _wait_for(destination.exists)

        text = destination.read_text(encoding="utf-8")
        assert "first selected" in text
        assert "second later" not in text


@pytest.mark.slow
async def test_thread_snapshot_aborts_if_results_reset_mid_copy(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunk-yielded result capture never launches with a mixed-time tuple."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = tuple(
        _record(tmp_path, f"body {index}", ordinal=index, session_id="session-a")
        for index in range(1, 402)
    )
    destination = tmp_path / "thread.md"
    first_chunk = asyncio.Event()
    continue_copy = asyncio.Event()
    real_stream_apply = _runtime.stream_apply

    async def paused_stream_apply(
        items: cabc.Sequence[SearchRecord],
        apply_chunk: cabc.Callable[[cabc.Sequence[SearchRecord]], None],
        *,
        chunk_size: int = 200,
        yield_between: cabc.Callable[[], cabc.Awaitable[None]] | None = None,
    ) -> None:
        del yield_between

        async def pause_once() -> None:
            first_chunk.set()
            await continue_copy.wait()

        await real_stream_apply(
            items,
            apply_chunk,
            chunk_size=chunk_size,
            yield_between=pause_once,
        )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, records)
        monkeypatch.setattr(_runtime, "stream_apply", paused_stream_apply)
        notes = _capture_notifications(app.screen, monkeypatch)
        worker_calls: list[dict[str, object]] = []
        real_run_worker = app.screen.run_worker

        def track_worker(*args: object, **kwargs: object) -> object:
            if kwargs.get("group") == "export":
                worker_calls.append(kwargs)
            return real_run_worker(*args, **kwargs)

        monkeypatch.setattr(app.screen, "run_worker", track_worker)

        app.screen.on_search_requested(_search_requested(f"/export-thread {destination}"))
        await asyncio.wait_for(first_chunk.wait(), 2)
        app.screen._reset_search_chrome()
        continue_copy.set()
        await _wait_for(lambda: bool(notes))

        assert worker_calls == []
        assert not destination.exists()
        assert "changed" in str(notes[0][0][0]).lower()
        assert app.screen._export_pending is False


@pytest.mark.slow
async def test_teardown_cancels_export_before_write_and_drops_callback(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A suspended worker observes teardown before starting durable output."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "large body " * 200_000, ordinal=1)
    destination = tmp_path / "canceled.md"
    started = threading.Event()
    release = threading.Event()
    write_calls = 0
    real_render = record_export.render_export
    real_write = record_export.write_export

    def slow_render(
        records: cabc.Iterable[SearchRecord],
        *,
        format: record_export.ExportFormat,  # noqa: A002 - mirrors public API.
        include_bodies: bool,
        selection: record_export.ExportSelection = "records",
    ) -> record_export.ExportArtifact:
        started.set()
        assert release.wait(3)
        return real_render(
            records,
            format=format,
            include_bodies=include_bodies,
            selection=selection,
        )

    def track_write(
        artifact: record_export.ExportArtifact,
        destination: str | pathlib.Path,
        *,
        force: bool = False,
        protected_paths: cabc.Iterable[str | pathlib.Path] = (),
    ) -> pathlib.Path:
        nonlocal write_calls
        write_calls += 1
        return real_write(
            artifact,
            destination,
            force=force,
            protected_paths=protected_paths,
        )

    monkeypatch.setattr(record_export, "render_export", slow_render)
    monkeypatch.setattr(record_export, "write_export", track_write)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (record,))
        notes = _capture_notifications(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested(f"/export {destination}"))
        assert await asyncio.to_thread(started.wait, 2)
        app.screen.on_unmount()
        release.set()
        await asyncio.sleep(0.1)

        assert write_calls == 0
        assert not destination.exists()
        assert notes == []


@pytest.mark.slow
async def test_stale_export_callback_cannot_clear_live_pending_state(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generation gating drops an old completion without touching a newer request."""
    from agentgrep.ui.layouts.hud import _ExportCompleted

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        notes = _capture_notifications(app.screen, monkeypatch)
        app.screen._export_generation = 8
        app.screen._export_pending = True

        app.screen._apply_export_completed(
            7,
            _ExportCompleted(
                filename="old.md",
                format="markdown",
                selection="records",
                record_count=1,
                preferences=None,
                preference_warning=None,
                error=None,
            ),
        )

        assert notes == []
        assert app.screen._export_pending is True


@pytest.mark.slow
async def test_export_success_notification_treats_filename_as_literal(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Printable bracket markup in an exported basename stays literal."""
    from agentgrep.ui.layouts.hud import _ExportCompleted

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        notes = _capture_notifications(app.screen, monkeypatch)
        app.screen._export_generation = 1
        app.screen._export_pending = True

        app.screen._apply_export_completed(
            1,
            _ExportCompleted(
                filename="[bold]spoof[/].md",
                format="markdown",
                selection="records",
                record_count=1,
                preferences=None,
                preference_warning=None,
                error=None,
            ),
        )

        assert notes[0][0][0] == "[bold]spoof[/].md · markdown · records · 1 record"
        assert notes[0][1]["markup"] is False


@pytest.mark.slow
async def test_large_export_worker_keeps_pump_responsive(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large body work stays off-pump while keystrokes continue to dispatch."""
    monkeypatch.setenv("AGENTGREP_TUI_WATCHDOG", "1")
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "large body\n" * 300_000, ordinal=1)
    destination = tmp_path / "large.md"
    started = threading.Event()
    release = threading.Event()
    real_render = record_export.render_export
    real_write = record_export.write_export

    def slow_render(
        records: cabc.Iterable[SearchRecord],
        *,
        format: record_export.ExportFormat,  # noqa: A002 - mirrors public API.
        include_bodies: bool,
        selection: record_export.ExportSelection = "records",
    ) -> record_export.ExportArtifact:
        _runtime.assert_off_pump("export render")
        started.set()
        assert release.wait(3)
        return real_render(
            records,
            format=format,
            include_bodies=include_bodies,
            selection=selection,
        )

    def checked_write(
        artifact: record_export.ExportArtifact,
        output: str | pathlib.Path,
        *,
        force: bool = False,
        protected_paths: cabc.Iterable[str | pathlib.Path] = (),
    ) -> pathlib.Path:
        _runtime.assert_off_pump("export write")
        return real_write(
            artifact,
            output,
            force=force,
            protected_paths=protected_paths,
        )

    monkeypatch.setattr(record_export, "render_export", slow_render)
    monkeypatch.setattr(record_export, "write_export", checked_write)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (record,))
        app.screen._search_input.focus()

        app.screen.on_search_requested(_search_requested(f"/export {destination}"))
        assert await asyncio.to_thread(started.wait, 2)
        await pilot.press("x")
        await pilot.pause()
        assert app.screen._search_input.value.endswith("x")

        release.set()
        await _wait_for(destination.exists)


@pytest.mark.slow
async def test_large_observed_thread_identity_and_output_stay_off_pump(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A many-record thread still leaves keystrokes responsive during identity work."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = tuple(
        _record(
            tmp_path,
            f"turn {index} " + ("x" * 5_000),
            ordinal=index,
            session_id="large-thread",
        )
        for index in range(1, 402)
    )
    destination = tmp_path / "large-thread.md"
    started = threading.Event()
    release = threading.Event()
    real_identity = identity.record_identity

    def slow_identity(record: SearchRecord) -> identity.RecordIdentity:
        _runtime.assert_off_pump("thread identity")
        if not started.is_set():
            started.set()
            assert release.wait(3)
        return real_identity(record)

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, records)
        await pilot.pause()
        monkeypatch.setattr(identity, "record_identity", slow_identity)
        app.screen._search_input.focus()

        app.screen.on_search_requested(_search_requested(f"/export-thread {destination}"))
        assert await asyncio.to_thread(started.wait, 2)
        await pilot.press("x")
        await pilot.pause()
        assert app.screen._search_input.value.endswith("x")

        release.set()
        await _wait_for(destination.exists, timeout=5)
        text = destination.read_text(encoding="utf-8")
        assert "- Record count: 401" in text
        assert text.startswith("# agentgrep observed thread export")
