"""Tests for the TUI-facing query-build helper.

Covers commit 4 of the stress-test fix project — the
:func:`agentgrep.query.build_query_from_input` helper that the
Textual search-input handler uses to translate user keystrokes
into a fresh :class:`agentgrep.SearchQuery`. Keeping the helper
pure makes it testable without a Textual ``Pilot``.

Convention: parametrize via :class:`typing.NamedTuple` with
``test_id`` as the first field, built with keyword arguments.
"""

from __future__ import annotations

import typing as t

import pytest

import agentgrep
from agentgrep.query import (
    QueryBuildResult,
    build_query_from_input,
    default_registry,
)


def _base_query() -> agentgrep.SearchQuery:
    """Build a synthetic base SearchQuery the helper inherits from."""
    return agentgrep.SearchQuery(
        terms=("placeholder",),
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agentgrep.AGENT_CHOICES,
        limit=None,
        dedupe=True,
    )


class BuildQueryFromInputCase(t.NamedTuple):
    """Parametrized case for :func:`build_query_from_input`."""

    test_id: str
    text: str
    expected_terms: tuple[str, ...]
    expected_compiled: bool
    expected_error_fragment: str | None


BUILD_QUERY_CASES: tuple[BuildQueryFromInputCase, ...] = (
    BuildQueryFromInputCase(
        test_id="empty-string",
        text="",
        expected_terms=(),
        expected_compiled=False,
        expected_error_fragment=None,
    ),
    BuildQueryFromInputCase(
        test_id="whitespace-only",
        text="   ",
        expected_terms=(),
        expected_compiled=False,
        expected_error_fragment=None,
    ),
    BuildQueryFromInputCase(
        test_id="bare-term",
        text="bliss",
        expected_terms=("bliss",),
        expected_compiled=False,
        expected_error_fragment=None,
    ),
    BuildQueryFromInputCase(
        test_id="two-bare-terms",
        text="bliss codex",
        expected_terms=("bliss", "codex"),
        expected_compiled=False,
        expected_error_fragment=None,
    ),
    BuildQueryFromInputCase(
        test_id="field-syntax-builds-compiled",
        text="agent:codex bliss",
        expected_terms=("bliss",),
        expected_compiled=True,
        expected_error_fragment=None,
    ),
    BuildQueryFromInputCase(
        test_id="or-composition-builds-compiled",
        text="(agent:codex OR agent:cursor-cli) AND deploy",
        expected_terms=("deploy",),
        expected_compiled=True,
        expected_error_fragment=None,
    ),
    BuildQueryFromInputCase(
        test_id="malformed-paren",
        text="(agent:codex",
        expected_terms=(),
        expected_compiled=False,
        expected_error_fragment="expected rparen",
    ),
    BuildQueryFromInputCase(
        test_id="invalid-enum-surfaces-error",
        text="agent:gpt4 bliss",
        expected_terms=(),
        expected_compiled=False,
        expected_error_fragment="invalid agent value 'gpt4'",
    ),
)


@pytest.mark.parametrize(
    "case",
    BUILD_QUERY_CASES,
    ids=[c.test_id for c in BUILD_QUERY_CASES],
)
def test_build_query_from_input_handles_every_shape(
    case: BuildQueryFromInputCase,
) -> None:
    """The TUI search-input helper covers empty / bare / field / malformed inputs."""
    result = build_query_from_input(case.text, _base_query(), default_registry())
    assert isinstance(result, QueryBuildResult)
    if case.expected_error_fragment is not None:
        assert result.query is None
        assert result.error is not None
        assert case.expected_error_fragment in result.error
    else:
        assert result.error is None
        assert result.query is not None
        assert result.query.terms == case.expected_terms
        if case.expected_compiled:
            assert result.query.compiled is not None
        else:
            assert result.query.compiled is None


def test_build_query_inherits_base_filter_scope() -> None:
    """The helper carries search_type / agents / limit through from base."""
    base = agentgrep.SearchQuery(
        terms=("placeholder",),
        search_type="conversations",
        any_term=True,
        regex=True,
        case_sensitive=True,
        agents=("codex",),
        limit=42,
        dedupe=False,
    )
    result = build_query_from_input("agent:codex bliss", base, default_registry())
    assert result.query is not None
    assert result.query.search_type == "conversations"
    assert result.query.any_term is True
    assert result.query.regex is True
    assert result.query.case_sensitive is True
    assert result.query.agents == ("codex",)
    assert result.query.limit == 42
    assert result.query.dedupe is False


def test_args_carry_raw_query_through() -> None:
    """GrepArgs / FindArgs all stash the original positionals."""
    grep_args = agentgrep.parse_args(["grep", "agent:codex", "bliss"])
    assert grep_args is not None
    assert isinstance(grep_args, agentgrep.GrepArgs)
    assert grep_args.raw_query == "agent:codex bliss"

    find_args = agentgrep.parse_args(["find", "agent:codex"])
    assert find_args is not None
    assert isinstance(find_args, agentgrep.FindArgs)
    assert find_args.raw_query == "agent:codex"


def test_bare_term_args_raw_query_matches_positionals() -> None:
    """Legacy bare-term invocations populate raw_query just like field-syntax."""
    args = agentgrep.parse_args(["grep", "bliss", "codex"])
    assert args is not None
    assert isinstance(args, agentgrep.GrepArgs)
    assert args.raw_query == "bliss codex"
