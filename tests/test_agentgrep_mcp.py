# ruff: noqa: D102, D103
"""Functional tests for the ``agentgrep`` FastMCP server."""

from __future__ import annotations

import json
import logging
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


class ToolResultLike(t.Protocol):
    """Minimal MCP tool-call result surface for response decoding."""

    content: object


def tool_payload(result: object) -> dict[str, t.Any]:
    """Decode a FastMCP tool result's JSON body into a dict."""
    typed = t.cast("ToolResultLike", result)
    content = t.cast("cabc.Sequence[ResourceTextLike]", typed.content)
    assert content
    text = content[0].text or ""
    return t.cast("dict[str, t.Any]", json.loads(text))


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

    assert {tool.name for tool in tools} == {
        "search",
        "find",
        "list_sources",
        "filter_sources",
        "summarize_discovery",
        "list_stores",
        "get_store_descriptor",
        "inspect_record_sample",
        "validate_query",
        "recent_sessions",
    }
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
    tools_advertised = t.cast("list[str]", data["tools"])
    assert "search" in tools_advertised
    assert "find" in tools_advertised
    assert "list_stores" in tools_advertised
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


async def test_audit_middleware_emits_extras(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every tool call emits an audit record with ``agentgrep_*`` extras."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    with caplog.at_level(logging.INFO, logger="agentgrep.audit"):
        async with Client(agentgrep_mcp.build_mcp_server()) as client:
            _ = await client.call_tool(
                "find",
                {"pattern": "missing", "agent": "all", "limit": 5},
            )

    audit_records = [r for r in caplog.records if getattr(r, "agentgrep_tool", None) == "find"]
    assert audit_records, "expected at least one audit record for the find tool"
    record = audit_records[-1]
    assert getattr(record, "agentgrep_outcome", None) == "ok"
    duration = t.cast("float", getattr(record, "agentgrep_duration_ms", None))
    assert duration >= 0.0


async def test_audit_middleware_redacts_pattern(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sensitive argument payloads are digested in the audit record."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    secret = "secret-token-do-not-leak"
    with caplog.at_level(logging.INFO, logger="agentgrep.audit"):
        async with Client(agentgrep_mcp.build_mcp_server()) as client:
            _ = await client.call_tool(
                "find",
                {"pattern": secret, "agent": "all", "limit": 1},
            )

    audit_records = [r for r in caplog.records if getattr(r, "agentgrep_tool", None) == "find"]
    assert audit_records
    summary = t.cast(
        "dict[str, t.Any]",
        getattr(audit_records[-1], "agentgrep_args_summary", None),
    )
    assert isinstance(summary["pattern"], dict)
    assert set(summary["pattern"]) == {"len", "sha256_prefix"}
    assert summary["pattern"]["len"] == len(secret)
    # The literal secret must not appear anywhere in the structured record.
    assert secret not in str(summary)


def test_response_limit_middleware_is_wired() -> None:
    """The server installs a ResponseLimitingMiddleware backstop."""
    from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware

    from agentgrep.mcp.middleware import AgentgrepAuditMiddleware

    agentgrep_mcp = load_agentgrep_mcp_module()
    server = agentgrep_mcp.build_mcp_server()
    classes = {type(m) for m in server.middleware}
    assert ResponseLimitingMiddleware in classes
    assert AgentgrepAuditMiddleware in classes


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


async def test_mcp_list_stores_returns_catalog_entries() -> None:
    """``list_stores`` enumerates the StoreCatalog."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool("list_stores", {"agent": "all"})

    data = tool_payload(result)
    assert data["total"] >= 10
    assert {s["agent"] for s in data["stores"]} >= {"codex", "claude", "cursor", "gemini"}


async def test_mcp_list_stores_filters_by_agent() -> None:
    """``list_stores`` respects the ``agent`` filter."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool("list_stores", {"agent": "cursor"})

    data = tool_payload(result)
    assert data["total"] >= 1
    assert {s["agent"] for s in data["stores"]} == {"cursor"}


async def test_mcp_get_store_descriptor_known_and_unknown() -> None:
    """``get_store_descriptor`` returns one entry or raises for unknown ids."""
    from fastmcp.exceptions import ToolError

    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        ok = await client.call_tool(
            "get_store_descriptor",
            {"store_id": "claude.projects.session"},
        )
        try:
            _ = await client.call_tool(
                "get_store_descriptor",
                {"store_id": "definitely.not.a.real.store"},
            )
        except ToolError as exc:
            error_message = str(exc)
        else:
            error_message = ""

    data = tool_payload(ok)
    assert data["store_id"] == "claude.projects.session"
    assert error_message and "definitely.not.a.real.store" in error_message


async def test_mcp_list_sources_with_filters(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``list_sources`` honors path_kind_filter."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    state_db = home / ".cursor" / "state.vscdb"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    state_db.touch()
    history_path = home / ".codex" / "history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    _ = history_path.write_text("[]", encoding="utf-8")

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "list_sources",
            {"path_kind_filter": "sqlite_db"},
        )

    data = tool_payload(result)
    assert data["total"] >= 1
    assert all(s["path_kind"] == "sqlite_db" for s in data["sources"])


async def test_mcp_filter_sources_requires_pattern() -> None:
    """``filter_sources`` rejects an empty pattern at the validation layer."""
    from fastmcp.exceptions import ToolError

    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        try:
            _ = await client.call_tool("filter_sources", {"pattern": ""})
        except ToolError as exc:
            error_message = str(exc)
        else:
            error_message = ""

    assert error_message  # validation should refuse the empty pattern


async def test_mcp_summarize_discovery_totals_match_list_sources(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``summarize_discovery.total_sources`` equals ``list_sources.total``."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    state_db = home / ".cursor" / "state.vscdb"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    state_db.touch()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        summary = await client.call_tool("summarize_discovery", {})
        listing = await client.call_tool("list_sources", {})

    summary_data = tool_payload(summary)
    listing_data = tool_payload(listing)
    assert summary_data["total_sources"] == listing_data["total"]


async def test_mcp_validate_query_invalid_regex() -> None:
    """``validate_query`` reports ``regex_valid=False`` on unclosed character classes."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "validate_query",
            {
                "terms": ["[unclosed"],
                "regex": True,
                "sample_text": "anything",
            },
        )

    data = tool_payload(result)
    assert data["regex_valid"] is False
    assert data["matches"] is False
    assert data["error_message"]


async def test_mcp_validate_query_substring_match() -> None:
    """``validate_query`` returns ``matches=True`` for a literal hit."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "validate_query",
            {"terms": ["foo"], "sample_text": "foobar baz"},
        )

    data = tool_payload(result)
    assert data["regex_valid"] is True
    assert data["matches"] is True


async def test_mcp_recent_sessions_filters_by_mtime(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sources older than ``hours`` are excluded."""
    import os

    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    state_db = home / ".cursor" / "state.vscdb"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    state_db.touch()
    # Backdate the file to 48 hours ago.
    old = state_db.stat().st_mtime - (48 * 3600)
    os.utime(state_db, (old, old))

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        recent = await client.call_tool("recent_sessions", {"hours": 24})
        broad = await client.call_tool("recent_sessions", {"hours": 24 * 7})

    recent_data = tool_payload(recent)
    broad_data = tool_payload(broad)
    # Paths come back with the home directory collapsed to '~', so compare
    # by suffix rather than by the absolute tmp_path string.
    suffix = ".cursor/state.vscdb"
    assert not any(s["path"].endswith(suffix) for s in recent_data["sources"])
    assert any(s["path"].endswith(suffix) for s in broad_data["sources"])
    _ = state_db  # quiet F841 — kept for readability of the test setup


async def test_mcp_inspect_record_sample_unknown_path(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown adapter+path returns an error_message and no records."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "inspect_record_sample",
            {
                "adapter_id": "codex.history_json.v1",
                "source_path": str(tmp_path / "no_such_file.json"),
                "sample_size": 1,
            },
        )

    data = tool_payload(result)
    assert data["sample_count"] == 0
    assert data["records"] == []
    assert data["error_message"] == "source not found"


async def test_mcp_inspect_record_sample_returns_codex_history(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known codex history file yields parsed sample records."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    history_path = home / ".codex" / "history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    _ = history_path.write_text(
        json.dumps([{"command": "echo alpha", "timestamp": "2026-01-01T00:00:00Z"}]),
        encoding="utf-8",
    )

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "inspect_record_sample",
            {
                "adapter_id": "codex.history_json.v1",
                "source_path": str(history_path),
                "sample_size": 1,
            },
        )

    data = tool_payload(result)
    assert data["error_message"] is None
    assert data["sample_count"] >= 1
