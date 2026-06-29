"""``LayoutScreen`` — the base for pluggable explorer layouts (ADR 0013).

A layout is a Textual ``Screen`` injected with the shared
:class:`~agentgrep.ui._context.UiContext`. Subclasses own their ``compose``, CSS,
bindings, and presentation; they reach the engine only through
``context.invoker`` (ADR 0012 RW-1) and run all blocking work off the pump
(ADR 0011). The App shell mounts one subclass as the active layout.
"""

from __future__ import annotations

import typing as t

from textual.binding import Binding
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
        self._workflow_attach_pending = False
        #: Bindings this screen installed for the active workflow, tracked by
        #: ``(key, binding)`` identity so a workflow swap removes exactly its own.
        self._installed_workflow_bindings: list[tuple[str, Binding]] = []

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
        self._attach_workflow()
        self._workflow_attach_pending = False

    def set_workflow(self, workflow: Workflow, *, attach: bool = True) -> None:
        """Swap the active workflow, optionally re-seeding its initial dispatch."""
        if attach:
            t.cast("t.Any", self).request_cancel()
        self._workflow = workflow
        self._workflow_attach_pending = not attach
        if attach:
            self._attach_workflow()
            self._workflow_attach_pending = False

    def attach_pending_workflow(self) -> None:
        """Attach a suspended workflow swap when the layout is resumed."""
        if not self._workflow_attach_pending:
            return
        self._workflow_attach_pending = False
        t.cast("t.Any", self).request_cancel()
        self._attach_workflow()

    def _attach_workflow(self) -> None:
        """Seed the active workflow and install its key bindings (ADR 0013/0014).

        Centralizes the attach so every entry point (mount, swap, resume) both
        runs the workflow's initial dispatch *and* installs its ``BINDINGS`` on
        the screen — the latter was previously declared but never wired, so
        workflow-owned keys (e.g. deductive's widen/clear) could not fire.
        """
        self._workflow.on_attach(t.cast("t.Any", self))
        self._install_workflow_bindings()

    def _install_workflow_bindings(self) -> None:
        """Install the active workflow's ``BINDINGS`` on this screen, replacing prior.

        ``BindingsMap.copy()`` shares each per-key list with the class-level map,
        so a fresh list replaces the bucket before appending — otherwise the
        append would mutate every instance's bindings.
        """
        self._remove_workflow_bindings()
        installed: list[tuple[str, Binding]] = []
        key_map = self._bindings.key_to_bindings
        for binding in Binding.make_bindings(self._workflow.BINDINGS):
            bucket = [*key_map[binding.key]] if binding.key in key_map else []
            bucket.append(binding)
            key_map[binding.key] = bucket
            installed.append((binding.key, binding))
        self._installed_workflow_bindings = installed
        self.refresh_bindings()

    def _remove_workflow_bindings(self) -> None:
        """Drop the bindings a prior workflow installed, matched by identity."""
        if not self._installed_workflow_bindings:
            return
        key_map = self._bindings.key_to_bindings
        for key, binding in self._installed_workflow_bindings:
            bucket = key_map.get(key)
            if not bucket:
                continue
            remaining = [existing for existing in bucket if existing is not binding]
            if remaining:
                key_map[key] = remaining
            else:
                del key_map[key]
        self._installed_workflow_bindings = []

    def action_workflow(self, action_id: str) -> None:
        """Route a workflow-owned key action into the active workflow.

        Bound via parameterized actions in a workflow's ``BINDINGS`` (e.g.
        ``("ctrl+up", 'workflow("widen")', "Widen")``) so the strategy object
        never imports Textual. Bounded (one delegating call) — pump-safe.
        """
        self._workflow.on_action(t.cast("t.Any", self), action_id)

    # --- input control defaults (the shared SearchInput reaches these) --------
    # SearchInput.on_key routes ctrl-c and the non-ctrl-c "disarm" through
    # ``self.screen``, so every layout that hosts it needs these. The HUD
    # overrides them with its staged confirm-exit gutter; other layouts get a
    # sane default (clear the box, then quit on an empty box).
    def _handle_input_ctrl_c(self, widget: object) -> None:
        """Default ctrl-c inside an input: clear it, else quit on an empty box."""
        target = t.cast("t.Any", widget)
        if str(getattr(target, "value", "")):
            target.value = ""
            return
        self.app.exit()

    def _disarm_confirm_exit(self) -> None:
        """No-op by default; the HUD overrides this to cancel its confirm gutter."""
