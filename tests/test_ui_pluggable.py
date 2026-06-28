"""Tests for the pluggable-layout registry, CLI selection, and live switching.

Covers the registry resolution, the ``agentgrep ui --layout/--workflow`` CLI
surface, the factory's validation, and the runtime ``f2`` / ``f3`` switching
(ADR 0013, commit 5).
"""

from __future__ import annotations

import collections.abc as cabc
import pathlib
import typing as t

import pytest

import agentgrep
from tests.test_agentgrep import _build_empty_ui_app


class _NoopInvoker:
    """Search seam stub for startup tests that need non-empty query terms."""

    def run(
        self,
        query: object,
        *,
        control: object,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Accept the search request without touching the engine."""


class ResolveCase(t.NamedTuple):
    """A registry lookup: a kind + name and the class its loader yields."""

    test_id: str
    kind: str  # "layout" or "workflow"
    name: str
    cls_name: str


RESOLVE_CASES = (
    ResolveCase("layout-hud", "layout", "hud", "HudLayout"),
    ResolveCase("layout-greplog", "layout", "greplog", "GrepLogLayout"),
    ResolveCase("workflow-search", "workflow", "search", "SearchWorkflow"),
    ResolveCase("workflow-browse", "workflow", "browse", "BrowseWorkflow"),
)


@pytest.mark.parametrize("case", RESOLVE_CASES, ids=lambda c: c.test_id)
def test_registry_resolves_builtins(case: ResolveCase) -> None:
    """Each built-in name resolves to its class via a lazy loader."""
    from agentgrep.ui import registry

    spec = (
        registry.layout_spec(case.name)
        if case.kind == "layout"
        else registry.workflow_spec(case.name)
    )
    assert spec is not None
    assert spec.loader().__name__ == case.cls_name


def test_registry_lists_names_and_rejects_unknown() -> None:
    """Names are listed in display order and unknown lookups return ``None``."""
    from agentgrep.ui import registry

    assert registry.layout_names() == ("hud", "greplog")
    assert registry.workflow_names() == ("search", "browse")
    assert registry.DEFAULT_LAYOUT == "hud"
    assert registry.DEFAULT_WORKFLOW == "search"
    assert registry.layout_spec("nope") is None
    assert registry.workflow_spec("nope") is None


class CliCase(t.NamedTuple):
    """A ``ui`` invocation and the layout/workflow it should parse to."""

    test_id: str
    argv: tuple[str, ...]
    layout: str
    workflow: str


CLI_CASES = (
    CliCase("defaults", ("ui",), "hud", "search"),
    CliCase("explicit", ("ui", "--layout", "greplog", "--workflow", "browse"), "greplog", "browse"),
)


@pytest.mark.parametrize("case", CLI_CASES, ids=lambda c: c.test_id)
def test_ui_command_parses_layout_workflow(case: CliCase) -> None:
    """``agentgrep ui`` parses ``--layout`` / ``--workflow`` into UIArgs."""
    args = agentgrep.parse_args(case.argv)
    assert isinstance(args, agentgrep.UIArgs)
    assert args.layout == case.layout
    assert args.workflow == case.workflow


def test_ui_command_rejects_unknown_layout() -> None:
    """Argparse ``choices`` reject an unregistered ``--layout`` value."""
    with pytest.raises(SystemExit):
        agentgrep.parse_args(["ui", "--layout", "nope"])


def test_build_streaming_ui_app_validates_selection(tmp_path: pathlib.Path) -> None:
    """The factory rejects an unknown layout/workflow before touching Textual."""
    query = agentgrep.SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )
    with pytest.raises(ValueError, match="unknown layout"):
        agentgrep.build_streaming_ui_app(
            tmp_path,
            query,
            control=agentgrep.SearchControl(),
            layout="nope",
        )
    with pytest.raises(ValueError, match="unknown workflow"):
        agentgrep.build_streaming_ui_app(
            tmp_path,
            query,
            control=agentgrep.SearchControl(),
            workflow="nope",
        )


async def test_f2_cycles_through_layouts(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``F2`` switches the active layout through the registry and wraps around."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert type(app.screen).__name__ == "HudLayout"
        await pilot.press("f2")
        await pilot.pause()
        assert type(app.screen).__name__ == "GrepLogLayout"
        await pilot.press("f2")
        await pilot.pause()
        assert type(app.screen).__name__ == "HudLayout"


async def test_f2_resumes_launch_layout(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching back to the launch layout resumes its existing screen."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        hud = app.screen
        await pilot.press("f2")
        await pilot.pause()
        assert type(app.screen).__name__ == "GrepLogLayout"
        await pilot.press("f2")
        await pilot.pause()
        assert app.screen is hud


async def test_f2_ignores_active_history_modal(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``F2`` must not replace a modal screen stack."""
    from agentgrep.ui._history import HistoryEntry
    from agentgrep.ui.widgets.history import HistoryRecall

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        hud = app.screen
        hud._history = [HistoryEntry(text="agent:codex refactor", ts=10)]
        hud._search_input.focus()
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, HistoryRecall)
        await pilot.press("f2")
        await pilot.pause()
        assert isinstance(app.screen, HistoryRecall)
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is hud
        assert hud._search_input.value == "agent:codex refactor"


async def test_launch_query_resumes_launch_layout(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An initial query does not break the named launch layout stack."""
    from agentgrep.ui._context import UiContext
    from agentgrep.ui._shell import ExplorerApp

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    query = agentgrep.SearchQuery(
        terms=("mobx",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    app = ExplorerApp(
        UiContext(
            home=home,
            invoker=_NoopInvoker(),
            query=query,
            control=agentgrep.SearchControl(),
        ),
    )
    assert app._current_mode == app.DEFAULT_MODE
    assert "hud" not in app._screen_stacks
    async with app.run_test() as pilot:
        await pilot.pause()
        hud = app.screen
        assert type(hud).__name__ == "HudLayout"
        await pilot.press("f2")
        await pilot.pause()
        assert type(app.screen).__name__ == "GrepLogLayout"
        await pilot.press("f2")
        await pilot.pause()
        assert app.screen is hud


async def test_f3_cycles_through_workflows(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``F3`` swaps the active layout's workflow through the registry."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen.workflow.name == "search"
        await pilot.press("f3")
        await pilot.pause()
        assert app.screen.workflow.name == "browse"
        await pilot.press("f3")
        await pilot.pause()
        assert app.screen.workflow.name == "search"


async def test_f3_updates_suspended_layout_workflow(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``F3`` updates already-created layouts before they are resumed."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        greplog = app.screen
        assert type(greplog).__name__ == "GrepLogLayout"
        assert greplog.workflow.name == "search"
        await pilot.press("f2")
        await pilot.pause()
        assert type(app.screen).__name__ == "HudLayout"
        await pilot.press("f3")
        await pilot.pause()
        assert app.screen.workflow.name == "browse"
        await pilot.press("f2")
        await pilot.pause()
        assert app.screen is greplog
        assert app.screen.workflow.name == "browse"


async def test_launch_into_greplog_layout(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building the app with ``layout='greplog'`` launches straight into it."""
    agentgrep_mod = t.cast("t.Any", agentgrep)
    query = agentgrep_mod.SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    app = agentgrep_mod.build_streaming_ui_app(
        home,
        query,
        control=agentgrep_mod.SearchControl(),
        layout="greplog",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert type(app.screen).__name__ == "GrepLogLayout"
