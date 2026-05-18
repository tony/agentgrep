"""FastMCP server assembly and stdio entry point."""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware

from agentgrep.mcp._library import SERVER_VERSION
from agentgrep.mcp.instructions import _build_instructions
from agentgrep.mcp.middleware import AgentgrepAuditMiddleware
from agentgrep.mcp.prompts import register_prompts
from agentgrep.mcp.resources import register_resources
from agentgrep.mcp.tools import register_tools

#: Byte ceiling for response truncation. Sized to fit a generous slice of
#: prompt/history records (a typical record is ~1 KB; 512 KB allows a few
#: hundred records before truncation fires).
DEFAULT_RESPONSE_LIMIT_BYTES = 512 * 1024


def build_mcp_server() -> FastMCP:
    """Build and return the FastMCP server instance."""
    mcp = FastMCP(
        name="agentgrep",
        version=SERVER_VERSION,
        instructions=_build_instructions(),
        # Middleware runs outermost-first. Order rationale:
        #   1. TimingMiddleware — neutral observer; start clock early so
        #      timing captures middleware cost too.
        #   2. ResponseLimitingMiddleware — bound the response before
        #      ErrorHandlingMiddleware can transform exceptions; keeps the
        #      size cap independent of error path.
        #   3. ErrorHandlingMiddleware — transforms exceptions into proper
        #      MCP errors; sits outside Audit so failed-tool records still
        #      log the failure with structured extras.
        #   4. AgentgrepAuditMiddleware — innermost log hook; records
        #      outcome=ok or outcome=error for every call.
        middleware=[
            TimingMiddleware(),
            ResponseLimitingMiddleware(max_size=DEFAULT_RESPONSE_LIMIT_BYTES),
            ErrorHandlingMiddleware(transform_errors=True),
            AgentgrepAuditMiddleware(),
        ],
        on_duplicate="error",
    )
    register_tools(mcp)
    register_resources(mcp)
    register_prompts(mcp)
    return mcp


def main() -> int:
    """Run the MCP server over stdio."""
    build_mcp_server().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
