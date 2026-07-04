"""Tests for :mod:`agentgrep.records` and its facade re-exports.

The decoupling moved the domain record types and shared vocabulary out of the
package facade into :mod:`agentgrep.records`. These tests pin the migration
invariant: ``agentgrep.X`` must be the *same object* as ``agentgrep.records.X``
so existing ``import agentgrep; agentgrep.SearchRecord`` call sites keep working
byte-for-byte. See ADR 0008.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep import records

REEXPORTED_NAMES = (
    "SearchRecord",
    "FindRecord",
    "SourceHandle",
    "MessageCandidate",
    "RecordOrigin",
    "SearchQuery",
    "BackendSelection",
    "SourceVersionDetection",
    "DiscoveryVersionContext",
    "SearchRecordPayload",
    "RecordOriginPayload",
    "FindRecordPayload",
    "SourceHandlePayload",
    "EnvelopePayload",
    "SourceVersionDetectionPayload",
    "AGENT_CHOICES",
    "SCHEMA_VERSION",
    "PROMPT_HISTORY_STORE_ROLES",
    "CONVERSATION_STORE_ROLES",
    "ITER_SOURCE_RECORD_ADAPTERS",
)


class SearchRecordPositionalCase(t.NamedTuple):
    """Parametrized positional-constructor compatibility case."""

    test_id: str
    metadata: dict[str, object]


SEARCH_RECORD_POSITIONAL_CASES: tuple[SearchRecordPositionalCase, ...] = (
    SearchRecordPositionalCase(
        test_id="legacy-metadata-final-argument",
        metadata={"project": "/workspace/agentgrep"},
    ),
)


def test_facade_reexports_records_identity() -> None:
    """Every re-exported name on the facade is the records.py object."""
    for name in REEXPORTED_NAMES:
        assert getattr(agentgrep, name) is getattr(records, name), name


def test_search_record_constructs_via_facade_and_module() -> None:
    """The same dataclass is reachable from both import paths."""
    import pathlib

    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.history",
        adapter_id="codex.history_jsonl.v1",
        path=pathlib.Path("/tmp/example.jsonl"),
        text="serenity and bliss",
    )
    assert isinstance(record, records.SearchRecord)
    assert record.title is None


@pytest.mark.parametrize(
    SearchRecordPositionalCase._fields,
    SEARCH_RECORD_POSITIONAL_CASES,
    ids=[case.test_id for case in SEARCH_RECORD_POSITIONAL_CASES],
)
def test_search_record_keeps_metadata_positional_slot(
    test_id: str,
    metadata: dict[str, object],
) -> None:
    """The pre-origin positional metadata argument stays compatible."""
    _ = test_id
    record = agentgrep.SearchRecord(
        "prompt",
        "codex",
        "codex.history",
        "codex.history_jsonl.v1",
        pathlib.Path("/tmp/example.jsonl"),
        "serenity and bliss",
        None,
        None,
        None,
        None,
        None,
        None,
        metadata,
    )

    assert record.metadata == metadata
    assert record.origin is None
    payload = agentgrep.serialize_search_record(record)
    assert payload["metadata"]["project"] == "/workspace/agentgrep/"
    assert payload["origin"] is None


def test_store_role_constants_are_disjoint() -> None:
    """Prompt-history and conversation role sets do not overlap."""
    assert not (records.PROMPT_HISTORY_STORE_ROLES & records.CONVERSATION_STORE_ROLES)
