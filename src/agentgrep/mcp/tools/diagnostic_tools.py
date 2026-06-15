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


def _validate_query_language(query: str) -> tuple[bool, str | None]:
    """Parse + compile a query-language string; return (valid, error)."""
    from agentgrep.query import (
        QueryCompileError,
        QueryParseError,
        compile_query,
        default_registry,
        parse_query,
    )

    registry = default_registry()
    try:
        ast = parse_query(query, registry)
        _ = compile_query(ast, registry)
    except (QueryParseError, QueryCompileError) as exc:
        return False, str(exc)
    return True, None


def _validate_query_sync(request: ValidateQueryRequest) -> ValidateQueryResponse:
    """Dry-run a query against sample text and/or validate query syntax."""
    query_valid: bool | None = None
    query_error: str | None = None
    if request.query is not None:
        query_valid, query_error = _validate_query_language(request.query)

    matches = False
    regex_valid = True
    if request.terms:
        query = agentgrep.SearchQuery(
            terms=tuple(request.terms),
            scope="all",
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
                query_valid=query_valid,
                error_message=query_error or str(exc),
            )
    return ValidateQueryResponse(
        matches=matches,
        regex_valid=regex_valid,
        query_valid=query_valid,
        error_message=query_error,
    )


def register(mcp: FastMCP) -> None:
    """Register diagnostic-domain tools."""

    @mcp.tool(
        name="validate_query",
        tags=READONLY_TAGS | {"diagnostic"},
        description=(
            "Dry-run terms against sample text and/or validate query-language "
            "syntax (field predicates, booleans, phrases) without searching files."
        ),
    )
    async def validate_query_tool(
        terms: t.Annotated[
            list[str] | None,
            Field(
                default=None,
                description="Literal/regex terms to test against sample_text.",
            ),
        ] = None,
        query: t.Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Query-language string to parse and compile; reports "
                    "query_valid and any parse/compile error."
                ),
            ),
        ] = None,
        sample_text: t.Annotated[
            str,
            Field(description="Sample text to test terms against."),
        ] = "",
        case_sensitive: t.Annotated[
            bool,
            Field(description="Perform case-sensitive matching."),
        ] = False,
    ) -> ValidateQueryResponse:
        request = ValidateQueryRequest(
            terms=terms,
            query=query,
            sample_text=sample_text,
            case_sensitive=case_sensitive,
        )
        return await asyncio.to_thread(_validate_query_sync, request)

    _ = validate_query_tool
