"""Export-domain MCP tools.

Render matched records to a portable artifact (ndjson / json / markdown / csv)
over the same frontend-neutral core the CLI ``export`` verb drives. Output is
deterministic; content larger than the inline cap is truncated with a
``truncated`` flag so the response stays under the MCP size ceiling. A
parameterized export resource + defer-fetch ResourceLink are a planned
follow-up.
"""

from __future__ import annotations

import asyncio
import pathlib
import typing as t

from pydantic import Field

from agentgrep.mcp._library import (
    READONLY_TAGS,
    AgentSelector,
    SearchScopeName,
    normalize_agent_selection,
)
from agentgrep.mcp.models import ExportRequestModel, ExportToolResponse

if t.TYPE_CHECKING:
    from fastmcp import FastMCP

    from agentgrep.records import AgentName

_MAX_INLINE_CHARS = 200_000
"""Inline export cap, well under the MCP 512KB response ceiling."""


def _render_export(request: ExportRequestModel) -> tuple[str, int]:
    """Return the rendered export and its record count (blocking)."""
    from agentgrep import export
    from agentgrep._engine.orchestration import run_search_query
    from agentgrep.progress import SearchControl, noop_search_progress
    from agentgrep.records import SearchQuery

    query = SearchQuery(
        terms=tuple(request.terms),
        scope=request.scope,
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=t.cast("tuple[AgentName, ...]", normalize_agent_selection(request.agent)),
        limit=None,
    )
    records = run_search_query(
        pathlib.Path.home(),
        query,
        progress=noop_search_progress(),
        control=SearchControl(),
    )
    count = len(records) if request.limit is None else min(len(records), request.limit)
    if request.format == "ndjson":
        rendered = "".join(
            f"{line}\n"
            for line in export.iter_ndjson_lines(
                records, redact=request.redact, limit=request.limit
            )
        )
    elif request.format == "json":
        rendered = export.render_json(records, redact=request.redact, limit=request.limit)
    elif request.format == "csv":
        rendered = export.render_csv(records, redact=request.redact, limit=request.limit)
    else:
        selected = (
            records
            if request.limit is None
            else sorted(records, key=export.export_total_order_key)[: request.limit]
        )
        rendered = export.render_markdown(
            export.assemble_conversations(selected),
            redact=request.redact,
        )
    return rendered, count


def _export_sync(request: ExportRequestModel) -> ExportToolResponse:
    """Build the export response, truncating oversize content inline."""
    rendered, count = _render_export(request)
    byte_size = len(rendered.encode("utf-8", "surrogatepass"))
    truncated = len(rendered) > _MAX_INLINE_CHARS
    content = rendered[:_MAX_INLINE_CHARS] if truncated else rendered
    return ExportToolResponse(
        request=request,
        format=request.format,
        record_count=count,
        byte_size=byte_size,
        truncated=truncated,
        content=content,
    )


def register(mcp: FastMCP) -> None:
    """Register export-domain tools."""

    @mcp.tool(
        name="export_records",
        tags=READONLY_TAGS | {"export"},
        description=(
            "Export matched records to a portable artifact (ndjson, json, "
            "markdown, or csv). Output is deterministic; oversize results are "
            "truncated inline (narrow the query or lower limit for the rest)."
        ),
    )
    async def export_records_tool(
        terms: t.Annotated[
            list[str] | None,
            Field(
                default=None, description="Terms selecting records to export (empty selects all)."
            ),
        ] = None,
        agent: t.Annotated[
            AgentSelector,
            Field(description="Limit the export to one agent or export all agents."),
        ] = "all",
        scope: t.Annotated[
            SearchScopeName,
            Field(description="Export prompts, conversations, or both."),
        ] = "prompts",
        export_format: t.Annotated[
            t.Literal["ndjson", "json", "markdown", "csv"],
            Field(alias="format", description="Export format."),
        ] = "ndjson",
        redact: t.Annotated[
            bool,
            Field(description="Replace prompt bodies with a stable hash."),
        ] = False,
        limit: t.Annotated[
            int | None,
            Field(default=None, ge=1, description="Maximum number of records to export."),
        ] = None,
    ) -> ExportToolResponse:
        request = ExportRequestModel(
            terms=terms or [],
            agent=agent,
            scope=scope,
            format=export_format,
            redact=redact,
            limit=limit,
        )
        return await asyncio.to_thread(_export_sync, request)

    _ = export_records_tool
