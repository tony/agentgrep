"""Tests for the TUI query-syntax highlighter.

:class:`~agentgrep.ui.highlighter.QueryHighlighter` styles the typed query in
the Textual search/filter inputs, reusing the same shared grammar
(:func:`agentgrep.highlight_query_spans`) as the CLI ``--help`` highlighter.
The highlighter is pure (no live app), so these tests style a Rich ``Text``
directly and assert the resulting spans.
"""

from __future__ import annotations

import typing as t

import pytest
from rich.text import Text

from agentgrep.ui.highlighter import QueryHighlighter


def _styled_spans(query: str, *, dark: bool = True) -> set[tuple[str, str]]:
    """Return ``{(token_text, style)}`` for a highlighted query."""
    text = Text(query)
    QueryHighlighter(dark=dark).highlight(text)
    return {(text.plain[span.start : span.end], str(span.style)) for span in text.spans}


class HighlightCase(t.NamedTuple):
    """One query and the (token, style) spans it must produce."""

    test_id: str
    query: str
    expected: tuple[tuple[str, str], ...]


HIGHLIGHT_CASES: tuple[HighlightCase, ...] = (
    HighlightCase(
        test_id="field-colon-value",
        query="agent:codex",
        expected=(("agent", "color(79)"), (":", "color(245)"), ("codex", "color(252)")),
    ),
    HighlightCase(
        test_id="wildcard",
        query="model:gpt*",
        expected=(("model", "color(79)"), ("gpt", "color(252)"), ("*", "bold color(222)")),
    ),
    HighlightCase(
        test_id="boolean-keyword",
        query="ruff OR uv",
        expected=(("OR", "bold color(215)"), ("ruff", "color(252)"), ("uv", "color(252)")),
    ),
    HighlightCase(
        test_id="comparison-operator",
        query="timestamp:>2026-01-01",
        expected=(
            ("timestamp", "color(79)"),
            (">", "color(215)"),
            ("2026-01-01", "color(252)"),
        ),
    ),
    HighlightCase(
        test_id="negation-sigil",
        query="-agent:codex",
        expected=(("-", "bold color(204)"), ("agent", "color(79)")),
    ),
    HighlightCase(
        test_id="phrase",
        query='"exact phrase"',
        expected=(('"', "color(245)"), ("exact phrase", "color(252)")),
    ),
)


@pytest.mark.parametrize("case", HIGHLIGHT_CASES, ids=[c.test_id for c in HIGHLIGHT_CASES])
def test_query_highlighter_styles(case: HighlightCase) -> None:
    """The highlighter styles each query construct with its Design-A hue."""
    spans = _styled_spans(case.query)
    for token, style in case.expected:
        assert (token, style) in spans, f"missing {(token, style)} in {spans}"


def test_query_highlighter_leaves_plain_terms_low_key() -> None:
    """Bare search terms get the near-foreground value hue, not a loud color."""
    spans = _styled_spans("streaming parser")
    assert ("streaming", "color(252)") in spans
    assert ("parser", "color(252)") in spans


def test_query_highlighter_empty_is_noop() -> None:
    """An empty query produces no styling spans."""
    text = Text("")
    QueryHighlighter().highlight(text)
    assert not text.spans


def test_query_highlighter_light_palette_uses_readable_semantic_hues() -> None:
    """The light palette preserves syntax roles with dark foregrounds."""
    spans = _styled_spans(
        '-agent:codex OR model:gpt* timestamp:>2026-01-01 "exact phrase"',
        dark=False,
    )
    assert {
        ("-", "bold #9b2242"),
        ("agent", "#006b75"),
        (":", "#5c5c5c"),
        ("codex", "#343434"),
        ("OR", "bold #8a4b00"),
        ("*", "bold #765f00"),
        (">", "#8a4b00"),
        ("exact phrase", "#343434"),
    }.issubset(spans)
