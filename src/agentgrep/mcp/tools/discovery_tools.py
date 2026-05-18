"""Discovery-domain MCP tools."""

from __future__ import annotations

import asyncio
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
    FindRecordModel,
    FindRequestModel,
    FindToolQuery,
    FindToolResponse,
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
