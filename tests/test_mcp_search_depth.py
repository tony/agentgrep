"""MCP contracts for fast and deep search."""

from __future__ import annotations

import json
import pathlib
import typing as t

import mcp.types as mt
import pytest
from fastmcp import Client
from fastmcp.exceptions import McpError, ToolError

from agentgrep.events import SearchFinished, SearchStarted
from agentgrep.mcp import refs
from agentgrep.mcp.models import (
    NormalizedSearchRequestModel,
    SearchRequestModel,
    SearchToolResponse,
)
from agentgrep.mcp.server import build_mcp_server
from agentgrep.mcp.tools.search_tools import _search_async
from agentgrep.records import SearchQuery
from agentgrep.results import RunCoverage, build_search_summary

pytestmark = pytest.mark.mcp

_LEGACY_FIND_CURSOR = (
    "agcur1:"
    "eyJhZ2VudCI6ImFsbCIsImxpbWl0IjoyMCwib2Zmc2V0IjoyMCwicGF0dGVybiI6InNl"
    "c3Npb25zIiwidG9vbCI6ImZpbmQiLCJ2IjoxfQ"
)


def test_normalized_request_accepts_engine_relevance_order() -> None:
    """Keep the MCP summary adapter aligned with engine-owned ordering."""
    query = SearchQuery(
        terms=("needle",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=1,
        effort="prompt",
        order="relevance",
    )
    summary = build_search_summary(
        query,
        effort="prompt",
        coverage=RunCoverage(
            sources_discovered=0,
            sources_eligible=0,
            sources_planned=0,
            sources_attempted=0,
            sources_completed=0,
            sources_bounded=0,
            sources_skipped=0,
            sources_unsupported=0,
            sources_failed=0,
            sources_cancelled=0,
            records_seen=0,
            matches_seen=0,
        ),
        match_count=0,
        elapsed_seconds=0.0,
    )

    request = NormalizedSearchRequestModel.from_summary(summary)

    assert request.order == "relevance"


async def test_mcp_targeted_effort_routes_transcript_backends(
    codex_transcript_home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch the MCP surface bypassing the engine's default read policy."""
    monkeypatch.setattr(
        pathlib.Path,
        "home",
        classmethod(lambda _cls: codex_transcript_home),
    )

    session_id = "00000000-0000-0000-0000-000000000200"
    session = next((codex_transcript_home / ".codex" / "sessions").rglob("*.jsonl"))
    _ = session.rename(
        session.with_name(f"rollout-2026-05-17T12-00-00-{session_id}.jsonl"),
    )
    history = codex_transcript_home / ".codex" / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "ts": 1_770_000_000,
                "text": "deep-only",
            },
        )
        + "\n",
        encoding="utf-8",
    )

    async def search(effort: t.Literal["targeted"] | None) -> SearchToolResponse:
        return await _search_async(
            SearchRequestModel(
                terms=["deep-only"],
                agent="codex",
                scope="prompts",
                case_sensitive=False,
                effort=effort,
                limit=20,
            ),
        )

    fast = await search(None)
    targeted = await search("targeted")

    assert [record.text for record in fast.results] == ["deep-only"]
    assert fast.effort.requested == "prompt"
    assert fast.effort.completed == "prompt"
    assert fast.outcome == "matches"
    assert fast.status.state == "complete"
    assert fast.status.conditions == []
    assert fast.coverage.sources_planned == 1
    assert fast.diagnostics == []
    assert {action.action_id for action in fast.next_actions} == {
        "search.targeted",
        "search.exhaustive",
    }
    assert {record.text for record in targeted.results} == {
        "deep-only",
        "deep-only prompt",
    }
    assert targeted.request.scope == "all"
    assert targeted.request.conversation_limit == 25
    assert targeted.effort.requested == "targeted"
    assert targeted.effort.completed == "targeted"
    assert targeted.status.state == "approximate"
    assert targeted.outcome == "matches"


async def test_mcp_prompt_scope_predicate_does_not_enable_transcript_reads(
    codex_transcript_home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep an inline prompt constraint inside the fast read policy."""
    monkeypatch.setattr(
        pathlib.Path,
        "home",
        classmethod(lambda _cls: codex_transcript_home),
    )

    response = await _search_async(
        SearchRequestModel(
            terms=["scope:prompts deep-only"],
            agent="codex",
            scope="prompts",
            case_sensitive=False,
            limit=20,
        ),
    )

    assert response.results == []


@pytest.mark.parametrize(
    ("terms", "scope_provenance"),
    [
        (["needle scope:prompts"], "inferred"),
        (["needle"], "explicit"),
    ],
    ids=["inline", "explicit"],
)
async def test_mcp_prompt_scope_rejects_targeted_before_iteration(
    monkeypatch: pytest.MonkeyPatch,
    terms: list[str],
    scope_provenance: t.Literal["inferred", "explicit"],
) -> None:
    """Reject every prompt-depth conflict before opening the event stream."""
    iteration_calls = 0

    async def forbidden_stream(
        *_args: object,
        **_kwargs: object,
    ) -> t.AsyncIterator[SearchStarted]:
        nonlocal iteration_calls
        iteration_calls += 1
        yield SearchStarted(source_count=0)

    monkeypatch.setattr(
        "agentgrep.mcp.tools.search_tools.agentgrep.aiter_search_events",
        forbidden_stream,
    )

    with pytest.raises(McpError) as raised:
        await _search_async(
            SearchRequestModel(
                terms=terms,
                agent="codex",
                scope="prompts",
                scope_provenance=scope_provenance,
                case_sensitive=False,
                effort="targeted",
                limit=20,
            ),
        )

    assert raised.value.error.code == mt.INVALID_PARAMS
    assert (
        raised.value.error.message
        == "Invalid params: targeted effort requires conversation or all scope"
    )
    assert iteration_calls == 0


async def test_mcp_response_echoes_the_engine_normalized_request(
    codex_transcript_home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose compiled scope and effort instead of stale raw input defaults."""
    monkeypatch.setattr(
        pathlib.Path,
        "home",
        classmethod(lambda _cls: codex_transcript_home),
    )

    response = await _search_async(
        SearchRequestModel(
            terms=["scope:conversations deep-only"],
            agent="codex",
            scope="prompts",
            case_sensitive=False,
            limit=20,
        ),
    )

    assert response.request.scope == "conversations"
    assert response.request.scope_provenance == "explicit"
    assert response.request.effort == "exhaustive"
    assert response.request.agents == ["codex"]
    assert response.request.order == "newest"
    assert response.page.limit == 20
    request_schema = response.request.model_json_schema()
    assert "limit" not in request_schema["properties"]
    assert "deep" not in request_schema["properties"]
    assert "cursor" not in request_schema["properties"]


async def test_mcp_discovery_failure_uses_privacy_safe_engine_summary(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not expose discovery exception text through structured tool errors."""
    private_detail = f"sensitive discovery failure at {tmp_path}"

    def fail_discovery(*_args: object, **_kwargs: object) -> list[object]:
        raise OSError(private_detail)

    monkeypatch.setattr(
        "agentgrep._engine.search.discover_sources_for_search",
        fail_discovery,
    )
    response = await _search_async(
        SearchRequestModel(
            terms=["missing"],
            agent="codex",
            scope="prompts",
            case_sensitive=False,
            limit=20,
        ),
    )

    assert response.status.state == "failed"
    assert response.status.reason == "engine_failure"
    assert [item.code for item in response.diagnostics] == ["engine_failure"]
    assert all(private_detail not in item.message for item in response.diagnostics)
    assert response.results == []


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("missing_terminal", "ended without SearchFinished"),
        ("post_terminal_event", "data after SearchFinished"),
        ("count_mismatch", "record count does not match"),
    ],
)
async def test_mcp_rejects_malformed_engine_terminal_streams(
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    message: str,
) -> None:
    """Fail closed when the engine violates its structured stream contract."""
    query = SearchQuery(
        terms=("missing",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
        effort="prompt",
    )
    match_count = 1 if fault == "count_mismatch" else 0
    summary = build_search_summary(
        query,
        effort="prompt",
        coverage=RunCoverage(
            sources_discovered=0,
            sources_eligible=0,
            sources_planned=0,
            sources_attempted=0,
            sources_completed=0,
            sources_bounded=0,
            sources_skipped=0,
            sources_unsupported=0,
            sources_failed=0,
            sources_cancelled=0,
            records_seen=0,
            matches_seen=0,
        ),
        match_count=match_count,
        elapsed_seconds=0.0,
    )
    terminal = SearchFinished(
        match_count=match_count,
        elapsed_seconds=0.0,
        summary=summary,
    )
    stream_events = (
        (SearchStarted(source_count=0),)
        if fault == "missing_terminal"
        else (
            (terminal, SearchStarted(source_count=0))
            if fault == "post_terminal_event"
            else (SearchStarted(source_count=0), terminal)
        )
    )

    async def malformed_stream(
        *_args: object,
        **_kwargs: object,
    ) -> t.AsyncIterator[SearchStarted | SearchFinished]:
        for event in stream_events:
            yield event

    monkeypatch.setattr(
        "agentgrep.mcp.tools.search_tools.agentgrep.aiter_search_events",
        malformed_stream,
    )

    with pytest.raises(RuntimeError, match=message):
        await _search_async(
            SearchRequestModel(
                terms=["missing"],
                agent="codex",
                scope="prompts",
                case_sensitive=False,
                limit=20,
            ),
        )


@pytest.mark.slow
async def test_registered_search_uses_cursorless_effort_contract() -> None:
    """Expose one effort enum and no retired cursor/deep arguments."""
    server = build_mcp_server()
    scope_conflict = "Invalid params: targeted effort requires conversation or all scope"
    async with Client(server) as client:
        tools = await client.list_tools_mcp()
        search = next(tool for tool in tools.tools if tool.name == "search")

        with pytest.raises(McpError) as retired_cursor:
            await client.call_tool_mcp(
                "search",
                {"terms": ["needle"], "cursor": "retired-search-cursor"},
            )
        for arguments in (
            {"terms": ["needle scope:prompts"], "effort": "targeted"},
            {"terms": ["needle"], "scope": "prompts", "effort": "targeted"},
        ):
            with pytest.raises(McpError) as scoped:
                await client.call_tool_mcp("search", arguments)
            assert scoped.value.error.code == mt.INVALID_PARAMS
            assert scoped.value.error.message == scope_conflict

    properties = search.input_schema["properties"]
    assert "cursor" not in properties
    assert "deep" not in properties
    assert "effort" in properties
    assert "conversation_limit" in properties
    assert retired_cursor.value.error.code == mt.INVALID_PARAMS

    # The response limiter may truncate search to plain text, so FastMCP hides
    # the output schema on the wire. The contract itself still stands.
    assert search.output_schema is None
    registered = await server.get_tool("search")
    assert registered is not None
    declared = registered.output_schema
    assert declared is not None
    action_effort_schema = declared["$defs"]["SearchRequestPatchModel"]["properties"]["effort"]
    assert action_effort_schema == {
        "anyOf": [
            {
                "enum": ["prompt", "targeted", "exhaustive"],
                "type": "string",
            },
            {"type": "null"},
        ],
    }


@pytest.mark.slow
async def test_registered_search_reports_invalid_params_concisely() -> None:
    """Return actionable validation without Pydantic internals or input values."""
    async with Client(build_mcp_server()) as client:
        with pytest.raises(McpError) as raised:
            await client.call_tool_mcp(
                "search",
                {
                    "terms": ["needle"],
                    "effort": "targeted",
                    "conversation_limit": 0,
                },
            )

    assert raised.value.error.code == mt.INVALID_PARAMS
    assert raised.value.error.message == (
        "Invalid params: conversation_limit: Input should be greater than or equal to 1"
    )


@pytest.mark.slow
async def test_registered_invalid_args_do_not_reach_server_logs(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Redact malformed sensitive values before FastMCP writes its warning."""
    terms_sentinel = "private-invalid-terms-sentinel"
    cursor_sentinel = "private-retired-cursor-sentinel"
    async with Client(build_mcp_server()) as client:
        for arguments in (
            {"terms": terms_sentinel},
            {"terms": ["needle"], "cursor": cursor_sentinel},
        ):
            with pytest.raises(McpError) as raised:
                await client.call_tool_mcp("search", arguments)
            assert raised.value.error.code == mt.INVALID_PARAMS

    captured = capfd.readouterr()
    assert terms_sentinel not in captured.err
    assert cursor_sentinel not in captured.err
    assert "[validation details redacted]" in captured.err


async def test_mcp_rejects_conversation_limit_without_targeted_effort() -> None:
    """Fail closed instead of silently accepting an unrelated limit."""
    search_request = SearchRequestModel(
        terms=["needle"],
        agent="codex",
        scope="prompts",
        case_sensitive=False,
        conversation_limit=7,
    )

    with pytest.raises(ToolError):
        await _search_async(search_request)


def test_find_cursor_remains_version_one() -> None:
    """Leave find pagination unchanged because it has no search effort."""
    expected = refs.FindCursor(
        offset=20,
        pattern="sessions",
        agent="all",
        limit=20,
    )

    assert refs.parse_find_cursor(_LEGACY_FIND_CURSOR) == expected

    token = refs.make_find_cursor(
        offset=20,
        pattern="sessions",
        agent="all",
        limit=20,
    )

    assert token.startswith("agcur1:")
    assert refs.parse_find_cursor(token) == expected


def test_search_response_requires_every_lifecycle_field() -> None:
    """Keep structured MCP clients from mistaking omitted evidence for empty evidence."""
    schema = SearchToolResponse.model_json_schema()

    assert set(schema["required"]) == {
        "schema_version",
        "request",
        "effort",
        "outcome",
        "coverage",
        "stats",
        "page",
        "status",
        "diagnostics",
        "next_actions",
        "results",
    }
    assert set(schema["$defs"]["RunStatusModel"]["required"]) == {
        "state",
        "reason",
        "conditions",
    }
    page_schema = schema["$defs"]["SearchPageModel"]
    assert set(page_schema["required"]) == {"limit", "count"}
    assert "next_cursor" not in page_schema["properties"]
