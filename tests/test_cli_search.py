"""Tests for the ``agentgrep search`` subcommand.

Covers argument parsing into :class:`agentgrep.SearchArgs`, the
ranking-specific flags (``--threshold``, ``--no-group``, ``--no-rank``),
and the integration between the ranking engine and the CLI dispatch.
"""

from __future__ import annotations

import typing as t

import pytest

import agentgrep

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class SearchParseCase(t.NamedTuple):
    """Parametrized case for :func:`agentgrep.parse_args` on ``search``."""

    test_id: str
    argv: tuple[str, ...]
    expected_terms: tuple[str, ...]
    expected_threshold: int
    expected_no_group: bool
    expected_no_rank: bool
    expected_search_type: agentgrep.SearchType
    expected_any_term: bool
    expected_regex: bool
    expected_case_sensitive: bool


SEARCH_PARSE_CASES: tuple[SearchParseCase, ...] = (
    SearchParseCase(
        "defaults-single-term",
        ("search", "bliss"),
        ("bliss",),
        0,
        False,
        False,
        "prompts",
        False,
        False,
        False,
    ),
    SearchParseCase(
        "multi-term",
        ("search", "streaming", "parser"),
        ("streaming", "parser"),
        0,
        False,
        False,
        "prompts",
        False,
        False,
        False,
    ),
    SearchParseCase(
        "threshold-flag",
        ("search", "--threshold", "70", "migration"),
        ("migration",),
        70,
        False,
        False,
        "prompts",
        False,
        False,
        False,
    ),
    SearchParseCase(
        "no-group-flag",
        ("search", "--no-group", "caching"),
        ("caching",),
        0,
        True,
        False,
        "prompts",
        False,
        False,
        False,
    ),
    SearchParseCase(
        "no-rank-flag",
        ("search", "--no-rank", "bliss"),
        ("bliss",),
        0,
        False,
        True,
        "prompts",
        False,
        False,
        False,
    ),
    SearchParseCase(
        "all-ranking-flags",
        ("search", "--threshold", "50", "--no-group", "--no-rank", "query"),
        ("query",),
        50,
        True,
        True,
        "prompts",
        False,
        False,
        False,
    ),
    SearchParseCase(
        "type-history",
        ("search", "--type", "history", "todo"),
        ("todo",),
        0,
        False,
        False,
        "history",
        False,
        False,
        False,
    ),
    SearchParseCase(
        "any-term-mode",
        ("search", "--any", "foo", "bar"),
        ("foo", "bar"),
        0,
        False,
        False,
        "prompts",
        True,
        False,
        False,
    ),
    SearchParseCase(
        "regex-flag",
        ("search", "--regex", "foo.*bar"),
        ("foo.*bar",),
        0,
        False,
        False,
        "prompts",
        False,
        True,
        False,
    ),
    SearchParseCase(
        "case-sensitive-flag",
        ("search", "--case-sensitive", "Bliss"),
        ("Bliss",),
        0,
        False,
        False,
        "prompts",
        False,
        False,
        True,
    ),
    SearchParseCase(
        "no-terms",
        ("search",),
        (),
        0,
        False,
        False,
        "prompts",
        False,
        False,
        False,
    ),
)


@pytest.mark.parametrize(
    SearchParseCase._fields,
    SEARCH_PARSE_CASES,
    ids=[case.test_id for case in SEARCH_PARSE_CASES],
)
def test_search_parse_args(
    test_id: str,
    argv: tuple[str, ...],
    expected_terms: tuple[str, ...],
    expected_threshold: int,
    expected_no_group: bool,
    expected_no_rank: bool,
    expected_search_type: agentgrep.SearchType,
    expected_any_term: bool,
    expected_regex: bool,
    expected_case_sensitive: bool,
) -> None:
    """Search subparser captures ranking-specific flags correctly."""
    _ = test_id
    parsed = agentgrep.parse_args(argv)
    assert isinstance(parsed, agentgrep.SearchArgs)
    assert parsed.terms == expected_terms
    assert parsed.threshold == expected_threshold
    assert parsed.no_group == expected_no_group
    assert parsed.no_rank == expected_no_rank
    assert parsed.search_type == expected_search_type
    assert parsed.any_term == expected_any_term
    assert parsed.regex == expected_regex
    assert parsed.case_sensitive == expected_case_sensitive


def test_search_parse_limit() -> None:
    """--limit is captured in SearchArgs."""
    parsed = agentgrep.parse_args(("search", "--limit", "5", "bliss"))
    assert isinstance(parsed, agentgrep.SearchArgs)
    assert parsed.limit == 5


def test_search_parse_output_json() -> None:
    """--json sets output_mode correctly."""
    parsed = agentgrep.parse_args(("search", "--json", "bliss"))
    assert isinstance(parsed, agentgrep.SearchArgs)
    assert parsed.output_mode == "json"


def test_search_parse_output_ndjson() -> None:
    """--ndjson sets output_mode correctly."""
    parsed = agentgrep.parse_args(("search", "--ndjson", "bliss"))
    assert isinstance(parsed, agentgrep.SearchArgs)
    assert parsed.output_mode == "ndjson"


def test_search_parse_progress_never() -> None:
    """--no-progress sets progress_mode to never."""
    parsed = agentgrep.parse_args(("search", "--no-progress", "bliss"))
    assert isinstance(parsed, agentgrep.SearchArgs)
    assert parsed.progress_mode == "never"


def test_search_parse_agent_filter() -> None:
    """--agent filters are captured."""
    parsed = agentgrep.parse_args(("search", "--agent", "codex", "bliss"))
    assert isinstance(parsed, agentgrep.SearchArgs)
    assert parsed.agents == ("codex",)
