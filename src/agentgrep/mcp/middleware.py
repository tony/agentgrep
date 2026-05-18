"""FastMCP middleware for the ``agentgrep`` server.

Holds :class:`AgentgrepAuditMiddleware`, a per-tool structured-logging hook
that records each invocation with ``agentgrep_*`` ``extra`` keys. FastMCP's
own ``TimingMiddleware`` / ``ResponseLimitingMiddleware`` /
``ErrorHandlingMiddleware`` are wired alongside it from
:mod:`agentgrep.mcp.server`.
"""

from __future__ import annotations

import hashlib
import logging
import time
import typing as t

from fastmcp.server.middleware import Middleware, MiddlewareContext

_SENSITIVE_ARG_NAMES: frozenset[str] = frozenset({"terms", "pattern", "sample_text"})
"""Tool argument names whose values get redacted before logging.

``terms`` and ``pattern`` can carry user secrets when an agent searches its
own history for tokens; ``sample_text`` is the validate-query payload and may
contain anything the caller pastes in.
"""

_MAX_LOGGED_STR_LEN: int = 200


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


def _summarize_args(args: dict[str, t.Any]) -> dict[str, t.Any]:
    """Summarize tool arguments for audit logging.

    Sensitive scalars get replaced by a digest dict. Sensitive list payloads
    (e.g. ``terms`` is ``list[str]``) get each element digested. Long
    non-sensitive strings get truncated with a marker. Everything else passes
    through as-is.

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
    """
    summary: dict[str, t.Any] = {}
    for key, value in args.items():
        if key in _SENSITIVE_ARG_NAMES and isinstance(value, str):
            summary[key] = _redact_digest(value)
        elif key in _SENSITIVE_ARG_NAMES and isinstance(value, list):
            summary[key] = [
                _redact_digest(str(item)) if isinstance(item, str) else item for item in value
            ]
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
        self._logger.info(
            "tool call completed",
            extra={
                "agentgrep_tool": tool_name,
                "agentgrep_outcome": "ok",
                "agentgrep_duration_ms": duration_ms,
                "agentgrep_client_id": client_id,
                "agentgrep_request_id": request_id,
                "agentgrep_args_summary": args_summary,
            },
        )
        return result
