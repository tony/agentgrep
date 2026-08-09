"""Read-only MCP tools for DB and insight artifacts."""

from __future__ import annotations

import asyncio
import functools
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
DEFAULT_SUGGESTIONS_LIST_LIMIT = 50


def _selected_db_path(db_path: str | None) -> pathlib.Path:
    """Return the selected agentgrep db path without creating it."""
    from agentgrep.db import default_db_path

    if db_path is not None:
        return pathlib.Path(db_path).expanduser()
    return default_db_path()


def _empty_insights_response(limit: int) -> InsightsListResponse:
    """Return the empty insights payload for missing or unreadable caches."""
    return InsightsListResponse(
        limit=limit,
        variant_edges_total=0,
        variant_edges_truncated=False,
        variant_edges=[],
        omission_findings_total=0,
        omission_findings_truncated=False,
        omission_findings=[],
    )


def _empty_suggestions_response(limit: int) -> SuggestionsListResponse:
    """Return the empty suggestions payload for missing or unreadable caches."""
    return SuggestionsListResponse(
        limit=limit,
        suggestions_total=0,
        suggestions_truncated=False,
        suggestions=[],
    )


def _insights_list_sync(
    db_path: str | None,
    *,
    limit: int = DEFAULT_INSIGHTS_LIST_LIMIT,
) -> InsightsListResponse:
    """Return persisted insight artifacts without running new insights."""
    import sqlite3

    from agentgrep.db import DbRuntime
    from agentgrep.insights import InsightEngine

    path = _selected_db_path(db_path)
    if not path.exists():
        return _empty_insights_response(limit)
    try:
        with DbRuntime.open_readonly(path) as runtime:
            engine = InsightEngine(runtime.store)
            variant_edges_total = engine.count_variant_edges()
            omission_findings_total = engine.count_omission_findings()
            variant_edges = engine.list_variant_edges(limit=limit)
            omission_findings = engine.list_omission_findings(limit=limit)
    except sqlite3.DatabaseError:
        # A foreign or corrupt file gets the same empty payload as a
        # missing one — matching db_status and the CLI read surfaces.
        return _empty_insights_response(limit)
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


def _suggestions_list_sync(
    db_path: str | None,
    *,
    limit: int = DEFAULT_SUGGESTIONS_LIST_LIMIT,
) -> SuggestionsListResponse:
    """Return a bounded page of persisted review-only suggestions."""
    import sqlite3

    from agentgrep.db import DbRuntime
    from agentgrep.suggestions import SuggestionEngine

    path = _selected_db_path(db_path)
    if not path.exists():
        return _empty_suggestions_response(limit)
    try:
        with DbRuntime.open_readonly(path) as runtime:
            engine = SuggestionEngine(runtime.store)
            suggestions_total = engine.count_suggestions()
            suggestions = engine.list_suggestions(limit=limit)
    except sqlite3.DatabaseError:
        # Same empty payload as a missing file; see _insights_list_sync.
        return _empty_suggestions_response(limit)
    return SuggestionsListResponse(
        limit=limit,
        suggestions_total=suggestions_total,
        suggestions_truncated=suggestions_total > len(suggestions),
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
            for suggestion in suggestions
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
        limit: t.Annotated[
            int,
            Field(
                default=DEFAULT_SUGGESTIONS_LIST_LIMIT,
                ge=1,
                description="Maximum suggestions to return.",
            ),
        ] = DEFAULT_SUGGESTIONS_LIST_LIMIT,
    ) -> SuggestionsListResponse:
        return await asyncio.to_thread(
            functools.partial(_suggestions_list_sync, db_path, limit=limit),
        )

    _ = suggestions_list_tool
