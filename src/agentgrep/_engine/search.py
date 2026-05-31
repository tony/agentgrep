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
        SearchRecord,
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
    planned_sources = agentgrep.plan_search_sources(
        query,
        sources,
        active_backends,
        control=active_control,
    )

    yield _events.SearchStarted(source_count=len(planned_sources))

    deduped_keys: set[tuple[str, str, str, str, str]] = set()
    raw_count = 0
    total = len(planned_sources)

    def current_count() -> int:
        return len(deduped_keys) if query.dedupe else raw_count

    for index, source in enumerate(planned_sources, start=1):
        if active_control.answer_now_requested() or (
            query.limit is not None and current_count() >= query.limit
        ):
            break

        yield _events.SourceStarted(
            adapter_id=source.adapter_id,
            index=index,
            total=total,
        )

        records_seen = 0
        matches_seen = 0
        matching_records: list[SearchRecord] = []
        for record in agentgrep.iter_source_records(source):
            if active_control.answer_now_requested():
                break
            records_seen += 1
            if agentgrep.matches_record(record, query):
                matches_seen += 1
                matching_records.append(record)

        matching_records.sort(key=agentgrep.search_record_sort_key, reverse=True)

        for record in matching_records:
            if query.dedupe:
                dedupe_key = agentgrep.record_dedupe_key(record)
                if dedupe_key in deduped_keys:
                    continue
                deduped_keys.add(dedupe_key)
            else:
                raw_count += 1
            yield _events.RecordEmitted(record=record)
            if active_control.answer_now_requested() or (
                query.limit is not None and current_count() >= query.limit
            ):
                break

        yield _events.SourceFinished(
            adapter_id=source.adapter_id,
            records_seen=records_seen,
            matches_seen=matches_seen,
        )

    yield _events.SearchFinished(
        match_count=current_count(),
        elapsed_seconds=time.monotonic() - start_time,
    )
