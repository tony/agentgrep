"""Tests for the agentgrep query language tokenizer + parser.

This file covers commit 1 of the query-language project — the lexer
in :mod:`agentgrep.query.parser`. The recursive-descent parser and
field registry land in follow-up commits and grow their own
``ParserCase`` / ``ParserErrorCase`` blocks below the tokenizer
cases.

Convention: parametrize via :class:`typing.NamedTuple` with
``test_id`` as the first field, constructed with keyword arguments so
new fields don't break existing rows.
"""

from __future__ import annotations

import typing as t

import pytest

from agentgrep.query import (
    AndNode,
    FieldCmpNode,
    FieldEqNode,
    FieldRangeNode,
    NotNode,
    OrNode,
    QueryNode,
    TermNode,
    Token,
    default_registry,
    parse_query,
    tokenize,
)
from agentgrep.query.parser import QueryParseError


class TokenizerCase(t.NamedTuple):
    """Parametrized case for :func:`agentgrep.query.parser.tokenize`."""

    test_id: str
    query: str
    expected_kinds: tuple[str, ...]
    expected_values: tuple[str, ...]


TOKENIZER_CASES: tuple[TokenizerCase, ...] = (
    TokenizerCase(
        test_id="bare-term",
        query="bliss",
        expected_kinds=("term", "eof"),
        expected_values=("bliss", ""),
    ),
    TokenizerCase(
        test_id="two-bare-terms-implicit-and",
        query="bliss codex",
        expected_kinds=("term", "term", "eof"),
        expected_values=("bliss", "codex", ""),
    ),
    TokenizerCase(
        test_id="field-eq-inline-value",
        query="agent:codex",
        expected_kinds=("ident", "colon", "term", "eof"),
        expected_values=("agent", ":", "codex", ""),
    ),
    TokenizerCase(
        test_id="field-eq-with-path-value",
        query="path:~/.codex",
        expected_kinds=("ident", "colon", "term", "eof"),
        expected_values=("path", ":", "~/.codex", ""),
    ),
    TokenizerCase(
        test_id="field-comparison-gt",
        query="timestamp:>2025-01-01",
        expected_kinds=("ident", "colon", "gt", "term", "eof"),
        expected_values=("timestamp", ":", ">", "2025-01-01", ""),
    ),
    TokenizerCase(
        test_id="field-comparison-lte",
        query="timestamp:<=2026-05-22",
        expected_kinds=("ident", "colon", "lte", "term", "eof"),
        expected_values=("timestamp", ":", "<=", "2026-05-22", ""),
    ),
    TokenizerCase(
        test_id="field-inclusive-range",
        query="timestamp:[2025-01-01 TO 2025-12-31]",
        expected_kinds=(
            "ident",
            "colon",
            "lbracket",
            "term",
            "to",
            "term",
            "rbracket",
            "eof",
        ),
        expected_values=(
            "timestamp",
            ":",
            "[",
            "2025-01-01",
            "TO",
            "2025-12-31",
            "]",
            "",
        ),
    ),
    TokenizerCase(
        test_id="field-exclusive-range",
        query="mtime:{2025-01 TO 2026-01}",
        expected_kinds=(
            "ident",
            "colon",
            "lbrace",
            "term",
            "to",
            "term",
            "rbrace",
            "eof",
        ),
        expected_values=(
            "mtime",
            ":",
            "{",
            "2025-01",
            "TO",
            "2026-01",
            "}",
            "",
        ),
    ),
    TokenizerCase(
        test_id="negation-sigil",
        query="-agent:claude",
        expected_kinds=("minus", "ident", "colon", "term", "eof"),
        expected_values=("-", "agent", ":", "claude", ""),
    ),
    TokenizerCase(
        test_id="negation-keyword",
        query="NOT agent:claude",
        expected_kinds=("not", "ident", "colon", "term", "eof"),
        expected_values=("NOT", "agent", ":", "claude", ""),
    ),
    TokenizerCase(
        test_id="hyphen-inside-word-is-not-negation",
        query="codex-test bliss",
        expected_kinds=("term", "term", "eof"),
        expected_values=("codex-test", "bliss", ""),
    ),
    TokenizerCase(
        test_id="boolean-and-or",
        query="agent:codex AND bliss OR deploy",
        expected_kinds=(
            "ident",
            "colon",
            "term",
            "and",
            "term",
            "or",
            "term",
            "eof",
        ),
        expected_values=(
            "agent",
            ":",
            "codex",
            "AND",
            "bliss",
            "OR",
            "deploy",
            "",
        ),
    ),
    TokenizerCase(
        test_id="grouped-or",
        query="(agent:codex OR agent:cursor) AND bliss",
        expected_kinds=(
            "lparen",
            "ident",
            "colon",
            "term",
            "or",
            "ident",
            "colon",
            "term",
            "rparen",
            "and",
            "term",
            "eof",
        ),
        expected_values=(
            "(",
            "agent",
            ":",
            "codex",
            "OR",
            "agent",
            ":",
            "cursor",
            ")",
            "AND",
            "bliss",
            "",
        ),
    ),
    TokenizerCase(
        test_id="quoted-string-with-spaces",
        query='"deploy v1.2.3"',
        expected_kinds=("term", "eof"),
        expected_values=("deploy v1.2.3", ""),
    ),
    TokenizerCase(
        test_id="quoted-string-with-escape",
        query=r'"deploy \"v1\""',
        expected_kinds=("term", "eof"),
        expected_values=('deploy "v1"', ""),
    ),
    TokenizerCase(
        test_id="single-quoted-string",
        query="'agent:codex'",
        expected_kinds=("term", "eof"),
        expected_values=("agent:codex", ""),
    ),
    TokenizerCase(
        test_id="whitespace-collapsed",
        query="  bliss   codex  ",
        expected_kinds=("term", "term", "eof"),
        expected_values=("bliss", "codex", ""),
    ),
    TokenizerCase(
        test_id="empty-query",
        query="",
        expected_kinds=("eof",),
        expected_values=("",),
    ),
    TokenizerCase(
        test_id="unicode-term",
        query="こんにちは",
        expected_kinds=("term", "eof"),
        expected_values=("こんにちは", ""),
    ),
    TokenizerCase(
        test_id="field-with-empty-inline-then-value",
        query="agent: codex",
        expected_kinds=("ident", "colon", "term", "eof"),
        expected_values=("agent", ":", "codex", ""),
    ),
    TokenizerCase(
        test_id="plus-sigil",
        query="+bliss -claude",
        expected_kinds=("plus", "term", "minus", "term", "eof"),
        expected_values=("+", "bliss", "-", "claude", ""),
    ),
    TokenizerCase(
        test_id="lowercase-keywords-are-terms",
        query="bliss and codex",
        expected_kinds=("term", "and", "term", "eof"),
        expected_values=("bliss", "AND", "codex", ""),
    ),
    TokenizerCase(
        test_id="timestamp-with-positive-tz-offset",
        query="timestamp:2026-05-22T14:30:00+05:30",
        expected_kinds=("ident", "colon", "term", "eof"),
        expected_values=("timestamp", ":", "2026-05-22T14:30:00+05:30", ""),
    ),
)


@pytest.mark.parametrize(
    "case",
    TOKENIZER_CASES,
    ids=[c.test_id for c in TOKENIZER_CASES],
)
def test_tokenize_produces_expected_stream(case: TokenizerCase) -> None:
    """Tokenizer emits the documented (kind, value) sequence ending in eof."""
    tokens = tokenize(case.query)
    actual_kinds = tuple(token.kind for token in tokens)
    actual_values = tuple(token.value for token in tokens)
    assert actual_kinds == case.expected_kinds
    assert actual_values == case.expected_values


class TokenizerErrorCase(t.NamedTuple):
    """Parametrized case for tokenizer error paths."""

    test_id: str
    query: str
    expected_position: int
    expected_message_fragment: str


TOKENIZER_ERROR_CASES: tuple[TokenizerErrorCase, ...] = (
    TokenizerErrorCase(
        test_id="unterminated-double-quote",
        query='bliss "deploy',
        expected_position=6,
        expected_message_fragment='unterminated " quoted string',
    ),
    TokenizerErrorCase(
        test_id="unterminated-single-quote",
        query="'agent:codex",
        expected_position=0,
        expected_message_fragment="unterminated ' quoted string",
    ),
)


@pytest.mark.parametrize(
    "case",
    TOKENIZER_ERROR_CASES,
    ids=[c.test_id for c in TOKENIZER_ERROR_CASES],
)
def test_tokenize_reports_clean_error(case: TokenizerErrorCase) -> None:
    """Bad input raises QueryParseError with the right position + message."""
    with pytest.raises(QueryParseError) as exc_info:
        _ = tokenize(case.query)
    assert exc_info.value.position == case.expected_position
    assert case.expected_message_fragment in str(exc_info.value)


def test_token_offsets_point_at_source() -> None:
    """Every emitted token carries an accurate ``start`` offset."""
    tokens = tokenize("agent:codex bliss")
    assert tokens[0] == Token(kind="ident", value="agent", start=0)
    assert tokens[1] == Token(kind="colon", value=":", start=5)
    assert tokens[2] == Token(kind="term", value="codex", start=6)
    assert tokens[3] == Token(kind="term", value="bliss", start=12)
    assert tokens[4].kind == "eof"
    assert tokens[4].start == len("agent:codex bliss")


# ----- recursive-descent parser ------------------------------------------


class ParserCase(t.NamedTuple):
    """Parametrized case for :func:`agentgrep.query.parse_query`."""

    test_id: str
    query: str
    expected: QueryNode


def _term(value: str) -> TermNode:
    """Concise constructor for `TermNode` test data."""
    return TermNode(value=value)


def _eq(field: str, value: str) -> FieldEqNode:
    """Concise constructor for `FieldEqNode` test data."""
    return FieldEqNode(field=field, value=value)


PARSER_CASES: tuple[ParserCase, ...] = (
    ParserCase(
        test_id="bare-term",
        query="bliss",
        expected=_term("bliss"),
    ),
    ParserCase(
        test_id="two-terms-implicit-and",
        query="bliss codex",
        expected=AndNode(children=(_term("bliss"), _term("codex"))),
    ),
    ParserCase(
        test_id="three-terms-implicit-and-flatten",
        query="a b c",
        expected=AndNode(children=(_term("a"), _term("b"), _term("c"))),
    ),
    ParserCase(
        test_id="explicit-and-equals-implicit",
        query="bliss AND codex",
        expected=AndNode(children=(_term("bliss"), _term("codex"))),
    ),
    ParserCase(
        test_id="field-equality",
        query="agent:codex",
        expected=_eq("agent", "codex"),
    ),
    ParserCase(
        test_id="field-and-term",
        query="agent:codex bliss",
        expected=AndNode(children=(_eq("agent", "codex"), _term("bliss"))),
    ),
    ParserCase(
        test_id="negation-sigil",
        query="-agent:claude",
        expected=NotNode(child=_eq("agent", "claude")),
    ),
    ParserCase(
        test_id="negation-keyword",
        query="NOT agent:claude",
        expected=NotNode(child=_eq("agent", "claude")),
    ),
    ParserCase(
        test_id="or-disjunction",
        query="agent:codex OR agent:cursor",
        expected=OrNode(children=(_eq("agent", "codex"), _eq("agent", "cursor"))),
    ),
    ParserCase(
        test_id="grouped-or-and-term",
        query="(agent:codex OR agent:cursor) AND bliss",
        expected=AndNode(
            children=(
                OrNode(children=(_eq("agent", "codex"), _eq("agent", "cursor"))),
                _term("bliss"),
            ),
        ),
    ),
    ParserCase(
        test_id="comparison-gt",
        query="timestamp:>2025-01-01",
        expected=FieldCmpNode(field="timestamp", op="gt", value="2025-01-01"),
    ),
    ParserCase(
        test_id="comparison-lte",
        query="timestamp:<=2026-05-22",
        expected=FieldCmpNode(field="timestamp", op="lte", value="2026-05-22"),
    ),
    ParserCase(
        test_id="inclusive-range",
        query="timestamp:[2025-01-01 TO 2025-12-31]",
        expected=FieldRangeNode(
            field="timestamp",
            lo="2025-01-01",
            hi="2025-12-31",
            inclusive_lo=True,
            inclusive_hi=True,
        ),
    ),
    ParserCase(
        test_id="exclusive-range",
        query="mtime:{2025-01 TO 2026-01}",
        expected=FieldRangeNode(
            field="mtime",
            lo="2025-01",
            hi="2026-01",
            inclusive_lo=False,
            inclusive_hi=False,
        ),
    ),
    ParserCase(
        test_id="alias-resolves",
        query="adapter:codex.sessions",
        expected=_eq("adapter_id", "codex.sessions"),
    ),
    ParserCase(
        test_id="plus-sigil-is-noop",
        query="+bliss",
        expected=_term("bliss"),
    ),
    ParserCase(
        test_id="path-glob-value",
        query="path:~/.codex",
        expected=_eq("path", "~/.codex"),
    ),
    ParserCase(
        test_id="nested-negation",
        query="-(agent:codex bliss)",
        expected=NotNode(
            child=AndNode(children=(_eq("agent", "codex"), _term("bliss"))),
        ),
    ),
)


@pytest.mark.parametrize(
    "case",
    PARSER_CASES,
    ids=[c.test_id for c in PARSER_CASES],
)
def test_parse_query_builds_expected_ast(case: ParserCase) -> None:
    """parse_query produces the documented AST shape."""
    result = parse_query(case.query, default_registry())
    assert result == case.expected


class ParserErrorCase(t.NamedTuple):
    """Parametrized case for parser failure paths."""

    test_id: str
    query: str
    expected_message_fragment: str


PARSER_ERROR_CASES: tuple[ParserErrorCase, ...] = (
    ParserErrorCase(
        test_id="unknown-field",
        query="agetn:codex",
        expected_message_fragment="unknown field 'agetn'",
    ),
    ParserErrorCase(
        test_id="missing-rparen",
        query="(agent:codex",
        expected_message_fragment="expected rparen",
    ),
    ParserErrorCase(
        test_id="dangling-or",
        query="agent:codex OR",
        expected_message_fragment="unexpected token eof",
    ),
    ParserErrorCase(
        test_id="missing-value-after-colon",
        query="agent:",
        expected_message_fragment="expected value, comparison, or range",
    ),
    ParserErrorCase(
        test_id="range-missing-TO",
        query="timestamp:[2025 2026]",
        expected_message_fragment="expected to",
    ),
    ParserErrorCase(
        test_id="comparison-missing-value",
        query="timestamp:>",
        expected_message_fragment="expected term",
    ),
)


@pytest.mark.parametrize(
    "case",
    PARSER_ERROR_CASES,
    ids=[c.test_id for c in PARSER_ERROR_CASES],
)
def test_parse_query_reports_clean_error(case: ParserErrorCase) -> None:
    """Malformed queries raise QueryParseError with a useful message."""
    with pytest.raises(QueryParseError) as exc_info:
        _ = parse_query(case.query, default_registry())
    assert case.expected_message_fragment in str(exc_info.value)


def test_parse_query_known_field_lists_in_error_message() -> None:
    """Unknown-field errors list the known field names so users can fix typos."""
    with pytest.raises(QueryParseError) as exc_info:
        _ = parse_query("bogus:value", default_registry())
    message = str(exc_info.value)
    assert "agent" in message
    assert "timestamp" in message
    assert "path" in message
