"""Search-domain MCP tools."""

from __future__ import annotations

import asyncio
import dataclasses
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
from agentgrep.origin import normalize_origin_path_text, origin_filter_nodes
from agentgrep.query.help import query_language_summary

if t.TYPE_CHECKING:
    from fastmcp import FastMCP

    from agentgrep._engine.runtime import SearchRuntime
    from agentgrep.records import SearchQuery


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
        if not request.terms and not _request_has_origin_filter(request):
            msg = "terms or an origin filter are required unless cursor is provided"
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
            cwd=cursor.cwd,
            repo=cursor.repo,
            branch=cursor.branch,
        ),
        cursor.offset,
    )


def _request_has_origin_filter(request: SearchRequestModel) -> bool:
    return bool(
        (request.cwd or "").strip()
        or (request.repo or "").strip()
        or (request.branch or "").strip(),
    )


def _compile_request_query(
    base_query: SearchQuery,
    request: SearchRequestModel,
) -> SearchQuery:
    """Apply the query language and origin filters to a search request.

    User terms compile exactly as the CLI's bare path compiles them —
    field predicates, booleans, phrases, and wildcards all apply, and
    plain terms stay literal substrings. Origin filters are ANDed in as
    synthetic AST nodes via :func:`agentgrep.query.compose_query_ast`.
    A malformed query raises a :class:`ToolError` with the parse/compile
    message.
    """
    from agentgrep.query import (
        QueryCompileError,
        QueryParseError,
        compile_query,
        compose_query_ast,
        default_registry,
        scope_widened_for_ast,
    )

    origin_nodes = origin_filter_nodes(
        cwd=normalize_origin_path_text(request.cwd),
        repo=normalize_origin_path_text(request.repo),
        branch=request.branch,
    )
    # Whitespace-split each element: MCP terms have always been words
    # (the pre-origin path joined and re-split them), unlike CLI argv
    # elements, which stay whole to match the bare fast path.
    terms = tuple(word for term in request.terms for word in term.split())
    if not origin_nodes and not terms:
        return base_query
    registry = default_registry()
    try:
        ast, user_ast = compose_query_ast(terms, origin_nodes, registry)
        compiled = compile_query(ast, registry, case_sensitive=base_query.case_sensitive)
    except (QueryParseError, QueryCompileError) as exc:
        message = f"invalid query: {exc}"
        raise ToolError(message) from exc
    scope = scope_widened_for_ast(user_ast, base_query.scope)
    return dataclasses.replace(
        base_query,
        terms=compiled.text_terms,
        compiled=None if compiled.is_pure_text else compiled,
        scope=scope,
    )


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
    query = _compile_request_query(base_query, effective_request)
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
                cwd=effective_request.cwd,
                repo=effective_request.repo,
                branch=effective_request.branch,
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
        cwd: t.Annotated[
            str | None,
            Field(
                default=None,
                description="Only return records whose recorded cwd matches this path.",
            ),
        ] = None,
        repo: t.Annotated[
            str | None,
            Field(
                default=None,
                description="Only return records whose recorded repository root matches this path.",
            ),
        ] = None,
        branch: t.Annotated[
            str | None,
            Field(
                default=None,
                description="Only return records whose recorded git branch matches this name.",
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
            cwd=cwd,
            repo=repo,
            branch=branch,
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
