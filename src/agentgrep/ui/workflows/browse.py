"""``BrowseWorkflow`` — load a set once, then filter it in-memory (ADR 0013).

The browse counterpart to :class:`~agentgrep.ui.workflows.search.SearchWorkflow`:
it runs the launch query a single time on attach, then treats the primary input
as an in-memory *filter* over the loaded records rather than a fresh engine
search. Same engine, same records, a different way to query — the second axis
(workflow) made concrete on any layout that hosts it.
"""

from __future__ import annotations

import collections.abc as cabc
import typing as t

if t.TYPE_CHECKING:
    from agentgrep.ui.workflows._protocol import WorkflowHost

__all__ = ["BrowseWorkflow"]


class BrowseWorkflow:
    """Route the primary input to an in-memory filter over a loaded set."""

    name: t.ClassVar[str] = "browse"
    summary: t.ClassVar[str] = "Browse a loaded set; the input filters in-memory"
    BINDINGS: t.ClassVar[cabc.Sequence[object]] = ()

    def on_attach(self, host: WorkflowHost) -> None:
        """Load the launch query's records once, to be filtered in place."""
        host.run_search(host.context.query)

    def on_query(self, host: WorkflowHost, text: str) -> None:
        """Submit: filter the loaded records in-memory — no fresh engine search."""
        host.filter_loaded(text)

    def on_action(self, host: WorkflowHost, action_id: str) -> bool:
        """Browse owns no extra key actions."""
        del host, action_id
        return False
