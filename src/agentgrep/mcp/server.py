"""FastMCP server assembly and stdio entry point."""

from __future__ import annotations

import logging
import typing as t

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware

from agentgrep._engine.runtime import SearchRuntime
from agentgrep.mcp._library import SERVER_VERSION
from agentgrep.mcp.instructions import _build_instructions
from agentgrep.mcp.middleware import (
    AgentgrepAuditMiddleware,
    AgentgrepResponseLimitingMiddleware,
)
from agentgrep.mcp.prompts import register_prompts
from agentgrep.mcp.resources import register_resources
from agentgrep.mcp.tools import register_tools

if t.TYPE_CHECKING:
    from agentgrep.db import DbRuntime

logger = logging.getLogger(__name__)

#: Byte ceiling for response truncation. Sized to fit a generous slice of
#: prompt/history records (a typical record is ~1 KB; 512 KB allows a few
#: hundred records before truncation fires).
DEFAULT_RESPONSE_LIMIT_BYTES = 512 * 1024


def _open_cache_runtime() -> DbRuntime | None:
    """Open the DB cache read-only in the calling thread, or return ``None``.

    SQLite connections are bound to their creating thread, and search
    tools run their work through ``asyncio.to_thread``, so the cache
    must be opened by the consulting thread — never held open by the
    server. A missing file means no cache; a foreign or corrupt file is
    probed eagerly (read-only connects are lazy) so it degrades here
    instead of exploding mid-query.
    """
    import sqlite3

    from agentgrep.db import DbRuntime, default_db_path

    db_path = default_db_path()
    if not db_path.exists():
        return None
    runtime = DbRuntime.open_readonly(db_path)
    try:
        _ = runtime.store.connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'",
        ).fetchone()
    except sqlite3.DatabaseError:
        runtime.close()
        logger.debug(
            "cache probe failed; serving without cache",
            extra={"agentgrep_cache_mode": "auto"},
        )
        return None
    return runtime


def _build_search_runtime() -> SearchRuntime:
    """Build the server's search runtime, honoring AGENTGREP_CACHE.

    MCP servers are configured through environment blocks, so the env
    var is the only cache lever that reaches a running server. The
    cache attaches as a per-consult opener — read-only, opened and
    closed by the thread that queries it — so the server never migrates
    or writes the cache file and never shares a connection across
    threads.
    """
    import os

    from agentgrep.cli.parser import resolve_cache_mode

    cache_mode = resolve_cache_mode(None, os.environ.get("AGENTGREP_CACHE"))
    runtime = SearchRuntime.with_source_scan_cache()
    runtime.cache_mode = cache_mode
    if cache_mode == "off":
        return runtime
    runtime.db_opener = _open_cache_runtime
    return runtime


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
    runtime = _build_search_runtime()
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
