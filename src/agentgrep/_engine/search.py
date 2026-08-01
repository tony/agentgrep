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

Cache-served searches keep the same envelope with zero sources: when
the DB cache answers the query, the stream is ``SearchStarted`` with
``source_count=0``, one ``RecordEmitted`` per cached record, then
``SearchFinished`` — no ``SourceStarted``/``SourceFinished`` pairs
fire because no source is scanned. ``source_count=0`` therefore means
either "nothing discovered" or "served from cache"; the
``search.cache.decision`` profile span disambiguates the two.

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

from agentgrep._engine.orchestration import (
    _db_search_result,
    discover_sources_for_search,
)
from agentgrep.progress import SearchControl, SearchProgress, noop_search_progress
from agentgrep.readers import select_backends
from agentgrep.records import BackendSelection, SearchQuery

if t.TYPE_CHECKING:
    from agentgrep import events as _events
    from agentgrep._engine.runtime import SearchRuntime
    from agentgrep.results import RunSummary, SearchResult


@dataclasses.dataclass(frozen=True, slots=True)
class _AsyncSearchError:
    """Worker-thread error sent through the async event queue."""

    error: BaseException


@dataclasses.dataclass(frozen=True, slots=True)
class _AsyncSearchDone:
    """Worker-thread completion sentinel sent through the async event queue."""


def _raise_execution_protocol_error(message: str) -> t.Never:
    """Raise one internal execution-stream contract violation."""
    raise RuntimeError(message)


def _finish_progress_with_summary(
    progress: SearchProgress,
    summary: RunSummary,
) -> None:
    """Finish a progress sink with the engine-owned terminal summary when supported."""
    summary_finished = getattr(progress, "summary_finished", None)
    if callable(summary_finished):
        summary_finished(summary)
    elif summary.status.state == "cancelled" or summary.status.reason == "answer_now":
        progress.answer_now(summary.match_count)
    else:
        progress.finish(summary.match_count)


def iter_search_events(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    backends: BackendSelection | None = None,
    control: SearchControl | None = None,
    runtime: SearchRuntime | None = None,
    progress: SearchProgress | None = None,
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
    progress : agentgrep.SearchProgress or None
        Optional interactive progress sink. Sinks exposing
        ``summary_finished(summary)`` receive the same terminal evidence
        carried by :class:`agentgrep.events.SearchFinished`.

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
        ExecutionRunFinished,
        ExecutionSourceFinished,
        ExecutionSourceStarted,
        select_execution_driver,
    )
    from agentgrep._engine.planning import (
        build_logical_search_plan,
        build_physical_search_plan,
    )
    from agentgrep._engine.source_filters import source_may_match_query

    active_control = SearchControl() if control is None else control
    active_progress = noop_search_progress() if progress is None else progress
    start_time = time.monotonic()
    validated_request = build_logical_search_plan(query).request
    active_progress.start(query)

    cache_handled, cache_records = _db_search_result(query, runtime)
    if cache_handled:
        from agentgrep.results import RunCoverage, build_search_summary

        yield _events.SearchStarted(source_count=0)
        match_count = 0
        for record in cache_records:
            if active_control.answer_now_requested():
                break
            match_count += 1
            yield _events.RecordEmitted(record=record)
        elapsed_seconds = time.monotonic() - start_time
        summary = build_search_summary(
            query,
            effort=validated_request.effort,
            coverage=RunCoverage(
                sources_discovered=0,
                sources_eligible=0,
                sources_planned=0,
                sources_attempted=0,
                sources_completed=0,
                sources_bounded=0,
                sources_skipped=0,
                sources_unsupported=0,
                sources_failed=0,
                sources_cancelled=0,
                records_seen=len(cache_records),
                matches_seen=len(cache_records),
            ),
            match_count=match_count,
            elapsed_seconds=elapsed_seconds,
            answer_now=active_control.answer_now_requested(),
        )
        _finish_progress_with_summary(active_progress, summary)
        active_progress.close()
        yield _events.SearchFinished(
            match_count=match_count,
            elapsed_seconds=elapsed_seconds,
            summary=summary,
        )
        return

    discovered_sources = []
    sources = []
    routing_plan = None
    try:
        active_backends = select_backends() if backends is None else backends
        discovered_sources = discover_sources_for_search(
            home,
            query,
            active_backends,
            version_detail="none",
        )
        if validated_request.effort == "targeted":
            from agentgrep._engine.routing import build_targeted_routing_plan

            if validated_request.conversation_limit is None:
                _raise_execution_protocol_error(
                    "targeted request has no conversation limit",
                )
            routing_plan = build_targeted_routing_plan(
                query,
                discovered_sources,
                conversation_limit=validated_request.conversation_limit,
                control=active_control,
            )
            discovered_sources.extend(routing_plan.sources)
        active_progress.sources_discovered(len(discovered_sources))
        sources = [s for s in discovered_sources if source_may_match_query(query, s)]
        plan = build_physical_search_plan(
            query,
            sources,
            active_backends,
            progress=active_progress,
            control=active_control,
        )
        active_progress.sources_planned(len(plan.tasks), len(sources))
    except Exception:
        from agentgrep.results import RunCoverage, build_search_summary

        elapsed_seconds = time.monotonic() - start_time
        summary = build_search_summary(
            query,
            effort=validated_request.effort,
            coverage=RunCoverage(
                sources_discovered=len(discovered_sources),
                sources_eligible=len(sources),
                sources_planned=0,
                sources_attempted=0,
                sources_completed=0,
                sources_bounded=0,
                sources_skipped=0,
                sources_unsupported=0,
                sources_failed=0,
                sources_cancelled=0,
                records_seen=0,
                matches_seen=0,
                conversations_eligible=(
                    0 if routing_plan is None else routing_plan.candidates_eligible
                ),
                conversations_selected=(
                    0 if routing_plan is None else routing_plan.candidates_selected
                ),
                conversations_completed=0,
            ),
            match_count=0,
            elapsed_seconds=elapsed_seconds,
            failed=True,
            diagnostics=(() if routing_plan is None else routing_plan.diagnostics),
        )
        _finish_progress_with_summary(active_progress, summary)
        active_progress.close()
        yield _events.SearchStarted(source_count=0)
        yield _events.SearchFinished(
            match_count=0,
            elapsed_seconds=elapsed_seconds,
            summary=summary,
        )
        return

    yield _events.SearchStarted(source_count=len(plan.tasks))

    match_count = 0
    attempted_sources = 0
    completed_sources = 0
    bounded_sources = 0
    unsupported_sources = 0
    failed_sources = 0
    cancelled_sources = 0
    records_seen = 0
    matches_seen = 0
    conversations_completed = 0
    result_limit_reached = False
    execution_finished = False
    source_stop_reasons: list[str] = []

    def finish_execution(
        has_more: bool | None,
        stop_reason: str | None,
    ) -> None:
        """Record the driver's unique terminal evidence."""
        nonlocal execution_finished, result_limit_reached
        if execution_finished:
            msg = "execution driver emitted multiple terminal events"
            raise RuntimeError(msg)
        execution_finished = True
        result_limit_reached = has_more is True or stop_reason == "result_limit"

    active_sources: dict[int, ExecutionSourceStarted] = {}
    execution_failed = False

    def fail_active_sources() -> cabc.Iterator[_events.SourceFinished]:
        """Close every unpaired source after an execution protocol failure."""
        nonlocal failed_sources
        failed_any = False
        for index, execution_event in tuple(active_sources.items()):
            active_sources.pop(index)
            failed_sources += 1
            failed_any = True
            yield _events.SourceFinished(
                adapter_id=execution_event.source.adapter_id,
                records_seen=0,
                matches_seen=0,
                outcome="failed",
                stop_reason="source_failure",
            )
        if failed_any and "source_failure" not in source_stop_reasons:
            source_stop_reasons.append("source_failure")

    try:
        for execution_event in select_execution_driver(query, plan).iter_search_plan(
            query,
            plan,
            progress=active_progress,
            control=active_control,
            runtime=runtime,
        ):
            if execution_finished:
                msg = "execution driver emitted data after its terminal event"
                _raise_execution_protocol_error(msg)
            if isinstance(execution_event, ExecutionSourceStarted):
                if execution_event.index in active_sources:
                    msg = "execution driver emitted a duplicate source start"
                    _raise_execution_protocol_error(msg)
                attempted_sources += 1
                active_sources[execution_event.index] = execution_event
                yield _events.SourceStarted(
                    adapter_id=execution_event.source.adapter_id,
                    index=execution_event.index,
                    total=execution_event.total,
                )
            elif isinstance(execution_event, ExecutionRecordEmitted):
                match_count = execution_event.result_count
                yield _events.RecordEmitted(record=execution_event.record)
            elif isinstance(execution_event, ExecutionSourceFinished):
                started_source = active_sources.pop(execution_event.index, None)
                if started_source is None:
                    msg = "execution driver emitted an unpaired source finish"
                    _raise_execution_protocol_error(msg)
                if (
                    execution_event.stop_reason is not None
                    and execution_event.stop_reason not in source_stop_reasons
                ):
                    source_stop_reasons.append(execution_event.stop_reason)
                if execution_event.outcome == "completed":
                    completed_sources += 1
                    if (
                        routing_plan is not None
                        and started_source.source.path in routing_plan.source_paths
                    ):
                        conversations_completed += 1
                elif execution_event.outcome == "bounded":
                    bounded_sources += 1
                elif execution_event.outcome == "unsupported":
                    unsupported_sources += 1
                elif execution_event.outcome == "failed":
                    failed_sources += 1
                else:
                    cancelled_sources += 1
                records_seen += execution_event.records_seen
                matches_seen += execution_event.matches_seen
                yield _events.SourceFinished(
                    adapter_id=execution_event.source.adapter_id,
                    records_seen=execution_event.records_seen,
                    matches_seen=execution_event.matches_seen,
                    outcome=execution_event.outcome,
                    stop_reason=execution_event.stop_reason,
                )
            elif isinstance(execution_event, ExecutionRunFinished):
                finish_execution(
                    execution_event.has_more,
                    execution_event.stop_reason,
                )
    except Exception:
        execution_failed = True
        yield from fail_active_sources()
    if active_sources:
        execution_failed = True
        yield from fail_active_sources()
    if not execution_failed and not execution_finished:
        execution_failed = True

    elapsed_seconds = time.monotonic() - start_time
    from agentgrep.results import RunCoverage, build_search_summary

    stop_reason = active_control.stop_reason()
    summary = build_search_summary(
        query,
        effort=plan.logical.request.effort,
        coverage=RunCoverage(
            sources_discovered=len(discovered_sources),
            sources_eligible=len(sources),
            sources_planned=len(plan.tasks),
            sources_attempted=attempted_sources,
            sources_completed=completed_sources,
            sources_bounded=bounded_sources,
            sources_skipped=max(0, len(plan.tasks) - attempted_sources),
            sources_unsupported=unsupported_sources,
            sources_failed=failed_sources,
            sources_cancelled=cancelled_sources,
            records_seen=records_seen,
            matches_seen=matches_seen,
            conversations_eligible=(
                0 if routing_plan is None else routing_plan.candidates_eligible
            ),
            conversations_selected=(
                0 if routing_plan is None else routing_plan.candidates_selected
            ),
            conversations_completed=conversations_completed,
            source_stop_reasons=tuple(source_stop_reasons),
        ),
        match_count=match_count,
        elapsed_seconds=elapsed_seconds,
        answer_now=stop_reason == "answer_now",
        cancelled=stop_reason in {"caller_cancelled", "deadline", "replacement"},
        failed=(
            execution_failed
            or (routing_plan is not None and routing_plan.evidence_sources_failed > 0)
        ),
        result_limit_reached=result_limit_reached,
        diagnostics=(() if routing_plan is None else routing_plan.diagnostics),
    )
    _finish_progress_with_summary(active_progress, summary)
    active_progress.close()
    yield _events.SearchFinished(
        match_count=match_count,
        elapsed_seconds=elapsed_seconds,
        summary=summary,
    )


def run_search_result(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    backends: BackendSelection | None = None,
    control: SearchControl | None = None,
    runtime: SearchRuntime | None = None,
    progress: SearchProgress | None = None,
) -> SearchResult:
    """Collect one validated event stream into records plus terminal evidence."""
    from agentgrep import events as _events
    from agentgrep.results import SearchResult

    records = []
    summary = None
    for event in iter_search_events(
        home,
        query,
        backends=backends,
        control=control,
        runtime=runtime,
        progress=progress,
    ):
        if summary is not None:
            msg = "search event stream emitted data after SearchFinished"
            raise RuntimeError(msg)
        if isinstance(event, _events.RecordEmitted):
            records.append(event.record)
        elif isinstance(event, _events.SearchFinished):
            summary = event.summary
    if summary is None:
        msg = "search event stream ended without SearchFinished"
        raise RuntimeError(msg)
    if len(records) != summary.match_count:
        msg = "search event record count does not match terminal summary"
        raise RuntimeError(msg)
    return SearchResult(records=tuple(records), summary=summary)


async def aiter_search_events(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    backends: BackendSelection | None = None,
    control: SearchControl | None = None,
    runtime: SearchRuntime | None = None,
    max_queue_size: int = 32,
) -> cabc.AsyncGenerator[_events.SearchEvent]:
    """Yield search events from a worker thread through an async queue.

    Closing the returned generator — via :func:`contextlib.aclosing`, or by
    any exception that unwinds through it — requests cancellation and stops
    the worker. Consumers that may leave the stream partially consumed must
    close it explicitly rather than relying on the event loop's async-generator
    finalization hook.

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
    ) -> None:
        # ``active_control`` stops producer work; only ``delivery_closed``
        # stops transport. The producer still owns partial-result resolution
        # and the terminal event after an answer-now request.
        while not delivery_closed.is_set():
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
        except BaseException as error:
            put_from_worker(_AsyncSearchError(error=error))
        finally:
            put_from_worker(_AsyncSearchDone())

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
            active_control.request_answer_now(reason="caller_cancelled")
            with contextlib.suppress(Exception):
                await worker_task
