"""Response-limiting contracts for the ``agentgrep`` MCP server."""

from __future__ import annotations

import json
import logging
import pathlib
import typing as t

import mcp.types as mt
import pydantic_core
import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from fastmcp.tools.base import ToolResult
from pydantic import BaseModel

from agentgrep.mcp.middleware import (
    AgentgrepAuditMiddleware,
    AgentgrepResponseLimitingMiddleware,
    _truncated_search_response,
)
from agentgrep.mcp.models import (
    RunStatusModel,
    SearchRequestModel,
    SearchToolResponse,
)
from agentgrep.mcp.server import build_mcp_server
from agentgrep.mcp.tools.search_tools import _search_async

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


def _tool_context(
    name: str = "oversized_response_probe",
) -> MiddlewareContext[mt.CallToolRequestParams]:
    """Return the middleware context shared by direct tool-call contracts."""
    return MiddlewareContext(
        message=mt.CallToolRequestParams(
            name=name,
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
    assert content.text == "[truncated]"
    assert len(pydantic_core.to_json(result, fallback=str)) <= limiter.max_size
    assert result.meta == metadata
    assert result.structured_content is None
    assert result.is_error is True
    assert len(records) == 1
    assert records[0].agentgrep_outcome == "error"
    assert records[0].agentgrep_error_type == "ToolResultError"


@pytest.mark.parametrize(
    ("state", "reason", "condition"),
    [
        ("bounded", "result_limit", "result_limit"),
        (
            "approximate",
            "heuristic_candidate_selection",
            "heuristic_candidate_selection",
        ),
    ],
)
async def test_search_truncation_precedes_lower_priority_conditions(
    codex_transcript_home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    state: t.Literal["bounded", "approximate"],
    reason: str,
    condition: str,
) -> None:
    """Keep sink-owned truncation in the engine's stable condition order."""
    monkeypatch.setattr(
        pathlib.Path,
        "home",
        classmethod(lambda _cls: codex_transcript_home),
    )
    response = await _search_async(
        SearchRequestModel(
            terms=["deep-only"],
            agent="codex",
            scope="prompts",
            case_sensitive=False,
            effort="exhaustive",
            limit=20,
        ),
    )
    response = response.model_copy(
        update={
            "status": RunStatusModel(
                state=state,
                reason=reason,
                conditions=[condition],
            ),
        },
    )

    truncated = _truncated_search_response(response, result_count=0)

    assert truncated.status.state == "truncated"
    assert truncated.status.reason == "response_truncated"
    assert truncated.status.conditions == ["response_truncated", condition]


async def test_search_limiter_preserves_a_structured_partial_envelope(
    codex_transcript_home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fit whole search records while retaining machine-readable lifecycle state."""
    monkeypatch.setattr(
        pathlib.Path,
        "home",
        classmethod(lambda _cls: codex_transcript_home),
    )
    base = await _search_async(
        SearchRequestModel(
            terms=["deep-only"],
            agent="codex",
            scope="prompts",
            case_sensitive=False,
            effort="exhaustive",
            limit=20,
        ),
    )
    first = base.results[0].model_copy(
        update={"ref": "first", "text": "first:" + ("a" * 4_096)},
    )
    second = first.model_copy(
        update={"ref": "second", "text": "second:" + ("b" * 4_096)},
    )
    response = base.model_copy(
        update={
            "results": [first, second],
            "page": base.page.model_copy(update={"count": 2}),
            "stats": base.stats.model_copy(update={"matched": 2, "emitted": 2}),
        },
    )
    metadata = {"request_id": "preserved"}
    original = ToolResult(
        content=response,
        structured_content=response,
        meta=metadata,
    )
    original_size = len(pydantic_core.to_json(original, fallback=str))
    limiter = AgentgrepResponseLimitingMiddleware(max_size=original_size - 1)
    audit = AgentgrepAuditMiddleware()

    async def _call_next(
        context: MiddlewareContext[mt.CallToolRequestParams],
    ) -> ToolResult:
        return original

    async def _call_limiter(
        context: MiddlewareContext[mt.CallToolRequestParams],
    ) -> ToolResult:
        return await limiter.on_call_tool(context, _call_next)

    with caplog.at_level(logging.INFO, logger="agentgrep.audit"):
        result = await audit.on_call_tool(_tool_context("search"), _call_limiter)

    fitted = SearchToolResponse.model_validate(result.structured_content)
    content = result.content[0]
    records = _audit_records(caplog)
    assert isinstance(content, mt.TextContent)
    assert json.loads(content.text) == result.structured_content
    assert len(pydantic_core.to_json(result, fallback=str)) <= limiter.max_size
    assert [record.ref for record in fitted.results] == ["first"]
    assert fitted.page.count == fitted.stats.emitted == len(fitted.results)
    assert fitted.status.state == "truncated"
    assert fitted.status.reason == "response_truncated"
    assert fitted.status.conditions == ["response_truncated"]
    assert fitted.effort.completed is None
    assert fitted.outcome == "undetermined"
    assert fitted.diagnostics[-1].code == "response_truncated"
    assert result.meta == metadata
    assert result.is_error is False
    assert len(records) == 1
    assert records[0].agentgrep_outcome == "ok"


async def test_search_limiter_falls_back_when_no_envelope_fits(
    codex_transcript_home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return a bounded error when even a zero-result envelope is too large."""
    monkeypatch.setattr(
        pathlib.Path,
        "home",
        classmethod(lambda _cls: codex_transcript_home),
    )
    response = await _search_async(
        SearchRequestModel(
            terms=["deep-only"],
            agent="codex",
            scope="prompts",
            case_sensitive=False,
            effort="exhaustive",
            limit=20,
        ),
    )
    original = ToolResult(content=response, structured_content=response)
    limiter = AgentgrepResponseLimitingMiddleware(max_size=100)

    async def _call_next(
        context: MiddlewareContext[mt.CallToolRequestParams],
    ) -> ToolResult:
        return original

    result = await limiter.on_call_tool(_tool_context("search"), _call_next)

    assert result.is_error is True
    assert result.structured_content is None
    assert len(pydantic_core.to_json(result, fallback=str)) <= limiter.max_size


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
            # The limiter may truncate this tool's result to plain text, so
            # FastMCP hides the schema it could not then honour.
            assert probe_tool.output_schema is None
            result = await client.call_tool_mcp("oversized_response_probe", {})

    records = _audit_records(caplog)
    assert result.is_error is True
    assert result.structured_content is None
    assert len(records) == 1
    assert records[0].agentgrep_tool == "oversized_response_probe"
    assert records[0].agentgrep_outcome == "error"
    assert records[0].agentgrep_error_type == "ToolResultError"


@pytest.mark.slow
async def test_client_accepts_semantically_truncated_search(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep search schema-valid when one whole record exceeds the byte budget."""
    history = tmp_path / ".codex" / "history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(
        json.dumps(
            {
                "session_id": "00000000-0000-0000-0000-000000000300",
                "ts": 1_785_000_000,
                "text": "large-needle " + ("x" * 300_000),
            },
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pathlib.Path,
        "home",
        classmethod(lambda _cls: tmp_path),
    )

    async with Client(build_mcp_server()) as client:
        result = await client.call_tool_mcp(
            "search",
            {"terms": ["large-needle"], "agent": "codex"},
        )

    response = SearchToolResponse.model_validate(result.structured_content)
    assert result.is_error is False
    assert response.results == []
    assert response.page.count == response.stats.emitted == 0
    assert response.status.state == "truncated"
    assert response.status.reason == "response_truncated"
    assert response.effort.completed is None
    assert response.outcome == "undetermined"


@pytest.mark.slow
async def test_response_cache_covers_resources_and_never_tools() -> None:
    """Only resource reads are cached; tool results must not be.

    A tool result cached on a timer would undercut ``SourceScanCache``,
    which invalidates exactly on file fingerprints. The cache has no
    invalidation hook of its own, so its TTL is the whole staleness story
    and it must not reach anything that already caches correctly.
    """
    from fastmcp.server.middleware.caching import ResponseCachingMiddleware

    from agentgrep.mcp.server import build_mcp_server

    server = build_mcp_server()
    cache = next(m for m in server.middleware if isinstance(m, ResponseCachingMiddleware))

    async with Client(server) as client:
        await client.read_resource("agentgrep://sources")
        await client.read_resource("agentgrep://sources")
        await client.call_tool("list_stores", {}, raise_on_error=False)
        await client.call_tool("list_stores", {}, raise_on_error=False)
        await client.list_tools()

    stats = cache.statistics()
    assert stats.read_resource is not None, "resource reads should be cached"
    assert stats.read_resource.get.hit >= 1, "the repeat read should hit the cache"
    assert stats.call_tool is None, "tool results must never be cached"
    assert stats.list_tools is None, "listings must never be cached"
