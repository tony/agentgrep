"""Contract tests for bounded export-directory completion."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import os
import pathlib
import threading
import time
import typing as t

import pytest
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Input, OptionList

import agentgrep.ui.widgets as widgets
from agentgrep.ui import _runtime
from agentgrep.ui.widgets import directory_popup
from agentgrep.ui.widgets.directory_popup import (
    DIRECTORY_CANDIDATE_LIMIT,
    DIRECTORY_COMPLETION_DEBOUNCE,
    DIRECTORY_SCAN_LIMIT,
    DirectoryCandidate,
    DirectoryCompletionPopup,
    ExportDirectoryPicker,
)

pytestmark = pytest.mark.tui


class _DirectoryPopupHost(App[None]):
    """Minimal export-dialog edit stage for Pilot interaction tests."""

    CSS = """
    Screen { layout: vertical; }
    #directory { width: 100%; }
    #filename { height: 3; }
    """

    def __init__(self, home: pathlib.Path) -> None:
        super().__init__()
        self._home = home

    def compose(self) -> ComposeResult:
        """Compose the owning picker and the next focus target."""
        yield ExportDirectoryPicker(value="", home=self._home, id="directory")
        yield Input(placeholder="Filename", id="filename")

    def on_mount(self) -> None:
        """Bind the pump guard and focus the picker input."""
        _runtime.bind_pump_thread()
        self.query_one(ExportDirectoryPicker).focus_input()

    def on_unmount(self) -> None:
        """Release the global test guard binding."""
        _runtime.unbind_pump_thread()


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
    pytest.fail("timed out waiting for directory candidates")


def _popup(app: _DirectoryPopupHost) -> DirectoryCompletionPopup:
    """Return the picker-owned literal completion popup."""
    return app.query_one(DirectoryCompletionPopup)


def _prompts(popup: DirectoryCompletionPopup) -> tuple[str, ...]:
    """Return popup labels exactly as rendered."""
    return tuple(str(option.prompt) for option in popup.options)


def test_export_directory_picker_interface_hides_internal_rows() -> None:
    """The widgets package exports the picker but not completion internals."""
    assert widgets.ExportDirectoryPicker is ExportDirectoryPicker
    assert "DirectoryCandidate" not in widgets.__all__
    assert "DirectoryCompletionPopup" not in widgets.__all__
    assert not hasattr(widgets, "DirectoryCandidate")
    assert not hasattr(widgets, "DirectoryCompletionPopup")
    assert issubclass(DirectoryCompletionPopup, OptionList)
    candidate = DirectoryCandidate(value="./alpha/", label="alpha")
    mutable_candidate = t.cast("t.Any", candidate)

    with pytest.raises(dataclasses.FrozenInstanceError):
        mutable_candidate.label = "changed"

    picker = ExportDirectoryPicker(value="", home=pathlib.Path("home"))
    path_input = t.cast("t.Any", picker)._input
    assert path_input.max_length == directory_popup.MAX_DIRECTORY_CHARS


def test_empty_directory_value_has_no_completion_candidates(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing the directory closes completion without scanning the cwd."""
    unexpected_scan = "empty completion must not scan a directory"

    def fail_scandir(_path: os.PathLike[str]) -> t.NoReturn:
        raise AssertionError(unexpected_scan)

    monkeypatch.setattr(directory_popup.os, "scandir", fail_scandir)

    result = directory_popup._enumerate_directory_candidates(
        "",
        home=tmp_path / "home",
        candidate_limit=DIRECTORY_CANDIDATE_LIMIT,
        scan_limit=DIRECTORY_SCAN_LIMIT,
    )

    assert result.values == ()
    assert result.truncated is False


@pytest.mark.slow
async def test_directory_enumeration_waits_for_inactivity(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the latest value starts enumeration after 150 ms of inactivity."""
    root = tmp_path / "choices"
    root.mkdir()
    (root / "alpha").mkdir()
    calls: list[tuple[str, float]] = []
    original = directory_popup._enumerate_directory_candidates

    def observed(
        value: str,
        *,
        home: pathlib.Path,
        candidate_limit: int,
        scan_limit: int,
    ) -> object:
        calls.append((value, time.monotonic()))
        return original(
            value,
            home=home,
            candidate_limit=candidate_limit,
            scan_limit=scan_limit,
        )

    monkeypatch.setattr(directory_popup, "_enumerate_directory_candidates", observed)
    app = _DirectoryPopupHost(tmp_path / "home")
    async with app.run_test(size=(60, 16)) as pilot:
        picker = app.query_one(ExportDirectoryPicker)
        picker.value = f"{root}{os.sep}a"
        await pilot.pause(DIRECTORY_COMPLETION_DEBOUNCE / 2)
        picker.value = f"{root}{os.sep}al"
        changed_at = time.monotonic()
        await pilot.pause(DIRECTORY_COMPLETION_DEBOUNCE - 0.04)
        assert calls == []

        await _wait_for(pilot, lambda: bool(calls))
        assert [value for value, _started_at in calls] == [f"{root}{os.sep}al"]
        assert calls[0][1] - changed_at >= DIRECTORY_COMPLETION_DEBOUNCE - 0.02


@pytest.mark.slow
async def test_directory_enumeration_coalesces_while_worker_is_blocked(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rapid edits queue only the latest scan behind one blocked enumeration."""
    root = tmp_path / "choices"
    root.mkdir()
    (root / "alpha").mkdir()
    first_started = threading.Event()
    release_first = threading.Event()
    values: list[str] = []
    active = 0
    maximum_active = 0
    lock = threading.Lock()
    original_enumerate = directory_popup._enumerate_directory_candidates
    original_scandir = os.scandir

    def observed_enumerate(
        value: str,
        *,
        home: pathlib.Path,
        candidate_limit: int,
        scan_limit: int,
    ) -> object:
        values.append(value)
        return original_enumerate(
            value,
            home=home,
            candidate_limit=candidate_limit,
            scan_limit=scan_limit,
        )

    def blocked_scandir(path: str | os.PathLike[str]) -> t.Any:
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            first = not first_started.is_set()
            if first:
                first_started.set()
        try:
            if first:
                release_first.wait(2)
            return original_scandir(path)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(
        directory_popup,
        "_enumerate_directory_candidates",
        observed_enumerate,
    )
    monkeypatch.setattr(directory_popup.os, "scandir", blocked_scandir)
    app = _DirectoryPopupHost(tmp_path / "home")
    async with app.run_test(size=(60, 16)) as pilot:
        picker = app.query_one(ExportDirectoryPicker)
        picker.value = f"{root}{os.sep}a"
        await _wait_for(pilot, first_started.is_set)
        picker.value = f"{root}{os.sep}al"
        await pilot.pause(DIRECTORY_COMPLETION_DEBOUNCE / 2)
        latest = f"{root}{os.sep}alp"
        picker.value = latest
        await pilot.pause(DIRECTORY_COMPLETION_DEBOUNCE + 0.1)
        release_first.set()
        await _wait_for(pilot, lambda: len(values) >= 2)

        assert values == [f"{root}{os.sep}a", latest]
        assert maximum_active == 1


class _InstrumentedEntry:
    """A scandir row that records directory probes."""

    def __init__(self, name: str, checks: list[tuple[str, bool]]) -> None:
        self.name = name
        self._checks = checks

    def is_dir(self, *, follow_symlinks: bool) -> bool:
        """Record one no-follow directory check."""
        self._checks.append((self.name, follow_symlinks))
        return True


class _InstrumentedScandir:
    """Context-managed iterator that records raw pulls."""

    def __init__(self, entries: list[_InstrumentedEntry]) -> None:
        self._entries = iter(entries)
        self.pulls = 0

    def __enter__(self) -> _InstrumentedScandir:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def __iter__(self) -> _InstrumentedScandir:
        return self

    def __next__(self) -> _InstrumentedEntry:
        entry = next(self._entries)
        self.pulls += 1
        return entry


def test_directory_scan_has_raw_bound_and_truncation_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 257th raw row detects truncation without a directory probe."""
    checks: list[tuple[str, bool]] = []
    entries = [
        _InstrumentedEntry(f"candidate-{index:03}", checks)
        for index in range(DIRECTORY_SCAN_LIMIT + 25)
    ]
    scandir = _InstrumentedScandir(entries)
    monkeypatch.setattr(directory_popup.os, "scandir", lambda _path: scandir)

    result = directory_popup._enumerate_directory_candidates(
        "./",
        home=pathlib.Path("/session-home"),
        candidate_limit=DIRECTORY_CANDIDATE_LIMIT,
        scan_limit=DIRECTORY_SCAN_LIMIT,
    )

    assert scandir.pulls == DIRECTORY_SCAN_LIMIT + 1
    assert len(checks) == DIRECTORY_SCAN_LIMIT
    assert all(follow_symlinks is False for _, follow_symlinks in checks)
    assert len(result.values) == DIRECTORY_CANDIDATE_LIMIT
    assert result.truncated is True


def test_symlink_directories_are_not_candidates(tmp_path: pathlib.Path) -> None:
    """Completion does not offer a symlink rejected by export safety."""
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "alias").symlink_to(target, target_is_directory=True)

    result = directory_popup._enumerate_directory_candidates(
        f"{tmp_path}{os.sep}a",
        home=tmp_path / "home",
        candidate_limit=DIRECTORY_CANDIDATE_LIMIT,
        scan_limit=DIRECTORY_SCAN_LIMIT,
    )

    assert result.values == ()


def test_tilde_completion_uses_session_home_without_expanduser(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completion resolves current-user tilde syntax against the TUI session home."""
    session_home = tmp_path / "session-home"
    choices = session_home / "choices"
    choices.mkdir(parents=True)
    (choices / "alpha").mkdir()
    process_home = tmp_path / "process-home"
    process_home.mkdir()
    monkeypatch.setenv("HOME", str(process_home))
    unexpected_expanduser = "process-global expanduser must not run"

    def reject_expanduser(_path: pathlib.Path) -> t.NoReturn:
        raise AssertionError(unexpected_expanduser)

    monkeypatch.setattr(pathlib.Path, "expanduser", reject_expanduser)

    result = directory_popup._enumerate_directory_candidates(
        "~/choices/a",
        home=session_home,
        candidate_limit=DIRECTORY_CANDIDATE_LIMIT,
        scan_limit=DIRECTORY_SCAN_LIMIT,
    )

    assert result.values == (DirectoryCandidate(value="~/choices/alpha/", label="alpha"),)


def test_other_user_tilde_completion_is_rejected(tmp_path: pathlib.Path) -> None:
    """Completion never delegates other-user tilde syntax to account lookup."""
    result = directory_popup._enumerate_directory_candidates(
        "~other/choices/a",
        home=tmp_path / "session-home",
        candidate_limit=DIRECTORY_CANDIDATE_LIMIT,
        scan_limit=DIRECTORY_SCAN_LIMIT,
    )

    assert result.values == ()


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("./choices/a", "./choices/alpha/"),
        ("~/choices/a", "~/choices/alpha/"),
        ("{absolute}/a", "{absolute}/alpha/"),
    ],
)
def test_candidate_labels_are_basenames_and_values_preserve_prefix(
    typed: str,
    expected: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Display labels stay compact without rewriting the user's path prefix."""
    choices = tmp_path / "choices"
    choices.mkdir()
    (choices / "alpha").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    typed = typed.format(absolute=choices)
    expected = expected.format(absolute=choices)

    result = directory_popup._enumerate_directory_candidates(
        typed,
        home=tmp_path,
        candidate_limit=DIRECTORY_CANDIDATE_LIMIT,
        scan_limit=DIRECTORY_SCAN_LIMIT,
    )

    assert result.values == (DirectoryCandidate(value=expected, label="alpha"),)


@pytest.mark.parametrize("unsafe", ["\u200b", "\u202e"])
def test_completion_omits_unreviewable_directory_names(
    unsafe: str,
    tmp_path: pathlib.Path,
) -> None:
    """Existing invisible and bidi directory names never enter completion."""
    choices = tmp_path / "choices"
    choices.mkdir()
    (choices / "alpha").mkdir()
    (choices / f"a{unsafe}hidden").mkdir()

    result = directory_popup._enumerate_directory_candidates(
        f"{choices}{os.sep}a",
        home=tmp_path,
        candidate_limit=DIRECTORY_CANDIDATE_LIMIT,
        scan_limit=DIRECTORY_SCAN_LIMIT,
    )

    assert result.values == (
        DirectoryCandidate(value=f"{choices}{os.sep}alpha{os.sep}", label="alpha"),
    )


@pytest.mark.slow
async def test_popup_is_literal_bounded_off_pump_and_reports_truncation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only six literal basename rows cross the worker boundary."""
    root = tmp_path / "choices"
    root.mkdir()
    for index in range(DIRECTORY_SCAN_LIMIT + 1):
        (root / f"candidate-[{index:03}]").mkdir()
    (root / "not-a-directory.md").write_text("file", encoding="utf-8")
    scan_threads: list[int] = []
    original_scandir = os.scandir

    def observed_scandir(path: str | os.PathLike[str]) -> t.Any:
        scan_threads.append(threading.get_ident())
        return original_scandir(path)

    monkeypatch.setattr(directory_popup.os, "scandir", observed_scandir)
    pump_thread = threading.get_ident()
    app = _DirectoryPopupHost(tmp_path / "home")
    async with app.run_test(size=(60, 16)) as pilot:
        picker = app.query_one(ExportDirectoryPicker)
        popup = _popup(app)
        picker.value = f"{root}{os.sep}"
        await _wait_for(pilot, lambda: popup.option_count == DIRECTORY_CANDIDATE_LIMIT + 1)

        assert _prompts(popup)[-1] == "… more entries"
        assert len(_prompts(popup)[:-1]) == DIRECTORY_CANDIDATE_LIMIT
        assert all(prompt.startswith("candidate-[") for prompt in _prompts(popup)[:-1])
        assert popup.get_option_at_index(DIRECTORY_CANDIDATE_LIMIT).disabled is True
        assert popup._markup is False
        assert scan_threads and all(thread_id != pump_thread for thread_id in scan_threads)


@pytest.mark.slow
async def test_up_down_wrap_and_right_accepts_only_at_end(tmp_path: pathlib.Path) -> None:
    """Navigation wraps while mid-string Right retains native cursor movement."""
    root = tmp_path / "choices"
    root.mkdir()
    for name in ("alpha", "beta"):
        (root / name).mkdir()
    app = _DirectoryPopupHost(tmp_path / "home")
    async with app.run_test(size=(60, 16)) as pilot:
        picker = app.query_one(ExportDirectoryPicker)
        field = picker.query_one(Input)
        popup = _popup(app)
        picker.value = f"{root}{os.sep}"
        await _wait_for(pilot, lambda: popup.option_count == 2)

        await pilot.press("up")
        assert popup.highlighted == 1
        assert field.has_focus
        await pilot.press("down")
        assert popup.highlighted == 0
        assert field.has_focus

        original = picker.value
        field.cursor_position = len(original) - 1
        await pilot.press("right")
        assert picker.value == original
        assert field.cursor_position == len(original)

        await pilot.press("right")
        assert picker.value == f"{root}{os.sep}alpha{os.sep}"
        assert field.has_focus


@pytest.mark.slow
async def test_tab_accepts_only_when_open_then_traverses(tmp_path: pathlib.Path) -> None:
    """Tab accepts one visible row, then resumes normal focus traversal."""
    root = tmp_path / "choices"
    root.mkdir()
    (root / "child").mkdir()
    app = _DirectoryPopupHost(tmp_path / "home")
    async with app.run_test(size=(60, 16)) as pilot:
        picker = app.query_one(ExportDirectoryPicker)
        filename = app.query_one("#filename", Input)
        popup = _popup(app)
        picker.value = f"{root}{os.sep}ch"
        await _wait_for(pilot, lambda: popup.option_count == 1)

        await pilot.press("tab")
        assert picker.value == f"{root}{os.sep}child{os.sep}"
        assert picker.query_one(Input).has_focus

        await pilot.press("tab")
        assert filename.has_focus


@pytest.mark.slow
async def test_late_directory_result_cannot_reopen_after_tab(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blur invalidates an already-running completion worker."""
    (tmp_path / "alpha").mkdir()
    started = threading.Event()
    release = threading.Event()
    original = directory_popup._enumerate_directory_candidates

    def delayed(
        value: str,
        *,
        home: pathlib.Path,
        candidate_limit: int,
        scan_limit: int,
    ) -> object:
        started.set()
        release.wait(1)
        return original(
            value,
            home=home,
            candidate_limit=candidate_limit,
            scan_limit=scan_limit,
        )

    monkeypatch.setattr(directory_popup, "_enumerate_directory_candidates", delayed)
    app = _DirectoryPopupHost(tmp_path / "home")
    async with app.run_test(size=(60, 16)) as pilot:
        picker = app.query_one(ExportDirectoryPicker)
        picker.value = f"{tmp_path}{os.sep}a"
        await _wait_for(pilot, started.is_set)
        await pilot.press("tab")
        release.set()
        await pilot.pause(0.2)

        assert _popup(app).display is False
        assert app.query_one("#filename", Input).has_focus


@pytest.mark.slow
async def test_unmount_cancels_worker_and_invalidates_generation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A removed picker cannot receive completion chrome from its worker."""
    (tmp_path / "alpha").mkdir()
    started = threading.Event()
    release = threading.Event()
    original = directory_popup._enumerate_directory_candidates

    def delayed(
        value: str,
        *,
        home: pathlib.Path,
        candidate_limit: int,
        scan_limit: int,
    ) -> object:
        started.set()
        release.wait(1)
        return original(
            value,
            home=home,
            candidate_limit=candidate_limit,
            scan_limit=scan_limit,
        )

    monkeypatch.setattr(directory_popup, "_enumerate_directory_candidates", delayed)
    app = _DirectoryPopupHost(tmp_path / "home")
    async with app.run_test(size=(60, 16)) as pilot:
        picker = app.query_one(ExportDirectoryPicker)
        popup = _popup(app)
        picker.value = f"{tmp_path}{os.sep}a"
        await _wait_for(pilot, started.is_set)
        generation = picker._candidate_generation
        workers = tuple(
            worker for worker in picker.workers if worker.group == "export-directory-completion"
        )

        await picker.remove()
        release.set()
        await pilot.pause(0.05)

        assert picker._candidate_generation > generation
        assert workers and all(worker.is_cancelled for worker in workers)
        assert popup.display is False
        assert not app.query(ExportDirectoryPicker)


@pytest.mark.slow
async def test_popup_stays_within_picker_at_compact_geometry(tmp_path: pathlib.Path) -> None:
    """The borderless overlay never exceeds its owning picker at 60 by 16."""
    (tmp_path / "alpha").mkdir()
    app = _DirectoryPopupHost(tmp_path / "home")
    async with app.run_test(size=(60, 16)) as pilot:
        picker = app.query_one(ExportDirectoryPicker)
        popup = _popup(app)
        picker.value = f"{tmp_path}{os.sep}a"
        await _wait_for(pilot, lambda: popup.option_count == 1)
        await pilot.pause()

        assert popup.styles.border.top[0] == ""
        assert popup.region.x >= picker.region.x
        assert popup.region.right <= picker.region.right
        assert popup.region.width <= picker.region.width
