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
            "Use the `search` tool to find full user prompts about "
            f"{topic!r}. Search `prompts` only, keep newest-first ordering, "
            f"and limit the search to agent={agent!r} if requested."
        )

    _ = search_prompts_prompt

    @mcp.prompt(
        name="search_history",
        description="Guide the client to search assistant or command history records.",
        tags={"search", "history", "readonly"},
    )
    def search_history_prompt(topic: str, agent: str = "all") -> str:
        return (
            "Use the `search` tool to find matching history records about "
            f"{topic!r}. Search `history` only, and restrict to "
            f"agent={agent!r} when appropriate."
        )

    _ = search_history_prompt

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
