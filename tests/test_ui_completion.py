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
from agentgrep.ui.completion import (
    FilterSuggester,
    QuerySuggester,
    apply_enum_choice,
    enum_value_candidates,
)


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


class EnumDropdownCase(t.NamedTuple):
    """One search input and the expected (field, candidates) for the dropdown."""

    test_id: str
    value: str
    expected: tuple[str, tuple[str, ...]] | None


ENUM_DROPDOWN_CASES: tuple[EnumDropdownCase, ...] = (
    EnumDropdownCase(
        test_id="empty-partial-lists-all-scope-values",
        value="scope:",
        expected=("scope", ("prompts", "conversations", "all")),
    ),
    EnumDropdownCase(
        test_id="partial-filters-agents",
        value="agent:cu",
        expected=("agent", ("cursor-cli", "cursor-ide")),
    ),
    EnumDropdownCase(
        test_id="trailing-token-after-term",
        value="ruff agent:co",
        expected=("agent", ("codex",)),
    ),
    EnumDropdownCase(
        test_id="non-enum-field-has-no-dropdown",
        value="model:gpt",
        expected=None,
    ),
    EnumDropdownCase(
        test_id="bare-token-has-no-dropdown",
        value="age",
        expected=None,
    ),
    EnumDropdownCase(
        test_id="no-enum-match-has-no-dropdown",
        value="agent:zzz",
        expected=None,
    ),
    EnumDropdownCase(
        test_id="fully-typed-single-value-has-no-dropdown",
        value="agent:codex",
        expected=None,
    ),
    EnumDropdownCase(
        test_id="partial-matching-multiple-still-shows",
        value="agent:cursor",
        expected=("agent", ("cursor-cli", "cursor-ide")),
    ),
)


@pytest.mark.parametrize(
    "case",
    ENUM_DROPDOWN_CASES,
    ids=[c.test_id for c in ENUM_DROPDOWN_CASES],
)
def test_enum_value_candidates(case: EnumDropdownCase) -> None:
    """The dropdown candidate function resolves enum field values."""
    assert enum_value_candidates(case.value, default_registry()) == case.expected


def test_apply_enum_choice_replaces_trailing_value() -> None:
    """Choosing a dropdown value rewrites the trailing field token."""
    assert apply_enum_choice("ruff agent:cu", "cursor-cli") == "ruff agent:cursor-cli"
    assert apply_enum_choice("scope:", "conversations") == "scope:conversations"
    assert apply_enum_choice("plain text", "x") == "plain text"
