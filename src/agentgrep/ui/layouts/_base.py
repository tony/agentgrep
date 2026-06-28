"""``LayoutScreen`` — the base for pluggable explorer layouts (ADR 0013).

A layout is a Textual ``Screen`` injected with the shared
:class:`~agentgrep.ui._context.UiContext`. Subclasses own their ``compose``, CSS,
bindings, and presentation; they reach the engine only through
``context.invoker`` (ADR 0012 RW-1) and run all blocking work off the pump
(ADR 0011). The App shell mounts one subclass as the active layout.
"""

from __future__ import annotations

import typing as t

from textual.screen import Screen

if t.TYPE_CHECKING:
    from agentgrep.ui._context import UiContext
    from agentgrep.ui.workflows import Workflow

__all__ = ["LayoutScreen"]

#: The ``Screen`` base, kept opaque to the type checker exactly as the former
#: ``ExplorerApp`` base was: the large relocated view bodies are not yet fully
#: typed against Textual, and ``DOMNode.query`` (the DOM query) would otherwise
#: collide with view helpers. The search-query state is ``self.search_query``
#: precisely to avoid that collision; fully typing the views is a follow-up.
_SCREEN_BASE: t.Any = Screen


class LayoutScreen(_SCREEN_BASE):
    """A swappable explorer layout that consumes a shared :class:`UiContext`.

    Parameters
    ----------
    ctx : UiContext
        Session-fixed dependencies (home, engine seam, launch query, control)
        the App shell injects. Reachable to subclasses via :attr:`context`.
    workflow : Workflow
        The active interaction strategy (search vs. filter). The layout
        implements ``WorkflowHost`` and the workflow drives it; it is attached
        on mount and re-attached when swapped via :meth:`set_workflow`.
    """

    def __init__(self, ctx: UiContext, workflow: Workflow) -> None:
        super().__init__()
        self._ctx = ctx
        self._workflow = workflow

    @property
    def context(self) -> UiContext:
        """The session-fixed dependencies injected by the App shell."""
        return self._ctx

    @property
    def workflow(self) -> Workflow:
        """The currently active workflow strategy."""
        return self._workflow

    def on_mount(self) -> None:
        """Attach the active workflow once the layout is mounted.

        Subclasses cache their widgets in their own ``on_mount`` and call
        ``super().on_mount()`` last, so the workflow's initial dispatch (which
        may start a search and paint chrome) runs after the widgets exist.
        """
        self._workflow.on_attach(t.cast("t.Any", self))

    def set_workflow(self, workflow: Workflow) -> None:
        """Swap the active workflow and re-seed its initial dispatch."""
        self._workflow = workflow
        self._workflow.on_attach(t.cast("t.Any", self))
