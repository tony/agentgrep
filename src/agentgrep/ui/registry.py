"""Typed registries of the built-in TUI layouts and workflows (ADR 0013).

A small, frozen, Textual-free catalog the App shell and the CLI consult to
resolve a ``--layout`` / ``--workflow`` name into a class. Each spec carries a
*lazy* loader (a function-local import) so listing the names never imports
Textual — only launching a layout does — keeping ``agentgrep --help`` cold. A
future third-party source (``importlib.metadata`` entry points) can feed the same
:class:`LayoutSpec` / :class:`WorkflowSpec` shape without changing consumers.
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
    """A registered layout: its CLI name, one-line summary, and a lazy loader."""

    name: str
    summary: str
    loader: cabc.Callable[[], type[LayoutScreen]]


@dataclasses.dataclass(frozen=True, slots=True)
class WorkflowSpec:
    """A registered workflow: its CLI name, one-line summary, and a lazy loader."""

    name: str
    summary: str
    loader: cabc.Callable[[], type[Workflow]]


def _load_hud() -> type[LayoutScreen]:
    from agentgrep.ui.layouts.hud import HudLayout

    return HudLayout


def _load_greplog() -> type[LayoutScreen]:
    from agentgrep.ui.layouts.greplog import GrepLogLayout

    return GrepLogLayout


def _load_chat() -> type[LayoutScreen]:
    from agentgrep.ui.layouts.chat import ChatLayout

    return ChatLayout


def _load_search() -> type[Workflow]:
    from agentgrep.ui.workflows.search import SearchWorkflow

    return SearchWorkflow


def _load_browse() -> type[Workflow]:
    from agentgrep.ui.workflows.browse import BrowseWorkflow

    return BrowseWorkflow


def _load_deductive() -> type[Workflow]:
    from agentgrep.ui.workflows.deductive import DeductiveWorkflow

    return DeductiveWorkflow


#: The built-in layouts, in display order. The first is the default.
LAYOUTS: tuple[LayoutSpec, ...] = (
    LayoutSpec("hud", "Search box, streaming results list, and detail pane", _load_hud),
    LayoutSpec("greplog", "Append-only streaming grep log", _load_greplog),
    LayoutSpec("chat", "Conversation transcript of streamed records", _load_chat),
)

#: The built-in workflows, in display order. The first is the default.
WORKFLOWS: tuple[WorkflowSpec, ...] = (
    WorkflowSpec("search", "Live incremental search over the engine", _load_search),
    WorkflowSpec("browse", "Browse a loaded set; the input filters in-memory", _load_browse),
    WorkflowSpec("deductive", "Narrow a fixed haystack; widen pops back out", _load_deductive),
)

#: The default layout / workflow when ``--layout`` / ``--workflow`` is unset.
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
