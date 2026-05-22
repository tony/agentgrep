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

from agentgrep import FindRecord, SearchRecord


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


class SearchStarted(_BaseEvent):
    """Engine resolved its sources and is about to begin scanning.

    Emitted exactly once per :func:`agentgrep.iter_search_events` call,
    immediately after :func:`agentgrep.discover_sources` returns and
    before the first :class:`SourceStarted` event.
    """

    type: t.Literal["search_started"] = "search_started"
    source_count: int


class SourceStarted(_BaseEvent):
    """One source has been picked up and is about to be scanned.

    ``index`` is 1-based; ``total`` matches the ``source_count`` from
    the preceding :class:`SearchStarted` event. ``adapter_id`` uniquely
    identifies the source (e.g. ``codex.sessions_jsonl.v1``); the full
    path is on the :class:`SourceFinished` event's ``records_seen`` /
    ``matches_seen`` tally if a consumer wants per-source detail.
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
    """

    type: t.Literal["record_emitted"] = "record_emitted"
    record: SearchRecord


class SourceFinished(_BaseEvent):
    """One source finished scanning. Carries per-source counters.

    ``records_seen`` is every record the adapter parsed from this source;
    ``matches_seen`` is the subset that matched the query (pre-dedup).
    The dedup decision happens later in the engine, so a
    :class:`RecordEmitted` event may fire for fewer records than
    ``matches_seen`` reports.
    """

    type: t.Literal["source_finished"] = "source_finished"
    adapter_id: str
    records_seen: int
    matches_seen: int


class SearchFinished(_BaseEvent):
    """Scan complete. Emitted exactly once per stream.

    ``match_count`` is the total of unique, included records — every
    :class:`RecordEmitted` that fired earlier counts once. Always the
    last event in a stream that ran to completion. A stream that
    raised an exception mid-scan will skip this event.
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
    """

    type: t.Literal["find_started"] = "find_started"
    source_count: int


class FindRecordEmitted(_BaseEvent):
    """One discovered source that survived the filter chain.

    The embedded :attr:`record` is :class:`agentgrep.FindRecord`. Same
    ``arbitrary_types_allowed`` trade-off as :class:`RecordEmitted`:
    consumers get the dataclass directly; transport-layer consumers
    convert via :class:`agentgrep.mcp.models.FindRecordModel`.
    """

    type: t.Literal["find_record_emitted"] = "find_record_emitted"
    record: FindRecord


class FindFinished(_BaseEvent):
    """Enumeration complete. ``match_count`` totals the emitted records."""

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
