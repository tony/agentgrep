"""Mounted-shell regressions for Textual command handling."""

from __future__ import annotations

import pathlib
import typing as t

import pytest
from textual.command import CommandPalette

from agentgrep.ui import theme as ui_theme
from agentgrep.ui._shell import ExplorerApp
from tests.test_agentgrep import _build_empty_ui_app


class ShellSizeCase(t.NamedTuple):
    """One terminal size whose shell state must survive Ctrl-P."""

    test_id: str
    size: tuple[int, int]


_SHELL_SIZE_CASES: tuple[ShellSizeCase, ...] = (
    ShellSizeCase(test_id="stacked-77x30", size=(77, 30)),
    ShellSizeCase(test_id="split-120x30", size=(120, 30)),
)


async def _mount_greplog(app: t.Any, pilot: t.Any) -> t.Any:
    """Push the grep-log layout with its normal search workflow."""
    from agentgrep.ui.layouts.greplog import GrepLogLayout
    from agentgrep.ui.workflows.search import SearchWorkflow

    layout = GrepLogLayout(app._ctx, SearchWorkflow())
    await app.push_screen(layout)
    await pilot.pause()
    return layout


async def _submit(pilot: t.Any, layout: t.Any, text: str) -> None:
    """Submit ``text`` through a mounted layout's real search input."""
    layout._search_input.value = text
    layout._search_input.cursor_position = len(text)
    layout._search_input.focus()
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


def test_explorer_app_disables_textual_command_palette() -> None:
    """The shell disables Textual's palette and exposes no providers."""
    assert ExplorerApp.ENABLE_COMMAND_PALETTE is False
    assert not ExplorerApp.COMMANDS


@pytest.mark.parametrize(
    "case",
    _SHELL_SIZE_CASES,
    ids=[case.test_id for case in _SHELL_SIZE_CASES],
)
async def test_ctrl_p_preserves_mounted_shell_state(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: ShellSizeCase,
) -> None:
    """Ctrl-P is inert across the stacked and split HUD layouts."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=case.size) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        body = app.screen.query_one("#body")
        search.value = "palette needle"
        search.cursor_position = len("palette")
        search.focus()
        await pilot.pause()

        screen_stack = tuple(app.screen_stack)
        focused = app.focused
        search_value = search.value
        search_cursor = search.cursor_position
        search_region = search.region
        body_region = body.region

        await pilot.press("ctrl+p")
        await pilot.pause()

        assert not CommandPalette.is_open(app)
        assert tuple(app.screen_stack) == screen_stack
        assert app.focused is focused
        assert search.value == search_value
        assert search.cursor_position == search_cursor
        assert search.region == search_region
        assert body.region == body_region


async def test_slash_keys_notifies_visible_layout_bindings_without_reflow(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/keys`` reports App/Screen bindings without mounting a help panel."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        notes: list[tuple[tuple[object, ...], dict[str, object]]] = []
        monkeypatch.setattr(layout, "notify", lambda *a, **k: notes.append((a, k)))
        screen_stack = tuple(app.screen_stack)
        body_region = layout.query_one("#body").region

        await _submit(pilot, layout, "/keys")

        assert tuple(app.screen_stack) == screen_stack
        assert layout.query_one("#body").region == body_region
        assert layout._search_input.value == ""
        assert len(notes) == 1
        message = str(notes[0][0][0]).lower()
        assert "layout" in message
        assert "workflow" in message
        assert "switch focus" in message
        assert "delete" not in message
        assert "cut" not in message
        assert "paste" not in message


async def test_slash_theme_selects_and_toggles_agentgrep_themes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/theme`` changes only between agentgrep's dark and light themes."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        def forbidden(*_args: object, **_kwargs: object) -> t.NoReturn:
            message = "theme command opened a palette screen"
            raise AssertionError(message)

        monkeypatch.setattr(app, "search_themes", forbidden, raising=False)
        monkeypatch.setattr(app, "push_screen", forbidden)

        await _submit(pilot, app.screen, "/theme light")
        assert app.theme == ui_theme.LIGHT_THEME_NAME
        assert app.screen._search_input.value == ""

        await _submit(pilot, app.screen, "/theme")
        assert app.theme == ui_theme.DARK_THEME_NAME
        assert app.screen._search_input.value == ""


async def test_invalid_slash_theme_remains_editable_and_does_not_search(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recognized invalid theme warns without clearing or searching the text."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = app.screen
        notes: list[tuple[tuple[object, ...], dict[str, object]]] = []
        queries: list[str] = []
        monkeypatch.setattr(layout, "notify", lambda *a, **k: notes.append((a, k)))
        monkeypatch.setattr(layout.workflow, "on_query", lambda _host, text: queries.append(text))

        await _submit(pilot, layout, "/theme sepia")

        assert app.theme == ui_theme.DARK_THEME_NAME
        assert layout._search_input.value == "/theme sepia"
        assert queries == []
        assert len(notes) == 1
        assert "dark or light" in str(notes[0][0][0]).lower()


async def test_common_commands_run_in_greplog(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grep-log exposes the same help and clear commands as the HUD."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        notes: list[tuple[tuple[object, ...], dict[str, object]]] = []
        monkeypatch.setattr(layout, "notify", lambda *a, **k: notes.append((a, k)))

        await _submit(pilot, layout, "/help")
        assert layout._search_input.value == ""
        assert len(notes) == 1
        assert "/theme" in str(notes[0][0][0])

        layout._records = [object()]
        old_control = layout.control
        await _submit(pilot, layout, "/clear")
        assert old_control.answer_now_requested() is True
        assert layout._records == []
        assert layout._search_input.value == ""


@pytest.mark.parametrize("text", ("/exit", "/quit"))
async def test_exit_aliases_run_in_greplog(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    """Both exit spellings remain reachable from the grep-log input."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        exits: list[object] = []
        monkeypatch.setattr(app, "exit", lambda *a, **k: exits.append((a, k)))

        await _submit(pilot, layout, text)

        assert len(exits) == 1
        assert layout._search_input.value == ""


async def test_unsupported_command_arguments_remain_greplog_search_text(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy command-plus-text forms still route through the grep workflow."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        queries: list[str] = []
        monkeypatch.setattr(layout.workflow, "on_query", lambda _host, text: queries.append(text))

        await _submit(pilot, layout, "/help find prompts")

        assert queries == ["/help find prompts"]
