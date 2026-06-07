# ruff: noqa: D102, D103
"""Functional tests for the ``agentgrep`` FastMCP server."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import typing as t

import pytest
from fastmcp import Client

import agentgrep
from agentgrep import mcp as _agentgrep_mcp_module

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from fastmcp import FastMCP

    from agentgrep.mcp import SearchRecordLike as McpSearchRecordLike


class SearchRecordLike(t.Protocol):
    """Structural type for search results returned by FastMCP."""

    text: str
    kind: str
    agent: str


class SearchRequestLike(t.Protocol):
    """Structural type for search request echoes."""

    terms: list[str]
    scope: str
    agent: str


class SearchToolDataLike(t.Protocol):
    """Structural type for search tool responses."""

    request: SearchRequestLike
    results: list[SearchRecordLike]


class FindRecordLike(t.Protocol):
    """Structural type for find results returned by FastMCP."""

    path: str
    agent: str


class FindToolDataLike(t.Protocol):
    """Structural type for find tool responses."""

    results: list[FindRecordLike]


class McpResultShapeCase(t.NamedTuple):
    """Parametrized case for common search/find result payload fields."""

    test_id: str
    tool_name: t.Literal["search", "find"]
    arguments: dict[str, t.Any]
    expected_request: dict[str, t.Any]


class AgentProductNameCase(t.NamedTuple):
    """Parametrized case mapping a supported backend to its product name."""

    test_id: str
    agent: agentgrep.AgentName
    product_name: str


RESULT_SHAPE_CASES = [
    McpResultShapeCase(
        test_id="search",
        tool_name="search",
        arguments={
            "terms": ["serenity"],
            "agent": "codex",
            "scope": "prompts",
            "limit": 1,
        },
        expected_request={
            "terms": ["serenity"],
            "agent": "codex",
            "scope": "prompts",
            "case_sensitive": False,
            "limit": 1,
        },
    ),
    McpResultShapeCase(
        test_id="find",
        tool_name="find",
        arguments={"pattern": "codex", "agent": "codex", "limit": 1},
        expected_request={"pattern": "codex", "agent": "codex", "limit": 1},
    ),
]


# Product-level display names the MCP handshake advertises for each supported
# backend. Split CLI/IDE backends share one product name because the
# instructions name the product, not each slug. Keyed by AgentName so adding a
# backend forces an entry here and a matching mention in the handshake prose.
AGENT_PRODUCT_NAMES: dict[agentgrep.AgentName, str] = {
    "claude": "Claude",
    "codex": "Codex",
    "cursor-cli": "Cursor",
    "cursor-ide": "Cursor",
    "gemini": "Gemini",
    "antigravity-cli": "Antigravity",
    "antigravity-ide": "Antigravity",
    "grok": "Grok",
    "pi": "Pi",
    "opencode": "OpenCode",
    "windsurf": "Windsurf",
    "vscode": "VS Code",
}


AGENT_PRODUCT_NAME_CASES: tuple[AgentProductNameCase, ...] = tuple(
    AgentProductNameCase(test_id=agent, agent=agent, product_name=product)
    for agent, product in sorted(AGENT_PRODUCT_NAMES.items())
)


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


def write_codex_prompt_session(
    path: pathlib.Path,
    *,
    session_id: str,
    timestamp: str,
    text: str,
) -> None:
    """Write a minimal Codex session containing one user prompt."""
    write_jsonl(
        path,
        [
            {
                "type": "session_meta",
                "payload": {"id": session_id, "model_provider": "openai"},
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
        ],
    )


def write_mcp_search_fixture(home: pathlib.Path) -> None:
    """Create enough Codex prompt data for bounded search pages."""
    sessions = home / ".codex" / "sessions" / "2026" / "01" / "01"
    write_codex_prompt_session(
        sessions / "new.jsonl",
        session_id="session-new",
        timestamp="2026-01-02T00:00:00Z",
        text="serenity new",
    )
    write_codex_prompt_session(
        sessions / "old.jsonl",
        session_id="session-old",
        timestamp="2026-01-01T00:00:00Z",
        text="serenity old",
    )


def write_mcp_find_fixture(home: pathlib.Path) -> None:
    """Create enough Codex sources for bounded find pages."""
    history_json = home / ".codex" / "history.json"
    history_json.parent.mkdir(parents=True, exist_ok=True)
    _ = history_json.write_text("[]", encoding="utf-8")
    write_jsonl(
        home / ".codex" / "history.jsonl",
        [{"session_id": "history-jsonl", "ts": 1_700_000_000, "text": "codex history"}],
    )


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
        "inspect_result",
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
    assert data.request.terms == ["serenity", "bliss"]
    assert data.request.scope == "prompts"
    assert data.request.agent == "codex"
    assert len(data.results) == 1
    assert data.results[0].kind == "prompt"
    assert data.results[0].agent == "codex"
    assert data.results[0].text == "serenity and bliss live here"


def _codex_user_session(session_id: str, text: str) -> list[dict[str, object]]:
    """Build a one-message codex session payload for query-language tests."""
    return [
        {
            "type": "session_meta",
            "payload": {"id": session_id, "model_provider": "openai"},
        },
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
    ]


async def test_mcp_search_honors_query_language(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP search tool compiles boolean and field-predicate syntax.

    A bare-substring AND of ``zzznope OR alpha`` would match nothing;
    honoring the query language turns it into a union that finds the
    record. The ``agent:`` predicate prunes by source.
    """
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    write_jsonl(
        home / ".codex" / "sessions" / "2026" / "01" / "01" / "rollout.jsonl",
        _codex_user_session("session-1", "alpha content here"),
    )

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        union = await client.call_tool(
            "search",
            {"terms": ["zzznope", "OR", "alpha"], "scope": "prompts", "limit": 5},
        )
        wrong_agent = await client.call_tool(
            "search",
            {"terms": ["agent:claude", "alpha"], "scope": "prompts", "limit": 5},
        )

    union_data = t.cast("SearchToolDataLike", union.data)
    wrong_agent_data = t.cast("SearchToolDataLike", wrong_agent.data)
    assert len(union_data.results) == 1
    assert union_data.results[0].text == "alpha content here"
    assert len(wrong_agent_data.results) == 0


async def test_mcp_search_rejects_invalid_query() -> None:
    """A malformed query predicate raises a ToolError with the reason."""
    from fastmcp.exceptions import ToolError

    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        try:
            _ = await client.call_tool("search", {"terms": ["agent:nope"]})
        except ToolError as exc:
            error_message = str(exc)
        else:
            error_message = ""

    assert "invalid query" in error_message
    assert "agent" in error_message


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


@pytest.mark.parametrize(
    "case",
    RESULT_SHAPE_CASES,
    ids=[case.test_id for case in RESULT_SHAPE_CASES],
)
async def test_mcp_result_payload_exposes_page_status_stats_and_refs(
    case: McpResultShapeCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``search`` and ``find`` expose a resumable result page shape."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    if case.tool_name == "search":
        write_mcp_search_fixture(home)
    else:
        write_mcp_find_fixture(home)

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(case.tool_name, case.arguments)

    data = tool_payload(result)
    assert "query" not in data
    for key, expected in case.expected_request.items():
        assert data["request"][key] == expected
    assert data["status"] == {"state": "bounded", "reason": "page_limit"}
    assert data["page"]["limit"] == 1
    assert data["page"]["count"] == 1
    assert isinstance(data["page"]["next_cursor"], str)
    assert data["stats"]["emitted"] == 1
    assert data["stats"]["matched"] >= 2
    assert data["stats"]["searched"] >= 2
    assert data["results"][0]["ref"].startswith("agref1:")


@pytest.mark.parametrize(
    "state",
    ["complete", "bounded", "truncated", "cancelled", "approximate", "failed"],
)
def test_mcp_run_status_model_accepts_adr_vocabulary(
    state: t.Literal["complete", "bounded", "truncated", "cancelled", "approximate", "failed"],
) -> None:
    """The MCP status schema accepts the ADR 0004 status vocabulary."""
    from agentgrep.mcp import RunStatusModel

    assert RunStatusModel(state=state).state == state


async def test_mcp_search_cursor_returns_next_page_without_duplicate(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A search cursor resumes without callers reconstructing the request."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    write_mcp_search_fixture(home)

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        first = await client.call_tool(
            "search",
            {"terms": ["serenity"], "agent": "codex", "scope": "prompts", "limit": 1},
        )
        first_data = tool_payload(first)
        cursor = first_data["page"]["next_cursor"]
        second = await client.call_tool("search", {"cursor": cursor})

    second_data = tool_payload(second)
    assert [row["text"] for row in first_data["results"]] == ["serenity new"]
    assert [row["text"] for row in second_data["results"]] == ["serenity old"]
    assert second_data["status"] == {"state": "complete", "reason": None}
    assert second_data["page"]["next_cursor"] is None
    assert second_data["request"]["terms"] == ["serenity"]


async def test_mcp_search_cursor_rejects_empty_terms(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tampered cursors cannot turn into an unfiltered search scan."""
    from fastmcp.exceptions import ToolError

    from agentgrep.mcp import refs

    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cursor = refs.make_search_cursor(
        offset=1,
        terms=[],
        agent="codex",
        scope="prompts",
        case_sensitive=False,
        limit=1,
    )

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        try:
            _ = await client.call_tool("search", {"cursor": cursor})
        except ToolError as exc:
            error_message = str(exc)
        else:
            error_message = ""

    assert "non-empty list" in error_message


async def test_mcp_filter_sources_cursor_returns_next_page(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``filter_sources`` accepts its own cursor for bounded pages."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    write_mcp_find_fixture(home)

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        first = await client.call_tool(
            "filter_sources",
            {"pattern": "codex", "agent": "codex", "limit": 1},
        )
        first_data = tool_payload(first)
        cursor = first_data["page"]["next_cursor"]
        second = await client.call_tool("filter_sources", {"cursor": cursor})

    second_data = tool_payload(second)
    assert first_data["results"][0]["ref"] != second_data["results"][0]["ref"]
    assert second_data["request"]["pattern"] == "codex"
    assert second_data["page"]["next_cursor"] is None
    assert second_data["status"] == {"state": "complete", "reason": None}


async def test_mcp_inspect_result_uses_opaque_ref(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``inspect_result`` resolves an opaque result ref to source records."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    write_mcp_search_fixture(home)

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        search_result = await client.call_tool(
            "search",
            {"terms": ["serenity"], "agent": "codex", "scope": "prompts", "limit": 1},
        )
        ref = tool_payload(search_result)["results"][0]["ref"]
        inspect_result = await client.call_tool("inspect_result", {"ref": ref})

    data = tool_payload(inspect_result)
    assert data["ref"] == ref
    assert data["error_message"] is None
    assert data["sample_count"] == 1
    assert data["records"][0]["text"] == "serenity new"


def test_mcp_search_ref_fingerprint_distinguishes_kind_and_role(
    tmp_path: pathlib.Path,
) -> None:
    """Prompt/history siblings with the same text do not collide."""
    from agentgrep.mcp import refs

    path = tmp_path / "duplicate.jsonl"
    path.touch()
    prompt = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        text="duplicate text",
        role="user",
        timestamp="2026-01-03T00:00:00Z",
        session_id="duplicate-session",
        conversation_id="duplicate-session",
    )
    history = agentgrep.SearchRecord(
        kind="history",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        text="duplicate text",
        role="assistant",
        timestamp="2026-01-03T00:00:00Z",
        session_id="duplicate-session",
        conversation_id="duplicate-session",
    )

    prompt_like = t.cast("McpSearchRecordLike", prompt)
    history_like = t.cast("McpSearchRecordLike", history)
    assert refs.search_record_fingerprint(prompt_like) != refs.search_record_fingerprint(
        history_like,
    )
    assert refs.make_search_ref(prompt_like) != refs.make_search_ref(history_like)


async def test_mcp_list_sources_exposes_searchability_metadata(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source rows tell agents whether a store is searched or inspected."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    write_mcp_find_fixture(home)
    state_db = home / ".codex" / "state_5.sqlite"
    state_db.touch()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "list_sources",
            {"agent": "codex", "include_non_default": True},
        )

    data = tool_payload(result)
    default_source = next(
        source for source in data["sources"] if source["coverage"] == "default_search"
    )
    inspectable_source = next(
        source for source in data["sources"] if source["coverage"] == "inspectable"
    )
    assert default_source["searchable"] is True
    assert default_source["search_by_default"] is True
    assert default_source["inspectable"] is True
    assert inspectable_source["searchable"] is False
    assert inspectable_source["search_by_default"] is False
    assert inspectable_source["inspectable"] is True
    assert inspectable_source["searchable_reason"]


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
        "antigravity_cli.history_jsonl.v1",
        "antigravity_cli.conversations_sqlite_protobuf.v1",
        "antigravity_ide.conversations_protobuf.v1",
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


async def test_audit_middleware_redacts_cursor(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cursor arguments are handles for sensitive terms and get digested."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    write_mcp_search_fixture(home)

    secret = "serenity"
    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        first = await client.call_tool(
            "search",
            {"terms": [secret], "agent": "codex", "scope": "prompts", "limit": 1},
        )
        cursor = t.cast("str", tool_payload(first)["page"]["next_cursor"])
        with caplog.at_level(logging.INFO, logger="agentgrep.audit"):
            _ = await client.call_tool("search", {"cursor": cursor})

    audit_records = [
        r
        for r in caplog.records
        if getattr(r, "agentgrep_tool", None) == "search"
        and getattr(r, "agentgrep_outcome", None) == "ok"
    ]
    assert audit_records
    summary = t.cast(
        "dict[str, t.Any]",
        getattr(audit_records[-1], "agentgrep_args_summary", None),
    )
    assert isinstance(summary["cursor"], dict)
    assert set(summary["cursor"]) == {"len", "sha256_prefix"}
    assert summary["cursor"]["len"] == len(cursor)
    assert cursor not in str(summary)
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
        "Result loop:",
        "Resources:",
        "Privacy:",
    ):
        assert marker in rendered, marker


@pytest.mark.parametrize(
    AgentProductNameCase._fields,
    AGENT_PRODUCT_NAME_CASES,
    ids=[case.test_id for case in AGENT_PRODUCT_NAME_CASES],
)
def test_mcp_instructions_name_every_supported_agent(
    test_id: str,
    agent: agentgrep.AgentName,
    product_name: str,
) -> None:
    """Handshake instructions name the product for every supported backend."""
    del test_id

    from agentgrep.mcp.instructions import _build_instructions

    rendered = _build_instructions()
    assert re.search(rf"\b{re.escape(product_name)}\b", rendered), f"{agent}: {product_name}"


def test_agent_product_name_map_tracks_agent_name_literal() -> None:
    """Every AgentName slug maps to a product the handshake test can assert."""
    assert set(AGENT_PRODUCT_NAMES) == set(t.get_args(agentgrep.AgentName))


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


async def test_mcp_validate_query_validates_query_language() -> None:
    """``validate_query`` reports query-language parse/compile validity."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        good = await client.call_tool(
            "validate_query",
            {"query": "agent:codex OR model:gpt*"},
        )
        bad = await client.call_tool(
            "validate_query",
            {"query": "agent:nope"},
        )

    good_data = tool_payload(good)
    bad_data = tool_payload(bad)
    assert good_data["query_valid"] is True
    assert good_data["error_message"] is None
    assert bad_data["query_valid"] is False
    assert "agent" in (bad_data["error_message"] or "")


async def test_mcp_query_language_resource_lists_every_field() -> None:
    """The query-language resource lists each registry field and operators."""
    from agentgrep.query import default_registry

    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        contents = await client.read_resource("agentgrep://query-language")

    payload = t.cast("dict[str, t.Any]", json.loads(extract_resource_text(contents)))
    field_names = {field["name"] for field in payload["fields"]}
    assert field_names == set(default_registry().known_names())
    assert any(op["syntax"] == "field:*" for op in payload["operators"])
    assert payload["summary"]


async def test_mcp_search_tool_description_mentions_query_language() -> None:
    """The search tool advertises the query language in its schema."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        tools = t.cast("list[ToolLike]", await client.list_tools())

    search = next(tool for tool in tools if tool.name == "search")
    description = t.cast("str | None", t.cast("t.Any", search).description)
    assert "query language" in (description or "")


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


def test_mcp_runtime_honors_cache_env(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server runtime takes its cache mode from AGENTGREP_CACHE."""
    import sqlite3

    import pytest

    from agentgrep import db as agentgrep_db
    from agentgrep.mcp import server as mcp_server

    db_path = tmp_path / "agentgrep.sqlite"
    agentgrep_db.DbRuntime.open(db_path).close()
    monkeypatch.setenv("AGENTGREP_DB", str(db_path))

    monkeypatch.setenv("AGENTGREP_CACHE", "off")
    off_runtime = mcp_server._build_search_runtime()
    assert off_runtime.cache_mode == "off"
    assert off_runtime.db is None

    monkeypatch.setenv("AGENTGREP_CACHE", "require")
    require_runtime = mcp_server._build_search_runtime()
    assert require_runtime.cache_mode == "require"
    assert require_runtime.db is not None
    with pytest.raises(sqlite3.OperationalError):
        _ = require_runtime.db.store.connection.execute(
            "INSERT INTO meta(key, value) VALUES('probe', '1')",
        )
    require_runtime.db.close()
