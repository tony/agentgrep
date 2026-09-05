"""FastMCP server assembly and stdio entry point."""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.middleware.caching import ResponseCachingMiddleware
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware

from agentgrep._engine.runtime import SearchRuntime
from agentgrep.mcp._library import SERVER_VERSION
from agentgrep.mcp.instructions import _build_instructions
from agentgrep.mcp.middleware import (
    AgentgrepArgumentPresenceMiddleware,
    AgentgrepAuditMiddleware,
    AgentgrepResponseLimitingMiddleware,
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

#: How long a cached ``resources/read`` stays fresh. The cache has no
#: invalidation hook, so this window IS the worst-case staleness. A minute
#: absorbs the repeat orientation reads inside one session while bounding how
#: wrong a long conversation can get; the walk itself dominates a miss, so a
#: short TTL costs nothing measurable.
RESOURCE_CACHE_TTL_SECONDS = 60

#: Ceiling on a single cached entry. The stock 1 MiB is below the source
#: listing this cache exists for, and an oversized entry is dropped silently —
#: no error, no log, a permanent no-op. Sized well clear of it.
RESOURCE_CACHE_MAX_ITEM_BYTES = 32 * 1024 * 1024


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
        #   3. ValidationErrorMiddleware — maps FastMCP argument failures to
        #      concise invalid-params errors before generic transformation.
        #   4. ArgumentPresenceMiddleware — retains raw presence before
        #      FastMCP injects tool defaults.
        #   5. AgentgrepAuditMiddleware — wraps response limiting so semantic
        #      search truncation stays successful and fallback errors are
        #      audit-visible as outcome=error.
        #   6. AgentgrepResponseLimitingMiddleware — bounds tool output before
        #      the result returns through Audit.
        middleware=[
            TimingMiddleware(),
            ErrorHandlingMiddleware(transform_errors=True),
            AgentgrepValidationErrorMiddleware(),
            AgentgrepArgumentPresenceMiddleware(),
            AgentgrepAuditMiddleware(),
            AgentgrepResponseLimitingMiddleware(max_size=DEFAULT_RESPONSE_LIMIT_BYTES),
            # Resource reads only. Tool results are deliberately excluded:
            # SourceScanCache already keys them on file fingerprints, which
            # invalidates exactly, where this cache expires only on time.
            ResponseCachingMiddleware(
                read_resource_settings={"ttl": RESOURCE_CACHE_TTL_SECONDS},
                list_tools_settings={"enabled": False},
                list_resources_settings={"enabled": False},
                list_prompts_settings={"enabled": False},
                get_prompt_settings={"enabled": False},
                call_tool_settings={"enabled": False},
                max_item_size=RESOURCE_CACHE_MAX_ITEM_BYTES,
            ),
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
