"""Narrow ``Protocol`` seam between the TUI and the search engine (ADR 0012 RW-1).

The app shell calls :class:`SearchInvoker` instead of importing
``agentgrep._engine``, ``agentgrep.query``, or ``agentgrep.stores`` directly, so
the UI layer stays engine-agnostic and testable with a fake. The concrete
adapter lives here and is the only place in the UI that imports the engine.
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


class EngineSearchInvoker:
    """Concrete :class:`SearchInvoker` wrapping the headless search engine.

    ``run_search_query`` has no ``emit`` parameter — streaming flows through a
    :class:`~agentgrep.progress.StreamingSearchProgress` passed as ``progress``.
    This adapter wraps ``emit`` in that reporter and owns the source-scan-cache
    ``runtime``, created once and reused across searches so the explorer keeps a
    single warm cache for the session.
    """

    def __init__(self, home: pathlib.Path) -> None:
        from agentgrep._engine.runtime import SearchRuntime

        self._home = home
        self._runtime = SearchRuntime.with_source_scan_cache()

    def run(
        self,
        query: SearchQuery,
        *,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Run ``query`` against the engine, forwarding events to ``emit``."""
        from agentgrep._engine.orchestration import run_search_query
        from agentgrep.progress import StreamingSearchProgress

        run_search_query(
            self._home,
            query,
            progress=StreamingSearchProgress(emit=emit),
            control=control,
            runtime=self._runtime,
        )
