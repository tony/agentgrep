"""Discovery-domain MCP tools."""

from __future__ import annotations

import collections
import pathlib
import typing as t

from fastmcp.exceptions import ToolError
from pydantic import Field

from agentgrep import _telemetry, events as ag_events
from agentgrep.mcp import refs
from agentgrep.mcp._library import (
    READONLY_TAGS,
    TOOL_ANNOTATIONS,
    AgentSelector,
    FindRecordLike,
    agentgrep,
    normalize_agent_selection,
)
from agentgrep.mcp.models import (
    DiagnosticModel,
    DiscoverySummaryRequest,
    DiscoverySummaryResponse,
    FilterSourcesRequest,
    FindRecordModel,
    FindRequestModel,
    FindToolResponse,
    ListSourcesRequest,
    ListSourcesResponse,
    PageInfoModel,
    ResultStatsModel,
    RunStatusModel,
    SourceRecordModel,
)

if t.TYPE_CHECKING:
    from fastmcp import FastMCP


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


def _request_from_cursor(request: FindRequestModel) -> tuple[FindRequestModel, int]:
    """Return the effective request and offset for a find page."""
    if request.cursor is None:
        return request, 0
    try:
        cursor = refs.parse_find_cursor(request.cursor)
    except refs.McpTokenError as exc:
        raise ToolError(str(exc)) from exc
    return (
        FindRequestModel(
            pattern=cursor.pattern,
            agent=cursor.agent,
            limit=cursor.limit,
            cursor=request.cursor,
        ),
        cursor.offset,
    )


def _find_sync(request: FindRequestModel) -> FindToolResponse:
    """Run the blocking find work and build a typed response."""
    effective_request, offset = _request_from_cursor(request)
    page_limit = effective_request.limit
    query_limit = None if page_limit is None else offset + page_limit + 1
    records: list[FindRecordLike] = []
    source_count = 0
    matched = 0
    for event in agentgrep.iter_find_events(
        pathlib.Path.home(),
        normalize_agent_selection(effective_request.agent),
        pattern=effective_request.pattern,
        limit=query_limit,
    ):
        if isinstance(event, ag_events.FindStarted):
            source_count = event.source_count
        elif isinstance(event, ag_events.FindRecordEmitted):
            records.append(t.cast("FindRecordLike", event.record))
        elif isinstance(event, ag_events.FindFinished):
            matched = max(matched, event.match_count)
    if page_limit is None:
        page_records = records[offset:]
        next_cursor = None
    else:
        page_records = records[offset : offset + page_limit]
        has_more = len(records) > offset + page_limit
        next_cursor = (
            refs.make_find_cursor(
                offset=offset + len(page_records),
                pattern=effective_request.pattern,
                agent=effective_request.agent,
                limit=page_limit,
            )
            if has_more
            else None
        )
    matched = max(matched, len(records))
    return FindToolResponse(
        request=effective_request,
        stats=ResultStatsModel(
            sources=source_count,
            searched=source_count,
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
        results=[FindRecordModel.from_record(record) for record in page_records],
    )


def _list_sources_sync(request: ListSourcesRequest) -> ListSourcesResponse:
    """Build a structured list of discovered sources."""
    backends = agentgrep.select_backends()
    sources = agentgrep.discover_sources(
        pathlib.Path.home(),
        normalize_agent_selection(request.agent),
        backends,
        include_non_default=request.include_non_default or request.coverage_filter is not None,
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
        if request.coverage_filter is not None and source.coverage != request.coverage_filter:
            continue
        filtered.append(SourceRecordModel.from_source(source))
        if request.limit is not None and len(filtered) >= request.limit:
            break
    return ListSourcesResponse(sources=filtered, total=len(filtered))


def _filter_sources_sync(request: FilterSourcesRequest) -> FindToolResponse:
    """Run the find pipeline with the requested pattern or cursor."""
    if request.cursor is None and request.pattern is None:
        msg = "pattern is required unless cursor is provided"
        raise ToolError(msg)
    return _find_sync(
        FindRequestModel(
            pattern=request.pattern,
            agent=request.agent,
            limit=request.limit,
            cursor=request.cursor,
        ),
    )


def _summarize_discovery_sync(request: DiscoverySummaryRequest) -> DiscoverySummaryResponse:
    """Aggregate counts of discovered sources by agent/format/path-kind."""
    backends = agentgrep.select_backends()
    sources = agentgrep.discover_sources(
        pathlib.Path.home(),
        normalize_agent_selection(request.agent),
        backends,
        version_detail="none",
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
        annotations=TOOL_ANNOTATIONS,
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
        cursor: t.Annotated[
            str | None,
            Field(
                default=None,
                description="Opaque page cursor returned by a previous find response.",
            ),
        ] = None,
    ) -> FindToolResponse:
        request = FindRequestModel(pattern=pattern, agent=agent, limit=limit, cursor=cursor)
        return await _telemetry.to_thread(_find_sync, request)

    _ = find_tool

    @mcp.tool(
        name="list_sources",
        tags=READONLY_TAGS | {"discovery"},
        annotations=TOOL_ANNOTATIONS,
        description="List discovered sources with structured path-kind/source-kind filters.",
    )
    async def list_sources_tool(
        agent: t.Annotated[
            AgentSelector,
            Field(description="Limit discovery to one agent or scan every agent."),
        ] = "all",
        path_kind_filter: t.Annotated[
            t.Literal["history_file", "session_file", "sqlite_db", "store_file"] | None,
            Field(default=None, description="Filter by path kind."),
        ] = None,
        source_kind_filter: t.Annotated[
            t.Literal["json", "jsonl", "sqlite", "text", "opaque"] | None,
            Field(default=None, description="Filter by on-disk source kind."),
        ] = None,
        coverage_filter: t.Annotated[
            t.Literal["default_search", "inspectable", "catalog_only", "private"] | None,
            Field(default=None, description="Filter by coverage level."),
        ] = None,
        include_non_default: t.Annotated[
            bool,
            Field(
                default=False,
                description="Include non-default inventory sources when true.",
            ),
        ] = False,
        limit: t.Annotated[
            int | None,
            Field(default=None, ge=1, description="Maximum number of sources to return."),
        ] = None,
    ) -> ListSourcesResponse:
        request = ListSourcesRequest(
            agent=agent,
            path_kind_filter=path_kind_filter,
            source_kind_filter=source_kind_filter,
            coverage_filter=coverage_filter,
            include_non_default=include_non_default,
            limit=limit,
        )
        return await _telemetry.to_thread(_list_sources_sync, request)

    _ = list_sources_tool

    @mcp.tool(
        name="filter_sources",
        tags=READONLY_TAGS | {"discovery"},
        annotations=TOOL_ANNOTATIONS,
        description="Filter discovered sources by required substring pattern.",
    )
    async def filter_sources_tool(
        pattern: t.Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                description="Required substring pattern unless cursor is provided.",
            ),
        ] = None,
        agent: t.Annotated[
            AgentSelector,
            Field(description="Limit discovery to one agent or scan every agent."),
        ] = "all",
        limit: t.Annotated[
            int | None,
            Field(default=50, ge=1, description="Maximum number of sources to return."),
        ] = 50,
        cursor: t.Annotated[
            str | None,
            Field(
                default=None,
                description="Opaque page cursor returned by a previous filter_sources response.",
            ),
        ] = None,
    ) -> FindToolResponse:
        request = FilterSourcesRequest(
            pattern=pattern,
            agent=agent,
            limit=limit,
            cursor=cursor,
        )
        return await _telemetry.to_thread(_filter_sources_sync, request)

    _ = filter_sources_tool

    @mcp.tool(
        name="summarize_discovery",
        tags=READONLY_TAGS | {"discovery"},
        annotations=TOOL_ANNOTATIONS,
        description="Aggregate counts of discovered sources by agent, format, and kind.",
    )
    async def summarize_discovery_tool(
        agent: t.Annotated[
            AgentSelector,
            Field(description="Limit discovery to one agent or scan every agent."),
        ] = "all",
    ) -> DiscoverySummaryResponse:
        request = DiscoverySummaryRequest(agent=agent)
        return await _telemetry.to_thread(_summarize_discovery_sync, request)

    _ = summarize_discovery_tool
