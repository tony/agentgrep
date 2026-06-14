"""Insights-domain MCP tools: recurring-request skills over the graph level."""

from __future__ import annotations

import asyncio
import pathlib
import typing as t

from pydantic import Field

from agentgrep.mcp._library import READONLY_TAGS, agentgrep, normalize_agent_selection
from agentgrep.mcp.models import InsightsSkillsRequest, InsightsSkillsResponse

if t.TYPE_CHECKING:
    from fastmcp import FastMCP

_GRAPH_SETUP = "uv pip install 'agentgrep[insights-graph]'"


def _conversation_key(record: t.Any) -> str:
    """Return a stable conversation grouping key for windowing."""
    return record.conversation_id or record.session_id or str(record.path)


def _apply_window(records: list[t.Any], *, since: str | None, until: str | None) -> list[t.Any]:
    """Keep whole conversations that touch the [since, until] ISO window.

    Conversation replies carry no timestamp, so a conversation is kept when
    any of its records falls in the window. With no timestamps at all, the
    records are returned unchanged.
    """
    if since is None and until is None:
        return records
    if not any(getattr(record, "timestamp", None) for record in records):
        return records

    def in_window(timestamp: str | None) -> bool:
        return bool(
            timestamp
            and (since is None or timestamp >= since)
            and (until is None or timestamp <= until)
        )

    keep = {_conversation_key(r) for r in records if in_window(r.timestamp)}
    return [r for r in records if _conversation_key(r) in keep or in_window(r.timestamp)]


def _insights_skills_sync(request: InsightsSkillsRequest) -> InsightsSkillsResponse:
    """Collect conversation turns, run the graph level, return its sections."""
    from agentgrep import insights
    from agentgrep.insights.model import ReportRequest

    agents = t.cast("t.Any", normalize_agent_selection(request.agent))
    query = agentgrep.SearchQuery(
        terms=(),
        scope="conversations",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agents,
        limit=request.limit,
        dedupe=False,
    )
    records = agentgrep.run_search_query(pathlib.Path.home(), query)
    records = _apply_window(records, since=request.since, until=request.until)
    if not records:
        return InsightsSkillsResponse(status="empty", records_analyzed=0)

    report = insights.build_report(
        records,
        ReportRequest(scope="conversations", requested_level="graph", record_limit=request.limit),
    )
    graph = next((e for e in report.enrichments if e.level == "graph"), None)
    if graph is None:
        setup = next(
            (d.setup_command for d in report.diagnostics if d.setup_command),
            _GRAPH_SETUP,
        )
        return InsightsSkillsResponse(
            status="unavailable",
            records_analyzed=report.records_analyzed,
            setup_command=setup,
        )
    data = graph.data
    return InsightsSkillsResponse(
        status="ok",
        records_analyzed=report.records_analyzed,
        skill_suggestions=list(data.get("skill_suggestions", [])),
        similar_prompts=list(data.get("similar_prompts", [])),
        recurring_conversations=list(data.get("recurring_conversations", [])),
        forgotten_similar=list(data.get("forgotten_similar", [])),
    )


def register(mcp: FastMCP) -> None:
    """Register insights-domain tools."""

    @mcp.tool(
        name="insights_skills",
        tags=READONLY_TAGS | {"insights"},
        description=(
            "Mine the user's recurring requests across conversations and suggest "
            "reusable Skills. Returns skill suggestions, clustered similar prompts, "
            "recurring conversations, and the nearest forgotten-but-similar past "
            "conversations. Needs the graph level (agentgrep[insights-graph]); "
            "reports status='unavailable' with a setup command otherwise."
        ),
    )
    async def insights_skills_tool(
        agent: t.Annotated[
            str,
            Field(description="Agent to analyze, or 'all'."),
        ] = "all",
        limit: t.Annotated[
            int,
            Field(ge=1, le=5000, description="Max records to analyze."),
        ] = 500,
        since: t.Annotated[
            str | None,
            Field(description="Only analyze records on/after this ISO date (e.g. 2026-05-14)."),
        ] = None,
        until: t.Annotated[
            str | None,
            Field(description="Only analyze records on/before this ISO date."),
        ] = None,
    ) -> InsightsSkillsResponse:
        request = InsightsSkillsRequest(
            agent=t.cast("t.Any", agent), limit=limit, since=since, until=until
        )
        return await asyncio.to_thread(_insights_skills_sync, request)

    _ = insights_skills_tool
