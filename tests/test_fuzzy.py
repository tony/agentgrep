"""Tests for the :mod:`agentgrep.fuzzy` wrapper around :mod:`rapidfuzz`.

Covers smart-case resolution, fuzzy scoring (algo v1 vs v2), the
fzf-style extended-search predicate, and the ranked iterator.
"""

from __future__ import annotations

import typing as t

import pytest

from agentgrep import fuzzy


class CaseResolutionCase(t.NamedTuple):
    """Parametrized case for :func:`fuzzy.resolve_case_sensitivity`."""

    test_id: str
    query: str
    mode: fuzzy.CaseSensitivity
    expected_case_sensitive: bool


CASE_RESOLUTION_CASES: tuple[CaseResolutionCase, ...] = (
    CaseResolutionCase("smart-lower-query-insensitive", "foo", "smart", False),
    CaseResolutionCase("smart-mixed-query-sensitive", "Foo", "smart", True),
    CaseResolutionCase("smart-all-caps-sensitive", "FOO", "smart", True),
    CaseResolutionCase("respect-forces-sensitive", "foo", "respect", True),
    CaseResolutionCase("ignore-forces-insensitive", "FOO", "ignore", False),
)


@pytest.mark.parametrize(
    "case",
    CASE_RESOLUTION_CASES,
    ids=[c.test_id for c in CASE_RESOLUTION_CASES],
)
def test_resolve_case_sensitivity(case: CaseResolutionCase) -> None:
    """Smart-case follows fzf's rule; respect/ignore are unconditional."""
    actual = fuzzy.resolve_case_sensitivity(case.query, case.mode)
    assert actual is case.expected_case_sensitive


class FuzzyScoreCase(t.NamedTuple):
    """Parametrized case for :func:`fuzzy.fuzzy_score`."""

    test_id: str
    query: str
    line: str
    case: fuzzy.CaseSensitivity
    algo: fuzzy.FuzzyAlgo
    expected_positive: bool


FUZZY_SCORE_CASES: tuple[FuzzyScoreCase, ...] = (
    FuzzyScoreCase("exact-substring-v2", "foo", "foobar", "smart", "v2", True),
    FuzzyScoreCase("exact-substring-v1", "foo", "foobar", "smart", "v1", True),
    FuzzyScoreCase("fuzzy-far-apart-v2", "abc", "axbxxxxxxxxxxxxxxxxxxc", "smart", "v2", True),
    FuzzyScoreCase("no-overlap-zero", "zzzzzz", "completely_disjoint", "smart", "v2", False),
    FuzzyScoreCase("smart-case-honors-uppercase", "FOO", "foobar", "smart", "v2", False),
    FuzzyScoreCase("ignore-case-matches-mixed", "FOO", "foobar", "ignore", "v2", True),
    FuzzyScoreCase("empty-query-zero", "", "anything", "smart", "v2", False),
)


@pytest.mark.parametrize(
    "case",
    FUZZY_SCORE_CASES,
    ids=[c.test_id for c in FUZZY_SCORE_CASES],
)
def test_fuzzy_score(case: FuzzyScoreCase) -> None:
    """Scoring honors algo, case mode, and returns 0 for empty/no-match."""
    score = fuzzy.fuzzy_score(case.query, case.line, case=case.case, algo=case.algo)
    if case.expected_positive:
        assert score > 0.0
    else:
        assert score == 0.0


class ExtendedMatchCase(t.NamedTuple):
    """Parametrized case for :func:`fuzzy.extended_match`."""

    test_id: str
    query: str
    line: str
    case: fuzzy.CaseSensitivity
    expected: bool


EXTENDED_MATCH_CASES: tuple[ExtendedMatchCase, ...] = (
    ExtendedMatchCase("single-positive-substring", "foo", "foobar", "ignore", True),
    ExtendedMatchCase("single-positive-miss", "foo", "barbaz", "ignore", False),
    ExtendedMatchCase("anchored-prefix-hit", "^foo", "foobar", "ignore", True),
    ExtendedMatchCase("anchored-prefix-miss", "^foo", "barfoo", "ignore", False),
    ExtendedMatchCase("anchored-suffix-hit", "bar$", "foobar", "ignore", True),
    ExtendedMatchCase("anchored-suffix-miss", "bar$", "barfoo", "ignore", False),
    ExtendedMatchCase("negation-excludes", "foo !bar", "foobar", "ignore", False),
    ExtendedMatchCase("negation-allows", "foo !bar", "foobaz", "ignore", True),
    ExtendedMatchCase("multi-positive-all-required", "foo baz", "foobaz", "ignore", True),
    ExtendedMatchCase("multi-positive-one-missing", "foo baz", "foobar", "ignore", False),
    ExtendedMatchCase("smart-case-uppercase-strict", "FOO", "foobar", "smart", False),
    ExtendedMatchCase("smart-case-lowercase-loose", "foo", "FOOBAR", "smart", True),
    ExtendedMatchCase("empty-query-everything-matches", "", "anything", "smart", True),
    ExtendedMatchCase("exact-quote-prefix-still-matches", "'foo", "foobar", "ignore", True),
)


@pytest.mark.parametrize(
    "case",
    EXTENDED_MATCH_CASES,
    ids=[c.test_id for c in EXTENDED_MATCH_CASES],
)
def test_extended_match(case: ExtendedMatchCase) -> None:
    """Extended-search syntax honors anchors, negation, and case mode."""
    actual = fuzzy.extended_match(case.query, case.line, case=case.case)
    assert actual is case.expected


class RankLinesCase(t.NamedTuple):
    """Parametrized case for :func:`fuzzy.rank_lines`."""

    test_id: str
    query: str
    lines: tuple[str, ...]
    case: fuzzy.CaseSensitivity
    sort: bool
    limit: int | None
    expected_lines: tuple[str, ...]


RANK_LINES_CASES: tuple[RankLinesCase, ...] = (
    RankLinesCase(
        "sorted-best-first",
        "ab",
        ("xyz", "ab123", "abxx", "noop"),
        "ignore",
        True,
        None,
        ("ab123", "abxx"),
    ),
    RankLinesCase(
        "limit-truncates",
        "ab",
        ("ab123", "abxx", "abyy", "abzz"),
        "ignore",
        True,
        2,
        ("ab123", "abxx"),
    ),
    RankLinesCase(
        "no-sort-preserves-order",
        "ab",
        ("abxx", "ab123", "abzz"),
        "ignore",
        False,
        None,
        ("abxx", "ab123", "abzz"),
    ),
    RankLinesCase(
        "no-matches-empty-output",
        "qqqq",
        ("foo", "bar", "baz"),
        "ignore",
        True,
        None,
        (),
    ),
    RankLinesCase(
        "extended-negation-filters",
        "foo !bar",
        ("foobar", "foobaz", "fooqux"),
        "ignore",
        True,
        None,
        ("foobaz", "fooqux"),
    ),
)


@pytest.mark.parametrize(
    "case",
    RANK_LINES_CASES,
    ids=[c.test_id for c in RANK_LINES_CASES],
)
def test_rank_lines(case: RankLinesCase) -> None:
    """Ranking honors sort/limit and the extended-search filter."""
    actual = tuple(
        line
        for line, _ in fuzzy.rank_lines(
            case.query,
            case.lines,
            case=case.case,
            sort=case.sort,
            limit=case.limit,
        )
    )
    if case.sort:
        # When sorted, order matters and ranking is by descending score.
        assert actual == case.expected_lines
    else:
        # Without sorting, only set equality matters — input order preserved.
        assert tuple(actual) == case.expected_lines
