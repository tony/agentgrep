"""Narrow ``Protocol`` seams between the TUI and the search engine (ADR 0012 RW-1).

The app shell calls these instead of importing ``agentgrep._engine``,
``agentgrep.query``, or ``agentgrep.stores`` directly, so the UI layer stays
engine-agnostic and testable with fakes. The concrete adapters live here and are
the only place in the UI that imports the engine.
"""

from __future__ import annotations

import collections.abc as cabc
import typing as t

if t.TYPE_CHECKING:
    import pathlib

    from agentgrep.progress import SearchControl
    from agentgrep.records import SearchQuery


class SearchInvoker(t.Protocol):
    """Run a search off the pump and forward its events to ``emit`` (NB-2/NB-3)."""

    def run(
        self,
        query: SearchQuery,
        *,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Run ``query`` and forward each streaming event to ``emit``."""
        ...


class PreviewProvider(t.Protocol):
    """Resolve a selectable item to a preview body string."""

    def fetch(self, item: object) -> str:
        """Return the preview body for ``item``."""
        ...


class EngineSearchInvoker:
    """Concrete :class:`SearchInvoker` wrapping the headless search engine.

    ``run_search_query`` has no ``emit`` parameter — streaming flows through a
    :class:`~agentgrep.progress.StreamingSearchProgress` passed as ``progress``.
    This adapter reproduces the call the explorer makes today (``progress`` plus
    ``control`` plus the source-scan-cache ``runtime``), so routing the app
    through it is behavior-preserving.
    """

    def __init__(self, home: pathlib.Path) -> None:
        self._home = home

    def run(
        self,
        query: SearchQuery,
        *,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Run ``query`` against the engine, forwarding events to ``emit``."""
        from agentgrep._engine.orchestration import run_search_query
        from agentgrep._engine.runtime import SearchRuntime
        from agentgrep.progress import StreamingSearchProgress

        run_search_query(
            self._home,
            query,
            progress=StreamingSearchProgress(emit=emit),
            control=control,
            runtime=SearchRuntime.with_source_scan_cache(),
        )
