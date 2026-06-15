"""Tests for the TUI inline-completion suggesters.

The suggesters drive Textual's inline ghost-text completion for the
search bar (field/enum aware) and the filter box (loaded-result terms).
They are pure and async, so the tests await ``get_suggestion`` directly
without a live Textual app.
"""

from __future__ import annotations

import typing as t

import pytest

from agentgrep.query import default_registry
from agentgrep.ui.completion import FilterSuggester, QuerySuggester


class QueryCase(t.NamedTuple):
    """One search-bar completion input and its expected suggestion."""

    test_id: str
    value: str
    expected: str | None


QUERY_CASES: tuple[QueryCase, ...] = (
    QueryCase(test_id="enum-value-codex", value="agent:co", expected="agent:codex"),
    QueryCase(test_id="enum-value-scope", value="scope:pr", expected="scope:prompts"),
    QueryCase(test_id="field-name-from-prefix", value="age", expected="agent:"),
    QueryCase(test_id="field-name-after-term", value="ruff age", expected="ruff agent:"),
    QueryCase(test_id="alias-field-name", value="dat", expected="date:"),
    QueryCase(test_id="empty-suggests-nothing", value="", expected=None),
    QueryCase(test_id="unknown-prefix-suggests-nothing", value="zzz", expected=None),
    QueryCase(
        test_id="already-complete-enum-suggests-nothing",
        value="agent:codex",
        expected=None,
    ),
    QueryCase(
        test_id="non-enum-field-value-suggests-nothing",
        value="model:gpt",
        expected=None,
    ),
)


@pytest.mark.parametrize("case", QUERY_CASES, ids=[c.test_id for c in QUERY_CASES])
async def test_query_suggester(case: QueryCase) -> None:
    """The query suggester completes field names and enum values."""
    suggester = QuerySuggester(default_registry())
    result = await suggester.get_suggestion(case.value)
    assert result == case.expected


class FilterCase(t.NamedTuple):
    """One filter-box completion input and its expected suggestion."""

    test_id: str
    value: str
    expected: str | None


FILTER_CASES: tuple[FilterCase, ...] = (
    FilterCase(test_id="completes-from-vocabulary", value="ru", expected="ruff"),
    FilterCase(test_id="completes-trailing-token", value="uv ru", expected="uv ruff"),
    FilterCase(test_id="no-vocabulary-match", value="zzz", expected=None),
    FilterCase(test_id="empty-suggests-nothing", value="", expected=None),
)


@pytest.mark.parametrize("case", FILTER_CASES, ids=[c.test_id for c in FILTER_CASES])
async def test_filter_suggester(case: FilterCase) -> None:
    """The filter suggester completes the trailing token from its vocabulary."""
    suggester = FilterSuggester(["ruff", "rust", "tmux", "uv"])
    result = await suggester.get_suggestion(case.value)
    assert result == case.expected


async def test_filter_suggester_vocabulary_is_updatable() -> None:
    """The filter vocabulary can be refreshed as records stream in."""
    suggester = FilterSuggester([])
    assert await suggester.get_suggestion("ali") is None
    suggester.set_vocabulary(["alignment", "alpha"])
    assert await suggester.get_suggestion("ali") == "alignment"
