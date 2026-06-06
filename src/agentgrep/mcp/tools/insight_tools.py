"""Read-only MCP tools for DB and insight artifacts."""

from __future__ import annotations

import asyncio
import pathlib
import typing as t

from pydantic import Field

from agentgrep.mcp._library import READONLY_TAGS
from agentgrep.mcp.models import (
    InsightsListResponse,
    OmissionFindingModel,
    SuggestionArtifactModel,
    SuggestionsListResponse,
    VariantEdgeModel,
)

if t.TYPE_CHECKING:
    from fastmcp import FastMCP

DEFAULT_INSIGHTS_LIST_LIMIT = 50


def _selected_db_path(db_path: str | None) -> pathlib.Path:
    """Return the selected agentgrep db path without creating it."""
    from agentgrep.db import default_db_path

    if db_path is not None:
        return pathlib.Path(db_path).expanduser()
    return default_db_path()


def _insights_list_sync(
    db_path: str | None,
    *,
    limit: int = DEFAULT_INSIGHTS_LIST_LIMIT,
) -> InsightsListResponse:
    """Return persisted insight artifacts without running new insights."""
    from agentgrep.db import DbRuntime
    from agentgrep.insights import InsightEngine

    path = _selected_db_path(db_path)
    if not path.exists():
        return InsightsListResponse(
            limit=limit,
            variant_edges_total=0,
            variant_edges_truncated=False,
            variant_edges=[],
            omission_findings_total=0,
            omission_findings_truncated=False,
            omission_findings=[],
        )
    engine = InsightEngine(DbRuntime.open(path).store)
    variant_edges_total = engine.count_variant_edges()
    omission_findings_total = engine.count_omission_findings()
    variant_edges = engine.list_variant_edges(limit=limit)
    omission_findings = engine.list_omission_findings(limit=limit)
    return InsightsListResponse(
        limit=limit,
        variant_edges_total=variant_edges_total,
        variant_edges_truncated=variant_edges_total > len(variant_edges),
        variant_edges=[
            VariantEdgeModel(
                edge_id=edge.edge_id,
                run_id=edge.run_id,
                left_record_id=edge.left_record_id,
                right_record_id=edge.right_record_id,
                variant_type=edge.variant_type,
                confidence=edge.confidence,
                explanation=edge.explanation,
            )
            for edge in variant_edges
        ],
        omission_findings_total=omission_findings_total,
        omission_findings_truncated=omission_findings_total > len(omission_findings),
        omission_findings=[
            OmissionFindingModel(
                finding_id=finding.finding_id,
                run_id=finding.run_id,
                target_path=str(finding.target_path),
                representative_record_id=finding.representative_record_id,
                confidence=finding.confidence,
                rationale=finding.rationale,
            )
            for finding in omission_findings
        ],
    )


def _suggestions_list_sync(db_path: str | None) -> SuggestionsListResponse:
    """Return persisted review-only suggestion artifacts."""
    from agentgrep.db import DbRuntime
    from agentgrep.suggestions import SuggestionEngine

    path = _selected_db_path(db_path)
    if not path.exists():
        return SuggestionsListResponse(suggestions=[])
    engine = SuggestionEngine(DbRuntime.open(path).store)
    return SuggestionsListResponse(
        suggestions=[
            SuggestionArtifactModel(
                suggestion_id=suggestion.suggestion_id,
                run_id=suggestion.run_id,
                target_path=str(suggestion.target_path),
                surface_kind=suggestion.surface_kind,
                title=suggestion.title,
                body=suggestion.body,
                confidence=suggestion.confidence,
                status=suggestion.status,
                rationale=suggestion.rationale,
                reload_note=suggestion.reload_note,
            )
            for suggestion in engine.list_suggestions()
        ],
    )


def register(mcp: FastMCP) -> None:
    """Register read-only insight and suggestion tools."""

    @mcp.tool(
        name="insights_list",
        tags=READONLY_TAGS | {"insights"},
        description="List persisted deterministic insight artifacts.",
    )
    async def insights_list_tool(
        db_path: t.Annotated[
            str | None,
            Field(default=None, description="Optional agentgrep db path."),
        ] = None,
        limit: t.Annotated[
            int,
            Field(
                default=DEFAULT_INSIGHTS_LIST_LIMIT,
                ge=1,
                description="Maximum rows to return per insight family.",
            ),
        ] = DEFAULT_INSIGHTS_LIST_LIMIT,
    ) -> InsightsListResponse:
        return await asyncio.to_thread(_insights_list_sync, db_path, limit=limit)

    _ = insights_list_tool

    @mcp.tool(
        name="suggestions_list",
        tags=READONLY_TAGS | {"insights", "suggestions"},
        description="List persisted review-only instruction suggestions.",
    )
    async def suggestions_list_tool(
        db_path: t.Annotated[
            str | None,
            Field(default=None, description="Optional agentgrep db path."),
        ] = None,
    ) -> SuggestionsListResponse:
        return await asyncio.to_thread(_suggestions_list_sync, db_path)

    _ = suggestions_list_tool
