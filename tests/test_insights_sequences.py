"""Tests for the PrefixSpan workflow miner."""

from __future__ import annotations

from agentgrep.insights.sequences import collapse_runs, maximal, prefixspan


def test_collapse_runs_removes_consecutive_duplicates() -> None:
    """Consecutive identical symbols collapse to a single transition step."""
    assert collapse_runs([1, 1, 2, 2, 2, 1, 1]) == (1, 2, 1)
    assert collapse_runs([]) == ()
    assert collapse_runs([5]) == (5,)


def test_prefixspan_finds_recurring_chain() -> None:
    """A chain shared by enough sequences is mined with its support."""
    sequences = [[1, 2, 3], [1, 2, 3], [1, 2, 4], [9]]
    found = {fs.pattern: fs.support for fs in prefixspan(sequences, min_support=2, min_length=2)}
    assert found[(1, 2)] == 3
    assert found[(1, 2, 3)] == 2
    assert (9,) not in found  # below min_length


def test_prefixspan_respects_min_support() -> None:
    """Patterns below the support threshold are not reported."""
    sequences = [[1, 2], [3, 4]]
    assert prefixspan(sequences, min_support=2, min_length=2) == []


def test_prefixspan_order_matters() -> None:
    """Subsequences are ordered: 2 before 1 is distinct from 1 before 2."""
    sequences = [[1, 2], [1, 2], [2, 1]]
    found = {fs.pattern: fs.support for fs in prefixspan(sequences, min_support=2, min_length=2)}
    assert found == {(1, 2): 2}


def test_maximal_drops_subsumed_prefixes() -> None:
    """A shorter pattern of equal support is dropped for the longer chain."""
    sequences = [[1, 2, 3], [1, 2, 3]]
    kept = {fs.pattern for fs in maximal(prefixspan(sequences, min_support=2, min_length=2))}
    assert (1, 2, 3) in kept
    assert (1, 2) not in kept  # subsumed by (1, 2, 3) at the same support
