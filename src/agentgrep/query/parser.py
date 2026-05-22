"""Tokenizer for the agentgrep query language.

This module owns the lexical layer — the recursive-descent parser
lands in a follow-up commit. The split is deliberate: the tokenizer
is a small, side-effect-free transform from ``str`` to
``tuple[Token, ...]`` that's easy to test exhaustively, and that the
parser can consume linearly.

Grammar at the token level (see :mod:`agentgrep.query.ast` for the
node shapes):

- Bare words and quoted strings → ``term``.
- ``IDENT`` followed by ``:`` → ``ident``, ``colon`` (the parser
  decides whether the next token is a value, comparison, or range).
- Keywords ``AND``, ``OR``, ``NOT``, ``TO`` are case-insensitive and
  whole-word.
- Sigils ``-``, ``+`` are tokenised only when they sit at the start
  of a new primary (immediately after whitespace, ``(``, or the
  start of input); a ``-`` inside a word is part of the word.
- Punctuation: ``( ) [ ] { }`` plus ``> < >= <=`` for comparisons.
"""

from __future__ import annotations

import re
import typing as t

from agentgrep.query.ast import Token

_KEYWORDS: dict[str, t.Literal["and", "or", "not", "to"]] = {
    "AND": "and",
    "OR": "or",
    "NOT": "not",
    "TO": "to",
}

_WORD_RE = re.compile(r"[\w\-./~*?@:]+", re.UNICODE)
"""Characters allowed in a bare term or identifier.

``\\w`` matches Unicode word characters (letters/digits/underscore in
any script), so non-ASCII terms like ``こんにちは`` survive.
Includes ``.``, ``/``, ``~`` so path-shaped values (``~/.codex``,
``./foo``) work without quoting. ``:`` is included so a value like
``foo:bar`` (rare but legal) survives — the parser disambiguates
field-syntax from value text by position, not by the colon's
presence alone.
"""

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
"""Stricter rule for field names — letters/digits/underscore, must
start with a letter or underscore. Reused when the parser needs to
re-validate the left side of a ``:`` as a real identifier.
"""


class QueryParseError(ValueError):
    """Raised when the tokenizer encounters input it can't classify.

    Carries the byte offset into the original query string so the
    parser can render a caret-style error message.
    """

    def __init__(self, message: str, *, position: int) -> None:
        super().__init__(message)
        self.position: int = position


def tokenize(query: str) -> tuple[Token, ...]:
    """Lex a query string into a tuple of tokens, ending with ``eof``.

    Parameters
    ----------
    query : str
        The user-supplied query string.

    Returns
    -------
    tuple[Token, ...]
        Tokens in source order, terminated by a synthetic ``eof``
        token at ``len(query)``.

    Raises
    ------
    QueryParseError
        If the lexer hits a character it can't classify (e.g. an
        unterminated quoted string).
    """
    tokens: list[Token] = []
    pos = 0
    length = len(query)
    # primary_position is True when the next non-whitespace token
    # starts a fresh primary expression. Used to disambiguate the
    # sigil ``-``: a leading ``-`` at primary position is NOT, a
    # ``-`` inside a word is just a character.
    primary_position = True
    while pos < length:
        char = query[pos]
        if char.isspace():
            pos += 1
            continue
        if char == "(":
            tokens.append(Token(kind="lparen", value="(", start=pos))
            pos += 1
            primary_position = True
            continue
        if char == ")":
            tokens.append(Token(kind="rparen", value=")", start=pos))
            pos += 1
            primary_position = True
            continue
        if char == "[":
            tokens.append(Token(kind="lbracket", value="[", start=pos))
            pos += 1
            primary_position = True
            continue
        if char == "]":
            tokens.append(Token(kind="rbracket", value="]", start=pos))
            pos += 1
            primary_position = True
            continue
        if char == "{":
            tokens.append(Token(kind="lbrace", value="{", start=pos))
            pos += 1
            primary_position = True
            continue
        if char == "}":
            tokens.append(Token(kind="rbrace", value="}", start=pos))
            pos += 1
            primary_position = True
            continue
        if char == ">":
            if pos + 1 < length and query[pos + 1] == "=":
                tokens.append(Token(kind="gte", value=">=", start=pos))
                pos += 2
            else:
                tokens.append(Token(kind="gt", value=">", start=pos))
                pos += 1
            primary_position = False
            continue
        if char == "<":
            if pos + 1 < length and query[pos + 1] == "=":
                tokens.append(Token(kind="lte", value="<=", start=pos))
                pos += 2
            else:
                tokens.append(Token(kind="lt", value="<", start=pos))
                pos += 1
            primary_position = False
            continue
        if char == "-" and primary_position:
            tokens.append(Token(kind="minus", value="-", start=pos))
            pos += 1
            primary_position = True
            continue
        if char == "+" and primary_position:
            tokens.append(Token(kind="plus", value="+", start=pos))
            pos += 1
            primary_position = True
            continue
        if char in {'"', "'"}:
            value, end = _read_quoted(query, pos)
            tokens.append(Token(kind="term", value=value, start=pos))
            pos = end
            primary_position = True
            continue
        # Bare word — could be a term, a keyword, or a field identifier.
        match = _WORD_RE.match(query, pos)
        if match is None:
            message = f"unexpected character {char!r}"
            raise QueryParseError(message, position=pos)
        raw = match.group(0)
        end = match.end()
        # Detect ``ident:`` shape: emit ident + colon as two tokens so
        # the parser can dispatch on the colon directly.
        colon_index = raw.find(":")
        if colon_index > 0 and _IDENT_RE.fullmatch(raw[:colon_index]):
            ident = raw[:colon_index]
            tokens.append(Token(kind="ident", value=ident, start=pos))
            tokens.append(
                Token(kind="colon", value=":", start=pos + colon_index),
            )
            # The remainder after the colon is the field value; emit
            # it as a term only if there's non-empty text. An empty
            # remainder means the value sits in the next token (e.g.
            # ``agent: codex`` or ``agent:>2025``).
            remainder = raw[colon_index + 1 :]
            if remainder:
                tokens.append(
                    Token(
                        kind="term",
                        value=remainder,
                        start=pos + colon_index + 1,
                    ),
                )
                primary_position = True
            else:
                primary_position = False
            pos = end
            continue
        upper = raw.upper()
        if upper in _KEYWORDS:
            tokens.append(Token(kind=_KEYWORDS[upper], value=upper, start=pos))
            pos = end
            primary_position = True
            continue
        tokens.append(Token(kind="term", value=raw, start=pos))
        pos = end
        primary_position = True
    tokens.append(Token(kind="eof", value="", start=length))
    return tuple(tokens)


def _read_quoted(query: str, start: int) -> tuple[str, int]:
    """Read a quoted string starting at ``query[start]``.

    Handles both single and double quotes. Backslash escapes the
    quote character and the backslash itself. Returns the unquoted
    value and the offset immediately after the closing quote.

    Raises
    ------
    QueryParseError
        If the quoted string never closes.
    """
    quote = query[start]
    buffer: list[str] = []
    pos = start + 1
    length = len(query)
    while pos < length:
        char = query[pos]
        if char == "\\" and pos + 1 < length:
            buffer.append(query[pos + 1])
            pos += 2
            continue
        if char == quote:
            return "".join(buffer), pos + 1
        buffer.append(char)
        pos += 1
    message = f"unterminated {quote} quoted string"
    raise QueryParseError(message, position=start)
