"""Typed event stream emitted by the agentgrep engine.

This module defines the discriminated-union of events that the search
and find engines emit during a scan. Consumers (the CLI, the Textual
TUI, the MCP server) subscribe to the iterator and route events
according to their needs:

- The CLI's text path prints :class:`RecordEmitted` payloads as they
  arrive and ignores the rest.
- The TUI consumes every event for status updates plus :class:`RecordEmitted`
  for the results list.
- The MCP server collects :class:`RecordEmitted` events into the
  response payload and ignores progress events.

Each event is a frozen ``pydantic.BaseModel`` tagged with a literal
``type`` field; the union below uses ``pydantic.Field(discriminator=...)``
so runtime validation and ``isinstance`` narrowing both work without
ceremony. Events embed agentgrep's existing dataclass record types
directly (``arbitrary_types_allowed=True``) so consumers can use the
record without an extra conversion step.

Examples
--------
Iterate events and filter for record payloads::

    from agentgrep import iter_search_events
    from agentgrep.events import RecordEmitted

    for event in iter_search_events(home, query):
        if isinstance(event, RecordEmitted):
            print(event.record.text)

Round-trip a stream through pydantic for transport (e.g. an HTTP
SSE endpoint)::

    from pydantic import TypeAdapter
    from agentgrep.events import SearchEvent

    adapter = TypeAdapter(SearchEvent)
    for event in iter_search_events(home, query):
        # ``arbitrary_types_allowed`` blocks dump_json on the dataclass
        # field, so transport layers should serialise via the existing
        # ``SearchRecordModel`` wrapper at the boundary.
        ...
"""

from __future__ import annotations

import typing as t

import pydantic

from agentgrep.records import FindRecord, SearchRecord


class _BaseEvent(pydantic.BaseModel):
    """Frozen base for every engine event.

    Subclasses set a ``type`` literal that participates in the
    discriminated-union narrowing in :data:`SearchEvent` and
    :data:`FindEvent`. Events are frozen so consumers can safely
    re-emit them through fan-out subscribers without worrying about
    mutation.
    """

    model_config: t.ClassVar[pydantic.ConfigDict] = pydantic.ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )
    """Pydantic settings shared by every event.

    Frozen so a fan-out subscriber cannot mutate what the next subscriber
    receives, extra fields rejected so a typo is a validation error rather than
    a silently ignored keyword, and arbitrary types allowed so events can embed
    agentgrep's record dataclasses without a pydantic copy.
    """


class SearchStarted(_BaseEvent):
    """Engine resolved its sources and is about to begin scanning.

    Emitted exactly once per :func:`agentgrep.iter_search_events` call,
    immediately after :func:`agentgrep.discover_sources` returns and
    before the first :class:`SourceStarted` event.

    Attributes
    ----------
    type : t.Literal["search_started"]
        Discriminator tag marking the event as the head of a search stream.
        :data:`SearchEvent` validates and narrows the union on it.
    source_count : int
        Sources the planner kept for this run, after discovery and query-level
        pruning. Zero means the stream ends without a single
        :class:`SourceStarted`.
    """

    type: t.Literal["search_started"] = "search_started"
    source_count: int


class SourceStarted(_BaseEvent):
    """One source has been picked up and is about to be scanned.

    Attributes
    ----------
    type : t.Literal["source_started"]
        Discriminator tag marking the start of one source's scan.
        :data:`SearchEvent` validates and narrows the union on it.
    adapter_id : str
        Adapter reading this source, e.g. ``codex.sessions_jsonl.v1``. It names
        the parse shape rather than the individual file, so every file a
        discovery glob matched shares one value.
    index : int
        Position of this source in the scan, counting from one.
    total : int
        Sources this scan covers, matching the preceding
        :class:`SearchStarted` event's ``source_count``.
    """

    type: t.Literal["source_started"] = "source_started"
    adapter_id: str
    index: int
    total: int


class RecordEmitted(_BaseEvent):
    """A unique, included record. The hot-path event consumers care about.

    The embedded :attr:`record` is agentgrep's existing
    :class:`agentgrep.SearchRecord` dataclass, not a pydantic copy —
    consumers (CLI renderer, TUI list) use the record's attributes
    directly without a conversion step. Pydantic allows this via
    ``arbitrary_types_allowed=True`` on the model config; the trade-off
    is that ``model_dump_json()`` won't round-trip these events
    unmodified, so transport-layer consumers should serialise the
    record via :class:`agentgrep.mcp.models.SearchRecordModel` at the
    boundary.

    Attributes
    ----------
    type : t.Literal["record_emitted"]
        Discriminator tag marking the one event that carries a search result.
        :data:`SearchEvent` validates and narrows the union on it, and it is
        what a consumer filters on to ignore progress traffic.
    record : SearchRecord
        The accepted record, already deduped against the ones emitted before
        it. No other event in the stream carries a result.
    """

    type: t.Literal["record_emitted"] = "record_emitted"
    record: SearchRecord


class SourceFinished(_BaseEvent):
    """One source finished scanning. Carries per-source counters.

    Attributes
    ----------
    type : t.Literal["source_finished"]
        Discriminator tag marking the end of one source's scan.
        :data:`SearchEvent` validates and narrows the union on it.
    adapter_id : str
        Adapter that read this source, matching the :class:`SourceStarted`
        event that opened it.
    records_seen : int
        Records the adapter parsed out of this source, matched or not.
    matches_seen : int
        Records that matched the query, before dedup. Dedup happens later in
        the engine, so fewer :class:`RecordEmitted` events may fire than this
        counts.
    """

    type: t.Literal["source_finished"] = "source_finished"
    adapter_id: str
    records_seen: int
    matches_seen: int


class SearchFinished(_BaseEvent):
    """Scan complete. Emitted exactly once per stream.

    Always the last event in a stream that ran to completion. A stream that
    raised an exception mid-scan will skip this event.

    Attributes
    ----------
    type : t.Literal["search_finished"]
        Discriminator tag marking the tail of a search stream.
        :data:`SearchEvent` validates and narrows the union on it.
    match_count : int
        Unique, included records for the whole search — every
        :class:`RecordEmitted` that fired earlier counts once.
    elapsed_seconds : float
        Monotonic seconds the search took, counted from before discovery, so
        it covers planning as well as scanning.
    """

    type: t.Literal["search_finished"] = "search_finished"
    match_count: int
    elapsed_seconds: float


SearchEvent = t.Annotated[
    SearchStarted | SourceStarted | RecordEmitted | SourceFinished | SearchFinished,
    pydantic.Field(discriminator="type"),
]
"""Discriminated union of every event :func:`agentgrep.iter_search_events` emits.

Tagged on the ``type`` literal field. Use ``isinstance(event, RecordEmitted)``
to narrow inside a loop; pydantic's discriminator metadata lets ``ty`` /
``mypy`` understand the narrowing without extra annotations.
"""


# --- find events -----------------------------------------------------------


class FindStarted(_BaseEvent):
    """Engine resolved sources and is about to begin enumerating.

    Emitted exactly once per :func:`agentgrep.iter_find_events` call.
    Unlike search, find has no per-source scan loop, so there is no
    ``SourceStarted`` / ``SourceFinished`` event pair.

    Attributes
    ----------
    type : t.Literal["find_started"]
        Discriminator tag marking the head of a find stream.
        :data:`FindEvent` validates and narrows the union on it.
    source_count : int
        Sources discovery returned, before the pattern and predicate filters
        run. It is the ceiling on the records that follow, not their count.
    """

    type: t.Literal["find_started"] = "find_started"
    source_count: int


class FindRecordEmitted(_BaseEvent):
    """One discovered source that survived the filter chain.

    The embedded :attr:`record` is :class:`agentgrep.FindRecord`. Same
    ``arbitrary_types_allowed`` trade-off as :class:`RecordEmitted`:
    consumers get the dataclass directly; transport-layer consumers
    convert via :class:`agentgrep.mcp.models.FindRecordModel`.

    Attributes
    ----------
    type : t.Literal["find_record_emitted"]
        Discriminator tag marking the one event that carries a discovered
        source. :data:`FindEvent` validates and narrows the union on it.
    record : FindRecord
        The discovered source, one per enumerated file. Find never opens the
        file, so the record describes the location rather than its contents.
    """

    type: t.Literal["find_record_emitted"] = "find_record_emitted"
    record: FindRecord


class FindFinished(_BaseEvent):
    """Enumeration complete.

    Attributes
    ----------
    type : t.Literal["find_finished"]
        Discriminator tag marking the tail of a find stream.
        :data:`FindEvent` validates and narrows the union on it.
    match_count : int
        Records emitted, one per source that passed the filters. Stops at the
        caller's limit when one was set.
    elapsed_seconds : float
        Monotonic seconds the enumeration took, counted from before discovery.
    """

    type: t.Literal["find_finished"] = "find_finished"
    match_count: int
    elapsed_seconds: float


FindEvent = t.Annotated[
    FindStarted | FindRecordEmitted | FindFinished,
    pydantic.Field(discriminator="type"),
]
"""Discriminated union of every event :func:`agentgrep.iter_find_events` emits."""


__all__ = [
    "FindEvent",
    "FindFinished",
    "FindRecordEmitted",
    "FindStarted",
    "RecordEmitted",
    "SearchEvent",
    "SearchFinished",
    "SearchStarted",
    "SourceFinished",
    "SourceStarted",
]
