"""Focused contracts for the Pi-like detail-pane export flow."""

from __future__ import annotations

import pathlib
import typing as t

import pytest
from textual.color import Color
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList
from textual.widgets.input import Selection

import agentgrep.ui.widgets as widgets
from agentgrep.ui.widgets.directory_popup import ExportDirectoryPicker
from tests._agentgrep_tui_support import _build_empty_ui_app
from tests.test_ui_export import _load_records, _record, _static_text, _wait_for

pytestmark = pytest.mark.tui

ExportPane = t.cast(t.Any, getattr(widgets, "ExportPane", None))


def _forbid_screen_push(*_args: object, **_kwargs: object) -> t.NoReturn:
    """Fail if selected-record export enters Textual's screen stack."""
    message = "selected-record export pushed a screen"
    raise AssertionError(message)


async def _open_review(
    pane: t.Any,
    pilot: t.Any,
    destination: pathlib.Path,
) -> None:
    """Fill one destination and wait for its literal review stage."""
    pane.query_one("#export-directory", ExportDirectoryPicker).value = str(
        destination.parent,
    )
    template = pane.query_one("#export-template", Input)
    template.value = destination.name
    template.focus()
    await pilot.press("enter")
    await _wait_for(lambda: pane.phase == "review")


@pytest.mark.slow
async def test_e_mounts_export_pane_without_replacing_reader_or_screen(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shortcut changes detail ownership without a modal or query mutation."""
    assert ExportPane is not None
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "frozen body", ordinal=1, title="Selected")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        hud = app.screen
        await _load_records(hud, (record,))
        reader = hud.query_one("#detail-scroll")
        search = hud._search_input
        search.value = "agent:codex selected"
        search.selection = Selection(2, 11)
        hud._results.focus()
        await pilot.pause()
        monkeypatch.setattr(app, "push_screen", _forbid_screen_push)

        await pilot.press("e")
        await pilot.pause()

        pane = hud.query_one(ExportPane)
        assert app.screen is hud
        assert not isinstance(pane, ModalScreen)
        assert pane.parent is hud.query_one("#detail-column")
        assert reader.is_mounted
        assert pane.selected_record is record
        assert search.value == "agent:codex selected"
        assert search.selection == Selection(2, 11)
        assert (
            pane.query_one("#export-directory", ExportDirectoryPicker)
            .query_one(
                Input,
            )
            .has_focus
        )


@pytest.mark.slow
async def test_export_pane_ignores_actions_during_deferred_mount(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-turn priority keys cannot query children before compose completes."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "body", ordinal=1)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        hud = app.screen
        await _load_records(hud, (record,))
        hud._results.focus()

        assert hud.open_export_pane("", selected_record=record)
        pane = hud._export_pane
        assert pane is not None
        assert not pane.is_mounted
        hud.action_focus_pane_down()
        hud.action_smart_quit()

        await pilot.pause()
        assert pane.is_mounted
        assert hud._export_pane is pane


@pytest.mark.slow
async def test_typed_export_path_restores_query_selection_and_search_focus(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slash command text is transient and returns to its exact query draft."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "body", ordinal=1, title="Selected")
    destination = tmp_path / "exports" / "chosen.md"
    destination.parent.mkdir()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        hud = app.screen
        await _load_records(hud, (record,))
        search = hud._search_input
        search.value = "exact query"
        search.selection = Selection(1, 7)
        await pilot.pause()
        search.value = f"/export {destination}"
        search.cursor_position = len(search.value)
        search.focus()
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()

        pane = hud.query_one(ExportPane)
        assert search.value == "exact query"
        assert search.selection == Selection(1, 7)
        assert pane.query_one("#export-directory", ExportDirectoryPicker).value == str(
            destination.parent,
        )
        assert pane.query_one("#export-template", Input).value == destination.name

        await pilot.press("escape")
        await _wait_for(lambda: not hud.query(ExportPane))

        assert search.has_focus
        assert search.value == "exact query"
        assert search.selection == Selection(1, 7)


@pytest.mark.slow
async def test_export_pane_priority_navigation_stays_inside_action(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HUD priority keys become field traversal while export owns the pane."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "body", ordinal=1)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        hud = app.screen
        await _load_records(hud, (record,))
        hud._results.focus()
        await pilot.press("e")
        pane = hud.query_one(ExportPane)
        directory = pane.query_one("#export-directory", ExportDirectoryPicker).query_one(
            Input,
        )
        template = pane.query_one("#export-template", Input)
        assert directory.has_focus

        await pilot.press("ctrl+j")
        assert template.has_focus
        await pilot.press("ctrl+h")
        assert directory.has_focus
        await pilot.press("ctrl+l")
        assert template.has_focus
        await pilot.press("ctrl+k")
        assert directory.has_focus

        await pilot.press("tab")
        assert template.has_focus
        await pilot.press("tab")
        assert directory.has_focus
        await pilot.press("shift+tab")
        assert template.has_focus
        assert app.focused is not hud._search_input

        await pilot.press("ctrl+r", "q")
        assert app.screen is hud
        assert not hud.query("HistoryRecall")
        assert hud.query_one(ExportPane) is pane


@pytest.mark.slow
async def test_export_pane_survives_resize_without_mutating_reader_intent(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The action stays full-body across the split breakpoint and restores state."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "body", ordinal=1)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        hud = app.screen
        await _load_records(hud, (record,))
        hud._set_zoomed_pane("results")
        hud._results.focus()
        await pilot.pause()
        hud._detail_opened = False
        hud._apply_responsive_layout()
        zoomed = hud._zoomed_pane
        detail_opened = hud._detail_opened

        await pilot.press("e")
        pane = hud.query_one(ExportPane)
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert pane.region.width == hud.query_one("#body").region.width
        assert pane.region.height > 0
        await pilot.resize_terminal(120, 30)
        await pilot.pause()
        assert pane.region.height > 0

        await pilot.press("escape")
        await _wait_for(lambda: not hud.query(ExportPane))
        assert hud._zoomed_pane == zoomed
        assert hud._detail_opened is detail_opened


@pytest.mark.slow
async def test_export_pane_saves_frozen_record_and_is_fresh_next_time(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Save uses the opening record and teardown permits one clean next session."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = (
        _record(tmp_path, "opening selection", ordinal=1, title="First"),
        _record(tmp_path, "later selection", ordinal=2, title="Second"),
    )
    destination = tmp_path / "frozen.md"
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        hud = app.screen
        await _load_records(hud, records, selected=0)
        hud._results.focus()
        await pilot.press("e")
        first_pane = hud.query_one(ExportPane)
        hud._results.highlighted = 1
        hud._current_detail_record = records[1]
        await _open_review(first_pane, pilot, destination)
        assert _static_text(first_pane, "#export-review-filename") == destination.name

        await pilot.press("y")
        await _wait_for(destination.exists)
        await _wait_for(lambda: not hud.query(ExportPane))

        exported = destination.read_text(encoding="utf-8")
        assert "opening selection" in exported
        assert "later selection" not in exported
        hud._results.highlighted = 1
        hud._results.focus()
        await pilot.press("e")
        second_pane = hud.query_one(ExportPane)
        assert second_pane is not first_pane
        assert second_pane.selected_record is records[1]


@pytest.mark.slow
async def test_export_review_remains_usable_at_minimum_terminal_height(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long review content scrolls while the Pi header and footer remain visible."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "body", ordinal=1, title="A long selected title")
    destination = tmp_path / ("wrapped-" + "x" * 70 + ".md")
    async with app.run_test(size=(24, 8)) as pilot:
        await pilot.pause()
        hud = app.screen
        await _load_records(hud, (record,))
        hud._results.focus()
        await pilot.press("e")
        pane = hud.query_one(ExportPane)
        await _open_review(pane, pilot, destination)

        header = pane.query_one("#export-pane-header")
        assert header.region.height == 1
        assert header.styles.color == Color.parse(app.theme_variables["accent"])
        assert pane.query_one("#export-review-status").region.height == 1
        confirm = pane.query_one("#export-confirm", OptionList)
        review = pane.query_one("#export-review")
        assert review.region.bottom <= pane.region.bottom
        assert confirm.region.overlaps(pane.region)
        assert confirm.highlighted == 0
        assert confirm.region.height > 0
        await pilot.press("tab", "shift+tab")
        assert confirm.has_focus
