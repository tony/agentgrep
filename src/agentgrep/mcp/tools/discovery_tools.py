"""Discovery-domain MCP tools."""

from __future__ import annotations

import asyncio
import collections
import pathlib
import typing as t

from pydantic import Field

from agentgrep.mcp._library import (
    READONLY_TAGS,
    AgentSelector,
    agentgrep,
    normalize_agent_selection,
)
from agentgrep.mcp.models import (
    DiscoverySummaryRequest,
    DiscoverySummaryResponse,
    FilterSourcesRequest,
    FindRecordModel,
    FindRequestModel,
    FindToolQuery,
    FindToolResponse,
    ListSourcesRequest,
    ListSourcesResponse,
    SourceRecordModel,
)

if t.TYPE_CHECKING:
    from fastmcp import FastMCP


def _find_sync(request: FindRequestModel) -> FindToolResponse:
    """Run the blocking find work and build a typed response."""
    records = agentgrep.run_find_query(
        pathlib.Path.home(),
        normalize_agent_selection(request.agent),
        pattern=request.pattern,
        limit=request.limit,
    )
    return FindToolResponse(
        query=FindToolQuery(
            pattern=request.pattern,
            agent=request.agent,
            limit=request.limit,
        ),
        results=[FindRecordModel.from_record(record) for record in records],
    )


def _list_sources_sync(request: ListSourcesRequest) -> ListSourcesResponse:
    """Build a structured list of discovered sources."""
    backends = agentgrep.select_backends()
    sources = agentgrep.discover_sources(
        pathlib.Path.home(),
        normalize_agent_selection(request.agent),
        backends,
    )
    filtered: list[SourceRecordModel] = []
    for source in sources:
        if request.path_kind_filter is not None and source.path_kind != request.path_kind_filter:
            continue
        if (
            request.source_kind_filter is not None
            and source.source_kind != request.source_kind_filter
        ):
            continue
        filtered.append(SourceRecordModel.from_source(source))
        if request.limit is not None and len(filtered) >= request.limit:
            break
    return ListSourcesResponse(sources=filtered, total=len(filtered))


def _filter_sources_sync(request: FilterSourcesRequest) -> FindToolResponse:
    """Run the find pipeline with the requested pattern."""
    records = agentgrep.run_find_query(
        pathlib.Path.home(),
        normalize_agent_selection(request.agent),
        pattern=request.pattern,
        limit=request.limit,
    )
    return FindToolResponse(
        query=FindToolQuery(
            pattern=request.pattern,
            agent=request.agent,
            limit=request.limit,
        ),
        results=[FindRecordModel.from_record(record) for record in records],
    )


def _summarize_discovery_sync(request: DiscoverySummaryRequest) -> DiscoverySummaryResponse:
    """Aggregate counts of discovered sources by agent/format/path-kind."""
    backends = agentgrep.select_backends()
    sources = agentgrep.discover_sources(
        pathlib.Path.home(),
        normalize_agent_selection(request.agent),
        backends,
    )
    by_agent: collections.Counter[str] = collections.Counter()
    by_format: collections.Counter[str] = collections.Counter()
    by_kind: collections.Counter[str] = collections.Counter()
    for source in sources:
        by_agent[source.agent] += 1
        by_format[source.source_kind] += 1
        by_kind[source.path_kind] += 1
    return DiscoverySummaryResponse(
        total_sources=len(sources),
        sources_by_agent=dict(by_agent),
        sources_by_format=dict(by_format),
        sources_by_kind=dict(by_kind),
    )


def register(mcp: FastMCP) -> None:
    """Register discovery-domain tools."""

    @mcp.tool(
        name="find",
        tags=READONLY_TAGS | {"discovery"},
        description="Find known agent stores, session files, and SQLite databases.",
    )
    async def find_tool(
        pattern: t.Annotated[
            str | None,
            Field(
                default=None,
                description="Optional substring filter against discovered paths and adapters.",
            ),
        ] = None,
        agent: t.Annotated[
            AgentSelector,
            Field(description="Limit discovery to one agent or search all agents."),
        ] = "all",
        limit: t.Annotated[
            int | None,
            Field(
                default=50,
                ge=1,
                description="Maximum number of discovered sources to return.",
            ),
        ] = 50,
    ) -> FindToolResponse:
        request = FindRequestModel(pattern=pattern, agent=agent, limit=limit)
        return await asyncio.to_thread(_find_sync, request)

    _ = find_tool

    @mcp.tool(
        name="list_sources",
        tags=READONLY_TAGS | {"discovery"},
        description="List discovered sources with structured path-kind/source-kind filters.",
    )
    async def list_sources_tool(
        agent: t.Annotated[
            AgentSelector,
            Field(description="Limit discovery to one agent or scan every agent."),
        ] = "all",
        path_kind_filter: t.Annotated[
            t.Literal["history_file", "session_file", "sqlite_db"] | None,
            Field(default=None, description="Filter by path kind."),
        ] = None,
        source_kind_filter: t.Annotated[
            t.Literal["json", "jsonl", "sqlite"] | None,
            Field(default=None, description="Filter by on-disk source kind."),
        ] = None,
        limit: t.Annotated[
            int | None,
            Field(default=None, ge=1, description="Maximum number of sources to return."),
        ] = None,
    ) -> ListSourcesResponse:
        request = ListSourcesRequest(
            agent=agent,
            path_kind_filter=path_kind_filter,
            source_kind_filter=source_kind_filter,
            limit=limit,
        )
        return await asyncio.to_thread(_list_sources_sync, request)

    _ = list_sources_tool

    @mcp.tool(
        name="filter_sources",
        tags=READONLY_TAGS | {"discovery"},
        description="Filter discovered sources by required substring pattern.",
    )
    async def filter_sources_tool(
        pattern: t.Annotated[
            str,
            Field(min_length=1, description="Required substring pattern."),
        ],
        agent: t.Annotated[
            AgentSelector,
            Field(description="Limit discovery to one agent or scan every agent."),
        ] = "all",
        limit: t.Annotated[
            int | None,
            Field(default=50, ge=1, description="Maximum number of sources to return."),
        ] = 50,
    ) -> FindToolResponse:
        request = FilterSourcesRequest(pattern=pattern, agent=agent, limit=limit)
        return await asyncio.to_thread(_filter_sources_sync, request)

    _ = filter_sources_tool

    @mcp.tool(
        name="summarize_discovery",
        tags=READONLY_TAGS | {"discovery"},
        description="Aggregate counts of discovered sources by agent, format, and kind.",
    )
    async def summarize_discovery_tool(
        agent: t.Annotated[
            AgentSelector,
            Field(description="Limit discovery to one agent or scan every agent."),
        ] = "all",
    ) -> DiscoverySummaryResponse:
        request = DiscoverySummaryRequest(agent=agent)
        return await asyncio.to_thread(_summarize_discovery_sync, request)

    _ = summarize_discovery_tool
