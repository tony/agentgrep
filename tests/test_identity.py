"""Tests for the canonical content-identity vocabulary (:mod:`agentgrep.identity`)."""

from __future__ import annotations

import hashlib
import json
import pathlib
import typing as t

import pytest

from agentgrep import identity
from agentgrep._text import format_display_path
from agentgrep.records import SearchRecord


def _make_record(**overrides: object) -> SearchRecord:
    """Build a SearchRecord with sensible defaults for identity tests."""
    fields: dict[str, object] = {
        "kind": "prompt",
        "agent": "codex",
        "store": "sessions",
        "adapter_id": "codex.sessions_jsonl.v1",
        "path": pathlib.Path.home() / ".codex/sessions/rollout.jsonl",
        "text": "refactor the parser",
        "session_id": "sid-1",
        "timestamp": "2026-01-01T00:00:00Z",
    }
    fields.update(overrides)
    return SearchRecord(**t.cast("t.Any", fields))


def _legacy_fingerprint(record: SearchRecord) -> str:
    """Reproduce the pre-unification MCP fingerprint byte-for-byte."""
    payload = {
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
        "text_sha256": hashlib.sha256(record.text.encode("utf-8")).hexdigest(),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8",
    )
    return hashlib.sha256(raw).hexdigest()


def test_content_id_is_deterministic() -> None:
    """The same normalized record hashes to the same content id every time."""
    record = _make_record()
    assert identity.record_content_id(record) == identity.record_content_id(_make_record())


def test_content_id_excludes_timestamp() -> None:
    """A re-scan that only advances the mtime timestamp keeps the content id."""
    base = _make_record(timestamp="2026-01-01T00:00:00Z")
    touched = _make_record(timestamp="2099-12-31T23:59:59Z")
    assert identity.record_content_id(base) == identity.record_content_id(touched)


def test_content_id_collapses_across_adapter_within_store() -> None:
    """Two records differing only by adapter_id/path collapse to one content id.

    They still receive distinct locator ids so the physical occurrences stay
    addressable.
    """
    left = _make_record(adapter_id="codex.sessions_jsonl.v1", path=pathlib.Path.home() / "a.jsonl")
    right = _make_record(adapter_id="codex.history_jsonl.v1", path=pathlib.Path.home() / "b.jsonl")
    assert identity.record_content_id(left) == identity.record_content_id(right)
    assert identity.record_locator_id(left) != identity.record_locator_id(right)


def test_content_id_distinguishes_stores() -> None:
    """Records in different stores keep different content ids (store is keyed)."""
    left = _make_record(store="sessions")
    right = _make_record(store="history")
    assert identity.record_content_id(left) != identity.record_content_id(right)


def test_id_less_store_fallback_is_pii_safe() -> None:
    """An id-less store coalesces to the home-collapsed display path, never absolute."""
    record = _make_record(
        agent="cursor-cli",
        store="prompt_history",
        adapter_id="cursor_cli.prompt_history_json.v1",
        path=pathlib.Path.home() / ".cursor/prompts.json",
        session_id=None,
        conversation_id=None,
    )
    anchor = identity.session_identity(record)
    assert anchor == format_display_path(record.path)
    assert anchor.startswith("~")
    assert str(pathlib.Path.home()) not in anchor
    # the engine dedupe key shares the same PII-safe coalesce
    assert identity.content_key(record)[3] == anchor


def test_content_key_and_content_id_share_session_identity() -> None:
    """The fast dedupe tuple and the content id agree on the session component."""
    record = _make_record(session_id=None, conversation_id="conv-9")
    assert identity.content_key(record)[3] == identity.session_identity(record)
    assert identity.conversation_anchor(record) == "conv-9"


def test_short_id_shape() -> None:
    """The short id is a 12-char lowercase base32hex prefix."""
    short = identity.short_id(identity.record_content_id(_make_record()))
    assert len(short) == 12
    assert set(short) <= set("0123456789abcdefghijklmnopqrstuv")


def test_short_id_preserves_order() -> None:
    """Sorting short ids matches sorting the full content ids (prefix-order)."""
    ids = [identity.record_content_id(_make_record(text=f"prompt {i}")) for i in range(50)]
    shorts = {full: identity.short_id(full) for full in ids}
    by_full = sorted(ids)
    by_short = sorted(ids, key=lambda full: shorts[full])
    assert [shorts[f] for f in by_full] == [shorts[f] for f in by_short]


def test_conversation_content_hash_is_order_independent() -> None:
    """The Merkle-style conversation hash ignores member order."""
    members = ["id-c", "id-a", "id-b"]
    assert identity.conversation_content_hash(members) == identity.conversation_content_hash(
        reversed(members),
    )


def test_conversation_content_hash_is_membership_sensitive() -> None:
    """Adding or changing a member changes the conversation hash."""
    base = identity.conversation_content_hash(["id-a", "id-b"])
    assert base != identity.conversation_content_hash(["id-a", "id-b", "id-c"])
    assert base != identity.conversation_content_hash(["id-a", "id-z"])


class _PrefixCase(t.NamedTuple):
    test_id: str
    prefix: str
    candidates: list[str]
    status: str
    match: str | None


_PREFIX_CASES = [
    _PrefixCase("unique", "abc", ["abcd", "wxyz"], "unique", "abcd"),
    _PrefixCase("ambiguous", "ab", ["abcd", "abce", "zz"], "ambiguous", None),
    _PrefixCase("none", "q", ["abcd", "wxyz"], "none", None),
    _PrefixCase("case_insensitive", "AB", ["abcd", "wxyz"], "unique", "abcd"),
    _PrefixCase("duplicate_ids_collapse", "abcd", ["abcd", "abcd"], "unique", "abcd"),
]


@pytest.mark.parametrize("case", _PREFIX_CASES, ids=lambda case: case.test_id)
def test_resolve_short_prefix(case: _PrefixCase) -> None:
    """Prefix resolution reports unique / ambiguous / none git-style."""
    result = identity.resolve_short_prefix(case.prefix, case.candidates)
    assert result.status == case.status
    assert result.match == case.match
    if case.status == "ambiguous":
        assert result.min_length is not None
        assert result.min_length >= len(case.prefix)
    elif case.status == "unique":
        # A unique match widens to the query length, never past it (duplicate
        # full ids must collapse rather than inflate the distinguishing length).
        assert result.min_length == len(case.prefix)


def test_locator_id_is_byte_compatible_with_legacy_fingerprint() -> None:
    """The locator id reproduces the historical fingerprint hex exactly.

    This is the contract that keeps every issued ``agref1:`` ref and the
    ``inspect_result`` equality check valid across the unification.
    """
    for record in (
        _make_record(),
        _make_record(role="assistant", conversation_id="conv-1", model="gpt-5.4"),
        _make_record(session_id=None, conversation_id=None, timestamp=None),
    ):
        assert identity.record_locator_id(record) == _legacy_fingerprint(record)


def test_lone_surrogate_text_hashes_without_raising() -> None:
    """Text carrying a lone surrogate hashes through both ids instead of raising.

    A strict ``str.encode('utf-8')`` raises ``UnicodeEncodeError`` here; the
    surrogatepass codec is what keeps the MCP search response from aborting.
    """
    record = _make_record(text="broken\ud800tail")
    with pytest.raises(UnicodeEncodeError):
        record.text.encode("utf-8")
    assert len(identity.record_content_id(record)) == 64
    assert len(identity.record_locator_id(record)) == 64
