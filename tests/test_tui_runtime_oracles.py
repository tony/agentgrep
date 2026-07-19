"""Deterministic adversarial contract for the TUI watchdog oracle."""

from __future__ import annotations

import collections.abc as cabc
import logging
import typing as t

import pytest

from agentgrep.ui import _runtime

pytestmark = pytest.mark.tui


class _ImmediateStop:
    """Event double that permits one watchdog sample, then stops."""

    def __init__(self) -> None:
        self.wait_count = 0

    def wait(self, _timeout: float) -> bool:
        """Return false once so the watcher samples a stale heartbeat."""
        self.wait_count += 1
        return self.wait_count > 1

    def set(self) -> None:
        """Mark the fake event stopped."""
        self.wait_count = 2


class _InlineThread:
    """Thread double that executes the watchdog target synchronously."""

    def __init__(
        self,
        *,
        target: cabc.Callable[[], None],
        **_kwargs: object,
    ) -> None:
        self._target = target

    def start(self) -> None:
        """Run the captured target without scheduling or sleeping."""
        self._target()

    def is_alive(self) -> bool:
        """Report that the inline target completed inside ``start``."""
        return False

    def join(self, timeout: float | None = None) -> None:
        """Accept the production cleanup protocol without blocking."""
        del timeout


def test_watchdog_reports_seeded_cpu_stall_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fake clock proves stale-heartbeat detection with no timing sleep."""
    clock = iter((10.0, 11.5))
    stop = _ImmediateStop()
    monkeypatch.setattr(_runtime.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(_runtime.threading, "Event", lambda: stop)
    monkeypatch.setattr(_runtime.threading, "Thread", _InlineThread)

    try:
        with caplog.at_level(logging.WARNING, logger="agentgrep.ui._runtime"):
            _runtime.start_pump_watchdog(
                stall_threshold_ms=1_000,
                poll_seconds=0.25,
            )
    finally:
        _runtime.stop_pump_watchdog(timeout=0)

    stalls = [
        record
        for record in caplog.records
        if getattr(record, "agentgrep_pump_stall_ms", None) is not None
    ]
    assert len(stalls) == 1
    assert stalls[0].message == "pump heartbeat stalled"
    stall = t.cast("t.Any", stalls[0])
    assert stall.agentgrep_pump_stall_ms == 1_500
    assert stall.agentgrep_pump_stall_threshold_ms == 1_000


# The explicit audit mode covers CPython-instrumented blocking-I/O initiation.
# This deterministic contract pins the heartbeat's complementary stall oracle.
