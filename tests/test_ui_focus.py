"""Focus-graph tests: ``ctrl+hjkl`` pane traversal and modal focus restore.

Tab order (``search`` → ``filter`` → ``results``) is already covered in
``test_agentgrep.py``; this pins the directional ``ctrl+jk`` pane navigation
(which has real per-pane branching in ``action_focus_pane_up`` /
``action_focus_pane_down``) and that dismissing the Ctrl-R recall modal restores
focus to the widget that was focused when it opened (ADR 0012 RW focus graph).
"""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
import threading
import typing as t

import pytest

from agentgrep.records import RecordOrigin
from agentgrep.ui.widgets import HistoryRecall
from tests.test_agentgrep_tui import _build_empty_ui_app, load_agentgrep_module


async def test_ctrl_jk_traverses_panes_vertically(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-J walks search -> filter -> results; Ctrl-K reverses the path."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        app.screen._search_input.focus()
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
        app.screen._search_input.focus()
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, HistoryRecall)
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "search"


@pytest.mark.parametrize("launch_kind", ["compiled", "origin"])
async def test_launch_search_keeps_focus_on_visible_input(
    launch_kind: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-flight launch routes typing to the visible search input."""
    from agentgrep.query import build_query_from_input, default_registry
    from agentgrep.ui import _seams

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    base = agentgrep.SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    if launch_kind == "compiled":
        built = build_query_from_input("agent:codex", base, default_registry())
        assert built.query is not None
        query = built.query
        initial_text = "agent:codex"
    else:
        query = dataclasses.replace(
            base,
            origin_filter=RecordOrigin(repo="example/repo"),
        )
        initial_text = None

    started = threading.Event()
    release = threading.Event()

    def hold_search(
        _self: object,
        _query: object,
        *,
        control: object,
        emit: object,
    ) -> None:
        del control, emit
        started.set()
        release.wait(timeout=2)

    monkeypatch.setattr(_seams.EngineSearchInvoker, "run", hold_search)
    app = agentgrep.build_streaming_ui_app(
        home,
        query,
        control=agentgrep.SearchControl(),
        initial_search_text=initial_text,
    )

    try:
        async with app.run_test(size=(80, 24)) as pilot:
            assert await asyncio.to_thread(started.wait, 1)
            await pilot.pause()
            search = app.screen.query_one("#search")
            filter_input = app.screen.query_one("#filter")
            assert app.screen.query_one("#body").has_class("-searching")
            assert search.is_on_screen
            assert not filter_input.is_on_screen
            assert app.focused is search

            await pilot.press("x")
            assert search.value.endswith("x")
            assert filter_input.value == ""
    finally:
        release.set()
