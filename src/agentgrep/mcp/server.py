"""FastMCP server assembly and stdio entry point."""

from __future__ import annotations

import pathlib

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware

from agentgrep import _telemetry
from agentgrep._engine.runtime import SearchRuntime
from agentgrep.mcp._library import SERVER_VERSION
from agentgrep.mcp.instructions import _build_instructions
from agentgrep.mcp.middleware import (
    AgentgrepArgumentPresenceMiddleware,
    AgentgrepAuditMiddleware,
    AgentgrepResponseLimitingMiddleware,
    AgentgrepTelemetryMiddleware,
    AgentgrepValidationErrorMiddleware,
    _install_fastmcp_validation_log_redaction,
)
from agentgrep.mcp.prompts import register_prompts
from agentgrep.mcp.resources import register_resources
from agentgrep.mcp.tools import register_tools

#: Byte ceiling for response truncation. Sized to fit a generous slice of
#: prompt/history records (a typical record is ~1 KB; 512 KB allows a few
#: hundred records before truncation fires).
DEFAULT_RESPONSE_LIMIT_BYTES = 512 * 1024


def build_mcp_server() -> FastMCP:
    """Build and return the FastMCP server instance."""
    _install_fastmcp_validation_log_redaction()
    mcp = FastMCP(
        name="agentgrep",
        version=SERVER_VERSION,
        instructions=_build_instructions(),
        # Middleware runs outermost-first. Order rationale:
        #   1. TimingMiddleware — neutral observer; start clock early so
        #      timing captures middleware cost too.
        #   2. ErrorHandlingMiddleware — transforms exceptions into proper MCP
        #      errors after Audit records the original failure type.
        #   3. AgentgrepTelemetryMiddleware — app request root; parents
        #      FastMCP request work and the tool-specific audit span.
        #   4. ValidationErrorMiddleware — maps FastMCP argument failures to
        #      concise invalid-params errors before generic transformation.
        #   5. ArgumentPresenceMiddleware — retains raw presence before
        #      FastMCP injects tool defaults.
        #   6. AgentgrepAuditMiddleware — wraps response limiting so semantic
        #      search truncation stays successful and fallback errors are
        #      audit-visible as outcome=error.
        #   7. AgentgrepResponseLimitingMiddleware — bounds tool output before
        #      the result returns through Audit.
        middleware=[
            TimingMiddleware(),
            ErrorHandlingMiddleware(transform_errors=True),
            AgentgrepTelemetryMiddleware(),
            AgentgrepValidationErrorMiddleware(),
            AgentgrepArgumentPresenceMiddleware(),
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
    telemetry = _telemetry.setup(
        repo_root=pathlib.Path(__file__).resolve().parents[3],
        service_name="agentgrep-mcp",
    )
    try:
        build_mcp_server().run()
        return 0
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
