"""MCP prompt templates that guide clients."""

from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:
    from fastmcp import FastMCP


def register_prompts(mcp: FastMCP) -> None:
    """Register every ``agentgrep`` prompt on ``mcp``."""

    @mcp.prompt(
        name="search_prompts",
        description="Guide the client to search for matching user prompts.",
        tags={"search", "prompts", "readonly"},
    )
    def search_prompts_prompt(topic: str, agent: str = "all") -> str:
        return (
            "Use the `search` tool with effort='prompt' and scope='prompts' to find "
            f"dedicated prompt-history records about {topic!r}. Keep newest-first "
            f"ordering and limit the search to agent={agent!r} if requested. "
            "A fast miss is not corpus-wide. Use effort='targeted' only when the user "
            "requests bounded deep search; do not auto-escalate."
        )

    _ = search_prompts_prompt

    @mcp.prompt(
        name="search_conversations",
        description="Guide the client to search full conversation/session records.",
        tags={"search", "conversations", "readonly"},
    )
    def search_conversations_prompt(topic: str, agent: str = "all") -> str:
        return (
            "Use the `search` tool to find matching conversation records about "
            f"{topic!r}. Set scope='conversations' and choose effort='targeted' "
            "for bounded approximate routing or effort='exhaustive' for complete "
            f"readable coverage. Restrict to agent={agent!r} when appropriate."
        )

    _ = search_conversations_prompt

    @mcp.prompt(
        name="inspect_stores",
        description="Guide the client to inspect discovered agent stores and session files.",
        tags={"discovery", "readonly"},
    )
    def inspect_stores_prompt(agent: str = "all", pattern: str = "") -> str:
        return (
            "Use the `find` tool to inspect discovered stores, session files, and "
            f"SQLite databases for agent={agent!r}. "
            f"Apply the pattern {pattern!r} when it is non-empty."
        )

    _ = inspect_stores_prompt
