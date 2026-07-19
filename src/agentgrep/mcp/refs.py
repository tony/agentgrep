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
    SearchScopeName,
    agentgrep,
)

_REF_PREFIX = "agref1:"
_CURSOR_PREFIX = "agcur1:"

MAX_RECORD_REF_CHARS = 48 * 1024
"""Maximum opaque record-ref length accepted at MCP boundaries.

Linux ``PATH_MAX`` includes the trailing NUL, leaving at most 4,095 path bytes.
JSON can expand each byte to a six-byte ``\\u00xx`` escape, and base64url then
expands by four thirds: ``4,095 * 6 * 4 / 3 = 32,760`` characters. The 48 KiB
ceiling leaves more than 16 KiB for the versioned envelope while still bounding
decode and audit work on untrusted input.
"""


class McpTokenError(ValueError):
    """Raised when an MCP ref or cursor token cannot be parsed."""


class _RecordRefPayload(t.TypedDict):
    v: int
    kind: t.Literal["search", "find"]
    adapter_id: str
    path: str
    fingerprint: str


class _SearchCursorPayload(t.TypedDict):
    v: int
    tool: t.Literal["search"]
    offset: int
    terms: list[str]
    agent: AgentSelector
    scope: SearchScopeName
    case_sensitive: bool
    limit: int
    cwd: str | None
    repo: str | None
    branch: str | None


class _FindCursorPayload(t.TypedDict):
    v: int
    tool: t.Literal["find"]
    offset: int
    pattern: str | None
    agent: AgentSelector
    limit: int


@dataclasses.dataclass(frozen=True, slots=True)
class ParsedRecordRef:
    """Decoded record reference."""

    kind: t.Literal["search", "find"]
    adapter_id: str
    path: pathlib.Path
    fingerprint: str


@dataclasses.dataclass(frozen=True, slots=True)
class SearchCursor:
    """Decoded search page cursor."""

    offset: int
    terms: list[str]
    agent: AgentSelector
    scope: SearchScopeName
    case_sensitive: bool
    limit: int
    cwd: str | None = None
    repo: str | None = None
    branch: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class FindCursor:
    """Decoded find page cursor."""

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
    if value.startswith("~"):
        msg = "token path has unsupported leading tilde"
        raise McpTokenError(msg)
    return pathlib.Path(value)


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
    if len(ref) > MAX_RECORD_REF_CHARS:
        msg = "ref exceeds maximum length"
        raise McpTokenError(msg)
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


def make_search_cursor(
    *,
    offset: int,
    terms: list[str],
    agent: AgentSelector,
    scope: SearchScopeName,
    case_sensitive: bool,
    limit: int,
    cwd: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
) -> str:
    """Build an opaque cursor for the next search page."""
    return _encode_token(
        _CURSOR_PREFIX,
        t.cast(
            "dict[str, object]",
            _SearchCursorPayload(
                v=1,
                tool="search",
                offset=offset,
                terms=terms,
                agent=agent,
                scope=scope,
                case_sensitive=case_sensitive,
                limit=limit,
                cwd=cwd,
                repo=repo,
                branch=branch,
            ),
        ),
    )


def parse_search_cursor(cursor: str) -> SearchCursor:
    """Parse an opaque search page cursor."""
    payload = _decode_token(_CURSOR_PREFIX, cursor)
    version = payload.get("v")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != 1
        or payload.get("tool") != "search"
    ):
        msg = "cursor is not a search cursor"
        raise McpTokenError(msg)
    offset = payload.get("offset")
    terms = payload.get("terms")
    agent = payload.get("agent")
    scope = payload.get("scope")
    case_sensitive = payload.get("case_sensitive")
    limit = payload.get("limit")
    cwd = payload.get("cwd")
    repo = payload.get("repo")
    branch = payload.get("branch")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        msg = "cursor offset must be non-negative"
        raise McpTokenError(msg)
    if not isinstance(terms, list) or not all(isinstance(term, str) for term in terms):
        msg = "cursor terms must be a list of strings"
        raise McpTokenError(msg)
    if not terms and not any(isinstance(value, str) and value for value in (cwd, repo, branch)):
        msg = "cursor terms must be a non-empty list of strings unless an origin filter is present"
        raise McpTokenError(msg)
    if agent not in t.get_args(AgentSelector):
        msg = "cursor agent is invalid"
        raise McpTokenError(msg)
    if scope not in t.get_args(SearchScopeName):
        msg = "cursor scope is invalid"
        raise McpTokenError(msg)
    if not isinstance(case_sensitive, bool):
        msg = "cursor case_sensitive must be a boolean"
        raise McpTokenError(msg)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        msg = "cursor limit must be positive"
        raise McpTokenError(msg)
    if cwd is not None and not isinstance(cwd, str):
        msg = "cursor cwd must be a string or null"
        raise McpTokenError(msg)
    if repo is not None and not isinstance(repo, str):
        msg = "cursor repo must be a string or null"
        raise McpTokenError(msg)
    if branch is not None and not isinstance(branch, str):
        msg = "cursor branch must be a string or null"
        raise McpTokenError(msg)
    return SearchCursor(
        offset=offset,
        terms=t.cast("list[str]", terms),
        agent=t.cast("AgentSelector", agent),
        scope=t.cast("SearchScopeName", scope),
        case_sensitive=case_sensitive,
        limit=limit,
        cwd=cwd,
        repo=repo,
        branch=branch,
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
        _CURSOR_PREFIX,
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
    payload = _decode_token(_CURSOR_PREFIX, cursor)
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
