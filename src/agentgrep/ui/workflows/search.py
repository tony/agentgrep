"""``SearchWorkflow`` — live incremental search over the engine (ADR 0013).

The default workflow: the primary input is a search box. Submitting it builds a
query and streams matches from the engine seam; an empty submission returns the
layout to its idle state. This is the behavior the explorer has always had, now
expressed as a swappable strategy so a layout can host a different one (e.g.
:class:`~agentgrep.ui.workflows.browse.BrowseWorkflow`).
"""

from __future__ import annotations

import collections.abc as cabc
import typing as t

if t.TYPE_CHECKING:
    from agentgrep.ui.workflows._protocol import WorkflowHost

__all__ = ["SearchWorkflow"]


class SearchWorkflow:
    """Route the primary input to an engine search (the default workflow)."""

    name: t.ClassVar[str] = "search"
    summary: t.ClassVar[str] = "Live incremental search over the engine"
    BINDINGS: t.ClassVar[cabc.Sequence[object]] = ()

    def on_attach(self, host: WorkflowHost) -> None:
        """Run the launch query if it has terms, else show the idle canvas."""
        query = host.context.query
        if query.terms:
            host.run_search(query)
        else:
            host.reset_view()

    def on_query(self, host: WorkflowHost, text: str) -> None:
        """Submit: cancel any in-flight search, then search ``text`` (empty resets)."""
        host.request_cancel()
        if not text:
            host.reset_view()
            return
        host.record_history(text)
        host.run_search(host.build_query(text))
