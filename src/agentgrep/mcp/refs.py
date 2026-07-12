"""Opaque MCP refs and cursors for result drilldown."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import pathlib
import typing as t

from agentgrep.mcp._library import (
    AgentSelector,
    FindRecordLike,
    SearchRecordLike,
    agentgrep,
)

_REF_PREFIX = "agref1:"
_FIND_CURSOR_PREFIX = "agcur1:"


class McpTokenError(ValueError):
    """Raised when an MCP ref or cursor token cannot be parsed."""


class _RecordRefPayload(t.TypedDict):
    v: int
    kind: t.Literal["search", "find"]
    adapter_id: str
    path: str
    fingerprint: str


class _FindCursorPayload(t.TypedDict):
    v: int
    tool: t.Literal["find"]
    offset: int
    pattern: str | None
    agent: AgentSelector
    limit: int


@dataclasses.dataclass(frozen=True, slots=True)
class ParsedRecordRef:
    """Decoded record reference.

    Attributes
    ----------
    kind : t.Literal["search", "find"]
        Tool the ref came from, which decides whether ``fingerprint`` is a search or a
        find fingerprint.
    adapter_id : str
        Adapter identity of the referenced record, unique across the merged registry.
    path : pathlib.Path
        Source file the record came from, re-expanded from the token's display path
        against the caller's home directory.
    fingerprint : str
        Hex SHA-256 over the record's identifying metadata, used to confirm a re-read
        landed on the same record. Carries no prompt text.
    """

    kind: t.Literal["search", "find"]
    adapter_id: str
    path: pathlib.Path
    fingerprint: str


@dataclasses.dataclass(frozen=True, slots=True)
class FindCursor:
    """Decoded find page cursor.

    Attributes
    ----------
    offset : int
        Source records already returned, which the next page skips. Non-negative.
    pattern : str | None
        Pattern of the original call. ``None`` when it listed every discovered source.
    agent : AgentSelector
        Agent the original call selected, or the selector meaning every agent.
    limit : int
        Page size to repeat, at least 1.
    """

    offset: int
    pattern: str | None
    agent: AgentSelector
    limit: int


def _encode_token(prefix: str, payload: dict[str, object]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{prefix}{encoded}"


def _decode_token(prefix: str, token: str) -> dict[str, object]:
    if not token.startswith(prefix):
        msg = f"token must start with {prefix!r}"
        raise McpTokenError(msg)
    encoded = token.removeprefix(prefix)
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        raw = base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeEncodeError, ValueError, json.JSONDecodeError) as exc:
        msg = "token is not valid encoded JSON"
        raise McpTokenError(msg) from exc
    canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if encoded != canonical:
        msg = "token is not valid encoded JSON"
        raise McpTokenError(msg)
    if not isinstance(payload, dict):
        msg = "token payload must be an object"
        raise McpTokenError(msg)
    return t.cast("dict[str, object]", payload)


def _display_path_to_path(value: object, home: pathlib.Path) -> pathlib.Path:
    if not isinstance(value, str) or not value:
        msg = "token path must be a non-empty string"
        raise McpTokenError(msg)
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return pathlib.Path(value).expanduser()


def _record_fingerprint(payload: dict[str, object]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()


def _search_record_coordinate(record: SearchRecordLike) -> tuple[str, str | int] | None:
    """Return the validated occurrence coordinate for a search record."""
    position = record.position
    if position is None:
        return None
    if isinstance(position.native_id, str) and position.native_id:
        return ("native", position.native_id)
    if (
        isinstance(position.ordinal, int)
        and not isinstance(position.ordinal, bool)
        and position.ordinal >= 0
    ):
        return ("ordinal", position.ordinal)
    return None


def _search_record_fingerprint_payload(
    record: SearchRecordLike,
    *,
    text_sha256: str,
) -> dict[str, object]:
    """Build the position-blind v1 search fingerprint payload."""
    return {
        "kind": "search",
        "record_kind": record.kind,
        "role": record.role,
        "agent": record.agent,
        "store": record.store,
        "adapter_id": record.adapter_id,
        "path": agentgrep.format_display_path(record.path),
        "timestamp": record.timestamp,
        "session_id": record.session_id,
        "conversation_id": record.conversation_id,
        "text_sha256": text_sha256,
    }


def _legacy_search_record_fingerprint(
    record: SearchRecordLike,
    *,
    text_sha256: str,
) -> str:
    """Return the historical position-blind v1 search fingerprint."""
    return _record_fingerprint(
        _search_record_fingerprint_payload(record, text_sha256=text_sha256),
    )


def search_record_fingerprint(
    record: SearchRecordLike,
    *,
    text_sha256: str | None = None,
) -> str:
    """Return a stable privacy-preserving fingerprint for a search record."""
    if text_sha256 is None:
        text_sha256 = hashlib.sha256(
            record.text.encode("utf-8", "surrogatepass"),
        ).hexdigest()
    payload = _search_record_fingerprint_payload(record, text_sha256=text_sha256)
    coordinate = _search_record_coordinate(record)
    if coordinate is not None:
        payload["position"] = coordinate
    return _record_fingerprint(payload)


def search_record_fingerprint_candidates(record: SearchRecordLike) -> tuple[str, ...]:
    """Prepare current and position-blind v1 fingerprints once for a record."""
    text_sha256 = hashlib.sha256(
        record.text.encode("utf-8", "surrogatepass"),
    ).hexdigest()
    current = search_record_fingerprint(record, text_sha256=text_sha256)
    if _search_record_coordinate(record) is None:
        return (current,)
    legacy = _legacy_search_record_fingerprint(record, text_sha256=text_sha256)
    return (current, legacy)


def search_record_fingerprint_matches(record: SearchRecordLike, fingerprint: str) -> bool:
    """Match current fields with a position-blind v1 fallback."""
    return fingerprint in search_record_fingerprint_candidates(record)


def find_record_fingerprint(record: FindRecordLike) -> str:
    """Return a stable fingerprint for a find record."""
    return _record_fingerprint(
        {
            "kind": "find",
            "agent": record.agent,
            "store": record.store,
            "adapter_id": record.adapter_id,
            "path": agentgrep.format_display_path(record.path),
            "path_kind": record.path_kind,
        },
    )


def make_search_ref(
    record: SearchRecordLike,
    *,
    text_sha256: str | None = None,
) -> str:
    """Build an opaque ref for a search result."""
    return _encode_token(
        _REF_PREFIX,
        t.cast(
            "dict[str, object]",
            _RecordRefPayload(
                v=1,
                kind="search",
                adapter_id=record.adapter_id,
                path=agentgrep.format_display_path(record.path),
                fingerprint=search_record_fingerprint(
                    record,
                    text_sha256=text_sha256,
                ),
            ),
        ),
    )


def make_find_ref(record: FindRecordLike) -> str:
    """Build an opaque ref for a find result."""
    return _encode_token(
        _REF_PREFIX,
        t.cast(
            "dict[str, object]",
            _RecordRefPayload(
                v=1,
                kind="find",
                adapter_id=record.adapter_id,
                path=agentgrep.format_display_path(record.path),
                fingerprint=find_record_fingerprint(record),
            ),
        ),
    )


def parse_record_ref(ref: str, *, home: pathlib.Path) -> ParsedRecordRef:
    """Parse an opaque result ref."""
    payload = _decode_token(_REF_PREFIX, ref)
    version = payload.get("v")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        msg = "unsupported ref version"
        raise McpTokenError(msg)
    kind = payload.get("kind")
    if kind not in {"search", "find"}:
        msg = "ref kind must be 'search' or 'find'"
        raise McpTokenError(msg)
    adapter_id = payload.get("adapter_id")
    if not isinstance(adapter_id, str) or not adapter_id:
        msg = "ref adapter_id must be a non-empty string"
        raise McpTokenError(msg)
    fingerprint = payload.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        msg = "ref fingerprint must be a non-empty string"
        raise McpTokenError(msg)
    return ParsedRecordRef(
        kind=t.cast("t.Literal['search', 'find']", kind),
        adapter_id=adapter_id,
        path=_display_path_to_path(payload.get("path"), home),
        fingerprint=fingerprint,
    )


def make_find_cursor(
    *,
    offset: int,
    pattern: str | None,
    agent: AgentSelector,
    limit: int,
) -> str:
    """Build an opaque cursor for the next find page."""
    return _encode_token(
        _FIND_CURSOR_PREFIX,
        t.cast(
            "dict[str, object]",
            _FindCursorPayload(
                v=1,
                tool="find",
                offset=offset,
                pattern=pattern,
                agent=agent,
                limit=limit,
            ),
        ),
    )


def parse_find_cursor(cursor: str) -> FindCursor:
    """Parse an opaque find page cursor."""
    payload = _decode_token(_FIND_CURSOR_PREFIX, cursor)
    version = payload.get("v")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != 1
        or payload.get("tool") != "find"
    ):
        msg = "cursor is not a find cursor"
        raise McpTokenError(msg)
    offset = payload.get("offset")
    pattern = payload.get("pattern")
    agent = payload.get("agent")
    limit = payload.get("limit")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        msg = "cursor offset must be non-negative"
        raise McpTokenError(msg)
    if pattern is not None and not isinstance(pattern, str):
        msg = "cursor pattern must be a string or null"
        raise McpTokenError(msg)
    if agent not in t.get_args(AgentSelector):
        msg = "cursor agent is invalid"
        raise McpTokenError(msg)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        msg = "cursor limit must be positive"
        raise McpTokenError(msg)
    return FindCursor(
        offset=offset,
        pattern=pattern,
        agent=t.cast("AgentSelector", agent),
        limit=limit,
    )
