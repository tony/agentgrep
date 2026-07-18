"""FastMCP middleware for the ``agentgrep`` server.

Holds the server's response-limiting and structured audit middleware. FastMCP's
own timing and error-handling middleware are wired alongside them from
:mod:`agentgrep.mcp.server`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
import time
import typing as t

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from fastmcp.tools.base import ToolResult

from agentgrep import _telemetry

_KNOWN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "search",
        "recent_sessions",
        "find",
        "list_sources",
        "filter_sources",
        "summarize_discovery",
        "list_stores",
        "get_store_descriptor",
        "inspect_record_sample",
        "inspect_result",
        "validate_query",
    },
)
"""Finite registered tool vocabulary safe for telemetry dimensions."""

_KNOWN_MCP_METHODS: frozenset[str] = frozenset(
    {
        "completion/complete",
        "initialize",
        "logging/setLevel",
        "ping",
        "prompts/get",
        "prompts/list",
        "resources/list",
        "resources/read",
        "resources/subscribe",
        "resources/templates/list",
        "resources/unsubscribe",
        "tasks/cancel",
        "tasks/get",
        "tasks/list",
        "tasks/result",
        "tools/call",
        "tools/list",
    },
)
"""Bounded client-to-server MCP request methods."""

_SAFE_ENUM_ARG_VALUES: dict[str, frozenset[str]] = {
    "agent": frozenset(
        {
            "all",
            "antigravity-cli",
            "antigravity-ide",
            "claude",
            "codex",
            "cursor-cli",
            "cursor-ide",
            "gemini",
            "grok",
            "opencode",
            "pi",
            "vscode",
            "windsurf",
        },
    ),
    "coverage_filter": frozenset(
        {"default_search", "inspectable", "catalog_only", "private"},
    ),
    "path_kind_filter": frozenset(
        {"history_file", "session_file", "sqlite_db", "store_file"},
    ),
    "scope": frozenset({"prompts", "conversations", "all"}),
    "source_kind_filter": frozenset({"json", "jsonl", "sqlite", "text", "opaque"}),
}
"""Enum arguments whose validated vocabulary is safe to export raw."""

_SAFE_BOOL_ARG_NAMES: frozenset[str] = frozenset(
    {"case_sensitive", "include_non_default", "regex", "search_default_only"},
)
_SAFE_COUNT_ARG_RANGES: dict[str, tuple[int, int]] = {
    "hours": (1, 24 * 30),
    "limit": (1, 10_000),
    "sample_size": (1, 20),
}
_PATH_ARG_NAMES: frozenset[str] = frozenset({"cwd", "repo"})
_KNOWN_ARG_NAMES: frozenset[str] = frozenset(
    {
        "adapter_id",
        "agent",
        "branch",
        "case_sensitive",
        "coverage_filter",
        "cursor",
        "cwd",
        "hours",
        "include_non_default",
        "limit",
        "path_kind_filter",
        "pattern",
        "query",
        "ref",
        "regex",
        "repo",
        "role_filter",
        "sample_size",
        "sample_text",
        "scope",
        "search_default_only",
        "source_kind_filter",
        "source_path",
        "store_id",
        "terms",
    },
)
_MAX_REDACTED_LEN = 1_000_000
_MAX_DIGEST_INPUT_LEN = 256
_MAX_ARGS_TO_SUMMARIZE = 64

logger = logging.getLogger(__name__)


class AgentgrepResponseLimitingMiddleware(ResponseLimitingMiddleware):
    """Mark truncated tool results as MCP errors.

    Truncation removes structured content, so a successful result would no
    longer satisfy the tool's advertised output schema. Error results preserve
    the bounded text and metadata without triggering output-schema validation.
    """

    def _truncate_to_result(
        self,
        text: str,
        meta: dict[str, t.Any] | None = None,
    ) -> ToolResult:
        truncated = super()._truncate_to_result(text, meta)
        return ToolResult(
            content=truncated.content,
            meta=truncated.meta,
            is_error=True,
        )


def _inbound_otel_context() -> object | None:
    """Return the inbound traceparent context from the MCP request meta, if any.

    None unless the caller propagated a W3C ``traceparent`` in request meta;
    stock MCP clients (including agentgrep's CLI) do not.
    """
    try:
        from mcp.server.lowlevel.server import request_ctx

        meta = getattr(request_ctx.get(), "meta", None)
    except Exception:
        return None
    if not meta:
        return None
    try:
        from fastmcp.telemetry import extract_trace_context

        return extract_trace_context(dict(meta))
    except Exception:
        return None


def _redact_digest(value: str) -> dict[str, t.Any]:
    """Return a capped length and bounded-prefix digest of ``value``.

    The digest is stable and deterministic, so operators can correlate the
    same payload across log lines without ever recording the payload itself.

    Examples
    --------
    >>> _redact_digest("hello")
    {'type': 'str', 'len': 5, 'sha256_prefix': '2cf24dba5fb0'}
    >>> _redact_digest("")
    {'type': 'str', 'len': 0, 'sha256_prefix': 'e3b0c44298fc'}
    """
    return {
        "type": "str",
        "len": min(len(value), _MAX_REDACTED_LEN),
        "sha256_prefix": hashlib.sha256(
            value[:_MAX_DIGEST_INPUT_LEN].encode("utf-8"),
        ).hexdigest()[:12],
    }


def _redact_path(value: str) -> dict[str, t.Any]:
    """Return path-shaped metadata without the path value."""
    redacted = _redact_digest(value)
    redacted["kind"] = "path"
    redacted["is_absolute"] = pathlib.PurePath(
        value[:_MAX_DIGEST_INPUT_LEN],
    ).is_absolute()
    return redacted


def _is_path_arg_name(key: str) -> bool:
    """Return whether an MCP argument name is expected to hold a path."""
    key_folded = key.casefold()
    return key_folded in _PATH_ARG_NAMES or key_folded == "path" or key_folded.endswith("_path")


def _redact_container(value: list[t.Any] | dict[str, t.Any]) -> dict[str, t.Any]:
    """Return constant-work shape metadata for a JSON container."""
    return {
        "type": "list" if isinstance(value, list) else "dict",
        "len": min(len(value), _MAX_REDACTED_LEN),
    }


def _summarize_private_value(value: t.Any) -> t.Any:
    """Return bounded non-reversible metadata for an untrusted argument."""
    if value is None:
        return None
    if isinstance(value, str):
        return _redact_digest(value)
    if isinstance(value, list | dict):
        return _redact_container(value)
    if isinstance(value, bool):
        return {"type": "bool"}
    if isinstance(value, int):
        return {"type": "int"}
    if isinstance(value, float):
        return {"type": "float"}
    return {"type": "other"}


def _safe_bounded_arg(key: str, value: t.Any) -> bool:
    """Return whether ``value`` belongs to a finite telemetry-safe domain."""
    enum_values = _SAFE_ENUM_ARG_VALUES.get(key)
    if enum_values is not None:
        return isinstance(value, str) and value in enum_values
    if key in _SAFE_BOOL_ARG_NAMES:
        return isinstance(value, bool)
    count_range = _SAFE_COUNT_ARG_RANGES.get(key)
    if count_range is None or isinstance(value, bool) or not isinstance(value, int):
        return False
    lower, upper = count_range
    return lower <= value <= upper


def _summarize_args(args: dict[str, t.Any]) -> dict[str, t.Any]:
    """Summarize tool arguments for audit logging.

    All input is untrusted because middleware runs before tool lookup and
    Pydantic validation. Only finite enum, boolean, and bounded count domains
    pass through. Strings become non-reversible digests, containers expose only
    constant-work shape metadata, and paths retain redacted path metadata.

    Examples
    --------
    Non-sensitive scalars pass through unchanged:

    >>> _summarize_args({"agent": "codex", "regex": True})
    {'agent': 'codex', 'regex': True}

    Sensitive scalar payloads are replaced by a digest dict:

    >>> _summarize_args({"pattern": "secret-token"})["pattern"]["len"]
    12

    Containers are summarized as one bounded value:

    >>> redacted = _summarize_args({"terms": ["alpha", "beta"]})
    >>> redacted["terms"]["len"]
    2
    >>> "alpha" in str(redacted)
    False

    Opaque page cursors are digested as a whole because they encode the
    original terms or pattern:

    >>> _summarize_args({"cursor": "agcur1:secret"})["cursor"]["len"]
    13

    Path-shaped arguments are redacted before logs or spans see them:

    >>> _summarize_args({"source_path": "/tmp/agentgrep/history.json"})["source_path"]["kind"]
    'path'
    """
    summary: dict[str, t.Any] = {}
    unknown_arg_count = 0
    for index, (key, value) in enumerate(args.items()):
        if index >= _MAX_ARGS_TO_SUMMARIZE:
            unknown_arg_count += len(args) - index
            break
        if not isinstance(key, str) or key not in _KNOWN_ARG_NAMES:
            unknown_arg_count += 1
            continue
        if _is_path_arg_name(key) and isinstance(value, str):
            summary[key] = _redact_path(value)
        elif _safe_bounded_arg(key, value):
            summary[key] = value
        else:
            summary[key] = _summarize_private_value(value)
    if unknown_arg_count:
        summary["unknown_arg_count"] = min(unknown_arg_count, 1_000)
    return summary


def _known_value(value: object, allowed: frozenset[str]) -> str:
    """Return a finite known value or the low-cardinality ``unknown`` label."""
    return value if isinstance(value, str) and value in allowed else "unknown"


class AgentgrepTelemetryMiddleware(Middleware):
    """Open an app-level span per observable FastMCP request."""

    async def on_request(
        self,
        context: MiddlewareContext[t.Any],
        call_next: t.Callable[[MiddlewareContext[t.Any]], t.Awaitable[t.Any]],
    ) -> t.Any:
        """Wrap an observable MCP request in its app-level span."""
        method = _known_value(context.method, _KNOWN_MCP_METHODS)
        if method == "initialize":
            return await call_next(context)
        start = time.monotonic()
        attributes: dict[str, object] = {
            "agentgrep_surface": "mcp",
            "agentgrep_operation": "mcp.request",
            "agentgrep_mcp_method": method,
        }
        inbound = _inbound_otel_context()
        detach = _telemetry.attach_otel_context(inbound)
        try:
            with _telemetry.span(
                "mcp.server.request",
                inherit_otel_context=inbound is not None,
                **attributes,
            ):
                try:
                    result = await call_next(context)
                except asyncio.CancelledError:
                    duration_ms = (time.monotonic() - start) * 1000.0
                    _telemetry.set_span_attribute("agentgrep_outcome", "cancelled")
                    _telemetry.set_span_attribute("agentgrep_duration_ms", duration_ms)
                    logger.info(
                        "mcp request cancelled",
                        extra={
                            **attributes,
                            "agentgrep_outcome": "cancelled",
                            "agentgrep_duration_ms": duration_ms,
                        },
                    )
                    raise
                except Exception as exc:
                    duration_ms = (time.monotonic() - start) * 1000.0
                    error_type = _telemetry.error_type_name(exc)
                    _telemetry.set_span_attribute("agentgrep_outcome", "error")
                    _telemetry.set_span_attribute("agentgrep_error_type", error_type)
                    _telemetry.set_span_attribute("agentgrep_duration_ms", duration_ms)
                    logger.info(
                        "mcp request failed",
                        extra={
                            **attributes,
                            "agentgrep_outcome": "error",
                            "agentgrep_error_type": error_type,
                            "agentgrep_duration_ms": duration_ms,
                        },
                    )
                    raise
                duration_ms = (time.monotonic() - start) * 1000.0
                _telemetry.set_span_attribute("agentgrep_outcome", "ok")
                _telemetry.set_span_attribute("agentgrep_duration_ms", duration_ms)
                logger.info(
                    "mcp request completed",
                    extra={
                        **attributes,
                        "agentgrep_outcome": "ok",
                        "agentgrep_duration_ms": duration_ms,
                    },
                )
                return result
        finally:
            detach()


class AgentgrepAuditMiddleware(Middleware):
    """Emit a structured log record per ``agentgrep`` tool invocation.

    Records carry ``agentgrep_tool``, ``agentgrep_outcome``,
    ``agentgrep_duration_ms``, ``agentgrep_error_type`` (on failure),
    ``agentgrep_args_summary``. The logger name defaults to
    ``agentgrep.audit`` so operators can route it independently of the
    ``agentgrep`` library logger. Client-visible :class:`ToolResult` errors use
    the stable error type ``ToolResultError``.

    Parameters
    ----------
    logger_name : str
        Name of the :mod:`logging` logger used for audit records.
    """

    def __init__(self, logger_name: str = "agentgrep.audit") -> None:
        self._logger = logging.getLogger(logger_name)

    async def on_call_tool(
        self,
        context: MiddlewareContext[t.Any],
        call_next: t.Callable[[MiddlewareContext[t.Any]], t.Awaitable[t.Any]],
    ) -> t.Any:
        """Wrap the tool call with a timer and emit one audit record."""
        start = time.monotonic()
        tool_name = _known_value(
            getattr(context.message, "name", None),
            _KNOWN_TOOL_NAMES,
        )
        raw_args = getattr(context.message, "arguments", None) or {}
        args_summary = _summarize_args(raw_args if isinstance(raw_args, dict) else {})

        span_attributes: dict[str, object] = {
            "agentgrep_surface": "mcp",
            "agentgrep_tool": tool_name,
        }
        span_attributes.update(
            _telemetry.flatten_safe_attributes("agentgrep_mcp_args", args_summary),
        )

        with _telemetry.span("mcp.server.tool", **span_attributes):
            try:
                result = await call_next(context)
            except asyncio.CancelledError:
                duration_ms = (time.monotonic() - start) * 1000.0
                _telemetry.set_span_attribute("agentgrep_outcome", "cancelled")
                _telemetry.set_span_attribute("agentgrep_duration_ms", duration_ms)
                self._logger.info(
                    "tool call cancelled",
                    extra={
                        "agentgrep_surface": "mcp",
                        "agentgrep_operation": "mcp.tool",
                        "agentgrep_tool": tool_name,
                        "agentgrep_outcome": "cancelled",
                        "agentgrep_duration_ms": duration_ms,
                        "agentgrep_args_summary": args_summary,
                    },
                )
                raise
            except Exception as exc:
                duration_ms = (time.monotonic() - start) * 1000.0
                error_type = _telemetry.error_type_name(exc)
                _telemetry.set_span_attribute("agentgrep_outcome", "error")
                _telemetry.set_span_attribute("agentgrep_error_type", error_type)
                _telemetry.set_span_attribute("agentgrep_duration_ms", duration_ms)
                self._logger.info(
                    "tool call failed",
                    extra={
                        "agentgrep_surface": "mcp",
                        "agentgrep_operation": "mcp.tool",
                        "agentgrep_tool": tool_name,
                        "agentgrep_outcome": "error",
                        "agentgrep_error_type": error_type,
                        "agentgrep_duration_ms": duration_ms,
                        "agentgrep_args_summary": args_summary,
                    },
                )
                raise

            duration_ms = (time.monotonic() - start) * 1000.0
            extra: dict[str, object] = {
                "agentgrep_surface": "mcp",
                "agentgrep_operation": "mcp.tool",
                "agentgrep_tool": tool_name,
                "agentgrep_outcome": "ok",
                "agentgrep_duration_ms": duration_ms,
                "agentgrep_args_summary": args_summary,
            }
            message = "tool call completed"
            if isinstance(result, ToolResult) and result.is_error:
                message = "tool call failed"
                extra["agentgrep_outcome"] = "error"
                extra["agentgrep_error_type"] = "ToolResultError"
            _telemetry.set_span_attribute(
                "agentgrep_outcome",
                t.cast(str, extra["agentgrep_outcome"]),
            )
            if "agentgrep_error_type" in extra:
                _telemetry.set_span_attribute(
                    "agentgrep_error_type",
                    t.cast(str, extra["agentgrep_error_type"]),
                )
            _telemetry.set_span_attribute("agentgrep_duration_ms", duration_ms)
            self._logger.info(message, extra=extra)
            return result
