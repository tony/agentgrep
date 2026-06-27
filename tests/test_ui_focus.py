"""Focus-graph tests: ``ctrl+hjkl`` pane traversal and modal focus restore.

Tab order (``search`` → ``filter`` → ``results``) is already covered in
``test_agentgrep.py``; this pins the directional ``ctrl+jk`` pane navigation
(which has real per-pane branching in ``action_focus_pane_up`` /
``action_focus_pane_down``) and that dismissing the Ctrl-R recall modal restores
focus to the widget that was focused when it opened (ADR 0012 RW focus graph).
"""

from __future__ import annotations

import pathlib

import pytest

from agentgrep.ui.widgets import HistoryRecall
from tests.test_agentgrep import _build_empty_ui_app


async def test_ctrl_jk_traverses_panes_vertically(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-J walks search -> filter -> results; Ctrl-K reverses the path."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        app._search_input.focus()
        await pilot.pause()
        assert app.focused.id == "search"
        await pilot.press("ctrl+j")
        assert app.focused.id == "filter"
        await pilot.press("ctrl+j")
        assert app.focused.id == "results"
        await pilot.press("ctrl+k")
        assert app.focused.id == "filter"
        await pilot.press("ctrl+k")
        assert app.focused.id == "search"


async def test_recall_modal_restores_focus_on_escape(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-R opens the recall modal; Escape pops it and restores prior focus."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        app._search_input.focus()
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, HistoryRecall)
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "search"
