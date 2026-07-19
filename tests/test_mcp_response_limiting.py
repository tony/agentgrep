"""Response-limiting contracts for the ``agentgrep`` MCP server."""

from __future__ import annotations

import logging
import typing as t

import mcp.types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from fastmcp.tools.base import ToolResult
from pydantic import BaseModel

from agentgrep.mcp.middleware import (
    AgentgrepAuditMiddleware,
    AgentgrepResponseLimitingMiddleware,
)
from agentgrep.mcp.server import build_mcp_server

pytestmark = pytest.mark.mcp

_TEST_RESPONSE_LIMIT_BYTES = 160
_OVERSIZED_TEXT = "oversized:" + ("x" * 4_096)


class _OversizedToolPayload(BaseModel):
    """Structured payload large enough to trigger the test limiter."""

    text: str


class _AuditLogRecord(logging.LogRecord):
    """Typed audit extras asserted by this module."""

    agentgrep_tool: str
    agentgrep_outcome: str
    agentgrep_error_type: str


def _configured_response_limiter(server: FastMCP) -> ResponseLimitingMiddleware:
    """Return the response limiter installed on ``server``."""
    return next(
        middleware
        for middleware in server.middleware
        if isinstance(middleware, ResponseLimitingMiddleware)
    )


def _tool_context() -> MiddlewareContext[mt.CallToolRequestParams]:
    """Return the middleware context shared by direct tool-call contracts."""
    return MiddlewareContext(
        message=mt.CallToolRequestParams(
            name="oversized_response_probe",
            arguments={},
        ),
        method="tools/call",
    )


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[_AuditLogRecord]:
    """Return only records emitted by the agentgrep audit logger."""
    return t.cast(
        "list[_AuditLogRecord]",
        [record for record in caplog.records if record.name == "agentgrep.audit"],
    )


async def test_limiter_marks_truncation_as_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Truncated results become metadata-preserving, audited errors."""
    limiter = AgentgrepResponseLimitingMiddleware(max_size=_TEST_RESPONSE_LIMIT_BYTES)
    audit = AgentgrepAuditMiddleware()
    metadata = {"request_id": "preserved"}
    original = ToolResult(
        content=[mt.TextContent(type="text", text=_OVERSIZED_TEXT)],
        structured_content={"text": _OVERSIZED_TEXT},
        meta=metadata,
    )

    async def _call_next(
        context: MiddlewareContext[mt.CallToolRequestParams],
    ) -> ToolResult:
        return original

    async def _call_limiter(
        context: MiddlewareContext[mt.CallToolRequestParams],
    ) -> ToolResult:
        return await limiter.on_call_tool(context, _call_next)

    with caplog.at_level(logging.INFO, logger="agentgrep.audit"):
        result = await audit.on_call_tool(_tool_context(), _call_limiter)

    records = _audit_records(caplog)
    assert len(result.content) == 1
    content = result.content[0]
    assert isinstance(content, mt.TextContent)
    assert content.text.endswith(limiter.truncation_suffix)
    assert len(content.text.encode("utf-8")) <= _TEST_RESPONSE_LIMIT_BYTES
    assert result.meta == metadata
    assert result.structured_content is None
    assert result.is_error is True
    assert len(records) == 1
    assert records[0].agentgrep_outcome == "error"
    assert records[0].agentgrep_error_type == "ToolResultError"


@pytest.mark.slow
async def test_client_accepts_truncated_structured_tool_as_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The configured chain exposes and audits truncation as one error."""
    server = build_mcp_server()
    limiter = _configured_response_limiter(server)
    limiter.max_size = _TEST_RESPONSE_LIMIT_BYTES

    def _oversized_structured_tool() -> _OversizedToolPayload:
        return _OversizedToolPayload(text=_OVERSIZED_TEXT)

    server.tool(name="oversized_response_probe")(_oversized_structured_tool)

    with caplog.at_level(logging.INFO, logger="agentgrep.audit"):
        async with Client(server) as client:
            tools = await client.list_tools_mcp()
            probe_tool = next(
                tool for tool in tools.tools if tool.name == "oversized_response_probe"
            )
            assert probe_tool.outputSchema is not None
            result = await client.call_tool_mcp("oversized_response_probe", {})

    records = _audit_records(caplog)
    assert result.isError is True
    assert result.structuredContent is None
    assert len(records) == 1
    assert records[0].agentgrep_tool == "oversized_response_probe"
    assert records[0].agentgrep_outcome == "error"
    assert records[0].agentgrep_error_type == "ToolResultError"
