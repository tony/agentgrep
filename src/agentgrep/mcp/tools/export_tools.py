"""Bounded inline record export for MCP clients."""

from __future__ import annotations

import asyncio
import typing as t

import pydantic_core
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent
from pydantic import Field, ValidationError

from agentgrep.mcp._library import (
    DEFAULT_RESPONSE_LIMIT_BYTES,
    READONLY_TAGS,
    TOOL_ANNOTATIONS,
)
from agentgrep.mcp.models import ExportRecordsRequest, ExportRecordsResponse
from agentgrep.mcp.resolver import (
    PhysicalRecordSelection,
    RecordRefResolverError,
    resolve_record_refs,
)
from agentgrep.record_export import ExportError, render_export

if t.TYPE_CHECKING:
    from fastmcp import FastMCP

    from agentgrep.records import SearchRecord


MAX_INLINE_EXPORT_BYTES = 400 * 1024
"""Maximum UTF-8 artifact size returned by ``export_records``."""


def _export_records_sync(request: ExportRecordsRequest) -> ToolResult:
    """Resolve selected records and render one bounded inline artifact."""
    try:
        resolved = resolve_record_refs(request.refs)
    except RecordRefResolverError as exc:
        raise ToolError(str(exc)) from None
    records: list[SearchRecord] = []
    physical_selections: set[PhysicalRecordSelection] = set()
    for index, item in enumerate(resolved, start=1):
        if item.error_message is not None:
            message = f"ref {index} could not be resolved: {item.error_message}"
            raise ToolError(message)
        if item.kind != "search" or len(item.records) != 1:
            message = f"ref {index} does not identify an exportable record"
            raise ToolError(message)
        if item.physical_selection is None:
            message = f"ref {index} has no physical record selection"
            raise ToolError(message)
        if item.physical_selection in physical_selections:
            message = "refs resolve to a duplicate physical record"
            raise ToolError(message)
        physical_selections.add(item.physical_selection)
        records.append(t.cast("SearchRecord", item.records[0]))
    try:
        artifact = render_export(
            records,
            format=request.format,
            selection=request.selection,
            include_bodies=request.include_bodies,
        )
    except ExportError as exc:
        raise ToolError(str(exc)) from exc
    except Exception:
        message = "export artifact could not be rendered"
        raise ToolError(message) from None
    if artifact.byte_count > MAX_INLINE_EXPORT_BYTES:
        message = "export artifact exceeds the 400 KiB inline limit"
        raise ToolError(message)
    response = ExportRecordsResponse(
        format=artifact.format,
        selection=artifact.selection,
        include_bodies=request.include_bodies,
        record_count=artifact.record_count,
        byte_count=artifact.byte_count,
    )
    result = ToolResult(
        content=[TextContent(type="text", text=artifact.text)],
        structured_content=response.model_dump(mode="json"),
    )
    if len(pydantic_core.to_json(result, fallback=str)) > DEFAULT_RESPONSE_LIMIT_BYTES:
        message = "export artifact exceeds the MCP response limit"
        raise ToolError(message)
    return result


def register(mcp: FastMCP) -> None:
    """Register the bounded record-export tool."""

    @mcp.tool(
        name="export_records",
        tags=READONLY_TAGS | {"export"},
        annotations=TOOL_ANNOTATIONS,
        output_schema=ExportRecordsResponse.model_json_schema(),
        description=(
            "Return selected refs as one NDJSON or Markdown TextContent artifact "
            "with structured export metadata."
        ),
    )
    async def export_records_tool(
        # FastMCP logs pre-handler validation inputs. Publish the exact wire
        # schema here, then validate inside the redacted tool boundary so an
        # oversized ref collection cannot put opaque source coordinates in logs.
        refs: t.Annotated[
            t.Any,
            Field(
                description="One to 20 opaque refs returned by search.",
                json_schema_extra={
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 20,
                },
            ),
        ],
        format: t.Annotated[  # noqa: A002 - required MCP argument name.
            t.Literal["ndjson", "markdown"],
            Field(description="Inline artifact format."),
        ] = "ndjson",
        selection: t.Annotated[
            t.Literal["records", "thread"],
            Field(description="Export flat records or one observed thread."),
        ] = "records",
        include_bodies: t.Annotated[
            bool,
            Field(description="Include prompt/history text in the artifact."),
        ] = False,
    ) -> ToolResult:
        try:
            request = ExportRecordsRequest(
                refs=refs,
                format=format,
                selection=selection,
                include_bodies=include_bodies,
            )
        except ValidationError:
            message = "invalid export request"
            raise ToolError(message) from None
        return await asyncio.to_thread(_export_records_sync, request)

    _ = export_records_tool
