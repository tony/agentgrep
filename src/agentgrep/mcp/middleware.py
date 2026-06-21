"""FastMCP middleware for the ``agentgrep`` server.

Holds :class:`AgentgrepAuditMiddleware`, a per-tool structured-logging hook
that records each invocation with ``agentgrep_*`` ``extra`` keys. FastMCP's
own ``TimingMiddleware`` / ``ResponseLimitingMiddleware`` /
``ErrorHandlingMiddleware`` are wired alongside it from
:mod:`agentgrep.mcp.server`.
"""

from __future__ import annotations

import collections.abc as cabc
import hashlib
import logging
import pathlib
import time
import typing as t

from fastmcp.server.middleware import Middleware, MiddlewareContext

from agentgrep import _telemetry

_SENSITIVE_ARG_NAMES: frozenset[str] = frozenset(
    {"terms", "pattern", "query", "sample_text", "cursor"},
)
"""Tool argument names whose values get redacted before logging.

``terms`` and ``pattern`` can carry user secrets when an agent searches its
own history for tokens; page ``cursor`` values encode those same inputs;
``query`` and ``sample_text`` are diagnostic payloads and may contain anything
the caller pastes in.
"""

_MAX_LOGGED_STR_LEN: int = 200

logger = logging.getLogger(__name__)


def _inbound_otel_context() -> object | None:
    """Return the inbound traceparent context from the MCP request meta, if any."""
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


def _attach_otel_context(inbound: object | None) -> cabc.Callable[[], None]:
    """Attach an inbound OTel context and return a detach callback."""
    if inbound is None:
        return lambda: None
    from opentelemetry import context as otel_context

    token = otel_context.attach(t.cast("t.Any", inbound))
    return lambda: otel_context.detach(token)


def _redact_digest(value: str) -> dict[str, t.Any]:
    """Return a length and SHA-256 prefix summary of ``value``.

    The digest is stable and deterministic, so operators can correlate the
    same payload across log lines without ever recording the payload itself.

    Examples
    --------
    >>> _redact_digest("hello")
    {'len': 5, 'sha256_prefix': '2cf24dba5fb0'}
    >>> _redact_digest("")
    {'len': 0, 'sha256_prefix': 'e3b0c44298fc'}
    """
    return {
        "len": len(value),
        "sha256_prefix": hashlib.sha256(value.encode("utf-8")).hexdigest()[:12],
    }


def _redact_path(value: str) -> dict[str, t.Any]:
    """Return path-shaped metadata without the path value."""
    redacted = _redact_digest(value)
    redacted["kind"] = "path"
    redacted["is_absolute"] = pathlib.PurePath(value).is_absolute()
    return redacted


def _is_path_arg_name(key: str) -> bool:
    """Return whether an MCP argument name is expected to hold a path."""
    key_folded = key.casefold()
    return key_folded == "path" or key_folded.endswith("_path")


def _summarize_args(args: dict[str, t.Any]) -> dict[str, t.Any]:
    """Summarize tool arguments for audit logging.

    Sensitive scalars get replaced by a digest dict. Sensitive list payloads
    (e.g. ``terms`` is ``list[str]``) get each element digested. Long
    non-sensitive strings get truncated with a marker. Path-named string
    payloads get path-shaped metadata without the path value. Everything else
    passes through as-is.

    Examples
    --------
    Non-sensitive scalars pass through unchanged:

    >>> _summarize_args({"agent": "codex", "regex": True})
    {'agent': 'codex', 'regex': True}

    Sensitive scalar payloads are replaced by a digest dict:

    >>> _summarize_args({"pattern": "secret-token"})["pattern"]["len"]
    12

    Sensitive list payloads digest each element:

    >>> redacted = _summarize_args({"terms": ["alpha", "beta"]})
    >>> [item["len"] for item in redacted["terms"]]
    [5, 4]
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
    for key, value in args.items():
        if key in _SENSITIVE_ARG_NAMES and isinstance(value, str):
            summary[key] = _redact_digest(value)
        elif key in _SENSITIVE_ARG_NAMES and isinstance(value, list):
            summary[key] = [
                _redact_digest(str(item)) if isinstance(item, str) else item for item in value
            ]
        elif _is_path_arg_name(key) and isinstance(value, str):
            summary[key] = _redact_path(value)
        elif isinstance(value, str) and len(value) > _MAX_LOGGED_STR_LEN:
            summary[key] = value[:_MAX_LOGGED_STR_LEN] + "...<truncated>"
        else:
            summary[key] = value
    return summary


def _context_ids(context: MiddlewareContext[t.Any]) -> dict[str, object]:
    """Return safe FastMCP request identifiers when available."""
    attributes: dict[str, object] = {}
    if context.fastmcp_context is None:
        return attributes
    client_id = getattr(context.fastmcp_context, "client_id", None)
    request_id = getattr(context.fastmcp_context, "request_id", None)
    if client_id is not None:
        attributes["agentgrep_client_id"] = client_id
    if request_id is not None:
        attributes["agentgrep_request_id"] = request_id
    return attributes


class AgentgrepTelemetryMiddleware(Middleware):
    """Create app-level MCP request roots for observable FastMCP operations."""

    async def on_request(
        self,
        context: MiddlewareContext[t.Any],
        call_next: t.Callable[[MiddlewareContext[t.Any]], t.Awaitable[t.Any]],
    ) -> t.Any:
        """Wrap MCP requests that should appear as app-level roots."""
        method = context.method or "unknown"
        if method == "initialize":
            return await call_next(context)
        start = time.monotonic()
        attributes: dict[str, object] = {
            "agentgrep_surface": "mcp",
            "agentgrep_operation": "mcp.request",
            "agentgrep_mcp_method": method,
        }
        attributes.update(_context_ids(context))
        inbound = _inbound_otel_context()
        detach = _attach_otel_context(inbound)
        try:
            with _telemetry.span(
                "mcp.server.request",
                inherit_otel_context=inbound is not None,
                **attributes,
            ):
                try:
                    result = await call_next(context)
                except Exception as exc:
                    duration_ms = (time.monotonic() - start) * 1000.0
                    _telemetry.set_span_attribute("agentgrep_outcome", "error")
                    _telemetry.set_span_attribute("agentgrep_error_type", type(exc).__name__)
                    _telemetry.set_span_attribute("agentgrep_duration_ms", duration_ms)
                    logger.info(
                        "mcp request failed",
                        extra={
                            **attributes,
                            "agentgrep_outcome": "error",
                            "agentgrep_error_type": type(exc).__name__,
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
    ``agentgrep_client_id`` / ``agentgrep_request_id`` (when available), and
    ``agentgrep_args_summary``. The logger name defaults to
    ``agentgrep.audit`` so operators can route it independently of the
    ``agentgrep`` library logger.

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
        tool_name = getattr(context.message, "name", "<unknown>")
        raw_args = getattr(context.message, "arguments", None) or {}
        args_summary = _summarize_args(raw_args)

        client_id: str | None = None
        request_id: str | None = None
        if context.fastmcp_context is not None:
            client_id = getattr(context.fastmcp_context, "client_id", None)
            request_id = getattr(context.fastmcp_context, "request_id", None)

        span_attributes: dict[str, object] = {
            "agentgrep_surface": "mcp",
            "agentgrep_tool": tool_name,
        }
        if client_id is not None:
            span_attributes["agentgrep_client_id"] = client_id
        if request_id is not None:
            span_attributes["agentgrep_request_id"] = request_id
        span_attributes.update(
            _telemetry.flatten_safe_attributes("agentgrep_mcp_args", args_summary),
        )

        with _telemetry.span("mcp.server.tool", **span_attributes):
            try:
                result = await call_next(context)
            except Exception as exc:
                duration_ms = (time.monotonic() - start) * 1000.0
                _telemetry.set_span_attribute("agentgrep_outcome", "error")
                _telemetry.set_span_attribute("agentgrep_error_type", type(exc).__name__)
                _telemetry.set_span_attribute("agentgrep_duration_ms", duration_ms)
                self._logger.info(
                    "tool call failed",
                    extra={
                        "agentgrep_surface": "mcp",
                        "agentgrep_operation": "mcp.tool",
                        "agentgrep_tool": tool_name,
                        "agentgrep_outcome": "error",
                        "agentgrep_error_type": type(exc).__name__,
                        "agentgrep_duration_ms": duration_ms,
                        "agentgrep_client_id": client_id,
                        "agentgrep_request_id": request_id,
                        "agentgrep_args_summary": args_summary,
                    },
                )
                raise

            duration_ms = (time.monotonic() - start) * 1000.0
            _telemetry.set_span_attribute("agentgrep_outcome", "ok")
            _telemetry.set_span_attribute("agentgrep_duration_ms", duration_ms)
            self._logger.info(
                "tool call completed",
                extra={
                    "agentgrep_surface": "mcp",
                    "agentgrep_operation": "mcp.tool",
                    "agentgrep_tool": tool_name,
                    "agentgrep_outcome": "ok",
                    "agentgrep_duration_ms": duration_ms,
                    "agentgrep_client_id": client_id,
                    "agentgrep_request_id": request_id,
                    "agentgrep_args_summary": args_summary,
                },
            )
            return result
