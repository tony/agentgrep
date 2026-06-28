"""The :class:`Workflow` strategy contract and its :class:`WorkflowHost` seam.

A workflow is the *behavior* axis of the TUI: it routes the layout's primary
input to an action — an engine search (:class:`~agentgrep.ui.workflows.search.SearchWorkflow`)
or an in-memory filter — and seeds the initial dispatch. It drives the layout
through ``WorkflowHost`` (the narrow surface a
:class:`~agentgrep.ui.layouts._base.LayoutScreen` exposes) and never imports
Textual or reaches into widgets, so the same workflow runs on any layout and is
testable against a fake host. Both contracts are ``Protocol`` s (structural,
matching the :class:`~agentgrep.ui._seams.SearchInvoker` seam style) rather than
ABCs, so a concrete workflow need not inherit and a test fake need not subclass.
"""

from __future__ import annotations

import collections.abc as cabc
import typing as t

if t.TYPE_CHECKING:
    from agentgrep.records import SearchQuery
    from agentgrep.ui._context import UiContext

__all__ = ["Workflow", "WorkflowHost"]


class WorkflowHost(t.Protocol):
    """The layout surface a workflow drives (implemented by ``LayoutScreen``).

    The host owns the non-blocking mechanics (off-thread dispatch, generation-gated
    apply, chrome); a workflow only chooses *policy* by calling these methods.
    """

    @property
    def context(self) -> UiContext:
        """The session-fixed :class:`UiContext` (home, seam, launch query)."""
        ...

    def build_query(self, text: str) -> SearchQuery:
        """Parse ``text`` into a :class:`SearchQuery` at the layout's base scope."""
        ...

    def run_search(self, query: SearchQuery) -> None:
        """Reset the view and stream ``query`` through the engine seam."""
        ...

    def filter_loaded(self, text: str) -> None:
        """Narrow the already-loaded records in-memory by ``text`` (empty clears)."""
        ...

    def reset_view(self) -> None:
        """Return the layout to its idle / empty state without a search."""
        ...

    def record_history(self, text: str) -> None:
        """Persist ``text`` to the search-input history (best effort)."""
        ...

    def request_cancel(self) -> None:
        """Cooperatively signal the in-flight search to wrap up."""
        ...


class Workflow(t.Protocol):
    """A pluggable interaction/query strategy over a layout's primary input.

    Implementations are plain objects; the host calls :meth:`on_attach` when the
    layout mounts (or the workflow is swapped in) and :meth:`on_query` when the
    primary input is submitted.
    """

    #: Stable registry id (lowercase, no spaces) used by ``--workflow`` and switching.
    name: t.ClassVar[str]
    #: One-line human description for the switcher and ``--help``.
    summary: t.ClassVar[str]
    #: Extra screen-level key bindings this workflow installs while active.
    BINDINGS: t.ClassVar[cabc.Sequence[object]]

    def on_attach(self, host: WorkflowHost) -> None:
        """Seed the initial dispatch when this workflow becomes active."""
        ...

    def on_query(self, host: WorkflowHost, text: str) -> None:
        """Handle the primary input being submitted with ``text``."""
        ...
