"""Tests for the ranking engine (``agentgrep.ranking``).

Covers the three-stage pipeline: rapidfuzz scoring, near-duplicate
collapsing, and session grouping.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep.ranking import (
    collapse_near_duplicates,
    group_by_session,
    rank_search_records,
)


def _record(
    text: str,
    *,
    session_id: str | None = None,
    agent: agentgrep.AgentName = "codex",
) -> agentgrep.SearchRecord:
    """Build a minimal SearchRecord for ranking tests."""
    return agentgrep.SearchRecord(
        kind="prompt",
        agent=agent,
        store="test",
        adapter_id="test.v1",
        path=pathlib.Path("/tmp/test"),
        text=text,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# rank_search_records
# ---------------------------------------------------------------------------


class RankCase(t.NamedTuple):
    """Parametrized case for :func:`rank_search_records`."""

    test_id: str
    texts: list[str]
    query: str
    threshold: int
    expected_first_text: str | None
    expected_min_count: int


RANK_CASES: tuple[RankCase, ...] = (
    RankCase(
        "higher-match-scores-first",
        ["unrelated noise", "the streaming parser is fast", "streaming"],
        "streaming",
        0,
        "streaming",
        3,
    ),
    RankCase(
        "threshold-filters-low",
        ["unrelated noise", "streaming parser"],
        "streaming",
        80,
        "streaming parser",
        1,
    ),
    RankCase(
        "empty-input",
        [],
        "anything",
        0,
        None,
        0,
    ),
)


@pytest.mark.parametrize(
    RankCase._fields,
    RANK_CASES,
    ids=[case.test_id for case in RANK_CASES],
)
def test_rank_search_records(
    test_id: str,
    texts: list[str],
    query: str,
    threshold: int,
    expected_first_text: str | None,
    expected_min_count: int,
) -> None:
    """rank_search_records scores, filters, and sorts correctly."""
    _ = test_id
    records = [_record(text) for text in texts]
    result = rank_search_records(records, query, threshold=threshold)
    assert len(result) >= expected_min_count
    if expected_first_text is not None:
        assert result[0][0].text == expected_first_text


def test_rank_scores_are_descending() -> None:
    """Scores are in non-increasing order."""
    records = [
        _record("unrelated noise here"),
        _record("the streaming parser approach"),
        _record("streaming"),
        _record("fully streaming parser engine"),
    ]
    result = rank_search_records(records, "streaming parser")
    scores = [score for _, score in result]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# collapse_near_duplicates
# ---------------------------------------------------------------------------


class CollapseCase(t.NamedTuple):
    """Parametrized case for :func:`collapse_near_duplicates`."""

    test_id: str
    texts: list[str]
    expected_count: int
    expected_any_similar: bool


COLLAPSE_CASES: tuple[CollapseCase, ...] = (
    CollapseCase(
        "identical-texts-collapse",
        ["hello world", "hello world", "hello world"],
        1,
        True,
    ),
    CollapseCase(
        "different-texts-stay",
        ["apple pie recipe", "quantum mechanics lecture", "jazz improvisation"],
        3,
        False,
    ),
    CollapseCase(
        "empty-input",
        [],
        0,
        False,
    ),
    CollapseCase(
        "near-identical-collapse",
        ["hello world today", "hello world today!"],
        1,
        True,
    ),
)


@pytest.mark.parametrize(
    CollapseCase._fields,
    COLLAPSE_CASES,
    ids=[case.test_id for case in COLLAPSE_CASES],
)
def test_collapse_near_duplicates(
    test_id: str,
    texts: list[str],
    expected_count: int,
    expected_any_similar: bool,
) -> None:
    """Near-duplicate collapsing produces expected representative count."""
    _ = test_id
    scored = [(r, 50.0) for r in (_record(text) for text in texts)]
    result = collapse_near_duplicates(scored)
    assert len(result) == expected_count
    if expected_any_similar:
        assert any(similar > 0 for _, _, similar in result)
    elif result:
        assert all(similar == 0 for _, _, similar in result)


def test_collapse_preserves_score_order() -> None:
    """Collapsed output preserves the pre-sorted score order."""
    scored: list[tuple[agentgrep.SearchRecord, float]] = [
        (_record("best match"), 95.0),
        (_record("good match"), 80.0),
        (_record("okay match"), 60.0),
    ]
    result = collapse_near_duplicates(scored)
    result_scores = [score for _, score, _ in result]
    assert result_scores == sorted(result_scores, reverse=True)


# ---------------------------------------------------------------------------
# group_by_session
# ---------------------------------------------------------------------------


class GroupCase(t.NamedTuple):
    """Parametrized case for :func:`group_by_session`."""

    test_id: str
    session_ids: list[str | None]
    expected_group_count: int
    expected_keys: list[str | None]


GROUP_CASES: tuple[GroupCase, ...] = (
    GroupCase(
        "groups-by-session",
        ["sess-a", "sess-a", "sess-b", "sess-b"],
        2,
        ["sess-a", "sess-b"],
    ),
    GroupCase(
        "none-sessions-grouped-together",
        [None, None, "sess-a"],
        2,
        [None, "sess-a"],
    ),
    GroupCase(
        "preserves-first-seen-order",
        ["sess-b", "sess-a", "sess-b"],
        2,
        ["sess-b", "sess-a"],
    ),
    GroupCase(
        "empty-input",
        [],
        0,
        [],
    ),
)


@pytest.mark.parametrize(
    GroupCase._fields,
    GROUP_CASES,
    ids=[case.test_id for case in GROUP_CASES],
)
def test_group_by_session(
    test_id: str,
    session_ids: list[str | None],
    expected_group_count: int,
    expected_keys: list[str | None],
) -> None:
    """Session grouping produces expected buckets."""
    _ = test_id
    records: list[tuple[agentgrep.SearchRecord, float, int]] = [
        (_record(f"text-{i}", session_id=sid), 50.0, 0) for i, sid in enumerate(session_ids)
    ]
    result = group_by_session(records)
    assert len(result) == expected_group_count
    assert [key for key, _ in result] == expected_keys


def test_group_preserves_within_group_order() -> None:
    """Records within a group keep score-descending order."""
    records: list[tuple[agentgrep.SearchRecord, float, int]] = [
        (_record("first", session_id="s1"), 95.0, 0),
        (_record("second", session_id="s1"), 80.0, 0),
        (_record("third", session_id="s1"), 60.0, 0),
    ]
    result = group_by_session(records)
    assert len(result) == 1
    _, entries = result[0]
    entry_scores = [score for _, score, _ in entries]
    assert entry_scores == [95.0, 80.0, 60.0]
