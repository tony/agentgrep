"""Search-domain MCP tools."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import datetime
import pathlib
import time
import typing as t

from fastmcp.exceptions import ToolError
from pydantic import Field

from agentgrep import events as ag_events
from agentgrep.mcp import refs
from agentgrep.mcp._library import (
    READONLY_TAGS,
    AgentSelector,
    SearchRecordLike,
    SearchScopeName,
    agentgrep,
    normalize_agent_selection,
)
from agentgrep.mcp.models import (
    DiagnosticModel,
    PageInfoModel,
    RecentSessionsRequest,
    RecentSessionsResponse,
    ResultStatsModel,
    RunStatusModel,
    SearchRecordModel,
    SearchRequestModel,
    SearchToolResponse,
    SourceRecordModel,
)
from agentgrep.query.help import query_language_summary

if t.TYPE_CHECKING:
    from fastmcp import FastMCP

    from agentgrep import SearchQuery
    from agentgrep._engine.runtime import SearchRuntime


def _page_status(next_cursor: str | None) -> RunStatusModel:
    """Return the status for a normal MCP result page."""
    if next_cursor is None:
        return RunStatusModel(state="complete")
    return RunStatusModel(state="bounded", reason="page_limit")


def _page_diagnostics(next_cursor: str | None) -> list[DiagnosticModel]:
    """Return diagnostics for a normal MCP result page."""
    if next_cursor is None:
        return []
    return [
        DiagnosticModel(
            code="page_limit",
            message="More records are available via page.next_cursor.",
        )
    ]


def _request_from_cursor(request: SearchRequestModel) -> tuple[SearchRequestModel, int]:
    """Return the effective request and offset for a search page."""
    if request.cursor is None:
        if not request.terms:
            msg = "terms are required unless cursor is provided"
            raise ToolError(msg)
        return request, 0
    try:
        cursor = refs.parse_search_cursor(request.cursor)
    except refs.McpTokenError as exc:
        raise ToolError(str(exc)) from exc
    return (
        SearchRequestModel(
            terms=cursor.terms,
            agent=cursor.agent,
            scope=cursor.scope,
            case_sensitive=cursor.case_sensitive,
            limit=cursor.limit,
            cursor=request.cursor,
        ),
        cursor.offset,
    )


def _compile_request_query(
    base_query: SearchQuery,
    terms: cabc.Sequence[str],
) -> SearchQuery:
    """Apply the query language to a search request's terms.

    Joins the request terms and routes them through
    :func:`agentgrep.query.build_query_from_input` so MCP clients get the
    same field predicates, booleans, phrases, and wildcards as the CLI.
    Bare terms stay literal substrings; a malformed query raises a
    :class:`ToolError` with the parse/compile message.
    """
    from agentgrep.query import build_query_from_input, default_registry

    joined = " ".join(terms).strip()
    if not joined:
        return base_query
    result = build_query_from_input(joined, base_query, default_registry())
    if result.query is None:
        message = f"invalid query: {result.error}"
        raise ToolError(message)
    return result.query


async def _search_async(
    request: SearchRequestModel,
    *,
    runtime: SearchRuntime | None = None,
) -> SearchToolResponse:
    """Run the async search stream and build a typed response."""
    effective_request, offset = _request_from_cursor(request)
    page_limit = effective_request.limit
    query_limit = None if page_limit is None else offset + page_limit + 1
    base_query = t.cast(
        "SearchQuery",
        agentgrep.SearchQuery(
            terms=tuple(effective_request.terms),
            scope=effective_request.scope,
            any_term=False,
            regex=False,
            case_sensitive=effective_request.case_sensitive,
            agents=normalize_agent_selection(effective_request.agent),
            limit=query_limit,
        ),
    )
    query = _compile_request_query(base_query, effective_request.terms)
    records: list[SearchRecordLike] = []
    source_count = 0
    searched = 0
    matched = 0
    async for event in agentgrep.aiter_search_events(
        pathlib.Path.home(),
        query,
        runtime=runtime,
    ):
        if isinstance(event, ag_events.SearchStarted):
            source_count = event.source_count
        elif isinstance(event, ag_events.SourceFinished):
            searched += event.records_seen
            matched += event.matches_seen
        elif isinstance(event, ag_events.RecordEmitted):
            records.append(t.cast("SearchRecordLike", event.record))
        elif isinstance(event, ag_events.SearchFinished):
            matched = max(matched, event.match_count)
    # The inline execution driver emits records per source, not in final
    # result order; restore the newest-first contract the list-returning
    # search path guarantees before building the response.
    records.sort(key=agentgrep.search_record_sort_key, reverse=True)
    if page_limit is None:
        page_records = records[offset:]
        next_cursor = None
    else:
        page_records = records[offset : offset + page_limit]
        has_more = len(records) > offset + page_limit
        next_cursor = (
            refs.make_search_cursor(
                offset=offset + len(page_records),
                terms=effective_request.terms,
                agent=effective_request.agent,
                scope=effective_request.scope,
                case_sensitive=effective_request.case_sensitive,
                limit=page_limit,
            )
            if has_more
            else None
        )
    matched = max(matched, len(records))
    searched = max(searched, matched)
    return SearchToolResponse(
        request=effective_request,
        stats=ResultStatsModel(
            sources=source_count,
            searched=searched,
            matched=matched,
            emitted=len(page_records),
        ),
        page=PageInfoModel(
            limit=page_limit,
            count=len(page_records),
            next_cursor=next_cursor,
        ),
        status=_page_status(next_cursor),
        diagnostics=_page_diagnostics(next_cursor),
        results=[SearchRecordModel.from_record(record) for record in page_records],
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
        description=(
            "Search normalized prompts by default; opt into conversations with "
            "scope. Terms accept agentgrep's query language (field predicates, "
            "booleans, phrases, and wildcards); see agentgrep://query-language."
        ),
    )
    async def search_tool(
        terms: t.Annotated[
            list[str] | None,
            Field(
                default=None,
                description=f"Search terms. {query_language_summary()}",
            ),
        ] = None,
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
        cursor: t.Annotated[
            str | None,
            Field(
                default=None,
                description="Opaque page cursor returned by a previous search response.",
            ),
        ] = None,
    ) -> SearchToolResponse:
        request = SearchRequestModel(
            terms=terms or [],
            agent=agent,
            scope=scope,
            case_sensitive=case_sensitive,
            limit=limit,
            cursor=cursor,
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
