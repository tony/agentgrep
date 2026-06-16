"""Tests for the agentgrep query Pygments lexer.

:class:`~agentgrep.query.pygments_lexer.AgentgrepQueryLexer` classifies query
syntax into standard Pygments tokens (the docs theme colors them). It reuses
:func:`agentgrep.highlight_query_spans`, so its boundaries match the CLI and
TUI highlighters.
"""

from __future__ import annotations

import typing as t

import pytest
from pygments.token import (
    Keyword,
    Literal,
    Name,
    Operator,
    Punctuation,
    String,
    Text,
)

from agentgrep.query.pygments_lexer import AgentgrepQueryLexer

if t.TYPE_CHECKING:
    from pygments.token import _TokenType


def _tokens(query: str) -> set[tuple[_TokenType, str]]:
    """Return the ``(token_type, value)`` pairs the lexer emits for a query."""
    return set(AgentgrepQueryLexer().get_tokens(query))


def test_lexer_aliases() -> None:
    """The lexer registers under the docs code-block alias."""
    assert "agentgrep-query" in AgentgrepQueryLexer.aliases


class LexCase(t.NamedTuple):
    """One query and the (token, value) pairs it must classify."""

    test_id: str
    query: str
    expected: tuple[tuple[_TokenType, str], ...]


LEX_CASES: tuple[LexCase, ...] = (
    LexCase(
        test_id="field-colon-value",
        query="agent:codex",
        expected=((Name.Attribute, "agent"), (Punctuation, ":"), (Text, "codex")),
    ),
    LexCase(
        test_id="keywords",
        query="AND OR NOT TO",
        expected=((Keyword, "AND"), (Keyword, "OR"), (Keyword, "NOT"), (Keyword, "TO")),
    ),
    LexCase(
        test_id="wildcard",
        query="model:gpt*",
        expected=((Name.Attribute, "model"), (Operator, "*")),
    ),
    LexCase(
        test_id="phrase",
        query='"deploy v1"',
        expected=((String.Double, '"deploy v1"'),),
    ),
    LexCase(
        test_id="comparison-and-date",
        query="timestamp:>2026-01-01",
        expected=((Operator, ">"), (Literal.Date, "2026-01-01")),
    ),
    LexCase(
        test_id="negation",
        query="-agent:codex",
        expected=((Operator, "-"), (Name.Attribute, "agent")),
    ),
    LexCase(
        test_id="range-brackets",
        query="[2026-01 TO 2026-03]",
        expected=((Punctuation, "["), (Keyword, "TO"), (Punctuation, "]")),
    ),
)


@pytest.mark.parametrize("case", LEX_CASES, ids=[c.test_id for c in LEX_CASES])
def test_lexer_classifies_query(case: LexCase) -> None:
    """Each query construct maps to its expected standard Pygments token."""
    tokens = _tokens(case.query)
    for token_type, value in case.expected:
        assert (token_type, value) in tokens, f"missing {(token_type, value)} in {tokens}"


def test_lexer_covers_full_input() -> None:
    """The lexer emits contiguous tokens covering the whole query."""
    query = "agent:codex model:gpt* NOT deploy"
    rebuilt = "".join(
        value for _index, _ttype, value in AgentgrepQueryLexer().get_tokens_unprocessed(query)
    )
    assert rebuilt == query
