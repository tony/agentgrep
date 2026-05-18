"""Search-domain MCP tools."""

from __future__ import annotations

import asyncio
import pathlib
import typing as t

from pydantic import Field

from agentgrep.mcp._library import (
    READONLY_TAGS,
    AgentSelector,
    SearchTypeName,
    agentgrep,
    normalize_agent_selection,
)
from agentgrep.mcp.models import (
    SearchRecordModel,
    SearchRequestModel,
    SearchToolQuery,
    SearchToolResponse,
)

if t.TYPE_CHECKING:
    from fastmcp import FastMCP


def _search_sync(request: SearchRequestModel) -> SearchToolResponse:
    """Run the blocking search work and build a typed response."""
    query = agentgrep.SearchQuery(
        terms=tuple(request.terms),
        search_type=request.search_type,
        any_term=request.any_term,
        regex=request.regex,
        case_sensitive=request.case_sensitive,
        agents=normalize_agent_selection(request.agent),
        limit=request.limit,
    )
    records = agentgrep.run_search_query(pathlib.Path.home(), query)
    return SearchToolResponse(
        query=SearchToolQuery(
            terms=request.terms,
            agent=request.agent,
            search_type=request.search_type,
            any_term=request.any_term,
            regex=request.regex,
            case_sensitive=request.case_sensitive,
            limit=request.limit,
        ),
        results=[SearchRecordModel.from_record(record) for record in records],
    )


def register(mcp: FastMCP) -> None:
    """Register search-domain tools."""

    @mcp.tool(
        name="search",
        tags=READONLY_TAGS | {"search"},
        description="Search normalized prompts or history across local agent stores.",
    )
    async def search_tool(
        terms: t.Annotated[
            list[str],
            Field(
                min_length=1,
                description="One or more literal or regex search terms.",
            ),
        ],
        agent: t.Annotated[
            AgentSelector,
            Field(description="Limit search to one agent or search all agents."),
        ] = "all",
        search_type: t.Annotated[
            SearchTypeName,
            Field(description="Search prompts, history, or both."),
        ] = "prompts",
        any_term: t.Annotated[
            bool,
            Field(description="Match any term instead of requiring all terms."),
        ] = False,
        regex: t.Annotated[
            bool,
            Field(description="Treat search terms as regular expressions."),
        ] = False,
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
            search_type=search_type,
            any_term=any_term,
            regex=regex,
            case_sensitive=case_sensitive,
            limit=limit,
        )
        return await asyncio.to_thread(_search_sync, request)

    _ = search_tool
