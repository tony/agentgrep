"""Tests for the internal pluggable-layout registry and fixed TUI shell.

Covers registry resolution, the fixed ``agentgrep ui`` CLI surface, factory
validation, and programmatic initial composition (ADR 0013).
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import inspect
import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery
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


def test_shell_accepts_one_typed_immutable_composition() -> None:
    """The internal shell receives one validated component pair."""
    from agentgrep.ui import registry
    from agentgrep.ui._shell import ExplorerApp

    assert "UiComposition" not in registry.__all__
    assert not hasattr(registry, "UiComposition")
    assert hasattr(registry, "_UiComposition")
    parameters = inspect.signature(ExplorerApp).parameters
    assert "composition" in parameters
    assert "layout" not in parameters
    assert "workflow" not in parameters

    layout = registry.layout_spec("hud")
    workflow = registry.workflow_spec("search")
    assert layout is not None
    assert workflow is not None
    composition = registry._UiComposition(layout=layout, workflow=workflow)
    assert dataclasses.is_dataclass(composition)
    field_name = "layout"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(composition, field_name, layout)


def test_ui_command_has_no_layout_workflow_fields() -> None:
    """The normal ``ui`` command carries no layout/workflow selection."""
    args = agentgrep.parse_args(["ui"])
    assert isinstance(args, agentgrep.UIArgs)
    assert not hasattr(args, "layout")
    assert not hasattr(args, "workflow")


@pytest.mark.parametrize(
    ("option", "value"),
    (("--layout", "greplog"), ("--workflow", "browse")),
)
def test_ui_command_rejects_layout_workflow_options(option: str, value: str) -> None:
    """The normal ``ui`` command does not advertise component selection."""
    with pytest.raises(SystemExit):
        agentgrep.parse_args(["ui", option, value])


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


async def test_explorer_app_has_fixed_shell_surface(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mounted shell exposes no normal layout/workflow switcher."""
    from agentgrep.ui._shell import ExplorerApp

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    assert ExplorerApp.BINDINGS == []
    assert not hasattr(ExplorerApp, "action_cycle_layout")
    assert not hasattr(ExplorerApp, "action_cycle_workflow")
    assert not hasattr(ExplorerApp, "_mode_factory")
    assert not hasattr(ExplorerApp, "_adopt_launch_mode")
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        workflow = screen.workflow
        assert app._modes == {}
        assert app.sub_title == ""
        await pilot.press("f2", "f3")
        await pilot.pause()
        assert app.screen is screen
        assert app.screen.workflow is workflow


async def test_factory_mounts_only_programmatically_selected_pair(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Factory injection selects one pair without registering switchable modes."""
    from agentgrep.ui.layouts._base import LayoutScreen

    query = agentgrep.SearchQuery(
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
    app = t.cast(
        "t.Any",
        agentgrep.build_streaming_ui_app(
            home,
            query,
            control=agentgrep.SearchControl(),
            layout="greplog",
            workflow="browse",
        ),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert type(app.screen).__name__ == "GrepLogLayout"
        assert app.screen.workflow.name == "browse"
        assert [screen for screen in app.screen_stack if isinstance(screen, LayoutScreen)] == [
            app.screen,
        ]
        assert app._modes == {}


@pytest.mark.parametrize(
    ("layout_name", "workflow_name", "layout_class_name"),
    (
        ("hud", "search", "HudLayout"),
        ("hud", "browse", "HudLayout"),
        ("greplog", "search", "GrepLogLayout"),
        ("greplog", "browse", "GrepLogLayout"),
    ),
)
async def test_explorer_app_composition_selects_initial_pair(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    layout_name: str,
    workflow_name: str,
    layout_class_name: str,
) -> None:
    """Direct embedding can still inject any registered initial pair."""
    from agentgrep.ui import registry
    from agentgrep.ui._context import UiContext
    from agentgrep.ui._shell import ExplorerApp
    from agentgrep.ui.layouts._base import LayoutScreen

    layout_spec = registry.layout_spec(layout_name)
    workflow_spec = registry.workflow_spec(workflow_name)
    assert layout_spec is not None
    assert workflow_spec is not None
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    query = SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )
    app = ExplorerApp(
        UiContext(
            home=home,
            invoker=_NoopInvoker(),
            query=query,
            control=SearchControl(),
        ),
        composition=registry._UiComposition(
            layout=layout_spec,
            workflow=workflow_spec,
        ),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert type(app.screen).__name__ == layout_class_name
        assert t.cast(LayoutScreen, app.screen).workflow.name == workflow_name
