"""Diagnostic-domain MCP tools."""

from __future__ import annotations

import asyncio
import re
import typing as t

from pydantic import Field

from agentgrep.mcp._library import READONLY_TAGS, agentgrep
from agentgrep.mcp.models import ValidateQueryRequest, ValidateQueryResponse

if t.TYPE_CHECKING:
    from fastmcp import FastMCP


def _validate_query_sync(request: ValidateQueryRequest) -> ValidateQueryResponse:
    """Dry-run a ``SearchQuery`` against sample text without searching files."""
    query = agentgrep.SearchQuery(
        terms=tuple(request.terms),
        search_type="all",
        any_term=False,
        regex=False,
        case_sensitive=request.case_sensitive,
        agents=agentgrep.AGENT_CHOICES,
        limit=None,
    )
    try:
        matches = agentgrep.matches_text(request.sample_text, query)
    except re.error as exc:
        return ValidateQueryResponse(
            matches=False,
            regex_valid=False,
            error_message=str(exc),
        )
    return ValidateQueryResponse(matches=matches, regex_valid=True)


def register(mcp: FastMCP) -> None:
    """Register diagnostic-domain tools."""

    @mcp.tool(
        name="validate_query",
        tags=READONLY_TAGS | {"diagnostic"},
        description="Dry-run a query against sample text without searching files.",
    )
    async def validate_query_tool(
        terms: t.Annotated[
            list[str],
            Field(
                min_length=1,
                description="One or more literal search terms (AND-matched).",
            ),
        ],
        sample_text: t.Annotated[
            str,
            Field(description="Sample text to test the query against."),
        ],
        case_sensitive: t.Annotated[
            bool,
            Field(description="Perform case-sensitive matching."),
        ] = False,
    ) -> ValidateQueryResponse:
        request = ValidateQueryRequest(
            terms=terms,
            sample_text=sample_text,
            case_sensitive=case_sensitive,
        )
        return await asyncio.to_thread(_validate_query_sync, request)

    _ = validate_query_tool
