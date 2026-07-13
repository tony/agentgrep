"""Focused tests for the TUI-private slow-source diagnostic state."""

from __future__ import annotations

from agentgrep.ui._source_diagnostics import (
    SlowSourceDiagnostics,
    SourceScanFinished,
    SourceScanStarted,
)


def test_slow_source_survives_a_concurrent_fast_tail() -> None:
    """An early slow source remains useful while later sources flicker past."""
    tracker = SlowSourceDiagnostics(threshold_seconds=0.5)
    tracker.begin()
    tracker.source_started(
        SourceScanStarted(
            source_id=3,
            store="cursor-ide.state_vscdb",
        ),
        now=0.0,
    )

    for source_id in range(4, 83):
        tracker.source_started(
            SourceScanStarted(
                source_id=source_id,
                store=f"fast.store.{source_id}",
            ),
            now=0.01,
        )
        tracker.source_finished(
            SourceScanFinished(
                source_id=source_id,
                finished_at=0.05,
            ),
        )

    assert tracker.sample(0.499) == ""
    live = tracker.sample(0.501)
    assert live == "Slow source\ncursor-ide.state_vscdb · 500ms+"
    assert tracker.sample(68.0) == live
    assert "fast.store" not in live

    tracker.source_finished(
        SourceScanFinished(source_id=3, finished_at=69.3),
    )
    terminal = tracker.finish("Search complete: 40 matches in 69.4s", now=69.4)
    assert terminal == (
        "Search complete: 40 matches in 69.4s\nSlow source: cursor-ide.state_vscdb · 69.3s"
    )

    tracker.source_started(
        SourceScanStarted(
            source_id=84,
            store="late.store",
        ),
        now=70.0,
    )
    assert tracker.sample(120.0) == terminal


def test_fast_sources_never_allocate_detail_chrome() -> None:
    """Sub-threshold work leaves the optional detail row completely empty."""
    tracker = SlowSourceDiagnostics(threshold_seconds=0.5)
    tracker.begin()
    tracker.source_started(
        SourceScanStarted(
            source_id=1,
            store="codex.history",
        ),
        now=10.0,
    )
    tracker.source_finished(
        SourceScanFinished(source_id=1, finished_at=10.499),
    )

    assert tracker.sample(11.0) == ""
    assert tracker.finish("Search complete: 1 match in 1.0s", now=11.0) == (
        "Search complete: 1 match in 1.0s"
    )


def test_live_sample_only_blames_active_sources() -> None:
    """Completed evidence is reserved for the terminal summary."""
    tracker = SlowSourceDiagnostics(threshold_seconds=0.5)
    tracker.begin()
    tracker.source_started(
        SourceScanStarted(source_id=1, store="completed.slow.store"),
        now=0.0,
    )
    tracker.source_finished(SourceScanFinished(source_id=1, finished_at=1.0))
    tracker.source_started(
        SourceScanStarted(source_id=2, store="active.fast.store"),
        now=1.0,
    )

    assert tracker.sample(1.1) == ""


def test_pump_delivery_delay_is_not_counted_as_source_latency() -> None:
    """Timing begins when the start marker reaches the pump, not before it."""
    tracker = SlowSourceDiagnostics(threshold_seconds=0.5)
    tracker.begin()
    tracker.source_started(
        SourceScanStarted(source_id=1, store="cursor-ide.state_vscdb"),
        now=0.55,
    )
    tracker.source_finished(SourceScanFinished(source_id=1, finished_at=0.56))

    assert tracker.finish("Search complete: 0 matches in 0.6s", now=0.57) == (
        "Search complete: 0 matches in 0.6s"
    )


def test_terminal_duration_uses_exact_completed_evidence() -> None:
    """A late finish event corrects a live estimate and selects the true slowest."""
    tracker = SlowSourceDiagnostics(threshold_seconds=0.5)
    tracker.begin()
    tracker.source_started(SourceScanStarted(source_id=1, store="store.one"), now=0.0)
    tracker.source_started(SourceScanStarted(source_id=2, store="store.two"), now=0.0)

    assert tracker.sample(10.0) == "Slow source\nstore.one · 500ms+"
    tracker.source_finished(SourceScanFinished(source_id=1, finished_at=9.8))
    tracker.source_finished(SourceScanFinished(source_id=2, finished_at=9.9))

    assert tracker.finish("Search complete: 0 matches in 10.1s", now=10.1) == (
        "Search complete: 0 matches in 10.1s\nSlow source: store.two · 9.9s"
    )
