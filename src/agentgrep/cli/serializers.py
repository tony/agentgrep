"""Serializers for the CLI's JSON and NDJSON output modes.

Turn normalized records, source handles, and result envelopes into the
plain-dict payloads the ``--json`` / ``--ndjson`` paths emit. Pydantic is a
required dependency for schema boundaries elsewhere; these direct serializers
own the CLI wire shape without a redundant validation round trip.
"""

from __future__ import annotations

import collections.abc as cabc

from agentgrep._query_gate import UNREGISTERED_FIELD_PREDICATE_CODE, UnregisteredFieldToken
from agentgrep._text import format_display_path
from agentgrep.origin_serializers import serialize_record_metadata, serialize_record_origin
from agentgrep.records import (
    SCHEMA_VERSION,
    EnvelopePayload,
    FindRecord,
    FindRecordPayload,
    SearchRecord,
    SearchRecordPayload,
    SourceHandle,
    SourceHandlePayload,
    SourceVersionDetection,
    SourceVersionDetectionPayload,
)
from agentgrep.results import RunSummary


def serialize_search_record(record: SearchRecord) -> SearchRecordPayload:
    """Serialize a search record to a JSON-compatible mapping."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": record.kind,
        "agent": record.agent,
        "store": record.store,
        "adapter_id": record.adapter_id,
        "path": format_display_path(record.path),
        "text": record.text,
        "title": record.title,
        "role": record.role,
        "timestamp": record.timestamp,
        "model": record.model,
        "session_id": record.session_id,
        "conversation_id": record.conversation_id,
        "origin": serialize_record_origin(record.origin),
        "metadata": serialize_record_metadata(record.metadata),
    }


def serialize_find_record(record: FindRecord) -> FindRecordPayload:
    """Serialize a find record to a JSON-compatible mapping."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": record.kind,
        "agent": record.agent,
        "store": record.store,
        "adapter_id": record.adapter_id,
        "path": format_display_path(record.path),
        "path_kind": record.path_kind,
        "metadata": record.metadata,
    }


def serialize_source_handle(source: SourceHandle) -> SourceHandlePayload:
    """Serialize a source handle to a JSON-compatible mapping."""
    return {
        "schema_version": SCHEMA_VERSION,
        "agent": source.agent,
        "store": source.store,
        "adapter_id": source.adapter_id,
        "path": format_display_path(source.path),
        "path_kind": source.path_kind,
        "source_kind": source.source_kind,
        "coverage": source.coverage,
        "version_detection": serialize_source_version_detection(source.version_detection),
        "search_root": (
            None
            if source.search_root is None
            else format_display_path(source.search_root, directory=True)
        ),
        "mtime_ns": source.mtime_ns,
    }


def serialize_source_version_detection(
    detection: SourceVersionDetection | None,
) -> SourceVersionDetectionPayload | None:
    """Serialize source version metadata for JSON/MCP discovery payloads."""
    if detection is None:
        return None
    return {
        "app_version": detection.app_version,
        "data_version": detection.data_version,
        "strategy": detection.strategy,
        "confidence": detection.confidence,
        "evidence": detection.evidence,
    }


def serialize_run_summary(summary: RunSummary) -> dict[str, object]:
    """Serialize engine-owned lifecycle evidence without re-deriving semantics."""
    request = summary.request
    coverage = summary.coverage
    return {
        "request": {
            "terms": list(request.terms),
            "scope": request.scope,
            "scope_provenance": request.scope_provenance,
            "effort": request.effort,
            "agents": list(request.agents),
            "limit": request.limit,
            "conversation_limit": request.conversation_limit,
            "dedupe": request.dedupe,
            "case_sensitive": request.case_sensitive,
            "order": request.order,
            "match_surface": request.match_surface,
        },
        "effort": {
            "requested": summary.requested_effort,
            "completed": summary.completed_effort,
        },
        "status": {
            "state": summary.status.state,
            "reason": summary.status.reason,
            "conditions": list(summary.status.conditions),
        },
        "outcome": summary.outcome,
        "coverage": {
            "sources_discovered": coverage.sources_discovered,
            "sources_eligible": coverage.sources_eligible,
            "sources_planned": coverage.sources_planned,
            "sources_attempted": coverage.sources_attempted,
            "sources_completed": coverage.sources_completed,
            "sources_bounded": coverage.sources_bounded,
            "sources_skipped": coverage.sources_skipped,
            "sources_unsupported": coverage.sources_unsupported,
            "sources_failed": coverage.sources_failed,
            "sources_cancelled": coverage.sources_cancelled,
            "records_seen": coverage.records_seen,
            "matches_seen": coverage.matches_seen,
            "conversations_eligible": coverage.conversations_eligible,
            "conversations_selected": coverage.conversations_selected,
            "conversations_completed": coverage.conversations_completed,
            "source_stop_reasons": list(coverage.source_stop_reasons),
        },
        "stats": {
            "matched": summary.match_count,
            "elapsed_seconds": summary.elapsed_seconds,
            "applied_order": summary.applied_order,
            "limit": summary.limit,
        },
        "diagnostics": [
            {
                "code": item.code,
                "message": item.message,
                "severity": item.severity,
            }
            for item in summary.diagnostics
        ],
        "next_actions": [
            {
                "action_id": action.action_id,
                "kind": action.kind,
                "label": action.label,
                "reason": action.reason,
                "requires_confirmation": action.requires_confirmation,
                "patch": {
                    "effort": action.patch.effort,
                    "scope": action.patch.scope,
                    "conversation_limit": action.patch.conversation_limit,
                },
            }
            for action in summary.next_actions
        ],
    }


def serialize_query_diagnostics(
    diagnostics: cabc.Sequence[UnregisteredFieldToken],
) -> list[dict[str, object]]:
    """Serialize non-fatal query diagnostics for the JSON/NDJSON ``warnings`` key.

    Mirrors :meth:`DiagnosticModel.from_query_diagnostic` on the MCP side: same
    non-fatal, structured shape, so a scripted consumer diffing CLI JSON
    against the MCP tool response sees the same field names.
    """
    return [
        {
            "code": UNREGISTERED_FIELD_PREDICATE_CODE,
            "field": item.field,
            "token": item.token,
            "suggestion": item.suggestion,
            "message": item.message,
        }
        for item in diagnostics
    ]


def build_envelope(
    command: str,
    query_data: dict[str, object],
    results: list[dict[str, object]],
) -> EnvelopePayload:
    """Build a JSON envelope."""
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "query": query_data,
        "results": results,
    }


def serialize_grep_record(
    record: SearchRecord,
    *,
    line_number: int | None = None,
) -> dict[str, object]:
    """Serialize a search record for ``grep --json`` event-stream output.

    Mirrors rg's ``--json`` shape at a high level: a ``match`` event
    carries the source path, the matched text, optional line number,
    and origin metadata (agent / store / session).

    Kept for backward compatibility (it's in the public re-export
    surface). Live ``--json`` / ``--ndjson`` output uses the per-line
    :func:`serialize_grep_begin`, :func:`serialize_grep_match_line`,
    and :func:`serialize_grep_end` helpers instead.
    """
    return {
        "type": "match",
        "data": {
            "agent": record.agent,
            "store": record.store,
            "adapter_id": record.adapter_id,
            "path": format_display_path(record.path),
            "line_number": line_number,
            "text": record.text,
            "timestamp": record.timestamp,
            "session_id": record.session_id,
            "conversation_id": record.conversation_id,
        },
    }


def serialize_grep_begin(record: SearchRecord) -> dict[str, object]:
    """Emit the ``begin`` event that opens each record in ``--json``.

    Mirrors rg's per-file ``begin`` envelope, adapted for agentgrep —
    carries the record's origin metadata so downstream consumers can
    route events by agent / store / session without waiting for the
    first ``match`` event.
    """
    return {
        "type": "begin",
        "data": {
            "path": {"text": format_display_path(record.path)},
            "agent": record.agent,
            "store": record.store,
            "adapter_id": record.adapter_id,
            "timestamp": record.timestamp,
            "session_id": record.session_id,
            "conversation_id": record.conversation_id,
        },
    }


def serialize_grep_match_line(
    record: SearchRecord,
    line_number: int,
    line_text: str,
    match_spans: list[tuple[int, int]],
) -> dict[str, object]:
    """Emit one rg-shaped ``match`` event per matching line.

    Mirrors rg's ``--json`` per-line event vocabulary: nested
    ``path.text`` and ``lines.text``, 1-indexed ``line_number``, and
    ``submatches`` as ``[{"match": {"text": ...}, "start": int,
    "end": int}, ...]`` carrying byte offsets within the line. Each
    submatch's ``text`` is the substring sliced from ``line_text``.
    """
    submatches = [
        {"match": {"text": line_text[start:end]}, "start": start, "end": end}
        for start, end in match_spans
    ]
    return {
        "type": "match",
        "data": {
            "path": {"text": format_display_path(record.path)},
            "line_number": line_number,
            "lines": {"text": line_text},
            "submatches": submatches,
        },
    }


def serialize_grep_end(
    record: SearchRecord,
    *,
    matched_lines: int,
    matches: int,
) -> dict[str, object]:
    """Emit the ``end`` event that closes each record in ``--json``.

    Carries the per-record tallies (matched lines vs total match spans)
    so downstream consumers can build summaries without re-counting.
    """
    return {
        "type": "end",
        "data": {
            "path": {"text": format_display_path(record.path)},
            "stats": {
                "matched_lines": matched_lines,
                "matches": matches,
            },
        },
    }


__all__ = (
    "build_envelope",
    "serialize_find_record",
    "serialize_grep_begin",
    "serialize_grep_end",
    "serialize_grep_match_line",
    "serialize_grep_record",
    "serialize_query_diagnostics",
    "serialize_run_summary",
    "serialize_search_record",
    "serialize_source_handle",
    "serialize_source_version_detection",
)
