"""Search event-stream producer.

The :func:`iter_search_events` generator is the primary entry point
into agentgrep's search engine: it scans the user's prompt and conversation
stores and yields :class:`agentgrep.events.SearchEvent` values as it
goes. Consumers (the CLI text path, the TUI worker, the MCP tool
wrapper) filter the event stream for the variants they need.

The generator owns these invariants:

- Exactly one :class:`agentgrep.events.SearchStarted` is yielded at
  the start. Even when the candidate-source list is empty, the
  ``Started`` / ``Finished`` pair fires.
- Per submitted source: one :class:`agentgrep.events.SourceStarted`
  and one :class:`agentgrep.events.SourceFinished`. The execution
  driver may merge records after source completion so concurrent
  scans can preserve deterministic newest-first output.
- :class:`agentgrep.events.RecordEmitted` fires only after the
  per-session dedup decision has decided "unique-and-included".
  Bounded (frontier) drivers buffer and restore final result ordering
  before emitting records; the inline driver emits per source as
  records arrive. Consumers that need global newest-first order sort
  the collected records by ``search_record_sort_key``, as the
  list-return wrappers do.
- Exactly one :class:`agentgrep.events.SearchFinished` is yielded
  last with the total match count and elapsed time. A stream that
  exits early via :attr:`agentgrep.SearchControl.request_answer_now`
  still fires ``SearchFinished`` so cleanup is uniform.

Cancellation honors the existing :class:`agentgrep.SearchControl`
primitive — call :meth:`agentgrep.SearchControl.request_answer_now`
to break out at the next per-record boundary. Async consumers wrap
the iterator in :func:`asyncio.to_thread` and signal cancellation by
flipping the control flag.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import concurrent.futures
import contextlib
import dataclasses
import pathlib
import threading
import time
import typing as t

from agentgrep._engine.orchestration import discover_sources_for_search
from agentgrep.progress import SearchControl
from agentgrep.readers import select_backends
from agentgrep.records import BackendSelection, SearchQuery

if t.TYPE_CHECKING:
    from agentgrep import events as _events
    from agentgrep._engine.runtime import SearchRuntime


@dataclasses.dataclass(frozen=True, slots=True)
class _AsyncSearchError:
    """Worker-thread error sent through the async event queue."""

    error: BaseException


@dataclasses.dataclass(frozen=True, slots=True)
class _AsyncSearchDone:
    """Worker-thread completion sentinel sent through the async event queue."""


def iter_search_events(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    backends: BackendSelection | None = None,
    control: SearchControl | None = None,
    runtime: SearchRuntime | None = None,
) -> cabc.Iterator[_events.SearchEvent]:
    """Yield typed events as the search engine scans sources.

    Parameters
    ----------
    home : pathlib.Path
        User home directory passed through to
        :func:`agentgrep.discover_sources`.
    query : SearchQuery
        Compiled query — terms, agents, dedup choice, limit.
    backends : BackendSelection or None
        Override the auto-detected backend selection (mainly used by
        tests). ``None`` selects backends via
        :func:`agentgrep.select_backends`.
    control : SearchControl or None
        Optional control handle. The generator polls
        :meth:`agentgrep.SearchControl.answer_now_requested` between
        records so consumers can break the scan early.
    runtime : agentgrep.SearchRuntime or None
        Optional reusable runtime state; supplies the source-scan
        cache when one is configured.

    Yields
    ------
    _events.SearchEvent
        Discriminated-union events. See module docstring for the
        guaranteed sequence.

    Examples
    --------
    Stream events, collecting matching records::

        for event in iter_search_events(pathlib.Path.home(), query):
            if isinstance(event, _events.RecordEmitted):
                print(event.record.text)
    """
    from agentgrep import events as _events
    from agentgrep._engine.execution import (
        ExecutionRecordEmitted,
        ExecutionSourceFinished,
        ExecutionSourceStarted,
        select_execution_driver,
    )
    from agentgrep._engine.planning import build_physical_search_plan
    from agentgrep._engine.source_filters import source_may_match_query

    active_backends = select_backends() if backends is None else backends
    active_control = SearchControl() if control is None else control
    start_time = time.monotonic()

    sources = discover_sources_for_search(
        home,
        query,
        active_backends,
        version_detail="none",
    )
    sources = [s for s in sources if source_may_match_query(query, s)]
    plan = build_physical_search_plan(
        query,
        sources,
        active_backends,
        control=active_control,
    )

    yield _events.SearchStarted(source_count=len(plan.tasks))

    match_count = 0
    for execution_event in select_execution_driver(query, plan).iter_search_plan(
        query,
        plan,
        control=active_control,
        runtime=runtime,
    ):
        if isinstance(execution_event, ExecutionSourceStarted):
            yield _events.SourceStarted(
                adapter_id=execution_event.source.adapter_id,
                index=execution_event.index,
                total=execution_event.total,
            )
        elif isinstance(execution_event, ExecutionRecordEmitted):
            match_count = execution_event.result_count
            yield _events.RecordEmitted(record=execution_event.record)
        elif isinstance(execution_event, ExecutionSourceFinished):
            yield _events.SourceFinished(
                adapter_id=execution_event.source.adapter_id,
                records_seen=execution_event.records_seen,
                matches_seen=execution_event.matches_seen,
            )

    yield _events.SearchFinished(
        match_count=match_count,
        elapsed_seconds=time.monotonic() - start_time,
    )


async def aiter_search_events(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    backends: BackendSelection | None = None,
    control: SearchControl | None = None,
    runtime: SearchRuntime | None = None,
    max_queue_size: int = 32,
) -> cabc.AsyncIterator[_events.SearchEvent]:
    """Yield search events from a worker thread through an async queue.

    Parameters
    ----------
    home : pathlib.Path
        User home directory passed through to :func:`iter_search_events`.
    query : SearchQuery
        Compiled query — terms, agents, dedupe choice, limit.
    backends : BackendSelection or None
        Optional backend override, mostly used by tests.
    control : SearchControl or None
        Optional cooperative cancellation handle.
    runtime : agentgrep.SearchRuntime or None
        Optional reusable runtime state; supplies the source-scan
        cache when one is configured.
    max_queue_size : int
        Bounded async queue size used to apply consumer backpressure.

    Yields
    ------
    _events.SearchEvent
        The same event sequence produced by :func:`iter_search_events`.
    """
    active_control = SearchControl() if control is None else control
    queue_size = max(1, max_queue_size)
    loop = asyncio.get_running_loop()
    delivery_closed = threading.Event()
    event_queue: asyncio.Queue[_events.SearchEvent | _AsyncSearchDone | _AsyncSearchError] = (
        asyncio.Queue(maxsize=queue_size)
    )

    def put_from_worker(
        item: _events.SearchEvent | _AsyncSearchDone | _AsyncSearchError,
        *,
        force: bool = False,
    ) -> None:
        while not delivery_closed.is_set():
            if not force and active_control.answer_now_requested():
                return
            future = asyncio.run_coroutine_threadsafe(event_queue.put(item), loop)
            try:
                future.result(timeout=0.05)
            except concurrent.futures.TimeoutError:
                future.cancel()
                continue
            return

    def run_worker() -> None:
        try:
            for event in iter_search_events(
                home,
                query,
                backends=backends,
                control=active_control,
                runtime=runtime,
            ):
                put_from_worker(event)
                if active_control.answer_now_requested():
                    break
        except BaseException as error:
            put_from_worker(_AsyncSearchError(error=error), force=True)
        finally:
            put_from_worker(_AsyncSearchDone(), force=True)

    worker_task = asyncio.create_task(asyncio.to_thread(run_worker))
    try:
        while True:
            item = await event_queue.get()
            if isinstance(item, _AsyncSearchDone):
                break
            if isinstance(item, _AsyncSearchError):
                raise item.error
            yield item
        await worker_task
    finally:
        if not worker_task.done():
            delivery_closed.set()
            active_control.request_answer_now()
            with contextlib.suppress(Exception):
                await worker_task
