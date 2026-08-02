"""MCP ``terms=`` contract for the ``depth:``/``effort:`` query field.

Companion to ``tests/test_query_depth_field.py`` (the field's registry,
query-language, CLI, and TUI-path contract) and ``tests/test_mcp_search_depth.py``
(the pre-existing MCP structured ``effort=``/``scope=`` parameter contract,
unchanged by this module). This module proves the new field specifically on
the MCP surface: an embedded ``depth:``/``effort:`` token inside the
``search`` tool's ``terms`` list resolves the same read policy the
structured ``effort=`` parameter would, and combining both syntaxes in one
request is a clean ``INVALID_PARAMS`` error rather than silently picking a
winner — see ``agentgrep.mcp.tools.search_tools._compile_request_query``'s
docstring for why effort (unlike scope) does not get scope's permissive
"inline widens the structured value" treatment.
"""

from __future__ import annotations

import json
import pathlib

import mcp.types as mt
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp import McpError

from agentgrep.mcp.models import SearchRequestModel
from agentgrep.mcp.server import build_mcp_server
from agentgrep.mcp.tools.search_tools import _search_async

pytestmark = pytest.mark.mcp


async def test_mcp_terms_depth_directive_resolves_effort(
    codex_transcript_home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An embedded ``depth:targeted`` term reads conversations, no ``effort=`` param.

    Mirrors ``test_mcp_search_depth.py``'s
    ``test_mcp_targeted_effort_routes_transcript_backends`` fixture shape:
    targeted effort routes from prompt-history evidence into a matching
    conversation transcript, so the session needs a matching
    ``history.jsonl`` entry to route from — a bare transcript alone (as
    ``codex_transcript_home`` ships by default) has no prompt evidence for
    targeted routing to key off.
    """
    monkeypatch.setattr(
        pathlib.Path,
        "home",
        classmethod(lambda _cls: codex_transcript_home),
    )
    # Matches codex_transcript_home's own session_meta id (tests/conftest.py).
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

    response = await _search_async(
        SearchRequestModel(
            terms=["depth:targeted", "deep-only"],
            agent="codex",
            scope="all",
            case_sensitive=False,
            limit=20,
        ),
    )

    assert response.request.effort == "targeted"
    assert response.request.scope == "all"
    assert {record.text for record in response.results} == {
        "deep-only",
        "deep-only prompt",
    }


async def test_mcp_terms_effort_alias_resolves_exhaustive_effort(
    codex_transcript_home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``effort:`` alias resolves identically to ``depth:``."""
    monkeypatch.setattr(
        pathlib.Path,
        "home",
        classmethod(lambda _cls: codex_transcript_home),
    )

    response = await _search_async(
        SearchRequestModel(
            terms=["effort:exhaustive", "deep-only"],
            agent="codex",
            scope="prompts",
            case_sensitive=False,
            limit=20,
        ),
    )

    assert response.request.effort == "exhaustive"
    assert response.request.scope == "prompts"
    assert [record.text for record in response.results] == ["deep-only prompt"]


async def test_mcp_effort_param_and_depth_term_collide() -> None:
    """Setting both the structured ``effort=`` param and an inline term errors.

    Unlike ``scope=``/``scope:`` (which has always let the inline predicate
    win), effort has downstream validation
    (``_normalize_request_depth``) that already ran against the structured
    value before the query compiles — silently overriding it here would mean
    that validation checked a value the request no longer uses.
    """
    with pytest.raises(McpError) as raised:
        await _search_async(
            SearchRequestModel(
                terms=["depth:targeted", "foo"],
                agent="codex",
                scope="all",
                case_sensitive=False,
                effort="exhaustive",
                limit=20,
            ),
        )

    assert raised.value.error.code == mt.INVALID_PARAMS
    assert "cannot combine the effort parameter with a depth:/effort: term" in (
        raised.value.error.message
    )


async def test_mcp_prompt_depth_value_conflicts_with_broad_scope() -> None:
    """``depth:prompt`` with a scope that leaves ``prompts`` is a clean error.

    Symmetric with the pre-existing ``targeted effort requires conversation
    or all scope`` check in ``test_mcp_search_depth.py``.
    """
    with pytest.raises(ToolError, match="prompt effort requires prompt scope"):
        await _search_async(
            SearchRequestModel(
                terms=["depth:prompt", "foo"],
                agent="codex",
                scope="all",
                case_sensitive=False,
                limit=20,
            ),
        )


async def test_mcp_terms_depth_directive_widens_inferred_prompts_scope(
    codex_transcript_home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``depth:targeted`` alone widens an inferred (client-omitted) prompts scope.

    Mirrors the CLI's ``depth:targeted foo`` auto-widening to ``--deep``'s own
    behavior (``tests/test_query_depth_field.py::
    test_depth_field_works_through_the_cli_path``): an MCP client that never
    set ``scope`` gets the same treatment, since ``scope_provenance`` defaults
    to ``"inferred"`` here exactly as the registered tool derives it when a
    real client omits the ``scope`` argument.
    """
    monkeypatch.setattr(
        pathlib.Path,
        "home",
        classmethod(lambda _cls: codex_transcript_home),
    )

    response = await _search_async(
        SearchRequestModel(
            terms=["depth:targeted", "deep-only"],
            agent="codex",
            scope="prompts",
            case_sensitive=False,
            limit=20,
        ),
    )

    assert response.request.effort == "targeted"
    assert response.request.scope == "all"


async def test_mcp_terms_depth_directive_conflicts_with_explicit_prompts_scope() -> None:
    """``depth:targeted`` still errors when the client explicitly pinned prompts scope.

    Symmetric with the CLI's ``--scope prompts`` + ``depth:targeted``
    rejection: a client that stated prompts scope on purpose gets a clean
    contradiction instead of a silent override.
    """
    with pytest.raises(McpError) as raised:
        await _search_async(
            SearchRequestModel(
                terms=["depth:targeted", "foo"],
                agent="codex",
                scope="prompts",
                scope_provenance="explicit",
                case_sensitive=False,
                limit=20,
            ),
        )

    assert raised.value.error.code == mt.INVALID_PARAMS
    assert "targeted effort requires conversation or all scope" in raised.value.error.message


async def test_registered_search_tool_accepts_inline_depth_term() -> None:
    """Drive the registered MCP tool schema end to end with an inline term."""
    async with Client(build_mcp_server()) as client:
        result = await client.call_tool_mcp(
            "search",
            {"terms": ["depth:targeted scope:all", "needle"]},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    payload = result.structuredContent
    assert payload["request"]["effort"] == "targeted"
    assert payload["request"]["scope"] == "all"
