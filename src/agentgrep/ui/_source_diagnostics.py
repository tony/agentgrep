"""TUI-private source lifecycle and slow-source projection.

The shared engine progress stream stays frontend-neutral. This module adds the
small amount of concurrent source identity the optional Textual detail row
needs, then projects it into a stable, thresholded diagnostic.
"""

from __future__ import annotations

import dataclasses

from agentgrep.progress import ProgressSnapshot

__all__ = [
    "SlowSourceDiagnostics",
    "SourceScanFinished",
    "SourceScanLifecycle",
    "SourceScanStarted",
    "UiProgressSnapshot",
]


@dataclasses.dataclass(frozen=True, slots=True)
class SourceScanStarted:
    """One UI-private source-start marker coalesced with a progress snapshot."""

    source_id: int
    store: str


@dataclasses.dataclass(frozen=True, slots=True)
class SourceScanFinished:
    """One UI-private source-finish marker coalesced with a progress snapshot."""

    source_id: int
    finished_at: float


type SourceScanLifecycle = SourceScanStarted | SourceScanFinished


@dataclasses.dataclass(frozen=True, slots=True)
class UiProgressSnapshot:
    """A canonical snapshot plus its TUI-only source lifecycle marker."""

    snapshot: ProgressSnapshot
    lifecycle: SourceScanLifecycle


@dataclasses.dataclass(frozen=True, slots=True)
class _SlowSource:
    """The longest threshold-crossing source observed in this search."""

    store: str
    duration: float


@dataclasses.dataclass(frozen=True, slots=True)
class _ActiveSource:
    """One source timed from pump receipt until the worker-side finish."""

    store: str
    started_at: float


class SlowSourceDiagnostics:
    """Track concurrent source lifetimes and expose only actionable detail."""

    def __init__(self, *, threshold_seconds: float = 0.5) -> None:
        self._threshold_seconds = max(0.0, threshold_seconds)
        self._active: dict[int, _ActiveSource] = {}
        self._slowest_completed: _SlowSource | None = None
        self._terminal = ""
        self._running = False

    @property
    def running(self) -> bool:
        """Whether the tracker is accepting lifecycle events for a search."""
        return self._running

    def begin(self) -> None:
        """Reset state for a fresh search."""
        self._active.clear()
        self._slowest_completed = None
        self._terminal = ""
        self._running = True

    def source_started(self, event: SourceScanStarted, *, now: float) -> None:
        """Record one active source while the search is current."""
        if self._running:
            self._active[event.source_id] = _ActiveSource(
                store=event.store,
                started_at=now,
            )

    def source_finished(self, event: SourceScanFinished) -> None:
        """Close one active source without changing the sampled text."""
        if not self._running:
            return
        started = self._active.pop(event.source_id, None)
        if started is None:
            return
        self._slowest_completed = self._longer(
            self._slowest_completed,
            started.store,
            event.finished_at - started.started_at,
        )

    def sample(self, now: float) -> str:
        """Return a stable live diagnostic after a source crosses the threshold."""
        if not self._running:
            return self._terminal
        slowest = self._slowest_completed
        for started in self._active.values():
            slowest = self._longer(slowest, started.store, now - started.started_at)
        if slowest is None:
            return ""
        return f"Slow source\n{slowest.store} · {self._threshold_label()}"

    def finish(self, summary: str, *, now: float) -> str:
        """Freeze ``summary`` and append the exact slowest-source duration."""
        if not self._running:
            return self._terminal or summary
        slowest = self._slowest_completed
        for started in self._active.values():
            slowest = self._longer(slowest, started.store, now - started.started_at)
        self._active.clear()
        self._running = False
        if slowest is None:
            self._terminal = summary
        else:
            duration = max(0.0, slowest.duration)
            self._terminal = f"{summary}\nSlow source: {slowest.store} · {duration:.1f}s"
        return self._terminal

    def go_idle(self) -> None:
        """Discard all state when no search owns the row."""
        self._active.clear()
        self._slowest_completed = None
        self._terminal = ""
        self._running = False

    def _longer(
        self,
        current: _SlowSource | None,
        store: str,
        duration: float,
    ) -> _SlowSource | None:
        """Return the longer threshold-crossing observation."""
        duration = max(0.0, duration)
        if duration < self._threshold_seconds:
            return current
        if current is None or duration > current.duration:
            return _SlowSource(store=store, duration=duration)
        return current

    def _threshold_label(self) -> str:
        """Format the fixed live threshold without a ticking duration."""
        if self._threshold_seconds < 1.0:
            return f"{self._threshold_seconds * 1000:.0f}ms+"
        return f"{self._threshold_seconds:g}s+"
