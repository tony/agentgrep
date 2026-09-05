"""MCP contracts for source depth metadata and prompt guidance."""

from __future__ import annotations

import pathlib
import typing as t

import mcp.types as mt
import pytest
from fastmcp import Client, FastMCP

from agentgrep.mcp import SourceHandleLike
from agentgrep.mcp.models import SourceRecordModel
from agentgrep.mcp.prompts import register_prompts
from agentgrep.records import SourceHandle
from agentgrep.stores import StoreCoverage

pytestmark = pytest.mark.mcp


@pytest.mark.parametrize(
    (
        "store",
        "adapter_id",
        "coverage",
        "source_kind",
        "path_kind",
        "expected",
    ),
    [
        pytest.param(
            "codex.history",
            "codex.history_jsonl.v1",
            StoreCoverage.DEFAULT_SEARCH,
            "jsonl",
            "history_file",
            {
                "searchable": True,
                "search_by_default": True,
                "inspectable": True,
                "store_role": "prompt_history",
                "required_effort": "prompt",
                "searchable_reason": "searched by fast prompt effort",
            },
            id="fast-prompt-history",
        ),
        pytest.param(
            "codex.sessions",
            "codex.sessions_jsonl.v1",
            StoreCoverage.DEFAULT_SEARCH,
            "jsonl",
            "session_file",
            {
                "searchable": True,
                "search_by_default": True,
                "inspectable": True,
                "store_role": "primary_chat",
                "required_effort": "exhaustive",
                "searchable_reason": (
                    "targeted effort may select this conversation store; exhaustive "
                    "effort guarantees it is eligible for direct search"
                ),
            },
            id="default-coverage-transcript",
        ),
        pytest.param(
            "cursor-cli.chats",
            "cursor_cli.chats_protobuf.v1",
            StoreCoverage.INSPECTABLE,
            "sqlite",
            "sqlite_db",
            {
                "searchable": True,
                "search_by_default": False,
                "inspectable": True,
                "store_role": "primary_chat",
                "required_effort": "exhaustive",
                "searchable_reason": (
                    "targeted effort may select this conversation store; exhaustive "
                    "effort guarantees it is eligible for direct search"
                ),
            },
            id="inspectable-transcript",
        ),
        pytest.param(
            "codex.logs_db",
            "codex.logs_sqlite.v1",
            StoreCoverage.CATALOG_ONLY,
            "sqlite",
            "sqlite_db",
            {
                "searchable": False,
                "search_by_default": False,
                "inspectable": True,
                "store_role": "app_state",
                "required_effort": None,
                "searchable_reason": ("not searchable; available for explicit inspection"),
            },
            id="catalog-only-app-state",
        ),
    ],
)
def test_source_metadata_separates_coverage_from_search_effort(
    store: str,
    adapter_id: str,
    coverage: StoreCoverage,
    source_kind: t.Literal["json", "jsonl", "sqlite", "text", "opaque"],
    path_kind: t.Literal[
        "history_file",
        "session_file",
        "sqlite_db",
        "store_file",
    ],
    expected: dict[str, object],
) -> None:
    """Describe discovery eligibility separately from the effort that reads it."""
    source = SourceHandle(
        agent="codex" if store.startswith("codex.") else "cursor-cli",
        store=store,
        adapter_id=adapter_id,
        path=pathlib.Path("source"),
        path_kind=path_kind,
        source_kind=source_kind,
        search_root=None,
        mtime_ns=0,
        coverage=coverage,
    )

    model = SourceRecordModel.from_source(t.cast("SourceHandleLike", source))

    assert model.model_dump(include=set(expected)) == expected


@pytest.mark.slow
async def test_search_prompts_discloses_search_coverage() -> None:
    """Keep the registered prompt from treating a fast miss as corpus-wide."""
    server = FastMCP("prompt-contract")
    register_prompts(server)

    async with Client(server) as client:
        result = await client.get_prompt_mcp(
            "search_prompts",
            arguments={"topic": "release notes", "agent": "codex"},
        )

    assert len(result.messages) == 1
    message = result.messages[0]
    assert message.role == "user"
    assert isinstance(message.content, mt.TextContent)

    text = message.content.text
    assert "'release notes'" in text
    assert "agent='codex'" in text
    assert "effort='prompt'" in text
    assert "effort='targeted'" in text
    assert "not corpus-wide" in text


async def test_unknown_agent_is_rejected_not_answered_empty() -> None:
    """An agent name outside the enum must fail, not read as "no sources".

    The parameter was a bare ``str`` cast to the selector, so a typo
    returned ``[]`` — indistinguishable from a real agent with nothing
    indexed, and silently wrong.
    """
    from agentgrep.mcp.server import build_mcp_server

    async with Client(build_mcp_server()) as client:
        known = await client.read_resource("agentgrep://sources/codex")
        assert known[0].text is not None

        with pytest.raises(Exception, match="validation error"):
            await client.read_resource("agentgrep://sources/definitely-not-an-agent")
