"""Serializers for the CLI's JSON and NDJSON output modes.

Turn normalized records, source handles, and result envelopes into the
plain-dict payloads the ``--json`` / ``--ndjson`` paths emit. Prefers the
pydantic-backed serializers and falls back to hand-written ones when pydantic
is unavailable, behind ``maybe_build_pydantic``.
"""

from __future__ import annotations

import pathlib
import re
import typing as t
import urllib.parse

from agentgrep import maybe_use_pydantic
from agentgrep._text import format_display_path
from agentgrep.origin import LEGACY_ORIGIN_METADATA_KEYS
from agentgrep.records import (
    SCHEMA_VERSION,
    EnvelopeFactory,
    EnvelopePayload,
    FindRecord,
    FindRecordPayload,
    RecordOrigin,
    RecordOriginPayload,
    SearchRecord,
    SearchRecordPayload,
    SourceHandle,
    SourceHandlePayload,
    SourceVersionDetection,
    SourceVersionDetectionPayload,
)


def maybe_build_pydantic() -> tuple[
    t.Callable[[SearchRecord], dict[str, object]],
    t.Callable[[FindRecord], dict[str, object]],
    EnvelopeFactory,
]:
    """Return Pydantic serializers or plain fallbacks."""
    try:
        return maybe_use_pydantic()
    except ImportError:
        return (
            lambda record: t.cast("dict[str, object]", serialize_search_record(record)),
            lambda record: t.cast("dict[str, object]", serialize_find_record(record)),
            lambda command, query_data, results: t.cast(
                "dict[str, object]",
                build_envelope(command, query_data, results),
            ),
        )


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


def serialize_record_origin(origin: RecordOrigin | None) -> RecordOriginPayload | None:
    """Serialize project-origin metadata with display-safe paths."""
    if origin is None or origin.is_empty():
        return None
    payload: RecordOriginPayload = {}
    if origin.cwd:
        payload["cwd"] = _display_path_text(origin.cwd)
    if origin.repo:
        payload["repo"] = _display_path_text(origin.repo)
    if origin.worktree:
        payload["worktree"] = _display_path_text(origin.worktree)
    if origin.branch:
        payload["branch"] = origin.branch
    if origin.remote:
        remote = _safe_remote_text(origin.remote)
        if remote:
            payload["remote"] = remote
    if origin.cwd_hash:
        payload["cwd_hash"] = origin.cwd_hash
    return payload or None


def serialize_record_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Return metadata with legacy path-like origin values redacted for display."""
    payload: dict[str, object] = {}
    for key, value in metadata.items():
        if key in LEGACY_ORIGIN_METADATA_KEYS and isinstance(value, str) and _is_path_like(value):
            payload[key] = _display_path_text(value)
        else:
            payload[key] = value
    return payload


def _display_path_text(value: str) -> str:
    return format_display_path(pathlib.Path(value), directory=True)


def _is_path_like(value: str) -> bool:
    return (
        value == "~"
        or value.startswith("~/")
        or value.startswith("/")
        or value.startswith("./")
        or value.startswith("../")
    )


_SCP_REMOTE_RE = re.compile(r"^[^@/\s:]+@(?P<host>[^:/\s]+):(?P<path>\S+)$")
_SAFE_REMOTE_SCHEMES = frozenset({"git", "http", "https", "ssh"})


def _safe_remote_text(value: str) -> str | None:
    remote = value.strip()
    if not remote:
        return None
    scp_match = _SCP_REMOTE_RE.match(remote)
    if scp_match is not None:
        return f"ssh://{scp_match.group('host')}/{scp_match.group('path').lstrip('/')}"
    parsed = urllib.parse.urlsplit(remote)
    if parsed.scheme not in _SAFE_REMOTE_SCHEMES or not parsed.netloc:
        return None
    hostname = parsed.hostname
    if hostname is None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = _remote_netloc(hostname, port)
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _remote_netloc(hostname: str, port: int | None) -> str:
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if port is not None:
        return f"{hostname}:{port}"
    return hostname


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
    "maybe_build_pydantic",
    "serialize_find_record",
    "serialize_grep_begin",
    "serialize_grep_end",
    "serialize_grep_match_line",
    "serialize_grep_record",
    "serialize_search_record",
    "serialize_source_handle",
    "serialize_source_version_detection",
)
