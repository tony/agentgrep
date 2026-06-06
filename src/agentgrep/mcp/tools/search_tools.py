"""Search-domain MCP tools."""

from __future__ import annotations

import asyncio
import datetime
import pathlib
import time
import typing as t

from pydantic import Field

from agentgrep import events as ag_events
from agentgrep.mcp._library import (
    READONLY_TAGS,
    AgentSelector,
    SearchRecordLike,
    SearchScopeName,
    agentgrep,
    normalize_agent_selection,
)
from agentgrep.mcp.models import (
    RecentSessionsRequest,
    RecentSessionsResponse,
    SearchRecordModel,
    SearchRequestModel,
    SearchToolQuery,
    SearchToolResponse,
    SourceRecordModel,
)

if t.TYPE_CHECKING:
    from fastmcp import FastMCP

    from agentgrep._engine.runtime import SearchRuntime


async def _search_async(
    request: SearchRequestModel,
    *,
    runtime: SearchRuntime | None = None,
) -> SearchToolResponse:
    """Run the async search stream and build a typed response."""
    query = agentgrep.SearchQuery(
        terms=tuple(request.terms),
        scope=request.scope,
        any_term=False,
        regex=False,
        case_sensitive=request.case_sensitive,
        agents=normalize_agent_selection(request.agent),
        limit=request.limit,
    )
    records = [
        t.cast("SearchRecordLike", event.record)
        async for event in agentgrep.aiter_search_events(
            pathlib.Path.home(),
            query,
            runtime=runtime,
        )
        if isinstance(event, ag_events.RecordEmitted)
    ]
    return SearchToolResponse(
        query=SearchToolQuery(
            terms=request.terms,
            agent=request.agent,
            scope=request.scope,
            case_sensitive=request.case_sensitive,
            limit=request.limit,
        ),
        results=[SearchRecordModel.from_record(record) for record in records],
    )


def _recent_sessions_sync(request: RecentSessionsRequest) -> RecentSessionsResponse:
    """Return recently modified sources sorted newest-first."""
    backends = agentgrep.select_backends()
    sources = agentgrep.discover_sources(
        pathlib.Path.home(),
        normalize_agent_selection(request.agent),
        backends,
        version_detail="none",
    )
    cutoff_ns = time.time_ns() - request.hours * 3600 * 1_000_000_000
    recent = [source for source in sources if source.mtime_ns >= cutoff_ns]
    recent.sort(key=lambda s: s.mtime_ns, reverse=True)
    if request.limit is not None:
        recent = recent[: request.limit]
    cutoff_iso = datetime.datetime.fromtimestamp(
        cutoff_ns / 1_000_000_000,
        tz=datetime.UTC,
    ).isoformat()
    return RecentSessionsResponse(
        cutoff_iso=cutoff_iso,
        sources=[SourceRecordModel.from_source(source) for source in recent],
    )


def register(mcp: FastMCP, *, runtime: SearchRuntime | None = None) -> None:
    """Register search-domain tools."""

    @mcp.tool(
        name="search",
        tags=READONLY_TAGS | {"search"},
        description=("Search normalized prompts by default; opt into conversations with scope."),
    )
    async def search_tool(
        terms: t.Annotated[
            list[str],
            Field(
                min_length=1,
                description="One or more literal search terms (AND-matched).",
            ),
        ],
        agent: t.Annotated[
            AgentSelector,
            Field(description="Limit search to one agent or search all agents."),
        ] = "all",
        scope: t.Annotated[
            SearchScopeName,
            Field(description="Search prompts, conversations, or both."),
        ] = "prompts",
        case_sensitive: t.Annotated[
            bool,
            Field(description="Perform case-sensitive matching."),
        ] = False,
        limit: t.Annotated[
            int | None,
            Field(
                default=20,
                ge=1,
                description="Maximum number of search results to return.",
            ),
        ] = 20,
    ) -> SearchToolResponse:
        request = SearchRequestModel(
            terms=terms,
            agent=agent,
            scope=scope,
            case_sensitive=case_sensitive,
            limit=limit,
        )
        return await _search_async(request, runtime=runtime)

    _ = search_tool

    @mcp.tool(
        name="recent_sessions",
        tags=READONLY_TAGS | {"search"},
        description="Return sources modified in the last N hours, newest-first.",
    )
    async def recent_sessions_tool(
        agent: t.Annotated[
            AgentSelector,
            Field(description="Limit discovery to one agent or scan every agent."),
        ] = "all",
        hours: t.Annotated[
            int,
            Field(
                default=24,
                ge=1,
                le=24 * 30,
                description="Look back this many hours (max 30 days).",
            ),
        ] = 24,
        limit: t.Annotated[
            int | None,
            Field(
                default=10,
                ge=1,
                description="Maximum number of sources to return.",
            ),
        ] = 10,
    ) -> RecentSessionsResponse:
        request = RecentSessionsRequest(agent=agent, hours=hours, limit=limit)
        return await asyncio.to_thread(_recent_sessions_sync, request)

    _ = recent_sessions_tool
