"""Unit tests for the compiled-query text/wildcard matchers.

These pin the user-visible string semantics the ``_string_match`` and
``_is_wildcard`` docstrings describe: anchored glob vs. substring, casefold
vs. case-sensitive, the ``?`` single-char wildcard, and the rule that a
``[...]`` class stays literal so ``model:gpt[4]`` is not reinterpreted as a
glob. The multi-surface ``_text_matches`` path (text/title/role/model/path)
is exercised branch by branch.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep.query.textmatch import _is_wildcard, _string_match, _text_matches


class StringMatchCase(t.NamedTuple):
    """One (haystack, needle, case_sensitive) -> expected match."""

    test_id: str
    haystack: str
    needle: str
    case_sensitive: bool
    expected: bool


STRING_MATCH_CASES: tuple[StringMatchCase, ...] = (
    StringMatchCase(
        test_id="plain-substring-insensitive",
        haystack="Claude Sonnet",
        needle="sonnet",
        case_sensitive=False,
        expected=True,
    ),
    StringMatchCase(
        test_id="plain-substring-sensitive-miss",
        haystack="Claude Sonnet",
        needle="sonnet",
        case_sensitive=True,
        expected=False,
    ),
    StringMatchCase(
        test_id="plain-substring-sensitive-hit",
        haystack="Claude Sonnet",
        needle="Sonnet",
        case_sensitive=True,
        expected=True,
    ),
    StringMatchCase(
        test_id="star-is-anchored-prefix",
        haystack="gpt-5",
        needle="gpt*",
        case_sensitive=False,
        expected=True,
    ),
    StringMatchCase(
        test_id="star-anchored-not-substring",
        haystack="my-gpt-5",
        needle="gpt*",
        case_sensitive=False,
        expected=False,
    ),
    StringMatchCase(
        test_id="double-star-is-substring",
        haystack="my-gpt-5",
        needle="*gpt*",
        case_sensitive=False,
        expected=True,
    ),
    StringMatchCase(
        test_id="question-matches-single-char",
        haystack="gpt5",
        needle="gpt?",
        case_sensitive=False,
        expected=True,
    ),
    StringMatchCase(
        test_id="question-needs-exactly-one-char",
        haystack="gpt55",
        needle="gpt?",
        case_sensitive=False,
        expected=False,
    ),
    StringMatchCase(
        test_id="bracket-class-stays-literal",
        haystack="gpt[4]",
        needle="gpt[4]",
        case_sensitive=False,
        expected=True,
    ),
    StringMatchCase(
        test_id="bracket-class-not-a-glob-class",
        haystack="gpt4",
        needle="gpt[4]",
        case_sensitive=False,
        expected=False,
    ),
    StringMatchCase(
        test_id="wildcard-casefolds-by-default",
        haystack="GPT-5",
        needle="gpt*",
        case_sensitive=False,
        expected=True,
    ),
    StringMatchCase(
        test_id="wildcard-case-sensitive-miss",
        haystack="GPT-5",
        needle="gpt*",
        case_sensitive=True,
        expected=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    STRING_MATCH_CASES,
    ids=[c.test_id for c in STRING_MATCH_CASES],
)
def test_string_match(case: StringMatchCase) -> None:
    """String matching honours anchored-glob, substring, case, and literals."""
    assert (
        _string_match(case.haystack, case.needle, case_sensitive=case.case_sensitive)
        is case.expected
    )


class WildcardCase(t.NamedTuple):
    """Whether a value carries a glob wildcard."""

    test_id: str
    value: str
    expected: bool


WILDCARD_CASES: tuple[WildcardCase, ...] = (
    WildcardCase(test_id="star-is-wildcard", value="gpt*", expected=True),
    WildcardCase(test_id="question-is-wildcard", value="gpt?", expected=True),
    WildcardCase(test_id="bracket-is-not-wildcard", value="gpt[4]", expected=False),
    WildcardCase(test_id="plain-is-not-wildcard", value="gpt-4", expected=False),
)


@pytest.mark.parametrize("case", WILDCARD_CASES, ids=[c.test_id for c in WILDCARD_CASES])
def test_is_wildcard(case: WildcardCase) -> None:
    """Only ``*`` and ``?`` mark a value as a wildcard; ``[...]`` stays literal."""
    assert _is_wildcard(case.value) is case.expected


def _record(
    *,
    text: str = "",
    title: str | None = None,
    role: str | None = None,
    model: str | None = None,
    path: str = "/tmp/codex/sessions/abc.jsonl",
) -> agentgrep.SearchRecord:
    """Build a record exercising one text surface at a time."""
    return agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path(path),
        text=text,
        title=title,
        role=role,
        timestamp=None,
        model=model,
        session_id=None,
        conversation_id=None,
        metadata={},
    )


class TextMatchCase(t.NamedTuple):
    """A needle checked against one populated record surface."""

    test_id: str
    record: agentgrep.SearchRecord
    needle: str
    expected: bool


TEXT_MATCH_CASES: tuple[TextMatchCase, ...] = (
    TextMatchCase(
        test_id="matches-in-text",
        record=_record(text="bliss and serenity"),
        needle="serenity",
        expected=True,
    ),
    TextMatchCase(
        test_id="matches-in-title",
        record=_record(text="body", title="Serene Session"),
        needle="serene",
        expected=True,
    ),
    TextMatchCase(
        test_id="matches-in-role",
        record=_record(text="body", role="assistant"),
        needle="assistant",
        expected=True,
    ),
    TextMatchCase(
        test_id="matches-in-model",
        record=_record(text="body", model="claude-sonnet"),
        needle="sonnet",
        expected=True,
    ),
    TextMatchCase(
        test_id="matches-in-path",
        record=_record(text="body", path="/tmp/codex/needle.jsonl"),
        needle="needle",
        expected=True,
    ),
    TextMatchCase(
        test_id="no-surface-matches",
        record=_record(text="body", title="t", role="user", model="m"),
        needle="absent",
        expected=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    TEXT_MATCH_CASES,
    ids=[c.test_id for c in TEXT_MATCH_CASES],
)
def test_text_matches_each_surface(case: TextMatchCase) -> None:
    """A bare term matches across text, title, role, model, and path."""
    assert _text_matches(case.record, case.needle) is case.expected


def test_text_matches_respects_case_sensitivity() -> None:
    """Case-sensitive matching skips a differently-cased title surface."""
    record = _record(text="body", title="Serene")
    assert _text_matches(record, "serene") is True
    assert _text_matches(record, "serene", case_sensitive=True) is False
