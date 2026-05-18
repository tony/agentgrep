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
        any_term=request.any_term,
        regex=request.regex,
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
                description="One or more literal or regex search terms.",
            ),
        ],
        sample_text: t.Annotated[
            str,
            Field(description="Sample text to test the query against."),
        ],
        regex: t.Annotated[
            bool,
            Field(description="Treat terms as regular expressions."),
        ] = False,
        case_sensitive: t.Annotated[
            bool,
            Field(description="Perform case-sensitive matching."),
        ] = False,
        any_term: t.Annotated[
            bool,
            Field(description="Match any term instead of requiring all terms."),
        ] = False,
    ) -> ValidateQueryResponse:
        request = ValidateQueryRequest(
            terms=terms,
            sample_text=sample_text,
            regex=regex,
            case_sensitive=case_sensitive,
            any_term=any_term,
        )
        return await asyncio.to_thread(_validate_query_sync, request)

    _ = validate_query_tool
