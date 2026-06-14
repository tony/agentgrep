"""MCP tool registration dispatcher for ``agentgrep``."""

from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:
    from fastmcp import FastMCP

    from agentgrep._engine.runtime import SearchRuntime


def register_tools(mcp: FastMCP, *, runtime: SearchRuntime | None = None) -> None:
    """Register every ``agentgrep`` MCP tool on ``mcp``."""
    from agentgrep.mcp.tools import (
        catalog_tools,
        diagnostic_tools,
        discovery_tools,
        insights_tools,
        search_tools,
    )

    search_tools.register(mcp, runtime=runtime)
    discovery_tools.register(mcp)
    catalog_tools.register(mcp)
    diagnostic_tools.register(mcp)
    insights_tools.register(mcp)
