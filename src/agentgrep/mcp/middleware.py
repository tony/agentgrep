"""FastMCP middleware for the ``agentgrep`` server.

Holds the server's response-limiting and structured audit middleware. FastMCP's
own timing and error-handling middleware are wired alongside them from
:mod:`agentgrep.mcp.server`.
"""

from __future__ import annotations

import hashlib
import logging
import time
import typing as t

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from fastmcp.tools.base import ToolResult

_SENSITIVE_ARG_NAMES: frozenset[str] = frozenset(
    {"terms", "pattern", "sample_text", "cursor", "ref", "refs", "source_path"},
)
"""Tool argument names whose values get redacted before logging.

``terms`` and ``pattern`` can carry user secrets when an agent searches its
own history for tokens; page ``cursor`` values encode those same inputs;
``sample_text`` is the validate-query payload and may contain anything the
caller pastes in. Record refs and source paths encode or reveal local source
coordinates and receive the same treatment.
"""

_MAX_LOGGED_STR_LEN: int = 200


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
        "sha256_prefix": hashlib.sha256(
            value.encode("utf-8", "surrogatepass"),
        ).hexdigest()[:12],
    }


def _summarize_args(args: dict[str, t.Any]) -> dict[str, t.Any]:
    """Summarize tool arguments for audit logging.

    Sensitive scalars get replaced by a digest dict. Sensitive list payloads
    (e.g. ``terms`` is ``list[str]``) get each string element digested; invalid
    non-string members expose only their type. Long non-sensitive strings get
    truncated with a marker. Everything else passes through as-is.

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

    Record refs are redacted individually, including list inputs:

    >>> refs = _summarize_args({"refs": ["agref1:first", "agref1:second"]})
    >>> [item["len"] for item in refs["refs"]]
    [12, 13]
    >>> "agref1" in str(refs)
    False
    """
    summary: dict[str, t.Any] = {}
    for key, value in args.items():
        if key in _SENSITIVE_ARG_NAMES and isinstance(value, str):
            summary[key] = _redact_digest(value)
        elif key in _SENSITIVE_ARG_NAMES and isinstance(value, list):
            summary[key] = [
                _redact_digest(item) if isinstance(item, str) else {"type": type(item).__name__}
                for item in value
            ]
        elif key in _SENSITIVE_ARG_NAMES:
            summary[key] = {"type": type(value).__name__}
        elif isinstance(value, str) and len(value) > _MAX_LOGGED_STR_LEN:
            summary[key] = value[:_MAX_LOGGED_STR_LEN] + "...<truncated>"
        else:
            summary[key] = value
    return summary


class AgentgrepAuditMiddleware(Middleware):
    """Emit a structured log record per ``agentgrep`` tool invocation.

    Records carry ``agentgrep_tool``, ``agentgrep_outcome``,
    ``agentgrep_duration_ms``, ``agentgrep_error_type`` (on failure),
    ``agentgrep_client_id`` / ``agentgrep_request_id`` (when available), and
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
        tool_name = getattr(context.message, "name", "<unknown>")
        raw_args = getattr(context.message, "arguments", None) or {}
        args_summary = _summarize_args(raw_args)

        client_id: str | None = None
        request_id: str | None = None
        if context.fastmcp_context is not None:
            client_id = getattr(context.fastmcp_context, "client_id", None)
            request_id = getattr(context.fastmcp_context, "request_id", None)

        try:
            result = await call_next(context)
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000.0
            self._logger.info(
                "tool call failed",
                extra={
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
        extra: dict[str, object] = {
            "agentgrep_tool": tool_name,
            "agentgrep_outcome": "ok",
            "agentgrep_duration_ms": duration_ms,
            "agentgrep_client_id": client_id,
            "agentgrep_request_id": request_id,
            "agentgrep_args_summary": args_summary,
        }
        message = "tool call completed"
        if isinstance(result, ToolResult) and result.is_error:
            message = "tool call failed"
            extra["agentgrep_outcome"] = "error"
            extra["agentgrep_error_type"] = "ToolResultError"
        self._logger.info(message, extra=extra)
        return result
