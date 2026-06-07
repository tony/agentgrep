# ruff: noqa: D102, D103
"""Functional tests for the ``agentgrep`` FastMCP server."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import typing as t

import pytest
from fastmcp import Client

from agentgrep import mcp as _agentgrep_mcp_module

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from fastmcp import FastMCP


class SearchRecordLike(t.Protocol):
    """Structural type for search results returned by FastMCP."""

    text: str
    kind: str
    agent: str


class SearchQueryLike(t.Protocol):
    """Structural type for search query echoes."""

    terms: list[str]
    scope: str
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
        "db_status",
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
                "scope": "prompts",
                "limit": 5,
            },
        )

    data = t.cast("SearchToolDataLike", result.data)
    assert data.query.terms == ["serenity", "bliss"]
    assert data.query.scope == "prompts"
    assert data.query.agent == "codex"
    assert len(data.results) == 1
    assert data.results[0].kind == "prompt"
    assert data.results[0].agent == "codex"
    assert data.results[0].text == "serenity and bliss live here"


async def test_mcp_search_tool_sorts_records_across_sources(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlimited multi-source searches return records newest-first.

    Regression guard: the async event stream emits records per source in
    source-mtime order, so a source with a newer mtime but older record
    timestamps would surface its records first without a final sort.
    """
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    def codex_session(timestamp: str, text: str) -> list[dict[str, object]]:
        return [
            {
                "type": "session_meta",
                "payload": {"id": f"session-{text}", "model_provider": "openai"},
            },
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            },
        ]

    sessions = home / ".codex" / "sessions" / "2026" / "01" / "01"
    newer_mtime_older_record = sessions / "newer-mtime.jsonl"
    older_mtime_newer_record = sessions / "older-mtime.jsonl"
    write_jsonl(
        newer_mtime_older_record,
        codex_session("2026-01-01T00:00:00Z", "bliss old"),
    )
    write_jsonl(
        older_mtime_newer_record,
        codex_session("2026-06-01T00:00:00Z", "bliss new"),
    )
    os.utime(newer_mtime_older_record, ns=(2_000_000_000, 2_000_000_000))
    os.utime(older_mtime_newer_record, ns=(1_000_000_000, 1_000_000_000))

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "search",
            {
                "terms": ["bliss"],
                "agent": "codex",
                "scope": "prompts",
                "limit": None,
            },
        )

    data = t.cast("SearchToolDataLike", result.data)
    assert [record.text for record in data.results] == ["bliss new", "bliss old"]


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
            await client.read_resource("agentgrep://sources/cursor-ide")
        )

    data = t.cast("FindToolDataLike", find_result.data)
    assert len(data.results) == 1
    assert data.results[0].agent == "cursor-ide"
    assert data.results[0].path.endswith("state.vscdb")

    source_payload = t.cast("list[dict[str, object]]", json.loads(source_text))
    assert source_payload
    assert all(row["agent"] == "cursor-ide" for row in source_payload)


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
    assert "search_conversations" in prompts
    assert "search_scopes" in data
    assert data["search_scopes"] == ["prompts", "conversations", "all"]


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
        "claude.projects_memory_text.v1",
        "codex.config_toml.v1",
        "codex.plugin_instruction_text.v1",
        "cursor_cli.transcripts_jsonl.v1",
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


async def test_mcp_search_tool_rejects_legacy_search_type() -> None:
    """The MCP search tool accepts ``scope`` instead of legacy ``search_type``."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "search",
            {
                "terms": ["serenity"],
                "agent": "codex",
                "search_type": "history",
                "limit": 5,
            },
            raise_on_error=False,
        )

    assert result.is_error is True


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
    assert {s["agent"] for s in data["stores"]} >= {"codex", "claude", "cursor-cli", "cursor-ide"}


async def test_mcp_list_stores_filters_by_agent() -> None:
    """``list_stores`` respects the ``agent`` filter."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool("list_stores", {"agent": "cursor-cli"})

    data = tool_payload(result)
    assert data["total"] >= 1
    assert {s["agent"] for s in data["stores"]} == {"cursor-cli"}


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


async def test_mcp_list_sources_exposes_non_default_coverage_on_request(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``list_sources`` keeps defaults narrow but can inventory non-default stores."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    history_path = home / ".codex" / "history.jsonl"
    write_jsonl(
        history_path,
        [{"session_id": "s", "ts": 1_700_000_000, "text": "history"}],
    )
    state_db = home / ".codex" / "state_5.sqlite"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    state_db.touch()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        default_result = await client.call_tool("list_sources", {"agent": "codex"})
        inventory_result = await client.call_tool(
            "list_sources",
            {
                "agent": "codex",
                "include_non_default": True,
                "coverage_filter": "inspectable",
            },
        )

    default_data = tool_payload(default_result)
    inventory_data = tool_payload(inventory_result)
    assert all(s["coverage"] == "default_search" for s in default_data["sources"])
    assert any(s["path"].endswith("state_5.sqlite") for s in inventory_data["sources"])
    assert {s["coverage"] for s in inventory_data["sources"]} == {"inspectable"}


async def test_mcp_list_sources_exposes_version_detection(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source discovery payloads expose concrete data-shape detection."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    history_path = home / ".codex" / "history.jsonl"
    write_jsonl(
        history_path,
        [{"session_id": "session-jsonl-1", "ts": 1_700_000_000, "text": "history"}],
    )

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "list_sources",
            {"agent": "codex"},
        )

    data = tool_payload(result)
    source = next(s for s in data["sources"] if s["adapter_id"] == "codex.history_jsonl.v1")
    assert source["version_detection"] == {
        "app_version": None,
        "data_version": "codex.history_jsonl.current",
        "strategy": "shape_inference",
        "confidence": "high",
        "evidence": "history.jsonl object keys include session_id, ts, text",
    }


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


async def test_mcp_inspect_record_sample_returns_non_default_adapter_records(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inspectable/catalog adapters with discovery can produce record samples."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    task_path = home / ".claude" / "tasks" / "team" / "1.json"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    _ = task_path.write_text(
        json.dumps(
            {
                "id": "1",
                "subject": "Sample task",
                "description": "Inspect task text",
                "status": "pending",
                "blocks": [],
                "blockedBy": [],
            },
        ),
        encoding="utf-8",
    )
    index_path = home / ".codex" / "session_index.jsonl"
    write_jsonl(
        index_path,
        [{"id": "thread-1", "thread_name": "Sample thread", "updated_at": "2026-05-30T12:00:00Z"}],
    )
    config_path = home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _ = config_path.write_text("model = 'do-not-index'\n", encoding="utf-8")

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        task_result = await client.call_tool(
            "inspect_record_sample",
            {
                "adapter_id": "claude.tasks_json.v1",
                "source_path": str(task_path),
                "sample_size": 1,
            },
        )
        index_result = await client.call_tool(
            "inspect_record_sample",
            {
                "adapter_id": "codex.session_index_jsonl.v1",
                "source_path": str(index_path),
                "sample_size": 1,
            },
        )
        config_result = await client.call_tool(
            "inspect_record_sample",
            {
                "adapter_id": "codex.config_toml.v1",
                "source_path": str(config_path),
                "sample_size": 1,
            },
        )

    task_data = tool_payload(task_result)
    index_data = tool_payload(index_result)
    config_data = tool_payload(config_result)
    assert task_data["error_message"] is None
    assert task_data["records"][0]["text"] == "Sample task\n\nInspect task text"
    assert index_data["error_message"] is None
    assert index_data["records"][0]["text"] == "Sample thread"
    assert config_data["error_message"] is None
    assert "model (str)" in config_data["records"][0]["text"]
    assert "do-not-index" not in config_data["records"][0]["text"]


async def test_mcp_catalog_resource_returns_full_catalog() -> None:
    """``agentgrep://catalog`` returns the StoreCatalog payload."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        text = extract_resource_text(await client.read_resource("agentgrep://catalog"))

    data = t.cast("dict[str, t.Any]", json.loads(text))
    assert "stores" in data
    assert len(data["stores"]) >= 10
    store_ids = {s["store_id"] for s in data["stores"]}
    assert "claude.projects.session" in store_ids


async def test_mcp_store_roles_resource() -> None:
    """``agentgrep://store-roles`` lists every StoreRole with a description."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        text = extract_resource_text(await client.read_resource("agentgrep://store-roles"))

    rows = t.cast("list[dict[str, str]]", json.loads(text))
    values = {row["value"] for row in rows}
    assert "primary_chat" in values
    assert "prompt_history" in values
    assert all(row["description"] for row in rows)


async def test_mcp_store_formats_resource() -> None:
    """``agentgrep://store-formats`` lists every StoreFormat with a description."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        text = extract_resource_text(await client.read_resource("agentgrep://store-formats"))

    rows = t.cast("list[dict[str, str]]", json.loads(text))
    values = {row["value"] for row in rows}
    assert "jsonl" in values
    assert "sqlite" in values
    assert all(row["description"] for row in rows)


async def test_mcp_capabilities_advertises_new_resources() -> None:
    """The capabilities resource must list the three new resource URIs."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        text = extract_resource_text(await client.read_resource("agentgrep://capabilities"))

    data = t.cast("dict[str, t.Any]", json.loads(text))
    advertised = set(data["resources"])
    assert {
        "agentgrep://catalog",
        "agentgrep://store-roles",
        "agentgrep://store-formats",
    } <= advertised


def test_db_status_tool_closes_its_connection(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The db_status helper closes the per-call SQLite connection."""
    import sqlite3

    import pytest as _pytest

    from agentgrep import db as agentgrep_db
    from agentgrep.mcp.tools import db_tools

    db_path = tmp_path / "agentgrep.sqlite"
    agentgrep_db.DbRuntime.open(db_path).close()
    opened: list[agentgrep_db.DbRuntime] = []
    real_open_readonly = agentgrep_db.DbRuntime.open_readonly

    def capturing_open_readonly(
        db_path: pathlib.Path | str | None = None,
    ) -> agentgrep_db.DbRuntime:
        runtime = real_open_readonly(db_path)
        opened.append(runtime)
        return runtime

    monkeypatch.setattr(agentgrep_db.DbRuntime, "open_readonly", capturing_open_readonly)
    payload = db_tools._db_status_sync(str(db_path))

    assert payload.sources == 0
    assert len(opened) == 1
    with _pytest.raises(sqlite3.ProgrammingError):
        _ = opened[0].store.connection.execute("SELECT 1")


def test_db_status_tool_reports_zeros_for_foreign_file(
    tmp_path: pathlib.Path,
) -> None:
    """A non-database file at the db path yields a zero-count payload."""
    from agentgrep.mcp.tools import db_tools

    db_path = tmp_path / "not-a-db.sqlite"
    db_path.write_text("plain text", encoding="utf-8")

    payload = db_tools._db_status_sync(str(db_path))

    assert payload.sources == 0
    assert payload.records == 0
    assert payload.db_schema_version == 0


class McpCacheRuntimeCase(t.NamedTuple):
    """Named case for the MCP runtime's AGENTGREP_CACHE handling."""

    test_id: str
    env_cache: str | None
    expected_cache_mode: str
    expects_opener: bool


MCP_CACHE_RUNTIME_CASES: tuple[McpCacheRuntimeCase, ...] = (
    McpCacheRuntimeCase(
        test_id="off-attaches-nothing",
        env_cache="off",
        expected_cache_mode="off",
        expects_opener=False,
    ),
    McpCacheRuntimeCase(
        test_id="auto-sets-opener",
        env_cache="auto",
        expected_cache_mode="auto",
        expects_opener=True,
    ),
    McpCacheRuntimeCase(
        test_id="require-sets-opener",
        env_cache="require",
        expected_cache_mode="require",
        expects_opener=True,
    ),
    McpCacheRuntimeCase(
        test_id="unset-defaults-auto",
        env_cache=None,
        expected_cache_mode="auto",
        expects_opener=True,
    ),
)


@pytest.mark.parametrize(
    "case",
    MCP_CACHE_RUNTIME_CASES,
    ids=[case.test_id for case in MCP_CACHE_RUNTIME_CASES],
)
def test_mcp_runtime_honors_cache_env(
    case: McpCacheRuntimeCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server runtime maps AGENTGREP_CACHE to a per-consult opener.

    The server never holds a SQLite connection: tool work runs in a
    worker thread, so the runtime carries an opener the consulting
    thread calls instead of an open ``db`` handle.
    """
    from agentgrep.mcp import server as mcp_server

    if case.env_cache is None:
        monkeypatch.delenv("AGENTGREP_CACHE", raising=False)
    else:
        monkeypatch.setenv("AGENTGREP_CACHE", case.env_cache)

    runtime = mcp_server._build_search_runtime()

    assert runtime.cache_mode == case.expected_cache_mode
    assert runtime.db is None
    assert (runtime.db_opener is not None) is case.expects_opener


class CacheOpenerCase(t.NamedTuple):
    """Named case for the MCP cache opener's filesystem handling."""

    test_id: str
    create: t.Literal["db", "foreign", "missing"]
    expects_runtime: bool


CACHE_OPENER_CASES: tuple[CacheOpenerCase, ...] = (
    CacheOpenerCase(test_id="synced-db-opens", create="db", expects_runtime=True),
    CacheOpenerCase(test_id="foreign-file-degrades", create="foreign", expects_runtime=False),
    CacheOpenerCase(test_id="missing-file-degrades", create="missing", expects_runtime=False),
)


@pytest.mark.parametrize(
    "case",
    CACHE_OPENER_CASES,
    ids=[case.test_id for case in CACHE_OPENER_CASES],
)
def test_mcp_cache_opener_opens_in_calling_thread(
    case: CacheOpenerCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opener yields a connection usable by the thread that called it."""
    import threading

    from agentgrep import db as agentgrep_db
    from agentgrep.mcp import server as mcp_server

    db_path = tmp_path / "cache" / "agentgrep.sqlite"
    monkeypatch.setenv("AGENTGREP_DB", str(db_path))
    if case.create == "db":
        agentgrep_db.DbRuntime.open(db_path).close()
    elif case.create == "foreign":
        db_path.parent.mkdir(parents=True)
        db_path.write_text("plain text", encoding="utf-8")

    outcomes: list[bool | BaseException] = []

    def consult() -> None:
        try:
            runtime = mcp_server._open_cache_runtime()
            if runtime is None:
                outcomes.append(False)
                return
            _ = runtime.status()
            runtime.close()
            outcomes.append(True)
        except BaseException as error:
            outcomes.append(error)

    worker = threading.Thread(target=consult)
    worker.start()
    worker.join()

    assert outcomes == [case.expects_runtime]


async def test_mcp_search_tool_serves_cached_records_under_require(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A require-mode MCP search serves cached records through the worker thread.

    Regression test for cross-thread SQLite use: the search tool runs
    ``iter_search_events`` via ``asyncio.to_thread``, so the cache must
    be opened by that worker thread rather than the server thread.
    """
    import agentgrep
    from agentgrep import db as agentgrep_db

    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENTGREP_CACHE", "require")

    source_path = tmp_path / "session.jsonl"
    source_path.write_text("cached", encoding="utf-8")
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=source_path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=source_path.parent,
        mtime_ns=source_path.stat().st_mtime_ns,
    )
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text="serve this straight from the cache",
        timestamp="2026-06-05T12:00:00Z",
        session_id="session-cache",
    )
    db_runtime = agentgrep_db.DbRuntime.open(agentgrep_db.default_db_path())
    _ = db_runtime.sync_records(((source, (record,)),))
    db_runtime.close()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "search",
            {
                "terms": ["straight"],
                "agent": "codex",
                "scope": "prompts",
                "limit": 5,
            },
        )

    data = t.cast("SearchToolDataLike", result.data)
    assert len(data.results) == 1
    assert data.results[0].text == "serve this straight from the cache"
