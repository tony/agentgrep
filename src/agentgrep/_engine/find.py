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

from agentgrep.adapters import find_store_roles_for_type_filter
from agentgrep.discovery import discover_sources
from agentgrep.readers import select_backends
from agentgrep.records import AgentName, BackendSelection, FindRecord, FindSourceTypeFilter

if t.TYPE_CHECKING:
    from agentgrep import events as _events
    from agentgrep.query.compile import CompiledQuery


def iter_find_events(
    home: pathlib.Path,
    agents: tuple[AgentName, ...],
    *,
    pattern: str | None,
    limit: int | None,
    backends: BackendSelection | None = None,
    compiled: CompiledQuery | None = None,
    type_filter: FindSourceTypeFilter = "all",
) -> cabc.Iterator[_events.FindEvent]:
    """Yield typed events as the find engine enumerates sources.

    Parameters
    ----------
    home : pathlib.Path
        User home directory passed through to
        :func:`agentgrep.discover_sources`.
    agents : tuple[AgentName, ...]
        Agent backends to query.
    pattern : str or None
        Optional case-insensitive substring filter. When ``None`` every
        discovered source qualifies.
    limit : int or None
        Optional cap on the number of records emitted.
    backends : BackendSelection or None
        Override the auto-detected backend selection (mainly used by
        tests). ``None`` selects via :func:`agentgrep.select_backends`.
    compiled : agentgrep.CompiledQuery or None
        Optional :class:`~agentgrep.CompiledQuery` from
        :func:`agentgrep.query.parse_query` + ``compile_query``. When
        set, its ``source_predicate`` prunes sources before they're
        emitted as records. The ``record_predicate`` is not honored
        — find emits one record per source by construction, and the
        per-record query semantics only make sense for the search
        pipeline.
    type_filter : {"prompts", "history", "sessions", "all"}, default "all"
        Coarse source type filter used to prune catalogue roles before
        discovery. CLI renderers still apply their exact fd-shaped
        path-kind filter after records are emitted.

    Yields
    ------
    _events.FindEvent
        Discriminated-union events. See module docstring for the
        guaranteed sequence.

    Examples
    --------
    Iterate events, collecting only the records::

        for event in iter_find_events(home, ("codex",), pattern="sessions", limit=None):
            if isinstance(event, _events.FindRecordEmitted):
                print(event.record.path)
    """
    # Lazy import keeps ``agentgrep.events`` off the eager ``import
    # agentgrep`` path (pinned by tests/test_import_time.py); the facade tail
    # re-exports this module, so a module-level import would load it.
    from agentgrep import events as _events

    active_backends = select_backends() if backends is None else backends
    start_time = time.monotonic()

    sources = discover_sources(
        home,
        agents,
        active_backends,
        version_detail="none",
        store_roles=find_store_roles_for_type_filter(type_filter),
    )
    yield _events.FindStarted(source_count=len(sources))

    query = pattern.casefold() if pattern is not None else None
    source_predicate = compiled.source_predicate if compiled is not None else None
    emitted = 0

    for source in sources:
        # Compiled-query source pruning happens before the legacy
        # substring filter so a field predicate like `agent:codex`
        # short-circuits without even building the haystack.
        if source_predicate is not None and not source_predicate(source):
            continue
        record = FindRecord(
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

    from agentgrep import _telemetry

    _telemetry.record_metric(
        "agentgrep.find.sources",
        len(sources),
        agentgrep_surface="engine",
        agentgrep_agent_count=len(agents),
    )
    _telemetry.record_metric(
        "agentgrep.find.results",
        emitted,
        agentgrep_surface="engine",
        agentgrep_agent_count=len(agents),
    )
    yield _events.FindFinished(
        match_count=emitted,
        elapsed_seconds=time.monotonic() - start_time,
    )
