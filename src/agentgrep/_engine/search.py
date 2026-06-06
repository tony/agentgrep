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
- Per source: one :class:`agentgrep.events.SourceStarted`, zero or
  more :class:`agentgrep.events.RecordEmitted`, one
  :class:`agentgrep.events.SourceFinished`.
- :class:`agentgrep.events.RecordEmitted` fires only after the
  per-session dedup decision has decided "unique-and-included". The
  legacy ``collect_search_records`` function buffers per-source
  matches, sorts them, then emits in newest-first order; this
  generator follows the same shape so the event order matches what
  the list-return wrapper produces.
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

import collections.abc as cabc
import pathlib
import time
import typing as t

import agentgrep

if t.TYPE_CHECKING:
    from agentgrep import (
        BackendSelection,
        SearchControl,
        SearchQuery,
        events as _events,
    )


def iter_search_events(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    backends: BackendSelection | None = None,
    control: SearchControl | None = None,
) -> cabc.Iterator[_events.SearchEvent]:
    """Yield typed events as the search engine scans sources.

    Parameters
    ----------
    home : pathlib.Path
        User home directory passed through to
        :func:`agentgrep.discover_sources`.
    query : agentgrep.SearchQuery
        Compiled query — terms, agents, dedup choice, limit.
    backends : agentgrep.BackendSelection or None
        Override the auto-detected backend selection (mainly used by
        tests). ``None`` selects backends via
        :func:`agentgrep.select_backends`.
    control : agentgrep.SearchControl or None
        Optional control handle. The generator polls
        :meth:`agentgrep.SearchControl.answer_now_requested` between
        records so consumers can break the scan early.

    Yields
    ------
    agentgrep.events.SearchEvent
        Discriminated-union events. See module docstring for the
        guaranteed sequence.

    Examples
    --------
    Stream events, collecting matching records::

        for event in iter_search_events(pathlib.Path.home(), query):
            if isinstance(event, agentgrep.events.RecordEmitted):
                print(event.record.text)
    """
    from agentgrep import events as _events
    from agentgrep._engine.execution import (
        ExecutionRecordEmitted,
        ExecutionSourceFinished,
        ExecutionSourceStarted,
        InlineExecutionDriver,
    )
    from agentgrep._engine.planning import build_physical_search_plan

    active_backends = agentgrep.select_backends() if backends is None else backends
    active_control = agentgrep.SearchControl() if control is None else control
    start_time = time.monotonic()

    sources = agentgrep.discover_sources_for_search(
        home,
        query,
        active_backends,
        version_detail="none",
    )
    source_predicate = query.compiled.source_predicate if query.compiled is not None else None
    if source_predicate is not None:
        sources = [s for s in sources if source_predicate(s)]
    plan = build_physical_search_plan(
        query,
        sources,
        active_backends,
        control=active_control,
    )

    yield _events.SearchStarted(source_count=len(plan.tasks))

    match_count = 0
    for execution_event in InlineExecutionDriver().iter_search_plan(
        query,
        plan,
        control=active_control,
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
