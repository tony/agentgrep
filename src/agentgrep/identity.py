"""Canonical content and thread identity for normalized records."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import typing as t

from agentgrep.origin import is_path_like_text

__all__ = (
    "ContentIdentityKey",
    "content_identity_key",
    "record_content_id",
    "record_thread_id",
)

_CONTENT_PREFIX = "agc1:"
_THREAD_PREFIX = "agt1:"


class _ContentIdentityRecord(t.Protocol):
    """Structural input required to derive content identity."""

    @property
    def kind(self) -> str:
        """Return the normalized record kind."""
        ...

    @property
    def role(self) -> str | None:
        """Return the source role, when present."""
        ...

    @property
    def text(self) -> str:
        """Return the exact normalized record text."""
        ...


class _ThreadIdentityRecord(t.Protocol):
    """Structural input required to derive thread identity."""

    @property
    def agent(self) -> str:
        """Return the normalized agent name."""
        ...

    @property
    def identity_namespace(self) -> str | None:
        """Return the adapter-owned logical identity namespace."""
        ...

    @property
    def session_id(self) -> str | None:
        """Return the backend-native session identifier."""
        ...

    @property
    def conversation_id(self) -> str | None:
        """Return the backend-native conversation identifier."""
        ...


@dataclasses.dataclass(frozen=True, slots=True)
class ContentIdentityKey:
    """Unhashed semantic key for one normalized record's content."""

    kind: str
    role: str | None
    text: str


def content_identity_key(record: _ContentIdentityRecord) -> ContentIdentityKey:
    """Return the semantic equality key for ``record``.

    Parameters
    ----------
    record
        Normalized record-like value.

    Returns
    -------
    ContentIdentityKey
        The normalized, unhashed content key.
    """
    role = record.role.casefold() if record.role else None
    return ContentIdentityKey(kind=record.kind, role=role, text=record.text)


def _canonical_bytes(payload: t.Mapping[str, object]) -> bytes:
    """Encode one canonical identity envelope as compact sorted JSON."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8", "surrogatepass")


def _format_id(prefix: str, payload: t.Mapping[str, object]) -> str:
    """Format the first 128 SHA-256 bits as lowercase base32hex."""
    digest = hashlib.sha256(_canonical_bytes(payload)).digest()[:16]
    encoded = base64.b32hexencode(digest).decode("ascii").rstrip("=").lower()
    return f"{prefix}{encoded}"


def record_content_id(record: _ContentIdentityRecord) -> str:
    """Return the fixed-width canonical content identifier for ``record``.

    Parameters
    ----------
    record
        Normalized record-like value.

    Returns
    -------
    str
        Versioned content identifier.
    """
    key = content_identity_key(record)
    text_sha256 = hashlib.sha256(key.text.encode("utf-8", "surrogatepass")).hexdigest()
    return _format_id(
        _CONTENT_PREFIX,
        {
            "kind": key.kind,
            "role": key.role,
            "text_sha256": text_sha256,
            "type": "record-content",
            "v": 1,
        },
    )


def record_thread_id(record: _ThreadIdentityRecord) -> str | None:
    """Return the fixed-width canonical thread identifier when defensible.

    Parameters
    ----------
    record
        Normalized record-like value.

    Returns
    -------
    str | None
        Versioned thread identifier, or ``None`` without a logical anchor.
    """
    namespace = record.identity_namespace
    if not namespace:
        return None

    if record.session_id:
        key_kind = "session"
        key_value = record.session_id
    elif record.conversation_id and not is_path_like_text(record.conversation_id):
        key_kind = "conversation"
        key_value = record.conversation_id
    else:
        return None

    return _format_id(
        _THREAD_PREFIX,
        {
            "agent": record.agent,
            "key_kind": key_kind,
            "key_value": key_value,
            "namespace": namespace,
            "type": "thread",
            "v": 1,
        },
    )
