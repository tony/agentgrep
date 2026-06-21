"""FastMCP server assembly and stdio entry point."""

from __future__ import annotations

import logging
import pathlib
import time

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware

from agentgrep import _telemetry
from agentgrep._engine.runtime import SearchRuntime
from agentgrep.mcp._library import SERVER_VERSION
from agentgrep.mcp.instructions import _build_instructions
from agentgrep.mcp.middleware import AgentgrepAuditMiddleware, AgentgrepTelemetryMiddleware
from agentgrep.mcp.prompts import register_prompts
from agentgrep.mcp.resources import register_resources
from agentgrep.mcp.tools import register_tools

logger = logging.getLogger(__name__)
_MCP_FORCE_FLUSH_TIMEOUT_MS = 2_000

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
        #   4. AgentgrepTelemetryMiddleware — app request root; parents
        #      FastMCP request work and the tool-specific audit span.
        #   5. AgentgrepAuditMiddleware — innermost log hook; records
        #      outcome=ok or outcome=error for every call.
        middleware=[
            TimingMiddleware(),
            ResponseLimitingMiddleware(max_size=DEFAULT_RESPONSE_LIMIT_BYTES),
            ErrorHandlingMiddleware(transform_errors=True),
            AgentgrepTelemetryMiddleware(),
            AgentgrepAuditMiddleware(),
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
    started_at = time.monotonic()
    try:
        with _telemetry.root_span(
            "agentgrep.mcp.server",
            agentgrep_surface="mcp",
            agentgrep_operation="mcp.server",
        ):
            logger.info(
                "mcp server started",
                extra={
                    "agentgrep_surface": "mcp",
                    "agentgrep_operation": "mcp.server",
                },
            )
            lifecycle_started_at = time.monotonic()
            try:
                with _telemetry.span(
                    "agentgrep.mcp.server.lifecycle",
                    agentgrep_surface="mcp",
                    agentgrep_operation="mcp.server.lifecycle",
                ):
                    build_mcp_server().run()
                    lifecycle_duration_ms = (time.monotonic() - lifecycle_started_at) * 1000.0
                    _telemetry.set_span_attribute("agentgrep_outcome", "ok")
                    _telemetry.set_span_attribute(
                        "agentgrep_duration_ms",
                        lifecycle_duration_ms,
                    )
                    logger.info(
                        "mcp server lifecycle completed",
                        extra={
                            "agentgrep_surface": "mcp",
                            "agentgrep_operation": "mcp.server.lifecycle",
                            "agentgrep_outcome": "ok",
                            "agentgrep_duration_ms": lifecycle_duration_ms,
                        },
                    )
            except BaseException as exc:
                duration_ms = (time.monotonic() - lifecycle_started_at) * 1000.0
                _telemetry.set_span_attribute("agentgrep_outcome", "error")
                _telemetry.set_span_attribute("agentgrep_error_type", type(exc).__name__)
                _telemetry.set_span_attribute("agentgrep_duration_ms", duration_ms)
                logger.info(
                    "mcp server lifecycle failed",
                    extra={
                        "agentgrep_surface": "mcp",
                        "agentgrep_operation": "mcp.server.lifecycle",
                        "agentgrep_outcome": "error",
                        "agentgrep_error_type": type(exc).__name__,
                        "agentgrep_duration_ms": duration_ms,
                    },
                )
                raise
            flush_started_at = time.monotonic()
            with _telemetry.span(
                "agentgrep.mcp.flush",
                agentgrep_surface="mcp",
                agentgrep_operation="mcp.flush",
                agentgrep_mcp_flush_timeout_ms=_MCP_FORCE_FLUSH_TIMEOUT_MS,
            ):
                flush_ok = telemetry.force_flush(timeout_millis=_MCP_FORCE_FLUSH_TIMEOUT_MS)
                flush_duration_ms = (time.monotonic() - flush_started_at) * 1000.0
                _telemetry.set_span_attribute("agentgrep_outcome", "ok")
                _telemetry.set_span_attribute("agentgrep_mcp_flush_ok", flush_ok)
                _telemetry.set_span_attribute("agentgrep_duration_ms", flush_duration_ms)
                _telemetry.record_metric(
                    "agentgrep.mcp.flush.duration",
                    flush_duration_ms,
                    agentgrep_surface="mcp",
                    agentgrep_operation="mcp.flush",
                    agentgrep_mcp_flush_ok=flush_ok,
                )
                logger.info(
                    "mcp telemetry flushed",
                    extra={
                        "agentgrep_surface": "mcp",
                        "agentgrep_operation": "mcp.flush",
                        "agentgrep_outcome": "ok",
                        "agentgrep_mcp_flush_ok": flush_ok,
                        "agentgrep_mcp_flush_timeout_ms": _MCP_FORCE_FLUSH_TIMEOUT_MS,
                        "agentgrep_duration_ms": flush_duration_ms,
                    },
                )
            duration_ms = (time.monotonic() - started_at) * 1000.0
            _telemetry.set_span_attribute("agentgrep_outcome", "ok")
            _telemetry.set_span_attribute("agentgrep_exit_code", 0)
            _telemetry.set_span_attribute("agentgrep_duration_ms", duration_ms)
            logger.info(
                "mcp server completed",
                extra={
                    "agentgrep_surface": "mcp",
                    "agentgrep_operation": "mcp.server",
                    "agentgrep_outcome": "ok",
                    "agentgrep_exit_code": 0,
                    "agentgrep_duration_ms": duration_ms,
                },
            )
        return 0
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
