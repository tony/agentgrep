"""The shared record-normalization seam.

``extract_*`` field readers, message-candidate walks, and
``build_search_record`` turn raw store mappings into normalized
:class:`~agentgrep.records.SearchRecord` inputs. Parsers consume this
module; it knows nothing about any single agent's on-disk layout.
"""

from __future__ import annotations

import collections.abc as cabc
import typing as t

from agentgrep.adapters._common import (
    _path_like_str,
    _record_origin,
)
from agentgrep.readers import (
    as_optional_str,
    decode_sqlite_value,
    parse_embedded_json,
)
from agentgrep.records import (
    USER_ROLES,
    MessageCandidate,
    RecordOrigin,
    SearchRecord,
    SourceHandle,
)


def _origin_from_mapping(
    mapping: dict[str, object],
    *,
    fallback: RecordOrigin | None = None,
) -> RecordOrigin | None:
    """Extract common cwd/branch/project-hash fields from a JSON mapping."""
    git = mapping.get("git")
    git_mapping = t.cast("dict[str, object]", git) if isinstance(git, dict) else {}
    cwd = (
        _path_like_str(mapping.get("cwd"))
        or _path_like_str(mapping.get("workspace"))
        or _path_like_str(mapping.get("directory"))
    )
    repo = _path_like_str(mapping.get("repo")) or _path_like_str(mapping.get("repository"))
    worktree = _path_like_str(mapping.get("worktree"))
    # Bare `branch`/`remote` are generic UI-state words in store blobs;
    # accept them only alongside git or path evidence from the same
    # mapping.
    trusted = bool(git_mapping) or "gitBranch" in mapping or bool(cwd or repo or worktree)
    return _record_origin(
        cwd=cwd,
        repo=repo,
        worktree=worktree,
        branch=(
            as_optional_str(mapping.get("gitBranch"))
            or as_optional_str(git_mapping.get("branch"))
            or (as_optional_str(mapping.get("branch")) if trusted else None)
        ),
        remote=(
            as_optional_str(git_mapping.get("repository_url"))
            or as_optional_str(git_mapping.get("repositoryUrl"))
            or as_optional_str(git_mapping.get("remote"))
            or (as_optional_str(mapping.get("remote")) if trusted else None)
        ),
        cwd_hash=(
            as_optional_str(mapping.get("cwd_hash"))
            or as_optional_str(mapping.get("project_hash"))
            or as_optional_str(mapping.get("projectHash"))
        ),
        fallback=fallback,
    )


# Every key _origin_from_mapping reads; lets hot walks skip extraction
# for the many nodes that carry none of them.
_ORIGIN_MAPPING_KEYS: frozenset[str] = frozenset(
    {
        "git",
        "cwd",
        "workspace",
        "directory",
        "repo",
        "repository",
        "worktree",
        "gitBranch",
        "branch",
        "remote",
        "cwd_hash",
        "project_hash",
        "projectHash",
    },
)


def candidate_from_mapping(
    mapping: dict[str, object],
    *,
    timestamp: str | None,
    model: str | None,
    session_id: str | None,
    conversation_id: str | None,
    origin: RecordOrigin | None = None,
) -> MessageCandidate | None:
    """Extract one message candidate from a known message-like mapping."""
    role = extract_role(mapping)
    if role is None:
        return None
    text = extract_message_text(mapping)
    if not text:
        return None
    return MessageCandidate(
        role=role,
        text=text,
        title=extract_title(mapping),
        timestamp=timestamp or extract_timestamp(mapping),
        model=model or extract_model(mapping),
        session_id=session_id or extract_session_id(mapping),
        conversation_id=conversation_id or extract_conversation_id(mapping),
        origin=_origin_from_mapping(mapping, fallback=origin),
    )


def iter_message_candidates(
    value: object,
    *,
    fallback_title: str | None = None,
    fallback_conversation_id: str | None = None,
    fallback_origin: RecordOrigin | None = None,
) -> cabc.Iterator[MessageCandidate]:
    """Recursively walk a JSON value and yield message candidates.

    ``value`` is typed ``object`` so the recursive descent into dict values
    and list items needs no per-node ``cast`` — the ``isinstance`` guards
    below narrow it, and scalars are ignored.
    """
    if isinstance(value, dict):
        mapping = t.cast("dict[str, object]", value)
        # Origin extraction is ~14 lookups plus an allocation; skip it
        # for the many structural nodes without any origin key.
        origin = (
            _origin_from_mapping(mapping, fallback=fallback_origin)
            if mapping.keys() & _ORIGIN_MAPPING_KEYS
            else fallback_origin
        )
        role = extract_role(mapping)
        # Text extraction drives the recursive content flatten; it is only
        # used when a role is present, so skip it for the many role-less nodes.
        text = extract_message_text(mapping) if role is not None else None
        if role is not None and text:
            yield MessageCandidate(
                role=role,
                text=text,
                title=extract_title(mapping) or fallback_title,
                timestamp=extract_timestamp(mapping),
                model=extract_model(mapping),
                session_id=extract_session_id(mapping),
                conversation_id=extract_conversation_id(mapping) or fallback_conversation_id,
                origin=origin,
            )
        for nested in mapping.values():
            yield from iter_message_candidates(
                nested,
                fallback_title=fallback_title,
                fallback_conversation_id=fallback_conversation_id,
                fallback_origin=origin,
            )
    elif isinstance(value, list):
        for item in value:
            yield from iter_message_candidates(
                item,
                fallback_title=fallback_title,
                fallback_conversation_id=fallback_conversation_id,
                fallback_origin=fallback_origin,
            )


def extract_role(mapping: dict[str, object]) -> str | None:
    """Extract a normalized role from a mapping."""
    for key in ("role", "sender", "author", "speaker"):
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested_mapping = t.cast("dict[str, object]", value)
            nested = as_optional_str(nested_mapping.get("role")) or as_optional_str(
                nested_mapping.get("name"),
            )
            if nested is not None:
                return nested
    return None


def extract_message_text(mapping: dict[str, object]) -> str | None:
    """Extract message text from common content fields."""
    for key in ("content", "text", "message", "body", "prompt", "value", "parts"):
        if key in mapping:
            flattened = flatten_content_value(mapping[key])
            if flattened:
                return flattened
    return None


def flatten_content_value(value: object) -> str | None:
    """Flatten a message content payload into text."""
    parts = list(iter_text_fragments(value))
    if not parts:
        return None
    return "\n".join(part for part in parts if part.strip()).strip() or None


def iter_text_fragments(
    value: object,
) -> cabc.Iterator[str]:
    """Yield text fragments from a nested content payload."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            yield stripped
        return
    if isinstance(value, list):
        for item in value:
            yield from iter_text_fragments(item)
        return
    if isinstance(value, dict):
        mapping = t.cast("dict[str, object]", value)
        for key in ("text", "content", "message", "body", "prompt", "value", "parts"):
            if key in mapping:
                yield from iter_text_fragments(mapping[key])


def extract_title(mapping: dict[str, object]) -> str | None:
    """Extract a title-like field."""
    for key in ("title", "name", "topic"):
        title = as_optional_str(mapping.get(key))
        if title is not None:
            return title
    return None


def extract_timestamp(mapping: dict[str, object]) -> str | None:
    """Extract a timestamp-like field."""
    for key in ("timestamp", "updatedAt", "createdAt", "ts"):
        timestamp = as_optional_str(mapping.get(key))
        if timestamp is not None:
            return timestamp
    return None


def extract_model(mapping: dict[str, object]) -> str | None:
    """Extract a model name."""
    for key in ("model", "modelName", "model_name"):
        model = as_optional_str(mapping.get(key))
        if model is not None:
            return model
    return None


def extract_session_id(mapping: dict[str, object]) -> str | None:
    """Extract a session identifier."""
    for key in ("session_id", "sessionId", "id"):
        value = as_optional_str(mapping.get(key))
        if value is not None:
            return value
    return None


def extract_conversation_id(mapping: dict[str, object]) -> str | None:
    """Extract a conversation identifier."""
    for key in ("conversation_id", "conversationId", "threadId"):
        value = as_optional_str(mapping.get(key))
        if value is not None:
            return value
    return None


def flatten_summary_bullets(value: object) -> str | None:
    """Flatten Cursor summary bullets."""
    if value is None:
        return None
    if isinstance(value, str):
        parsed = parse_embedded_json(value)
        if isinstance(parsed, list):
            bullets = [item for item in parsed if isinstance(item, str) and item.strip()]
            return "\n".join(f"- {item}" for item in bullets) if bullets else value.strip() or None
        return value.strip() or None
    if isinstance(value, (bytes, bytearray)):
        decoded = decode_sqlite_value(value)
        return flatten_summary_bullets(decoded)
    return None


def build_search_record(source: SourceHandle, candidate: MessageCandidate) -> SearchRecord:
    """Convert a parsed candidate into a normalized search record."""
    role = candidate.role.casefold() if candidate.role is not None else None
    kind: t.Literal["prompt", "history"] = "prompt" if role in USER_ROLES else "history"
    return SearchRecord(
        kind=kind,
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=candidate.text,
        title=candidate.title,
        role=candidate.role,
        timestamp=candidate.timestamp,
        model=candidate.model,
        session_id=candidate.session_id,
        conversation_id=candidate.conversation_id,
        origin=candidate.origin,
        identity_namespace=candidate.identity_namespace,
    )
