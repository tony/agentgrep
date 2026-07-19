# ruff: noqa: D102, D103
"""Functional tests for the ``agentgrep`` FastMCP server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import re
import typing as t

import pytest
from fastmcp import Client, FastMCP

import agentgrep
from agentgrep import mcp as _agentgrep_mcp_module

pytestmark = pytest.mark.mcp

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


class ToolAnnotationCase(t.NamedTuple):
    """Parametrized case for one client-visible MCP tool behavior hint."""

    test_id: str
    hint: str
    expected: bool


TOOL_ANNOTATION_CASES: list[ToolAnnotationCase] = [
    ToolAnnotationCase(
        test_id="read-only",
        hint="readOnlyHint",
        expected=True,
    ),
    ToolAnnotationCase(
        test_id="idempotent",
        hint="idempotentHint",
        expected=True,
    ),
    ToolAnnotationCase(
        test_id="closed-world",
        hint="openWorldHint",
        expected=False,
    ),
]


class McpOriginPhraseCase(t.NamedTuple):
    """Parametrized case for MCP origin filters with phrase terms."""

    test_id: str
    terms: list[str]
    matching_text: str
    nonmatching_text: str
    outside_text: str
    expected_texts: list[str]


class McpOriginLiteralTermCase(t.NamedTuple):
    """Parametrized case for MCP origin filters with literal punctuation terms."""

    test_id: str
    terms: list[str]
    matching_text: str
    nonmatching_text: str
    expected_texts: list[str]


class McpOriginCaseSensitiveCase(t.NamedTuple):
    """Parametrized case for MCP origin filters with case-sensitive terms."""

    test_id: str
    text: str
    expected_texts: list[str]


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
            "cwd": None,
            "repo": None,
            "branch": None,
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

MCP_ORIGIN_PHRASE_CASES: tuple[McpOriginPhraseCase, ...] = (
    McpOriginPhraseCase(
        # MCP terms are words: a space-containing element ANDs its
        # words, exactly as it does without origin filters.
        test_id="multiword-term-ands-words",
        terms=["exact phrase"],
        matching_text="exact phrase same",
        nonmatching_text="exact words then phrase same",
        outside_text="exact phrase other",
        expected_texts=["exact phrase same", "exact words then phrase same"],
    ),
    McpOriginPhraseCase(
        # A single boolean-carrying token parses as query language, the
        # same as it does without origin filters.
        test_id="boolean-word-token",
        terms=["rock OR roll"],
        matching_text="rock OR roll same",
        nonmatching_text="rock same",
        outside_text="rock OR roll other",
        expected_texts=["rock OR roll same", "rock same"],
    ),
    McpOriginPhraseCase(
        test_id="not-word-token",
        terms=["rock NOT roll"],
        matching_text="rock NOT roll same",
        nonmatching_text="rock same",
        outside_text="rock NOT roll other",
        expected_texts=["rock same"],
    ),
    McpOriginPhraseCase(
        test_id="paren-phrase",
        terms=["rock (roll)"],
        matching_text="rock (roll) same",
        nonmatching_text="rock roll same",
        outside_text="rock (roll) other",
        expected_texts=["rock (roll) same"],
    ),
)

MCP_ORIGIN_LITERAL_TERM_CASES: tuple[McpOriginLiteralTermCase, ...] = (
    McpOriginLiteralTermCase(
        test_id="https-url-literal",
        terms=["https://example.com"],
        matching_text="please inspect https://example.com",
        nonmatching_text="please inspect https and example",
        expected_texts=["please inspect https://example.com"],
    ),
    McpOriginLiteralTermCase(
        test_id="unknown-field-looking-literal",
        terms=["foo:bar"],
        matching_text="foo:bar appears literally",
        nonmatching_text="foo and bar appear separately",
        expected_texts=["foo:bar appears literally"],
    ),
    McpOriginLiteralTermCase(
        test_id="comma-literal",
        terms=["foo,bar"],
        matching_text="foo,bar appears literally",
        nonmatching_text="foo and bar appear separately",
        expected_texts=["foo,bar appears literally"],
    ),
    McpOriginLiteralTermCase(
        test_id="equals-literal",
        terms=["key=value"],
        matching_text="key=value appears literally",
        nonmatching_text="key and value appear separately",
        expected_texts=["key=value appears literally"],
    ),
    McpOriginLiteralTermCase(
        test_id="hash-literal",
        terms=["foo#bar"],
        matching_text="foo#bar appears literally",
        nonmatching_text="foo and bar appear separately",
        expected_texts=["foo#bar appears literally"],
    ),
    McpOriginLiteralTermCase(
        test_id="emoji-literal",
        terms=["\U0001f600"],
        matching_text="emoji \U0001f600 appears literally",
        nonmatching_text="emoji appears textually",
        expected_texts=["emoji \U0001f600 appears literally"],
    ),
    McpOriginLiteralTermCase(
        test_id="negative-literal",
        terms=["-foo"],
        matching_text="-foo appears literally",
        nonmatching_text="foo appears without dash",
        expected_texts=["-foo appears literally"],
    ),
    McpOriginLiteralTermCase(
        test_id="paren-literal",
        terms=["foo(bar)"],
        matching_text="foo(bar) appears literally",
        nonmatching_text="foo and then bar appear separately",
        expected_texts=["foo(bar) appears literally"],
    ),
)

MCP_ORIGIN_CASE_SENSITIVE_CASES: tuple[McpOriginCaseSensitiveCase, ...] = (
    McpOriginCaseSensitiveCase(
        test_id="exact-case",
        text="Needle appears here",
        expected_texts=["Needle appears here"],
    ),
    McpOriginCaseSensitiveCase(
        test_id="lowercase-miss",
        text="needle appears here",
        expected_texts=[],
    ),
)


class ResourceTextLike(t.Protocol):
    """Minimal text resource surface."""

    text: str | None


class ToolAnnotationsLike(t.Protocol):
    """Minimal MCP tool-annotation surface (client-visible behavior hints)."""

    readOnlyHint: bool | None
    idempotentHint: bool | None
    openWorldHint: bool | None


class ToolLike(t.Protocol):
    """Minimal MCP tool metadata surface."""

    name: str
    annotations: ToolAnnotationsLike | None


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
    cwd: str | None = None,
    branch: str | None = None,
) -> None:
    """Write a minimal Codex session containing one user prompt."""
    metadata: dict[str, object] = {"id": session_id, "model_provider": "openai"}
    if cwd is not None:
        metadata["cwd"] = cwd
    if branch is not None:
        metadata["git"] = {"branch": branch}
    write_jsonl(
        path,
        [
            {
                "type": "session_meta",
                "payload": metadata,
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


@pytest.mark.slow
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
    }
    assert any(str(resource.uri) == "agentgrep://capabilities" for resource in resources)
    assert any(str(resource.uri) == "agentgrep://sources" for resource in resources)
    assert any(prompt.name == "search_prompts" for prompt in prompts)
    assert any(template.uriTemplate == "agentgrep://sources/{agent}" for template in templates)


@pytest.mark.parametrize(
    "case",
    TOOL_ANNOTATION_CASES,
    ids=[case.test_id for case in TOOL_ANNOTATION_CASES],
)
async def test_mcp_tools_advertise_readonly_annotations(case: ToolAnnotationCase) -> None:
    """Every registered tool carries the hints that let a client auto-approve it.

    ``READONLY_TAGS`` is a FastMCP-internal selection filter and never crosses
    the wire; ``annotations`` are the protocol-level metadata a host reads back
    on ``tools/list``. Asserting over the live tool list rather than a frozen
    name set means a newly registered tool cannot silently omit these hints.
    """
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        tools = t.cast("list[ToolLike]", await client.list_tools())

    assert tools
    advertised = {
        tool.name: getattr(tool.annotations, case.hint, None) if tool.annotations else None
        for tool in tools
    }
    assert advertised == dict.fromkeys(advertised, case.expected)


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


@pytest.mark.slow
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


@pytest.mark.slow
async def test_mcp_search_honors_query_language_in_single_tokens(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace-containing terms without origin params stay query language."""
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
            {"terms": ["zzznope OR alpha"], "scope": "prompts", "limit": 5},
        )
        predicate = await client.call_tool(
            "search",
            {"terms": ["agent:codex alpha"], "scope": "prompts", "limit": 5},
        )

    union_data = t.cast("SearchToolDataLike", union.data)
    predicate_data = t.cast("SearchToolDataLike", predicate.data)
    assert len(union_data.results) == 1
    assert union_data.results[0].text == "alpha content here"
    assert len(predicate_data.results) == 1
    assert predicate_data.results[0].text == "alpha content here"


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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
async def test_mcp_search_explicit_origin_filters_survive_cursor(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit MCP origin filters narrow results and survive pagination."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    sessions = home / ".codex" / "sessions" / "2026" / "01" / "01"
    write_codex_prompt_session(
        sessions / "same-new.jsonl",
        session_id="same-new",
        timestamp="2026-06-02T00:00:00Z",
        text="origin serenity new",
        cwd="/workspace/agentgrep",
        branch="project-context",
    )
    write_codex_prompt_session(
        sessions / "same-old.jsonl",
        session_id="same-old",
        timestamp="2026-06-01T00:00:00Z",
        text="origin serenity old",
        cwd="/workspace/agentgrep",
        branch="project-context",
    )
    write_codex_prompt_session(
        sessions / "other.jsonl",
        session_id="other",
        timestamp="2026-06-03T00:00:00Z",
        text="origin serenity other",
        cwd="/workspace/other",
        branch="main",
    )

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        first = await client.call_tool(
            "search",
            {
                "terms": ["origin", "serenity"],
                "agent": "codex",
                "scope": "prompts",
                "cwd": "/workspace/agentgrep",
                "branch": "project-context",
                "limit": 1,
            },
        )
        first_data = tool_payload(first)
        cursor = first_data["page"]["next_cursor"]
        second = await client.call_tool("search", {"cursor": cursor})

    second_data = tool_payload(second)
    assert [row["text"] for row in first_data["results"]] == ["origin serenity new"]
    assert [row["text"] for row in second_data["results"]] == ["origin serenity old"]
    assert second_data["request"]["cwd"] == "/workspace/agentgrep"
    assert second_data["request"]["branch"] == "project-context"
    assert second_data["page"]["next_cursor"] is None


def test_mcp_explicit_origin_filters_keep_plain_terms_fast_path() -> None:
    """Explicit MCP origin filters keep plain terms out of compiled predicates."""
    from agentgrep.mcp import SearchRequestModel
    from agentgrep.mcp.tools.search_tools import _compile_request_query

    base_query = agentgrep.SearchQuery(
        terms=("origin", "serenity"),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=10,
    )
    request = SearchRequestModel(
        terms=["origin", "serenity"],
        agent="codex",
        scope="prompts",
        case_sensitive=False,
        cwd="/workspace/agentgrep",
        branch="project-context",
    )

    query = _compile_request_query(base_query, request)

    assert query.compiled is None
    assert query.terms == ("origin", "serenity")
    assert query.origin_filter == agentgrep.RecordOrigin(
        cwd="/workspace/agentgrep",
        branch="project-context",
    )
    assert agentgrep.matches_record(
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=pathlib.Path("/tmp/session.jsonl"),
            text="origin serenity",
            origin=agentgrep.RecordOrigin(
                cwd="/workspace/agentgrep/src",
                branch="project-context",
            ),
        ),
        query,
    )
    assert not agentgrep.matches_record(
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=pathlib.Path("/tmp/session.jsonl"),
            text="origin serenity",
            origin=agentgrep.RecordOrigin(
                cwd="/workspace/other",
                branch="project-context",
            ),
        ),
        query,
    )


@pytest.mark.slow
async def test_mcp_search_normalizes_origin_path_filters(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative and ~-prefixed MCP cwd filters resolve like the CLI flags."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    workspace = home / "work" / "agentgrep"
    workspace.mkdir(parents=True)
    sessions = home / ".codex" / "sessions" / "2026" / "01" / "01"
    write_codex_prompt_session(
        sessions / "same.jsonl",
        session_id="same",
        timestamp="2026-06-02T00:00:00Z",
        text="origin serenity",
        cwd=str(workspace.resolve()),
    )
    monkeypatch.chdir(workspace)

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        relative = await client.call_tool(
            "search",
            {"terms": ["serenity"], "agent": "codex", "scope": "prompts", "cwd": "."},
        )
        tilde = await client.call_tool(
            "search",
            {
                "terms": ["serenity"],
                "agent": "codex",
                "scope": "prompts",
                "cwd": "~/work/agentgrep",
            },
        )

    relative_data = tool_payload(relative)
    tilde_data = tool_payload(tilde)
    assert [row["text"] for row in relative_data["results"]] == ["origin serenity"]
    assert [row["text"] for row in tilde_data["results"]] == ["origin serenity"]


@pytest.mark.slow
async def test_mcp_search_ignores_blank_origin_filters(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blank cwd/repo/branch values never filter against the server's cwd."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    sessions = home / ".codex" / "sessions" / "2026" / "01" / "01"
    write_codex_prompt_session(
        sessions / "blank.jsonl",
        session_id="blank",
        timestamp="2026-06-02T00:00:00Z",
        text="origin serenity",
        cwd="/workspace/elsewhere",
    )

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        blank = await client.call_tool(
            "search",
            {
                "terms": ["serenity"],
                "agent": "codex",
                "scope": "prompts",
                "cwd": "",
                "repo": " ",
                "branch": "",
            },
        )

    blank_data = tool_payload(blank)
    assert [row["text"] for row in blank_data["results"]] == ["origin serenity"]


@pytest.mark.slow
async def test_mcp_search_origin_filters_scope_boolean_query(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generated MCP origin predicates apply to the whole boolean query."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    sessions = home / ".codex" / "sessions" / "2026" / "01" / "01"
    write_codex_prompt_session(
        sessions / "same-foo.jsonl",
        session_id="same-foo",
        timestamp="2026-06-03T00:00:00Z",
        text="foo same",
        cwd="/workspace/agentgrep",
    )
    write_codex_prompt_session(
        sessions / "same-bar.jsonl",
        session_id="same-bar",
        timestamp="2026-06-02T00:00:00Z",
        text="bar same",
        cwd="/workspace/agentgrep/src",
    )
    write_codex_prompt_session(
        sessions / "other-bar.jsonl",
        session_id="other-bar",
        timestamp="2026-06-01T00:00:00Z",
        text="bar other",
        cwd="/workspace/other",
    )

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "search",
            {
                "terms": ["foo", "OR", "bar"],
                "agent": "codex",
                "scope": "prompts",
                "cwd": "/workspace/agentgrep",
                "limit": 10,
            },
        )

    data = tool_payload(result)
    assert [row["text"] for row in data["results"]] == ["foo same", "bar same"]


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    MCP_ORIGIN_PHRASE_CASES,
    ids=[case.test_id for case in MCP_ORIGIN_PHRASE_CASES],
)
async def test_mcp_search_origin_filters_preserve_phrase_terms(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: McpOriginPhraseCase,
) -> None:
    """Generated MCP origin predicates leave user term semantics unchanged."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    sessions = home / ".codex" / "sessions" / "2026" / "01" / "01"
    write_codex_prompt_session(
        sessions / "same-exact.jsonl",
        session_id="same-exact",
        timestamp="2026-06-03T00:00:00Z",
        text=case.matching_text,
        cwd="/workspace/agentgrep",
    )
    write_codex_prompt_session(
        sessions / "same-separated.jsonl",
        session_id="same-separated",
        timestamp="2026-06-02T00:00:00Z",
        text=case.nonmatching_text,
        cwd="/workspace/agentgrep",
    )
    write_codex_prompt_session(
        sessions / "other-exact.jsonl",
        session_id="other-exact",
        timestamp="2026-06-01T00:00:00Z",
        text=case.outside_text,
        cwd="/workspace/other",
    )

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "search",
            {
                "terms": case.terms,
                "agent": "codex",
                "scope": "prompts",
                "cwd": "/workspace/agentgrep",
                "limit": 10,
            },
        )

    data = tool_payload(result)
    assert [row["text"] for row in data["results"]] == case.expected_texts


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    MCP_ORIGIN_LITERAL_TERM_CASES,
    ids=[case.test_id for case in MCP_ORIGIN_LITERAL_TERM_CASES],
)
async def test_mcp_search_origin_filters_preserve_literal_punctuation_terms(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: McpOriginLiteralTermCase,
) -> None:
    """Generated MCP origin predicates keep punctuation-heavy terms literal."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    sessions = home / ".codex" / "sessions" / "2026" / "01" / "01"
    write_codex_prompt_session(
        sessions / "same-match.jsonl",
        session_id="same-match",
        timestamp="2026-06-03T00:00:00Z",
        text=case.matching_text,
        cwd="/workspace/agentgrep",
    )
    write_codex_prompt_session(
        sessions / "same-miss.jsonl",
        session_id="same-miss",
        timestamp="2026-06-02T00:00:00Z",
        text=case.nonmatching_text,
        cwd="/workspace/agentgrep",
    )
    write_codex_prompt_session(
        sessions / "other-match.jsonl",
        session_id="other-match",
        timestamp="2026-06-01T00:00:00Z",
        text=case.matching_text,
        cwd="/workspace/other",
    )

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "search",
            {
                "terms": case.terms,
                "agent": "codex",
                "scope": "prompts",
                "cwd": "/workspace/agentgrep",
                "limit": 10,
            },
        )

    data = tool_payload(result)
    assert [row["text"] for row in data["results"]] == case.expected_texts


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    MCP_ORIGIN_CASE_SENSITIVE_CASES,
    ids=[case.test_id for case in MCP_ORIGIN_CASE_SENSITIVE_CASES],
)
async def test_mcp_search_origin_filters_preserve_case_sensitive_terms(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: McpOriginCaseSensitiveCase,
) -> None:
    """Generated MCP origin predicates preserve the request case mode."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    sessions = home / ".codex" / "sessions" / "2026" / "01" / "01"
    write_codex_prompt_session(
        sessions / "same-case.jsonl",
        session_id="same-case",
        timestamp="2026-06-03T00:00:00Z",
        text=case.text,
        cwd="/workspace/agentgrep",
    )

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "search",
            {
                "terms": ["Needle"],
                "agent": "codex",
                "scope": "prompts",
                "cwd": "/workspace/agentgrep",
                "case_sensitive": True,
                "limit": 10,
            },
        )

    data = tool_payload(result)
    assert [row["text"] for row in data["results"]] == case.expected_texts


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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
        "antigravity_ide.skills_text.v1",
    ):
        assert adapter_id in advertised_adapters, adapter_id


def test_mcp_capabilities_hide_backend_executable_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capability metadata exposes backend names without machine-local paths."""
    import agentgrep
    from agentgrep.mcp import resources

    monkeypatch.setattr(
        resources.agentgrep,
        "select_backends",
        lambda: agentgrep.BackendSelection(
            find_tool="/private/tooling/fd",
            grep_tool="/opt/tools/rg",
            json_tool=None,
        ),
    )

    backends = resources.build_capabilities().backends

    assert backends.find_tool == "fd"
    assert backends.grep_tool == "rg"
    assert backends.json_tool is None


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


def test_mcp_instructions_describe_path_privacy_boundaries() -> None:
    """Handshake privacy guidance does not overpromise path redaction."""
    from agentgrep.mcp.instructions import _build_instructions

    rendered = _build_instructions()
    assert "all paths returned are absolute" not in rendered
    assert "Home-directory prefixes" in rendered
    assert "external paths may remain absolute" in rendered
    assert "opaque result refs" in rendered


def test_mcp_instructions_scope_model_example() -> None:
    """Handshake guidance makes the conversation-only model example effective."""
    from agentgrep.mcp.instructions import _build_instructions

    rendered = _build_instructions()
    assert "scope:all model:gpt*" in rendered
    assert "agent:codex, model:gpt*" not in rendered


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


def test_catalog_agent_selector_tracks_store_catalog() -> None:
    """The MCP catalog filter accepts every agent emitted by the catalog."""
    from agentgrep.mcp import CatalogAgentSelector
    from agentgrep.store_catalog import CATALOG

    catalog_agents = {descriptor.agent for descriptor in CATALOG.stores}
    assert set(t.get_args(CatalogAgentSelector)) == catalog_agents | {"all"}


@pytest.mark.slow
async def test_mcp_list_stores_returns_catalog_entries() -> None:
    """``list_stores`` enumerates the StoreCatalog."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool("list_stores", {"agent": "all"})

    data = tool_payload(result)
    assert data["total"] >= 10
    assert {s["agent"] for s in data["stores"]} >= {"codex", "claude", "cursor-cli", "cursor-ide"}


@pytest.mark.slow
async def test_mcp_list_stores_filters_by_agent() -> None:
    """``list_stores`` respects the ``agent`` filter."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool("list_stores", {"agent": "cursor-cli"})

    data = tool_payload(result)
    assert data["total"] >= 1
    assert {s["agent"] for s in data["stores"]} == {"cursor-cli"}


@pytest.mark.slow
async def test_mcp_list_stores_filters_catalog_only_agent() -> None:
    """Catalog filtering accepts an unsupported agent emitted by the catalog."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool("list_stores", {"agent": "windsurf"})

    data = tool_payload(result)
    assert data["total"] >= 1
    assert {store["agent"] for store in data["stores"]} == {"windsurf"}


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
async def test_mcp_validate_query_empty_returns_guidance() -> None:
    """An empty validation request returns a structured usage diagnostic."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool("validate_query", {})

    data = tool_payload(result)
    assert data["matches"] is False
    assert data["regex_valid"] is True
    assert data["query_valid"] is None
    assert data["error_message"] == "provide terms, query, or both"


@pytest.mark.slow
async def test_mcp_query_language_resource_lists_every_field() -> None:
    """The query-language resource lists each registry field and operators."""
    from agentgrep.query import default_registry, parse_query, scope_widened_for_ast

    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        contents = await client.read_resource("agentgrep://query-language")

    payload = t.cast("dict[str, t.Any]", json.loads(extract_resource_text(contents)))
    field_names = {field["name"] for field in payload["fields"]}
    assert field_names == set(default_registry().known_names())
    exists = next(op for op in payload["operators"] if op["syntax"] == "field:*")
    exists_ast = parse_query(exists["example"], default_registry())
    assert exists["example"] == "agent:*"
    assert scope_widened_for_ast(exists_ast, "prompts") == "prompts"
    wildcard = next(op for op in payload["operators"] if op["syntax"] == "field:glob*")
    assert wildcard["example"] == "scope:all model:gpt*"
    assert payload["summary"]


@pytest.mark.slow
async def test_mcp_search_tool_description_mentions_query_language() -> None:
    """The search tool advertises the query language in its schema."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        tools = t.cast("list[ToolLike]", await client.list_tools())

    search = next(tool for tool in tools if tool.name == "search")
    description = t.cast("str | None", t.cast("t.Any", search).description)
    assert "query language" in (description or "")


@pytest.mark.documentation
async def test_docs_tool_input_schemas_match_live_mcp_schemas() -> None:
    """Every docs-only tool shim mirrors its live MCP input schema."""
    from docs._ext import agentgrep_fastmcp as docs_tools

    def without_examples(value: t.Any) -> t.Any:
        if isinstance(value, dict):
            return {key: without_examples(item) for key, item in value.items() if key != "examples"}
        if isinstance(value, list):
            return [without_examples(item) for item in value]
        return value

    agentgrep_mcp = load_agentgrep_mcp_module()
    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        live_tools = t.cast("list[ToolLike]", await client.list_tools())

    docs_server = FastMCP("agentgrep docs schema")
    for tool in live_tools:
        docs_server.tool(name=tool.name)(getattr(docs_tools, tool.name))
    async with Client(docs_server) as client:
        documented_tools = t.cast("list[ToolLike]", await client.list_tools())

    documented_by_name = {tool.name: tool for tool in documented_tools}
    description_mismatches: list[str] = []
    for live_tool in live_tools:
        documented = documented_by_name[live_tool.name]
        assert without_examples(t.cast("t.Any", documented).inputSchema) == without_examples(
            t.cast("t.Any", live_tool).inputSchema,
        ), live_tool.name
        live_description = " ".join(
            (t.cast("t.Any", live_tool).description or "").split(),
        )
        documented_description = " ".join(
            (t.cast("t.Any", documented).description or "").split(),
        )
        if documented_description != live_description:
            description_mismatches.append(live_tool.name)
    assert description_mismatches == []


@pytest.mark.documentation
@pytest.mark.slow
async def test_docs_list_stores_agent_examples_are_valid_selectors() -> None:
    """Documented agent examples stay inside the MCP selector enum."""
    from docs._ext import agentgrep_fastmcp as docs_tools

    docs_server = FastMCP("agentgrep docs examples")
    docs_server.tool(name="list_stores")(docs_tools.list_stores)
    async with Client(docs_server) as client:
        tools = t.cast("list[ToolLike]", await client.list_tools())

    schema = t.cast("dict[str, t.Any]", t.cast("t.Any", tools[0]).inputSchema)
    agent_schema = t.cast("dict[str, t.Any]", schema["properties"]["agent"])
    assert set(agent_schema["examples"]) <= set(agent_schema["enum"])


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


async def test_mcp_capabilities_match_registered_surface() -> None:
    """Capabilities exactly match the live tool, resource, and prompt surface."""
    agentgrep_mcp = load_agentgrep_mcp_module()

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        tools = t.cast("list[ToolLike]", await client.list_tools())
        resources = t.cast("list[ResourceLike]", await client.list_resources())
        templates = t.cast(
            "list[ResourceTemplateLike]",
            await client.list_resource_templates(),
        )
        prompts = t.cast("list[PromptLike]", await client.list_prompts())
        text = extract_resource_text(await client.read_resource("agentgrep://capabilities"))

    data = t.cast("dict[str, t.Any]", json.loads(text))
    assert set(data["tools"]) == {tool.name for tool in tools}
    assert set(data["resources"]) == {
        *(str(resource.uri) for resource in resources),
        *(template.uriTemplate for template in templates),
    }
    assert set(data["prompts"]) == {prompt.name for prompt in prompts}


class McpTermTokenizationCase(t.NamedTuple):
    """Parametrized case for MCP term word-splitting without origin params."""

    test_id: str
    terms: list[str]
    matching_text: str
    nonmatching_text: str
    expected_texts: list[str]


MCP_TERM_TOKENIZATION_CASES: tuple[McpTermTokenizationCase, ...] = (
    McpTermTokenizationCase(
        test_id="multiword-term-ands-words",
        terms=["error handling"],
        matching_text="error recovery and handling",
        nonmatching_text="error only",
        expected_texts=["error recovery and handling"],
    ),
    McpTermTokenizationCase(
        test_id="padded-term-strips-whitespace",
        terms=["alpha "],
        matching_text="deploy alpha.",
        nonmatching_text="beta only",
        expected_texts=["deploy alpha."],
    ),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    MCP_TERM_TOKENIZATION_CASES,
    ids=[case.test_id for case in MCP_TERM_TOKENIZATION_CASES],
)
async def test_mcp_search_terms_tokenize_as_words(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: McpTermTokenizationCase,
) -> None:
    """MCP terms whitespace-split into ANDed words, with or without origin."""
    agentgrep_mcp = load_agentgrep_mcp_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    sessions = home / ".codex" / "sessions" / "2026" / "01" / "01"
    write_codex_prompt_session(
        sessions / "match.jsonl",
        session_id="match",
        timestamp="2026-06-03T00:00:00Z",
        text=case.matching_text,
        cwd="/workspace/agentgrep",
    )
    write_codex_prompt_session(
        sessions / "miss.jsonl",
        session_id="miss",
        timestamp="2026-06-02T00:00:00Z",
        text=case.nonmatching_text,
        cwd="/workspace/agentgrep",
    )

    async with Client(agentgrep_mcp.build_mcp_server()) as client:
        result = await client.call_tool(
            "search",
            {"terms": case.terms, "agent": "codex", "scope": "prompts", "limit": 10},
        )

    data = tool_payload(result)
    assert [row["text"] for row in data["results"]] == case.expected_texts


class SearchStreamCloseCase(t.NamedTuple):
    """Parametrized case for an exit path out of the MCP search event loop."""

    test_id: str
    cancel_mid_scan: bool


SEARCH_STREAM_CLOSE_CASES = [
    SearchStreamCloseCase(test_id="cancelled-mid-scan", cancel_mid_scan=True),
    SearchStreamCloseCase(test_id="stream-exhausted", cancel_mid_scan=False),
]


@pytest.mark.parametrize(
    "case",
    SEARCH_STREAM_CLOSE_CASES,
    ids=[case.test_id for case in SEARCH_STREAM_CLOSE_CASES],
)
async def test_mcp_search_closes_event_stream(
    case: SearchStreamCloseCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The search tool finalizes its event stream on every exit path.

    The engine requests cancellation from the stream's ``finally`` block, so a
    client that cancels mid-scan only stops the scan if the tool closes the
    generator it opened.
    """
    from agentgrep import events as ag_events
    from agentgrep.mcp import SearchRequestModel
    from agentgrep.mcp.tools import search_tools

    scanning = asyncio.Event()
    closed = asyncio.Event()

    async def fake_stream(
        home: pathlib.Path,
        query: object,
        *,
        runtime: object | None = None,
    ) -> t.AsyncGenerator[object]:
        try:
            yield ag_events.SearchStarted(source_count=1)
            scanning.set()
            if not case.cancel_mid_scan:
                yield ag_events.SearchFinished(match_count=0, elapsed_seconds=0.0)
                return
            while True:  # a scan the consumer never drains
                await asyncio.sleep(0.01)
        finally:
            closed.set()

    monkeypatch.setattr(agentgrep, "aiter_search_events", fake_stream)
    request = SearchRequestModel(
        terms=["bliss"],
        agent="all",
        scope="prompts",
        case_sensitive=False,
        limit=5,
    )

    task = asyncio.create_task(search_tools._search_async(request))
    async with asyncio.timeout(5.0):
        if case.cancel_mid_scan:
            await scanning.wait()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        else:
            await task
        await closed.wait()

    assert closed.is_set()
