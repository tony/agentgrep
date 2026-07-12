"""FastMCP server assembly and stdio entry point."""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware

from agentgrep._engine.runtime import SearchRuntime
from agentgrep.mcp._library import DEFAULT_RESPONSE_LIMIT_BYTES, SERVER_VERSION
from agentgrep.mcp.instructions import _build_instructions
from agentgrep.mcp.middleware import (
    AgentgrepAuditMiddleware,
    AgentgrepResponseLimitingMiddleware,
)
from agentgrep.mcp.prompts import register_prompts
from agentgrep.mcp.resources import register_resources
from agentgrep.mcp.tools import register_tools


def build_mcp_server() -> FastMCP:
    """Build and return the FastMCP server instance."""
    mcp = FastMCP(
        name="agentgrep",
        version=SERVER_VERSION,
        instructions=_build_instructions(),
        # Middleware runs outermost-first. Order rationale:
        #   1. TimingMiddleware — neutral observer; start clock early so
        #      timing captures middleware cost too.
        #   2. ErrorHandlingMiddleware — transforms exceptions into proper MCP
        #      errors after Audit records the original failure type.
        #   3. AgentgrepAuditMiddleware — wraps response limiting so truncated
        #      ToolResult errors are audit-visible as outcome=error.
        #   4. AgentgrepResponseLimitingMiddleware — bounds successful tool
        #      output before the result returns through Audit.
        middleware=[
            TimingMiddleware(),
            ErrorHandlingMiddleware(transform_errors=True),
            AgentgrepAuditMiddleware(),
            AgentgrepResponseLimitingMiddleware(max_size=DEFAULT_RESPONSE_LIMIT_BYTES),
        ],
        on_duplicate="error",
    )
    runtime = SearchRuntime.with_source_scan_cache()
    register_tools(mcp, runtime=runtime)
    register_resources(mcp)
    register_prompts(mcp)
    return mcp


def main() -> int:
    """Run the MCP server over stdio."""
    build_mcp_server().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
