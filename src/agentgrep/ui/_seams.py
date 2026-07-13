"""Narrow ``Protocol`` seam between the TUI and the search engine (ADR 0012 RW-1).

The app shell calls :class:`SearchInvoker` instead of importing
``agentgrep._engine``, ``agentgrep.query``, or ``agentgrep.stores`` directly, so
the UI layer stays engine-agnostic and testable with a fake. The concrete
adapter lives here and is the only place the UI runs a search through the engine.
"""

from __future__ import annotations

import collections.abc as cabc
import threading
import time
import typing as t

from agentgrep.progress import ProgressSnapshot, StreamingSearchProgress
from agentgrep.ui._source_diagnostics import (
    SourceScanFinished,
    SourceScanLifecycle,
    SourceScanStarted,
    UiProgressSnapshot,
)

if t.TYPE_CHECKING:
    import pathlib

    from agentgrep.progress import SearchControl
    from agentgrep.records import SearchQuery, SourceHandle


class _LifecycleContext(threading.local):
    """Per-callback lifecycle state safe from concurrent progress emitters."""

    marker: SourceScanLifecycle | None = None


class _UiStreamingSearchProgress(StreamingSearchProgress):
    """Coalesce TUI-only source lifecycle with existing progress emissions."""

    def __init__(self, emit: cabc.Callable[[object], None]) -> None:
        self._downstream_emit = emit
        self._lifecycle_context = _LifecycleContext()
        super().__init__(emit=self._emit_ui_event)

    def source_started(self, index: int, total: int, source: SourceHandle) -> None:
        """Attach a stable store identity to the canonical start snapshot."""
        self._lifecycle_context.marker = SourceScanStarted(
            source_id=index,
            store=source.store,
        )
        try:
            super().source_started(index, total, source)
        finally:
            self._lifecycle_context.marker = None

    def source_finished(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        records: int,
        matches: int,
    ) -> None:
        """Attach a finish time to the canonical completion snapshot."""
        self._lifecycle_context.marker = SourceScanFinished(
            source_id=index,
            finished_at=time.monotonic(),
        )
        try:
            super().source_finished(index, total, source, records, matches)
        finally:
            self._lifecycle_context.marker = None

    def _emit_ui_event(self, event: object) -> None:
        """Wrap only the lifecycle-bearing snapshot; forward every other event."""
        lifecycle = self._lifecycle_context.marker
        if lifecycle is not None and isinstance(event, ProgressSnapshot):
            self._downstream_emit(UiProgressSnapshot(snapshot=event, lifecycle=lifecycle))
            return
        self._downstream_emit(event)


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

        run_search_query(
            self._home,
            query,
            progress=_UiStreamingSearchProgress(emit=emit),
            control=control,
            runtime=self._runtime,
        )
