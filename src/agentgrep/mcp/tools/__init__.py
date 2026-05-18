"""MCP tool registration dispatcher for ``agentgrep``."""

from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:
    from fastmcp import FastMCP


def register_tools(mcp: FastMCP) -> None:
    """Register every ``agentgrep`` MCP tool on ``mcp``."""
    from agentgrep.mcp.tools import (
        catalog_tools,
        diagnostic_tools,
        discovery_tools,
        search_tools,
    )

    search_tools.register(mcp)
    discovery_tools.register(mcp)
    catalog_tools.register(mcp)
    diagnostic_tools.register(mcp)
