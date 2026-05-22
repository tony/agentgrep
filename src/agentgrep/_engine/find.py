"""Find event-stream producer.

The :func:`iter_find_events` generator walks every source the user's
configured agents expose and yields one :class:`agentgrep.events.FindRecordEmitted`
per source that survives the (optional) substring filter. Unlike the
search side, find has no per-source scan loop — every source produces
exactly one record — so the event vocabulary is smaller:

- Exactly one :class:`agentgrep.events.FindStarted` at the head.
- Zero or more :class:`agentgrep.events.FindRecordEmitted`.
- Exactly one :class:`agentgrep.events.FindFinished` at the tail.

The pattern argument is a case-folded substring match against the
record's agent / store / adapter_id / path / path_kind concatenation —
identical semantics to the legacy :func:`agentgrep.find_sources`
function so the list-return wrapper (kept for MCP / TUI / tests) and
the new stream agree on which sources qualify.
"""

from __future__ import annotations

import collections.abc as cabc
import pathlib
import time
import typing as t

import agentgrep
from agentgrep import events as _events

if t.TYPE_CHECKING:
    from agentgrep import AgentName, BackendSelection


def iter_find_events(
    home: pathlib.Path,
    agents: tuple[AgentName, ...],
    *,
    pattern: str | None,
    limit: int | None,
    backends: BackendSelection | None = None,
) -> cabc.Iterator[_events.FindEvent]:
    """Yield typed events as the find engine enumerates sources.

    Parameters
    ----------
    home : pathlib.Path
        User home directory passed through to
        :func:`agentgrep.discover_sources`.
    agents : tuple[agentgrep.AgentName, ...]
        Agent backends to query.
    pattern : str or None
        Optional case-insensitive substring filter. When ``None`` every
        discovered source qualifies.
    limit : int or None
        Optional cap on the number of records emitted.
    backends : agentgrep.BackendSelection or None
        Override the auto-detected backend selection (mainly used by
        tests). ``None`` selects via :func:`agentgrep.select_backends`.

    Yields
    ------
    agentgrep.events.FindEvent
        Discriminated-union events. See module docstring for the
        guaranteed sequence.

    Examples
    --------
    Iterate events, collecting only the records::

        for event in iter_find_events(home, ("codex",), pattern="sessions", limit=None):
            if isinstance(event, agentgrep.events.FindRecordEmitted):
                print(event.record.path)
    """
    active_backends = agentgrep.select_backends() if backends is None else backends
    start_time = time.monotonic()

    sources = agentgrep.discover_sources(home, agents, active_backends)
    yield _events.FindStarted(source_count=len(sources))

    query = pattern.casefold() if pattern is not None else None
    emitted = 0

    for source in sources:
        record = agentgrep.FindRecord(
            kind="find",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            path_kind=source.path_kind,
            metadata={"source_kind": source.source_kind},
        )
        if query is not None:
            haystack = " ".join(
                (
                    record.agent,
                    record.store,
                    record.adapter_id,
                    str(record.path),
                    record.path_kind,
                ),
            ).casefold()
            if query not in haystack:
                continue
        yield _events.FindRecordEmitted(record=record)
        emitted += 1
        if limit is not None and emitted >= limit:
            break

    yield _events.FindFinished(
        match_count=emitted,
        elapsed_seconds=time.monotonic() - start_time,
    )
