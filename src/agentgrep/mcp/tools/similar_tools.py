"""Similarity-domain MCP tools."""

from __future__ import annotations

import asyncio
import pathlib
import typing as t

from pydantic import Field

from agentgrep.mcp._library import (
    READONLY_TAGS,
    AgentSelector,
    SearchRecordLike,
    SearchScopeName,
    normalize_agent_selection,
)
from agentgrep.mcp.models import (
    SearchRecordModel,
    SimilarMatchModel,
    SimilarRequestModel,
    SimilarToolResponse,
)

if t.TYPE_CHECKING:
    from fastmcp import FastMCP

    from agentgrep.records import AgentName


def _find_similar_sync(request: SimilarRequestModel) -> SimilarToolResponse:
    """Rank the scope-narrowed corpus against the seed text (blocking)."""
    from agentgrep.similar import run_find_similar

    matches = run_find_similar(
        pathlib.Path.home(),
        seed_text=request.text,
        agents=t.cast("tuple[AgentName, ...]", normalize_agent_selection(request.agent)),
        scope=request.scope,
        top_k=request.top_k,
        threshold=request.threshold,
        exclude_exact=request.exclude_exact,
    )
    return SimilarToolResponse(
        request=request,
        results=[
            SimilarMatchModel(
                score=score,
                record=SearchRecordModel.from_record(t.cast("SearchRecordLike", record)),
            )
            for record, score in matches
        ],
    )


def register(mcp: FastMCP) -> None:
    """Register similarity-domain tools."""

    @mcp.tool(
        name="find_similar",
        tags=READONLY_TAGS | {"similar"},
        description=(
            "Find records most similar to a seed text across every backend, "
            "ranked by a zero-dependency similarity score (0..1). Seeding by a "
            "record id is a planned addition."
        ),
    )
    async def find_similar_tool(
        text: t.Annotated[
            str,
            Field(description="Seed text to find neighbors of."),
        ],
        agent: t.Annotated[
            AgentSelector,
            Field(description="Limit the search to one agent or search all agents."),
        ] = "all",
        scope: t.Annotated[
            SearchScopeName,
            Field(description="Search prompts, conversations, or both."),
        ] = "prompts",
        top_k: t.Annotated[
            int,
            Field(default=20, ge=1, le=100, description="Maximum neighbors to return."),
        ] = 20,
        threshold: t.Annotated[
            float,
            Field(default=0.0, ge=0.0, le=1.0, description="Minimum similarity in 0..1."),
        ] = 0.0,
        exclude_exact: t.Annotated[
            bool,
            Field(description="Drop neighbors whose text is identical to the seed."),
        ] = False,
    ) -> SimilarToolResponse:
        request = SimilarRequestModel(
            text=text,
            agent=agent,
            scope=scope,
            top_k=top_k,
            threshold=threshold,
            exclude_exact=exclude_exact,
        )
        return await asyncio.to_thread(_find_similar_sync, request)

    _ = find_similar_tool
