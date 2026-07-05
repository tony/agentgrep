"""Canonical content identity for normalized agentgrep records.

This module owns the *one* recipe for turning a normalized
:class:`~agentgrep.records.SearchRecord` into a stable, privacy-safe
identity, so the engine's dedupe key and the MCP drilldown ref stop
hand-rolling disagreeing schemes. It is records-adjacent per ADR 0010:
it imports only the standard library and :func:`agentgrep._text.format_display_path`,
never the engine, adapters, or any frontend.

Three identities, one field vocabulary:

``content_key``
    The cheap 5-tuple the engine's per-record dedupe hot loop consumes.
    Equality in a ``set``/``dict`` with no hashing, so a default headless
    search pays no id tax.

``record_content_id``
    The stable content-core id: a hex digest over ``{version, kind, agent,
    store, session-identity, text-digest}``. It deliberately excludes the
    timestamp (mtime-derived for several backends) and the physical path,
    so it survives a file touch, a re-scan, and a store move, and two
    records that differ only in ``adapter_id`` collapse to one id.

``record_locator_id``
    The physical handle: the exact field set of the historical
    ``search_record_fingerprint`` (kind/role/agent/store/adapter_id/display
    path/timestamp/session/conversation/text-digest), so every issued
    ``agref1:`` ref and the ``inspect_result`` equality contract stay
    byte-compatible. It only gains the ``surrogatepass`` codec that keeps a
    lone surrogate in decoded store text from raising.

The short :func:`short_id` is a base32hex projection of the content id:
copy/tmux/filename-safe, case-insensitive, and order-preserving, so a
short prefix resolves git-style against a corpus via
:func:`resolve_short_prefix`.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import typing as t

from agentgrep._text import format_display_path

if t.TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agentgrep.records import SearchRecord

__all__ = [
    "ShortIdResolution",
    "content_key",
    "conversation_anchor",
    "conversation_content_hash",
    "record_content_id",
    "record_locator_id",
    "resolve_short_prefix",
    "session_identity",
    "short_id",
]

_ID_VERSION = 1
_SHORT_ID_LENGTH = 12
ContentKey = tuple[str, str, str, str, str]


def _canonical_bytes(fields: t.Mapping[str, object]) -> bytes:
    """Return the canonical UTF-8 encoding of ``fields`` for hashing.

    Uses sorted keys and compact separators so the byte stream is a pure
    function of the field values, and ``surrogatepass`` so text carrying a
    lone surrogate from an imperfectly decoded store hashes instead of
    raising.
    """
    return json.dumps(
        fields,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8", "surrogatepass")


def _text_digest_hex(text: str) -> str:
    """Return the sha256 hex digest of ``text`` (surrogate-tolerant)."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def session_identity(record: SearchRecord) -> str:
    """Return the privacy-safe conversation-grouping key for ``record``.

    Coalesces the backend-native ``session_id`` then ``conversation_id``,
    falling back to the home-collapsed display path for id-less stores
    (Cursor CLI prompt history, VS Code inline history). The path fallback
    routes through :func:`agentgrep._text.format_display_path` so it never
    embeds an absolute home path.

    Parameters
    ----------
    record : SearchRecord
        The normalized record to key.

    Returns
    -------
    str
        A non-empty grouping key.
    """
    return record.session_id or record.conversation_id or format_display_path(record.path)


def content_key(record: SearchRecord) -> ContentKey:
    """Return the cheap engine dedupe tuple for ``record``.

    This is the fast projection the per-record dedupe loop consumes: plain
    tuple equality, no hashing. It shares the exact field extraction with
    :func:`record_content_id`, so the fast path and the serialized id can
    never drift.

    Parameters
    ----------
    record : SearchRecord
        The normalized record to key.

    Returns
    -------
    tuple of str
        ``(kind, agent, store, session-identity, text)``.
    """
    return (
        record.kind,
        record.agent,
        record.store,
        session_identity(record),
        record.text,
    )


def record_content_id(record: SearchRecord) -> str:
    """Return the stable content-core id for ``record`` as a hex digest.

    Stable across a re-scan, a file touch (no timestamp), and a store move
    (no physical path except as the id-less session fallback). Two records
    that share ``store`` but differ only in ``adapter_id`` collapse to one
    id; records in different ``store`` values stay distinct, matching the
    engine's dedupe intent.

    Parameters
    ----------
    record : SearchRecord
        The normalized record to identify.

    Returns
    -------
    str
        A 64-character sha256 hex digest.
    """
    fields = {
        "v": _ID_VERSION,
        "kind": record.kind,
        "agent": record.agent,
        "store": record.store,
        "session": session_identity(record),
        "text_sha256": _text_digest_hex(record.text),
    }
    return hashlib.sha256(_canonical_bytes(fields)).hexdigest()


def record_locator_id(record: SearchRecord) -> str:
    """Return the physical locator id for ``record`` as a hex digest.

    Addresses one specific on-disk occurrence, folding the display path,
    timestamp, and full provenance. The field set matches the historical
    MCP fingerprint exactly, so the derived ``agref1:`` refs stay
    byte-compatible; the only change is the surrogate-tolerant text codec.

    Parameters
    ----------
    record : SearchRecord
        The normalized record to locate.

    Returns
    -------
    str
        A 64-character sha256 hex digest.
    """
    fields = {
        "kind": "search",
        "record_kind": record.kind,
        "role": record.role,
        "agent": record.agent,
        "store": record.store,
        "adapter_id": record.adapter_id,
        "path": format_display_path(record.path),
        "timestamp": record.timestamp,
        "session_id": record.session_id,
        "conversation_id": record.conversation_id,
        "text_sha256": _text_digest_hex(record.text),
    }
    return hashlib.sha256(_canonical_bytes(fields)).hexdigest()


def short_id(content_id: str, *, length: int = _SHORT_ID_LENGTH) -> str:
    """Return a short, copy-safe base32hex handle for a content id.

    Extended-hex base32 packs 5 bits per character, is case-insensitive,
    filename/tmux-safe, and order-preserving, so a truncated prefix keeps
    the sort order of the full digests and resolves git-style.

    Parameters
    ----------
    content_id : str
        A hex digest, e.g. from :func:`record_content_id`.
    length : int, optional
        The number of leading characters to keep (default 12 ~= 60 bits).

    Returns
    -------
    str
        A lowercase base32hex prefix of ``length`` characters.

    Examples
    --------
    >>> short_id("0" * 64)
    '000000000000'
    >>> short_id("ff" * 32, length=4)
    'vvvv'
    """
    raw = bytes.fromhex(content_id)
    return base64.b32hexencode(raw).decode("ascii").rstrip("=").lower()[:length]


def conversation_anchor(record: SearchRecord) -> str:
    """Return the durable conversation anchor for ``record``.

    The anchor is the coalesced :func:`session_identity`; it must not
    change as a conversation grows, so it is what a bookmark or an export
    ref cites at the conversation level.

    Parameters
    ----------
    record : SearchRecord
        A member record of the conversation.

    Returns
    -------
    str
        The durable conversation-grouping key.
    """
    return session_identity(record)


def conversation_content_hash(member_content_ids: Iterable[str]) -> str:
    """Return a one-level Merkle content hash over a conversation's members.

    A sha256 over the sorted member content ids. Order-independent and
    membership-sensitive, so it changes on any edit or append and is a
    diffable per-conversation key for consumers with the *complete* member
    set (export). It is intentionally not derived from a partial search
    page, which would depend on the query and pagination.

    Parameters
    ----------
    member_content_ids : iterable of str
        The :func:`record_content_id` of every member turn.

    Returns
    -------
    str
        A 64-character sha256 hex digest.

    Examples
    --------
    >>> conversation_content_hash(["b", "a"]) == conversation_content_hash(["a", "b"])
    True
    """
    fields = {"v": _ID_VERSION, "members": sorted(member_content_ids)}
    return hashlib.sha256(_canonical_bytes(fields)).hexdigest()


class ShortIdResolution(t.NamedTuple):
    """The outcome of resolving a short id prefix against a corpus.

    Attributes
    ----------
    status : {"unique", "ambiguous", "none"}
        Whether the prefix matched exactly one, several, or no candidates.
    match : str or None
        The single full id when ``status`` is ``"unique"``, else ``None``.
    candidates : tuple of str
        The sorted full ids the prefix matched (empty when ``"none"``).
    min_length : int or None
        For ``"ambiguous"``, the shortest prefix length that separates the
        matched candidates; for ``"unique"``, the length of the query
        prefix; ``None`` for ``"none"``.
    """

    status: t.Literal["unique", "ambiguous", "none"]
    match: str | None
    candidates: tuple[str, ...]
    min_length: int | None


def _min_distinguishing_length(sorted_ids: Sequence[str]) -> int:
    """Return the shortest prefix length that separates ``sorted_ids``."""
    longest_common = 0
    for left, right in itertools.pairwise(sorted_ids):
        shared = 0
        for a, b in zip(left, right, strict=False):
            if a != b:
                break
            shared += 1
        longest_common = max(longest_common, shared)
    return longest_common + 1


def resolve_short_prefix(prefix: str, candidates: Iterable[str]) -> ShortIdResolution:
    """Resolve a short id ``prefix`` against a corpus of full ids, git-style.

    A prefix that is unique widens to its single match; an ambiguous prefix
    reports the matches and the length needed to disambiguate; a prefix that
    matches nothing reports ``"none"``.

    Parameters
    ----------
    prefix : str
        A leading fragment of a :func:`short_id` (case-insensitive).
    candidates : iterable of str
        The full short ids to resolve against.

    Returns
    -------
    ShortIdResolution
        The resolution outcome.

    Examples
    --------
    >>> resolve_short_prefix("abc", ["abcd", "xyz"]).match
    'abcd'
    >>> resolve_short_prefix("ab", ["abcd", "abce", "zz"]).status
    'ambiguous'
    >>> resolve_short_prefix("q", ["abcd"]).status
    'none'
    """
    needle = prefix.lower()
    # Dedupe first: record_content_id is designed to collapse across adapter/path
    # within a store, so the same full id routinely appears more than once. Two
    # copies of one id are still one match, not an ambiguous pair.
    matches = tuple(sorted({candidate for candidate in candidates if candidate.startswith(needle)}))
    if not matches:
        return ShortIdResolution("none", None, (), None)
    if len(matches) == 1:
        return ShortIdResolution("unique", matches[0], matches, len(needle))
    return ShortIdResolution("ambiguous", None, matches, _min_distinguishing_length(matches))
