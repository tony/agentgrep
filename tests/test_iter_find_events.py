"""Tests for :func:`agentgrep.iter_find_events`.

Find is simpler than search — no per-source scan loop. Each discovered
source produces exactly one record. The tests below confirm the event
envelope, the substring filter, and the ``limit`` early-stop semantics.
"""

from __future__ import annotations

import json
import pathlib
import typing as t

import pytest

import agentgrep
import agentgrep._engine.find as _rm_find
from agentgrep import events


def _seed_codex_session(home: pathlib.Path, name: str) -> pathlib.Path:
    """Write a minimal Codex session-jsonl so discovery picks it up."""
    path = home / ".codex" / "sessions" / "2025" / "01" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "response_item", "payload": {"role": "user", "content": "hi"}}) + "\n",
    )
    return path


def test_iter_find_events_emits_started_and_finished_when_empty(
    tmp_path: pathlib.Path,
) -> None:
    """Empty home still emits Started/Finished bookends with count=0."""
    out = list(agentgrep.iter_find_events(tmp_path, ("codex",), pattern=None, limit=None))
    assert isinstance(out[0], events.FindStarted)
    assert isinstance(out[-1], events.FindFinished)
    assert out[-1].match_count == 0


def test_iter_find_events_yields_record_per_discovered_source(
    tmp_path: pathlib.Path,
) -> None:
    """Each discovered source produces one FindRecordEmitted."""
    _ = _seed_codex_session(tmp_path, "a.jsonl")
    _ = _seed_codex_session(tmp_path, "b.jsonl")
    out = list(agentgrep.iter_find_events(tmp_path, ("codex",), pattern=None, limit=None))
    records = [ev for ev in out if isinstance(ev, events.FindRecordEmitted)]
    assert len(records) == 2


def test_iter_find_events_pattern_filters_sources(tmp_path: pathlib.Path) -> None:
    """A pattern substring excludes non-matching sources."""
    _ = _seed_codex_session(tmp_path, "alpha.jsonl")
    _ = _seed_codex_session(tmp_path, "beta.jsonl")
    out = list(
        agentgrep.iter_find_events(
            tmp_path,
            ("codex",),
            pattern="alpha",
            limit=None,
        ),
    )
    records = [ev for ev in out if isinstance(ev, events.FindRecordEmitted)]
    assert len(records) == 1
    assert "alpha" in str(records[0].record.path)


class FindTypeDiscoveryCase(t.NamedTuple):
    """Expected discovery role narrowing for one find type filter."""

    test_id: str
    type_filter: str
    expected_store_roles: frozenset[agentgrep.StoreRole] | None


FIND_TYPE_DISCOVERY_CASES: tuple[FindTypeDiscoveryCase, ...] = (
    FindTypeDiscoveryCase(
        test_id="all-keeps-default-discovery",
        type_filter="all",
        expected_store_roles=None,
    ),
    FindTypeDiscoveryCase(
        test_id="prompts-discovers-prompt-history",
        type_filter="prompts",
        expected_store_roles=agentgrep.PROMPT_HISTORY_STORE_ROLES,
    ),
    FindTypeDiscoveryCase(
        test_id="history-discovers-prompt-history",
        type_filter="history",
        expected_store_roles=agentgrep.PROMPT_HISTORY_STORE_ROLES,
    ),
    FindTypeDiscoveryCase(
        test_id="sessions-discovers-conversations",
        type_filter="sessions",
        expected_store_roles=agentgrep.CONVERSATION_STORE_ROLES,
    ),
)


@pytest.mark.parametrize(
    "case",
    FIND_TYPE_DISCOVERY_CASES,
    ids=[c.test_id for c in FIND_TYPE_DISCOVERY_CASES],
)
def test_iter_find_events_pushes_type_filter_into_discovery(
    case: FindTypeDiscoveryCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Find type filters prune catalogue roles before source enumeration."""
    observed_store_roles: list[frozenset[agentgrep.StoreRole] | None] = []

    def discover_sources(
        *_args: object,
        **kwargs: object,
    ) -> list[agentgrep.SourceHandle]:
        observed_store_roles.append(
            t.cast("frozenset[agentgrep.StoreRole] | None", kwargs.get("store_roles")),
        )
        return []

    monkeypatch.setattr(_rm_find, "discover_sources", discover_sources)

    _ = list(
        agentgrep.iter_find_events(
            tmp_path,
            ("codex",),
            pattern=None,
            limit=None,
            type_filter=t.cast("agentgrep.FindSourceTypeFilter", case.type_filter),
        ),
    )

    assert observed_store_roles == [case.expected_store_roles]


class LimitCase(t.NamedTuple):
    """Parametrized case for ``limit`` early-stop on find."""

    test_id: str
    source_count: int
    limit: int
    expected_record_count: int


LIMIT_CASES: tuple[LimitCase, ...] = (
    LimitCase("limit-one-of-three", 3, 1, 1),
    LimitCase("limit-equals-source-count", 3, 3, 3),
    LimitCase("limit-above-source-count", 3, 99, 3),
)


@pytest.mark.parametrize(
    "case",
    LIMIT_CASES,
    ids=[c.test_id for c in LIMIT_CASES],
)
def test_iter_find_events_respects_limit(
    case: LimitCase,
    tmp_path: pathlib.Path,
) -> None:
    """The generator stops yielding RecordEmitted once ``limit`` is hit."""
    for i in range(case.source_count):
        _ = _seed_codex_session(tmp_path, f"source-{i:02d}.jsonl")
    out = list(
        agentgrep.iter_find_events(
            tmp_path,
            ("codex",),
            pattern=None,
            limit=case.limit,
        ),
    )
    records = [ev for ev in out if isinstance(ev, events.FindRecordEmitted)]
    assert len(records) == case.expected_record_count


def test_iter_find_events_match_count_matches_emitted_records(
    tmp_path: pathlib.Path,
) -> None:
    """The terminal Finished event's match_count equals RecordEmitted count."""
    for i in range(3):
        _ = _seed_codex_session(tmp_path, f"source-{i:02d}.jsonl")
    out = list(agentgrep.iter_find_events(tmp_path, ("codex",), pattern=None, limit=None))
    records = [ev for ev in out if isinstance(ev, events.FindRecordEmitted)]
    finished = [ev for ev in out if isinstance(ev, events.FindFinished)]
    assert len(finished) == 1
    assert finished[0].match_count == len(records)


def test_iter_find_events_event_sequence_is_well_formed(
    tmp_path: pathlib.Path,
) -> None:
    """Documented sequence: Started → *RecordEmitted → Finished."""
    _ = _seed_codex_session(tmp_path, "one.jsonl")
    out = list(agentgrep.iter_find_events(tmp_path, ("codex",), pattern=None, limit=None))
    types = [ev.type for ev in out]
    assert types[0] == "find_started"
    assert types[-1] == "find_finished"
    assert all(t == "find_record_emitted" for t in types[1:-1])
