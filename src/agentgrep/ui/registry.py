"""Typed registries of the internal TUI layouts and workflows (ADR 0013).

A small, frozen, Textual-free catalog the Python app factories consult to
resolve injected names. They pass one frozen composition value to the shell, so
Textual code receives an already-validated pair. Each spec carries a *lazy*
loader (a function-local import), so inspecting the registry never imports
Textual.
"""

from __future__ import annotations

import dataclasses
import typing as t

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from agentgrep.ui.layouts._base import LayoutScreen
    from agentgrep.ui.workflows import Workflow

__all__ = [
    "DEFAULT_LAYOUT",
    "DEFAULT_WORKFLOW",
    "LAYOUTS",
    "WORKFLOWS",
    "LayoutSpec",
    "WorkflowSpec",
    "layout_names",
    "layout_spec",
    "workflow_names",
    "workflow_spec",
]


@dataclasses.dataclass(frozen=True, slots=True)
class LayoutSpec:
    """A registered layout: its stable name, one-line summary, and lazy loader."""

    name: str
    summary: str
    loader: cabc.Callable[[], type[LayoutScreen]]
    uses_history: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class WorkflowSpec:
    """A registered workflow: its stable name, summary, and lazy loader."""

    name: str
    summary: str
    loader: cabc.Callable[[], type[Workflow]]


@dataclasses.dataclass(frozen=True, slots=True)
class _UiComposition:
    """One resolved layout/workflow pair for the internal App shell."""

    layout_type: type[LayoutScreen]
    workflow_type: type[Workflow]


def _load_hud() -> type[LayoutScreen]:
    from agentgrep.ui.layouts.hud import HudLayout

    return HudLayout


def _load_greplog() -> type[LayoutScreen]:
    from agentgrep.ui.layouts.greplog import GrepLogLayout

    return GrepLogLayout


def _load_search() -> type[Workflow]:
    from agentgrep.ui.workflows.search import SearchWorkflow

    return SearchWorkflow


def _load_browse() -> type[Workflow]:
    from agentgrep.ui.workflows.browse import BrowseWorkflow

    return BrowseWorkflow


#: The built-in layouts, in display order. The first is the default.
LAYOUTS: tuple[LayoutSpec, ...] = (
    LayoutSpec(
        "hud",
        "Search box, streaming results list, and detail pane",
        _load_hud,
        uses_history=True,
    ),
    LayoutSpec("greplog", "Append-only streaming grep log", _load_greplog),
)

#: The built-in workflows, in display order. The first is the default.
WORKFLOWS: tuple[WorkflowSpec, ...] = (
    WorkflowSpec("search", "Live incremental search over the engine", _load_search),
    WorkflowSpec("browse", "Browse a loaded set; the input filters in-memory", _load_browse),
)

#: The fixed shipped pair and the defaults for omitted Python injection.
DEFAULT_LAYOUT = LAYOUTS[0].name
DEFAULT_WORKFLOW = WORKFLOWS[0].name


def layout_spec(name: str) -> LayoutSpec | None:
    """Return the layout spec named ``name``, or ``None`` if unknown."""
    return next((spec for spec in LAYOUTS if spec.name == name), None)


def workflow_spec(name: str) -> WorkflowSpec | None:
    """Return the workflow spec named ``name``, or ``None`` if unknown."""
    return next((spec for spec in WORKFLOWS if spec.name == name), None)


def layout_names() -> tuple[str, ...]:
    """Return the registered layout names, in display order."""
    return tuple(spec.name for spec in LAYOUTS)


def workflow_names() -> tuple[str, ...]:
    """Return the registered workflow names, in display order."""
    return tuple(spec.name for spec in WORKFLOWS)
