# ruff: noqa: D102, D103
"""Functional tests for the ``agentgrep`` FastMCP server."""

from __future__ import annotations

import json
import pathlib
import typing as t

from fastmcp import Client

from agentgrep import mcp as _agentgrep_mcp_module

if t.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest
    from fastmcp import FastMCP


class SearchRecordLike(t.Protocol):
    """Structural type for search results returned by FastMCP."""

    text: str
    kind: str
    agent: str


class SearchQueryLike(t.Protocol):
    """Structural type for search query echoes."""

    terms: list[str]
    search_type: str
    agent: str


class SearchToolDataLike(t.Protocol):
    """Structural type for search tool responses."""

    query: SearchQueryLike
    results: list[SearchRecordLike]


class FindRecordLike(t.Protocol):
    """Structural type for find results returned by FastMCP."""

    path: str
    agent: str


class FindToolDataLike(t.Protocol):
    """Structural type for find tool responses."""

    results: list[FindRecordLike]


class ResourceTextLike(t.Protocol):
    """Minimal text resource surface."""

    text: str | None


class ToolLike(t.Protocol):
    """Minimal MCP tool metadata surface."""

    name: str


class PromptLike(t.Protocol):
    """Minimal MCP prompt metadata surface."""

    name: str


class ResourceLike(t.Protocol):
    """Minimal MCP resource metadata surface."""

    uri: object


class ResourceTemplateLike(t.Protocol):
    """Minimal MCP resource template metadata surface."""

    uriTemplate: str


class AgentGrepMcpModule(t.Protocol):
    """Structural type for the loaded MCP module."""

    def build_mcp_server(self) -> FastMCP: ...


def load_agentgrep_mcp_module() -> AgentGrepMcpModule:
    """Return the installed ``agentgrep.mcp`` module."""
    return t.cast("AgentGrepMcpModule", t.cast("object", _agentgrep_mcp_module))


def write_jsonl(path: pathlib.Path, rows: cabc.Sequence[object]) -> None:
    """Write JSONL rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def extract_resource_text(contents: object) -> str:
    """Extract text from a FastMCP resource read response."""
    items = t.cast("cabc.Sequence[ResourceTextLike]", contents)
    assert items
    return items[0].text or ""


async def test_mcp_lists_tools_resources_prompts_and_templates() -> None:
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        tools = t.cast("list[ToolLike]", await client.list_tools())
        resources = t.cast("list[ResourceLike]", await client.list_resources())
        prompts = t.cast("list[PromptLike]", await client.list_prompts())
        templates = t.cast(
            "list[ResourceTemplateLike]",
            await client.list_resource_templates(),
        )

    assert {tool.name for tool in tools} == {"search", "find"}
    assert any(str(resource.uri) == "agentgrep://capabilities" for resource in resources)
    assert any(str(resource.uri) == "agentgrep://sources" for resource in resources)
    assert any(prompt.name == "search_prompts" for prompt in prompts)
    assert any(template.uriTemplate == "agentgrep://sources/{agent}" for template in templates)


async def test_mcp_search_tool_returns_full_prompt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "rollout.jsonl"
    write_jsonl(
        session_path,
        [
            {
                "type": "session_meta",
                "payload": {"id": "session-1", "model_provider": "openai"},
            },
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "serenity and bliss live here"},
                    ],
                },
            },
        ],
    )

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "search",
            {
                "terms": ["serenity", "bliss"],
                "agent": "codex",
                "search_type": "prompts",
                "limit": 5,
            },
        )

    data = t.cast("SearchToolDataLike", result.data)
    assert data.query.terms == ["serenity", "bliss"]
    assert data.query.search_type == "prompts"
    assert data.query.agent == "codex"
    assert len(data.results) == 1
    assert data.results[0].kind == "prompt"
    assert data.results[0].agent == "codex"
    assert data.results[0].text == "serenity and bliss live here"


async def test_mcp_find_tool_and_sources_resource(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    history_path = home / ".codex" / "history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    _ = history_path.write_text("[]", encoding="utf-8")

    state_db = home / ".cursor" / "state.vscdb"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    state_db.touch()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        find_result = await client.call_tool(
            "find",
            {"pattern": "state", "agent": "all", "limit": 10},
        )
        source_text = extract_resource_text(
            await client.read_resource("agentgrep://sources/cursor")
        )

    data = t.cast("FindToolDataLike", find_result.data)
    assert len(data.results) == 1
    assert data.results[0].agent == "cursor"
    assert data.results[0].path.endswith("state.vscdb")

    source_payload = t.cast("list[dict[str, object]]", json.loads(source_text))
    assert source_payload
    assert all(row["agent"] == "cursor" for row in source_payload)


async def test_mcp_capabilities_resource_reports_read_only() -> None:
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        text = extract_resource_text(await client.read_resource("agentgrep://capabilities"))

    data = t.cast("dict[str, object]", json.loads(text))
    assert data["read_only"] is True
    assert data["tools"] == ["search", "find"]
    prompts = t.cast("list[str]", data["prompts"])
    assert "search_history" in prompts


async def test_mcp_capabilities_lists_every_supported_agent_and_adapter() -> None:
    """``agentgrep://capabilities`` must advertise every agent and adapter id.

    The runtime list of agents and adapter ids has to stay in lockstep with
    the CLI's ``AGENT_CHOICES`` and the discover-function adapter ids, or
    MCP clients route queries to surfaces they don't know exist.
    """
    import agentgrep

    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        text = extract_resource_text(await client.read_resource("agentgrep://capabilities"))

    data = t.cast("dict[str, object]", json.loads(text))
    advertised_agents = t.cast("list[str]", data["agents"])
    assert set(advertised_agents) == set(agentgrep.AGENT_CHOICES)

    advertised_adapters = set(t.cast("list[str]", data["adapters"]))
    for adapter_id in (
        "cursor.cli_jsonl.v1",
        "gemini.tmp_chats_jsonl.v1",
        "gemini.tmp_logs_json.v1",
    ):
        assert adapter_id in advertised_adapters, adapter_id


async def test_mcp_prompt_guides_search() -> None:
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.get_prompt(
            "search_prompts",
            {"topic": "serenity", "agent": "codex"},
        )

    rendered = str(result)
    assert "search" in rendered
    assert "serenity" in rendered
    assert "codex" in rendered


def test_mcp_instructions_carry_every_segment_header() -> None:
    """Server instructions must include each named ``_INSTR_*`` segment.

    The instructions are composed from segments and an accidental deletion of
    one would silently shorten what MCP clients see on handshake. Asserting on
    segment-header sentinels catches that without locking in exact wording.
    """
    from agentgrep.mcp.instructions import _build_instructions

    rendered = _build_instructions()
    for marker in (
        "agentgrep MCP server",
        "TRIGGERS:",
        "ANTI-TRIGGERS:",
        "search vs discovery:",
        "Defaults:",
        "Resources:",
        "Privacy:",
    ):
        assert marker in rendered, marker
