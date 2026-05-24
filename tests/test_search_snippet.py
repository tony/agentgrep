"""Tests for search snippet extraction and match highlighting.

Style conventions: ``t.NamedTuple`` + ``test_id`` parametrize cases.
"""

from __future__ import annotations

import re
import typing as t

import pytest

import agentgrep
from agentgrep.cli.render import (
    extract_search_snippet,
    highlight_search_spans,
)

# ---------------------------------------------------------------------------
# extract_search_snippet
# ---------------------------------------------------------------------------


class SnippetCase(t.NamedTuple):
    """Parametrized case for snippet extraction."""

    test_id: str
    text: str
    pattern: str
    max_lines: int
    expected_snippet: str
    expected_remaining: int


_TEN_LINES = "\n".join(f"line {i}" for i in range(1, 11))

_SNIPPET_CASES: tuple[SnippetCase, ...] = (
    SnippetCase(
        test_id="match-at-start",
        text=_TEN_LINES,
        pattern="line 1",
        max_lines=5,
        expected_snippet="\n".join(f"line {i}" for i in range(1, 6)),
        expected_remaining=5,
    ),
    SnippetCase(
        test_id="match-in-middle",
        text=_TEN_LINES,
        pattern="line 5",
        max_lines=5,
        expected_snippet="\n".join(f"line {i}" for i in range(4, 9)),
        expected_remaining=5,
    ),
    SnippetCase(
        test_id="match-at-end",
        text=_TEN_LINES,
        pattern="line 10",
        max_lines=5,
        expected_snippet="\n".join(f"line {i}" for i in range(6, 11)),
        expected_remaining=5,
    ),
    SnippetCase(
        test_id="no-match-fallback",
        text=_TEN_LINES,
        pattern="nonexistent",
        max_lines=5,
        expected_snippet="\n".join(f"line {i}" for i in range(1, 6)),
        expected_remaining=5,
    ),
    SnippetCase(
        test_id="empty-text",
        text="",
        pattern="anything",
        max_lines=5,
        expected_snippet="",
        expected_remaining=0,
    ),
    SnippetCase(
        test_id="single-line-text",
        text="only line",
        pattern="only",
        max_lines=5,
        expected_snippet="only line",
        expected_remaining=0,
    ),
    SnippetCase(
        test_id="text-shorter-than-max",
        text="line 1\nline 2\nline 3",
        pattern="line 2",
        max_lines=5,
        expected_snippet="line 1\nline 2\nline 3",
        expected_remaining=0,
    ),
    SnippetCase(
        test_id="multi-pattern-first-wins",
        text=_TEN_LINES,
        pattern="line 3",
        max_lines=5,
        expected_snippet="\n".join(f"line {i}" for i in range(2, 7)),
        expected_remaining=5,
    ),
)


@pytest.mark.parametrize("case", _SNIPPET_CASES, ids=[c.test_id for c in _SNIPPET_CASES])
def test_extract_search_snippet(case: SnippetCase) -> None:
    """extract_search_snippet returns the expected window and remaining count."""
    patterns = [re.compile(re.escape(case.pattern))]
    snippet, remaining = extract_search_snippet(
        case.text,
        patterns,
        max_lines=case.max_lines,
    )
    assert snippet == case.expected_snippet
    assert remaining == case.expected_remaining


def test_extract_snippet_empty_patterns_takes_first_n() -> None:
    """With no patterns, falls back to the first max_lines lines."""
    snippet, remaining = extract_search_snippet(_TEN_LINES, [], max_lines=3)
    assert snippet == "\n".join(f"line {i}" for i in range(1, 4))
    assert remaining == 7


# ---------------------------------------------------------------------------
# highlight_search_spans
# ---------------------------------------------------------------------------


class HighlightCase(t.NamedTuple):
    """Parametrized case for span highlighting."""

    test_id: str
    text: str
    pattern: str
    flags: int
    expected_contains: str


_COLORS = agentgrep.AnsiColors(enabled=True)
_ACCENT = agentgrep.AnsiColors.ACCENT
_RESET = agentgrep.AnsiColors.RESET

_HIGHLIGHT_CASES: tuple[HighlightCase, ...] = (
    HighlightCase(
        test_id="single-match",
        text="find the needle here",
        pattern="needle",
        flags=0,
        expected_contains=f"{_ACCENT}needle{_RESET}",
    ),
    HighlightCase(
        test_id="multiple-matches-same-line",
        text="foo bar foo",
        pattern="foo",
        flags=0,
        expected_contains=f"{_ACCENT}foo{_RESET} bar {_ACCENT}foo{_RESET}",
    ),
    HighlightCase(
        test_id="no-match-unchanged",
        text="nothing to highlight",
        pattern="missing",
        flags=0,
        expected_contains="nothing to highlight",
    ),
    HighlightCase(
        test_id="case-insensitive",
        text="Find the Needle",
        pattern="needle",
        flags=re.IGNORECASE,
        expected_contains=f"{_ACCENT}Needle{_RESET}",
    ),
)


@pytest.mark.parametrize("case", _HIGHLIGHT_CASES, ids=[c.test_id for c in _HIGHLIGHT_CASES])
def test_highlight_search_spans(case: HighlightCase) -> None:
    """highlight_search_spans wraps matches in accent color."""
    patterns = [re.compile(re.escape(case.pattern), case.flags)]
    result = highlight_search_spans(case.text, patterns, colors=_COLORS)
    assert case.expected_contains in result


def test_highlight_empty_text() -> None:
    """Empty text returns empty string."""
    result = highlight_search_spans("", [re.compile("x")], colors=_COLORS)
    assert result == ""


def test_highlight_no_patterns() -> None:
    """No patterns returns text unchanged."""
    result = highlight_search_spans("hello world", [], colors=_COLORS)
    assert result == "hello world"


def test_highlight_disabled_colors() -> None:
    """Disabled colors produce plain text without ANSI codes."""
    no_colors = agentgrep.AnsiColors(enabled=False)
    patterns = [re.compile(re.escape("world"))]
    result = highlight_search_spans("hello world", patterns, colors=no_colors)
    assert result == "hello world"


def test_highlight_multiline() -> None:
    """Highlighting works across multiple lines."""
    text = "line one foo\nline two bar\nline three foo"
    patterns = [re.compile(re.escape("foo"))]
    result = highlight_search_spans(text, patterns, colors=_COLORS)
    lines = result.split("\n")
    assert _ACCENT in lines[0]
    assert _ACCENT not in lines[1]
    assert _ACCENT in lines[2]
