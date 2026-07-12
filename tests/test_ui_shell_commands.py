"""Mounted-shell regressions for Textual command handling."""

from __future__ import annotations

import pathlib
import typing as t

import pytest
from textual.command import CommandPalette

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
