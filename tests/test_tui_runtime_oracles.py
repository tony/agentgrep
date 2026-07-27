"""Deterministic adversarial contracts for TUI runtime placement.

Covers the watchdog's stall oracle and the request a search worker is bound
to — both properties that only hold off the message pump, and neither
provable by racing.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import logging
import pathlib
import typing as t

import pytest

from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery
from agentgrep.ui import _runtime, registry
from agentgrep.ui._context import UiContext

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


# The explicit audit mode covers CPython-instrumented blocking-I/O initiation.
# This deterministic contract pins the heartbeat's complementary stall oracle.
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


class _RecordingInvoker:
    """Search-invoker double recording the whole request each run was handed.

    Attributes
    ----------
    requests : list[tuple[SearchQuery, SearchControl, cabc.Callable[[object], None]]]
        Query, cancel flag, and event sink received, in call order.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[SearchQuery, SearchControl, cabc.Callable[[object], None]]] = []

    def run(
        self,
        query: SearchQuery,
        *,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Record the request this run owns without doing any work."""
        self.requests.append((query, control, emit))


@pytest.mark.parametrize("layout_name", registry.layout_names())
def test_search_worker_keeps_the_control_it_started_with(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    layout_name: str,
) -> None:
    """Bind a worker's request on the pump, not when its thread first runs.

    Starting a search installs a fresh :class:`SearchControl`. A worker that
    resolved the control off the layout would read whichever one was current
    when the thread happened to start, so a replacement arriving in that window
    would take the outgoing run's control with it — and the signal aimed at the
    outgoing run would reach nobody, leaving it scanning behind its replacement.
    """
    invoker = _RecordingInvoker()
    ctx = UiContext(
        home=tmp_path,
        invoker=t.cast("t.Any", invoker),
        query=SearchQuery(
            terms=(),
            scope="prompts",
            any_term=False,
            regex=False,
            case_sensitive=False,
            agents=(),
            limit=None,
        ),
        control=SearchControl(),
        base_scope="prompts",
        base_effort="prompt",
    )
    layout_spec = registry.layout_spec(layout_name)
    workflow_spec = registry.workflow_spec("search")
    assert layout_spec is not None
    assert workflow_spec is not None
    layout = t.cast("t.Any", layout_spec.loader()(ctx, workflow_spec.loader()()))
    spawned: list[cabc.Callable[[], None]] = []
    monkeypatch.setattr(
        layout,
        "run_worker",
        lambda target, **_kwargs: spawned.append(target),
    )
    # The gated emitter hands events back through the running app; the request
    # binding under test does not need a real one, only a stable identity.
    emitters: list[cabc.Callable[[object], None]] = []

    def _make_gated_emit() -> cabc.Callable[[object], None]:
        emitters.append(lambda _event: None)
        return emitters[-1]

    monkeypatch.setattr(layout, "_make_gated_emit", _make_gated_emit)

    started_query = dataclasses.replace(ctx.query, terms=("needle",))
    layout.run_search(started_query)
    started_control = layout.control
    started_emit = emitters[-1]

    # The replacement lands before the worker thread reaches its first
    # statement — the window these bindings exist to close. Every piece of the
    # request moves on; the pending worker must keep the one it started with.
    layout.control = SearchControl()
    layout.search_query = dataclasses.replace(ctx.query, terms=("replacement",))
    layout._search_emit = _make_gated_emit()
    spawned[0]()

    assert len(invoker.requests) == 1
    query, control, emit = invoker.requests[0]
    assert query is started_query
    assert control is started_control
    assert emit is started_emit
    # A stale emitter is the worst of the three: it carries the replacement's
    # generation, so the outgoing run's events would pass the stale-event gate
    # and land in the replacement's results.
    assert emit is not layout._search_emit
