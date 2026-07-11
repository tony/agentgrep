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
    "RecordIdentity",
    "content_identity_key",
    "record_content_id",
    "record_identity",
    "record_thread_id",
)

_CONTENT_PREFIX = "agc1:"
_RECORD_PREFIX = "agr1:"
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


class _RecordPosition(t.Protocol):
    """Structural source coordinate required for occurrence identity."""

    @property
    def native_id(self) -> str | None:
        """Return the backend-native occurrence identifier, when present."""
        ...

    @property
    def ordinal(self) -> int | None:
        """Return the stable source ordinal, when present."""
        ...


class _RecordIdentityRecord(_ContentIdentityRecord, _ThreadIdentityRecord, t.Protocol):
    """Structural input required to derive one prepared identity bundle."""

    @property
    def store(self) -> str:
        """Return the normalized store name."""
        ...

    @property
    def adapter_id(self) -> str:
        """Return the normalized adapter identifier."""
        ...

    @property
    def position(self) -> _RecordPosition | None:
        """Return the normalized logical occurrence position, when present."""
        ...


@dataclasses.dataclass(frozen=True, slots=True)
class ContentIdentityKey:
    """Unhashed semantic key for one normalized record's content."""

    kind: str
    role: str | None
    text: str


@dataclasses.dataclass(frozen=True, slots=True)
class RecordIdentity:
    """Prepared canonical identifiers for one normalized record."""

    text_sha256: str
    content_id: str
    record_id: str | None
    record_id_stability: t.Literal["native", "source_order"] | None
    thread_id: str | None


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


def _content_id(key: ContentIdentityKey, text_sha256: str) -> str:
    """Return the canonical content ID for one prepared text digest."""
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
    return _content_id(key, text_sha256)


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


def record_identity(record: _RecordIdentityRecord) -> RecordIdentity:
    """Return one prepared canonical identity bundle for ``record``.

    Parameters
    ----------
    record
        Normalized record-like value.

    Returns
    -------
    RecordIdentity
        Canonical content, record, and thread identifiers.
    """
    key = content_identity_key(record)
    text_sha256 = hashlib.sha256(key.text.encode("utf-8", "surrogatepass")).hexdigest()
    content_id = _content_id(key, text_sha256)
    thread_id = record_thread_id(record)
    position = record.position

    coordinate_kind: str | None = None
    coordinate_value: str | int | None = None
    record_id_stability: t.Literal["native", "source_order"] | None = None
    if thread_id is not None and position is not None:
        if isinstance(position.native_id, str) and position.native_id:
            coordinate_kind = "native"
            coordinate_value = position.native_id
            record_id_stability = "native"
        elif (
            isinstance(position.ordinal, int)
            and not isinstance(position.ordinal, bool)
            and position.ordinal >= 0
        ):
            coordinate_kind = "ordinal"
            coordinate_value = position.ordinal
            record_id_stability = "source_order"

    if coordinate_kind is None or coordinate_value is None:
        record_id = None
    else:
        payload: dict[str, object] = {
            "agent": record.agent,
            "content_id": content_id,
            "coordinate_kind": coordinate_kind,
            "coordinate_value": coordinate_value,
            "thread_id": thread_id,
            "type": "record",
            "v": 1,
        }
        if coordinate_kind == "ordinal":
            payload["coordinate_domain"] = (record.store, record.adapter_id)
        record_id = _format_id(_RECORD_PREFIX, payload)

    return RecordIdentity(
        text_sha256=text_sha256,
        content_id=content_id,
        record_id=record_id,
        record_id_stability=record_id_stability,
        thread_id=thread_id,
    )
