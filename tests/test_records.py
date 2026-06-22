"""Tests for :mod:`agentgrep.records` and its facade re-exports.

The decoupling moved the domain record types and shared vocabulary out of the
package facade into :mod:`agentgrep.records`. These tests pin the migration
invariant: ``agentgrep.X`` must be the *same object* as ``agentgrep.records.X``
so existing ``import agentgrep; agentgrep.SearchRecord`` call sites keep working
byte-for-byte. See ADR 0008.
"""

from __future__ import annotations

import agentgrep
from agentgrep import records

REEXPORTED_NAMES = (
    "SearchRecord",
    "FindRecord",
    "SourceHandle",
    "MessageCandidate",
    "SearchQuery",
    "BackendSelection",
    "SourceVersionDetection",
    "DiscoveryVersionContext",
    "SearchRecordPayload",
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


def test_store_role_constants_are_disjoint() -> None:
    """Prompt-history and conversation role sets do not overlap."""
    assert not (records.PROMPT_HISTORY_STORE_ROLES & records.CONVERSATION_STORE_ROLES)
