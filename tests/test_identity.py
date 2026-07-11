"""Canonical identity tests for normalized agent records."""

from __future__ import annotations

import importlib.util
import pathlib
import re
import typing as t

import pytest

from agentgrep.adapters import build_search_record
from agentgrep.identity import (
    ContentIdentityKey,
    content_identity_key,
    record_content_id,
    record_thread_id,
)
from agentgrep.records import MessageCandidate, RecordOrigin, SearchRecord, SourceHandle


class ContentVector(t.NamedTuple):
    """One canonical content-identity test vector."""

    test_id: str
    kind: t.Literal["prompt", "history"]
    role: str | None
    text: str
    expected: str


class IdentityMutationCase(t.NamedTuple):
    """One content- or thread-identity mutation case."""

    test_id: str
    changes: tuple[tuple[str, object], ...]
    expected_same: bool


HELLO_VECTOR = ContentVector(
    "hello",
    "prompt",
    "user",
    "hello",
    "agc1:2vlm1978v1np5kg5fkqv539kic",
)

CONTENT_VECTORS: tuple[ContentVector, ...] = (
    ContentVector(
        "unicode",
        "history",
        "assistant",
        "café 🦀",
        "agc1:r44ve3shjjbalu1hvik50ai23g",
    ),
    ContentVector(
        "nfc",
        "prompt",
        "user",
        "é",
        "agc1:nkkvnhpa3smnlibl9ral6e4b08",
    ),
    ContentVector(
        "nfd",
        "prompt",
        "user",
        "e\N{COMBINING ACUTE ACCENT}",
        "agc1:jdskr3ppb5b6m8s4fr47nud7b4",
    ),
    ContentVector(
        "crlf",
        "prompt",
        "user",
        "line 1\r\nline 2",
        "agc1:4e8g5oumevds43g868v42oqd38",
    ),
    ContentVector(
        "lf",
        "prompt",
        "user",
        "line 1\nline 2",
        "agc1:6bsc51lcrptlgbo6hf92de0r5s",
    ),
    ContentVector(
        "empty-text",
        "prompt",
        "user",
        "",
        "agc1:fuov2mas49ueism5bb92omh3g0",
    ),
    ContentVector(
        "null-role",
        "history",
        None,
        "hello",
        "agc1:63sa5b1jftnkpm1gen4p5ogg1g",
    ),
    ContentVector(
        "lone-surrogate",
        "history",
        "assistant",
        "a\ud800b",
        "agc1:unale8egkej0oa2kvr96edklq8",
    ),
)

CONTENT_MUTATION_CASES: tuple[IdentityMutationCase, ...] = (
    IdentityMutationCase("path", (("path", pathlib.Path("elsewhere.jsonl")),), True),
    IdentityMutationCase("adapter", (("adapter_id", "duplicate.adapter.v2"),), True),
    IdentityMutationCase("timestamp", (("timestamp", "2030-01-02T03:04:05Z"),), True),
    IdentityMutationCase("title", (("title", "Changed title"),), True),
    IdentityMutationCase("model", (("model", "changed-model"),), True),
    IdentityMutationCase(
        "origin",
        (("origin", RecordOrigin(cwd="/different/project", branch="next")),),
        True,
    ),
    IdentityMutationCase("session", (("session_id", "different-session"),), True),
    IdentityMutationCase(
        "conversation",
        (("conversation_id", "different-conversation"),),
        True,
    ),
    IdentityMutationCase("agent", (("agent", "claude"),), True),
    IdentityMutationCase("store", (("store", "duplicate.store"),), True),
    IdentityMutationCase(
        "identity-namespace",
        (("identity_namespace", "duplicate.namespace"),),
        True,
    ),
    IdentityMutationCase("role-case", (("role", "USER"),), True),
    IdentityMutationCase("kind", (("kind", "history"),), False),
    IdentityMutationCase("normalized-role", (("role", "assistant"),), False),
    IdentityMutationCase("exact-text", (("text", "hello!"),), False),
)

THREAD_MUTATION_CASES: tuple[IdentityMutationCase, ...] = (
    IdentityMutationCase("physical-store", (("store", "duplicate.store"),), True),
    IdentityMutationCase("physical-adapter", (("adapter_id", "duplicate.v2"),), True),
    IdentityMutationCase(
        "session-wins-over-conversation",
        (("conversation_id", "ignored-conversation"),),
        True,
    ),
    IdentityMutationCase("agent", (("agent", "claude"),), False),
    IdentityMutationCase(
        "identity-namespace",
        (("identity_namespace", "codex.other-session"),),
        False,
    ),
    IdentityMutationCase("native-session", (("session_id", "def"),), False),
    IdentityMutationCase(
        "native-key-kind",
        (("session_id", None), ("conversation_id", "abc")),
        False,
    ),
)


@pytest.fixture
def search_record() -> SearchRecord:
    """Return a normalized record with defensible content and thread identity."""
    return SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("session.jsonl"),
        text="hello",
        title="Greeting",
        role="user",
        timestamp="2026-07-10T12:00:00Z",
        model="gpt-test",
        session_id="abc",
        conversation_id=None,
        origin=RecordOrigin(cwd="/project", branch="main"),
        identity_namespace="codex.session",
    )


def _apply_changes(record: SearchRecord, changes: tuple[tuple[str, object], ...]) -> None:
    """Apply one typed mutation case to a mutable test record."""
    for field_name, value in changes:
        setattr(record, field_name, value)


def test_identity_module_is_available() -> None:
    """The canonical identity owner module is importable."""
    assert importlib.util.find_spec("agentgrep.identity") is not None


def test_content_identity_key_normalizes_role(search_record: SearchRecord) -> None:
    """The unhashed semantic key casefolds a non-empty role."""
    search_record.role = "UsEr"

    assert content_identity_key(search_record) == ContentIdentityKey(
        kind="prompt",
        role="user",
        text="hello",
    )


def test_content_identity_key_maps_empty_role_to_none(search_record: SearchRecord) -> None:
    """An empty role has the same canonical absence as a null role."""
    search_record.role = ""

    assert content_identity_key(search_record).role is None


def test_build_search_record_preserves_identity_namespace() -> None:
    """Normalization carries the adapter-owned logical namespace."""
    source = SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("session.jsonl"),
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=0,
    )
    candidate = MessageCandidate(
        role="user",
        text="hello",
        identity_namespace="codex.session",
    )

    record = build_search_record(source, candidate)

    assert record.identity_namespace == "codex.session"


@pytest.mark.parametrize(
    "case",
    CONTENT_VECTORS,
    ids=[case.test_id for case in CONTENT_VECTORS],
)
def test_record_content_id_matches_golden_vectors(
    search_record: SearchRecord,
    case: ContentVector,
) -> None:
    """Canonical content IDs match byte-sensitive fixed vectors."""
    search_record.kind = case.kind
    search_record.role = case.role
    search_record.text = case.text

    assert record_content_id(search_record) == case.expected


def test_record_content_id_matches_golden_vector(search_record: SearchRecord) -> None:
    """The public hello vector remains stable across implementations."""
    search_record.kind = HELLO_VECTOR.kind
    search_record.role = HELLO_VECTOR.role
    search_record.text = HELLO_VECTOR.text

    assert HELLO_VECTOR.expected == "agc1:2vlm1978v1np5kg5fkqv539kic"
    assert record_content_id(search_record) == HELLO_VECTOR.expected


@pytest.mark.parametrize(
    "case",
    CONTENT_MUTATION_CASES,
    ids=[case.test_id for case in CONTENT_MUTATION_CASES],
)
def test_record_content_id_uses_only_canonical_content_fields(
    search_record: SearchRecord,
    case: IdentityMutationCase,
) -> None:
    """Only kind, normalized role, and exact text affect content identity."""
    original = record_content_id(search_record)

    _apply_changes(search_record, case.changes)

    assert (record_content_id(search_record) == original) is case.expected_same


def test_record_content_id_has_one_fixed_width_form(search_record: SearchRecord) -> None:
    """Content identity exposes one full lowercase base32hex form."""
    assert re.fullmatch(r"agc1:[0-9a-v]{26}", record_content_id(search_record))


def test_record_thread_id_matches_golden_vector(search_record: SearchRecord) -> None:
    """The public Codex session vector remains stable across implementations."""
    search_record.agent = "codex"
    search_record.store = "codex.sessions"
    search_record.identity_namespace = "codex.session"
    search_record.session_id = "abc"
    search_record.conversation_id = None

    assert record_thread_id(search_record) == "agt1:bkd9k19ok4vvbsf73jornija04"


@pytest.mark.parametrize(
    "case",
    THREAD_MUTATION_CASES,
    ids=[case.test_id for case in THREAD_MUTATION_CASES],
)
def test_record_thread_id_uses_logical_native_identity(
    search_record: SearchRecord,
    case: IdentityMutationCase,
) -> None:
    """Thread identity excludes physical stores but includes logical anchors."""
    original = record_thread_id(search_record)

    _apply_changes(search_record, case.changes)

    assert (record_thread_id(search_record) == original) is case.expected_same


@pytest.mark.parametrize(
    ("namespace", "session_id", "conversation_id"),
    [
        pytest.param(None, "abc", None, id="missing-namespace"),
        pytest.param("", "abc", None, id="empty-namespace"),
        pytest.param("codex.session", None, None, id="missing-anchor"),
        pytest.param("codex.session", "", "", id="empty-anchor"),
    ],
)
def test_record_thread_id_requires_namespace_and_anchor(
    search_record: SearchRecord,
    namespace: str | None,
    session_id: str | None,
    conversation_id: str | None,
) -> None:
    """Thread identity is absent without both namespace and native anchor."""
    search_record.identity_namespace = namespace
    search_record.session_id = session_id
    search_record.conversation_id = conversation_id

    assert record_thread_id(search_record) is None


@pytest.mark.parametrize(
    "conversation_id",
    [
        pytest.param("/tmp/conversation", id="posix"),
        pytest.param(r"C:\\conversations\\abc", id="windows"),
        pytest.param("~", id="home"),
    ],
)
def test_record_thread_id_rejects_path_shaped_conversation(
    search_record: SearchRecord,
    conversation_id: str,
) -> None:
    """Filesystem-shaped conversation fallbacks cannot mint durable IDs."""
    search_record.session_id = None
    search_record.conversation_id = conversation_id

    assert record_thread_id(search_record) is None


def test_record_thread_id_accepts_native_conversation(search_record: SearchRecord) -> None:
    """A namespaced native conversation token can mint a fixed-width ID."""
    search_record.session_id = None
    search_record.conversation_id = "conversation-abc"

    thread_id = record_thread_id(search_record)

    assert thread_id is not None
    assert re.fullmatch(r"agt1:[0-9a-v]{26}", thread_id)
