"""Tokenizer + recursive-descent parser for the agentgrep query language.

The two layers are split into named functions so each is testable
in isolation:

- :func:`tokenize` turns a ``str`` into a tuple of :class:`Token`.
- :func:`parse_query` consumes that token stream and produces a
  :data:`~agentgrep.query.ast.QueryNode` AST.

Grammar:

.. code-block:: text

    query        := disjunction
    disjunction  := conjunction ("OR" conjunction)*
    conjunction  := negation ("AND"? negation)*
    negation     := ("NOT" | "-" | "+")? primary
    primary      := group | field-expr | term
    group        := "(" disjunction ")"
    field-expr   := IDENT ":" field-value
    field-value  := comparison | range | exact-value
    comparison   := (">" | "<" | ">=" | "<=") TERM
    range        := "[" TERM "TO" TERM "]"
                  | "{" TERM "TO" TERM "}"
    exact-value  := TERM
    term         := TERM

Implicit AND between bare terms is preserved. Field names are
validated against the passed :class:`agentgrep.query.registry.FieldRegistry`
at parse time, so a typo in `agetn:codex` errors immediately rather
than failing silently against an empty result set.
"""

from __future__ import annotations

import re
import typing as t

from agentgrep.query.ast import (
    AndNode,
    FieldCmpNode,
    FieldEqNode,
    FieldRangeNode,
    NotNode,
    OrNode,
    QueryNode,
    TermNode,
    Token,
    TokenKind,
)
from agentgrep.query.registry import FieldRegistry

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


def parse_query(query: str, registry: FieldRegistry) -> QueryNode:
    """Parse a query string into an AST.

    Parameters
    ----------
    query : str
        The user-supplied query string.
    registry : FieldRegistry
        Field schema used to validate field names appearing in the
        query (e.g. ``agent:``, ``timestamp:``). Unknown fields raise
        :class:`QueryParseError` at parse time so typos surface
        immediately.

    Returns
    -------
    QueryNode
        The root of the parsed AST. Single-child AND/OR are
        flattened — `parse_query("bliss")` returns a bare
        :class:`TermNode`, not a one-element :class:`AndNode`.

    Raises
    ------
    QueryParseError
        On any tokenizer or grammar failure, with a ``position``
        attribute pointing at the offending offset.
    """
    tokens = tokenize(query)
    parser = _Parser(tokens=tokens, registry=registry)
    node = parser.parse_disjunction()
    parser.expect("eof")
    return node


class _Parser:
    """Recursive-descent parser over a tokenized query.

    Implements one method per grammar production. State is the
    token cursor ``pos``; each helper advances it. The parser
    deliberately uses an explicit token stream (not generators)
    so error messages can point at exact offsets.
    """

    __slots__ = ("_pos", "registry", "tokens")

    def __init__(self, *, tokens: tuple[Token, ...], registry: FieldRegistry) -> None:
        self.tokens: tuple[Token, ...] = tokens
        self._pos: int = 0
        self.registry: FieldRegistry = registry

    def peek(self) -> Token:
        """Return the current token without advancing."""
        return self.tokens[self._pos]

    def advance(self) -> Token:
        """Consume and return the current token."""
        token = self.tokens[self._pos]
        self._pos += 1
        return token

    def expect(self, kind: TokenKind) -> Token:
        """Consume the current token; error if its kind differs."""
        token = self.peek()
        if token.kind != kind:
            message = f"expected {kind}, got {token.kind} ({token.value!r})"
            raise QueryParseError(message, position=token.start)
        return self.advance()

    def parse_disjunction(self) -> QueryNode:
        """Parse one or more conjunctions joined by ``OR``."""
        children: list[QueryNode] = [self.parse_conjunction()]
        while self.peek().kind == "or":
            _ = self.advance()
            children.append(self.parse_conjunction())
        if len(children) == 1:
            return children[0]
        return OrNode(children=tuple(children))

    def parse_conjunction(self) -> QueryNode:
        """Parse one or more negations joined by implicit or explicit AND."""
        children: list[QueryNode] = [self.parse_negation()]
        while True:
            current = self.peek()
            if current.kind == "and":
                _ = self.advance()
                children.append(self.parse_negation())
                continue
            if self._starts_primary(current):
                children.append(self.parse_negation())
                continue
            break
        if len(children) == 1:
            return children[0]
        return AndNode(children=tuple(children))

    def parse_negation(self) -> QueryNode:
        """Parse an optional ``NOT`` / ``-`` / ``+`` prefix and a primary."""
        current = self.peek()
        if current.kind == "not" or current.kind == "minus":
            _ = self.advance()
            return NotNode(child=self.parse_primary())
        if current.kind == "plus":
            # ``+`` is rg-style "required" — semantically a no-op
            # since implicit AND already requires all terms. Consume
            # it and return the bare primary.
            _ = self.advance()
            return self.parse_primary()
        return self.parse_primary()

    def parse_primary(self) -> QueryNode:
        """Parse a group, field expression, or bare term."""
        current = self.peek()
        if current.kind == "lparen":
            _ = self.advance()
            node = self.parse_disjunction()
            _ = self.expect("rparen")
            return node
        if current.kind == "ident":
            return self.parse_field_expr()
        if current.kind == "term":
            _ = self.advance()
            return TermNode(value=current.value)
        message = f"unexpected token {current.kind} ({current.value!r})"
        raise QueryParseError(message, position=current.start)

    def parse_field_expr(self) -> QueryNode:
        """Parse ``IDENT : (comparison | range | value)``."""
        ident = self.advance()
        spec = self.registry.get(ident.value)
        if spec is None:
            known = ", ".join(self.registry.known_names())
            message = f"unknown field {ident.value!r}; known fields: {known}"
            raise QueryParseError(message, position=ident.start)
        _ = self.expect("colon")
        current = self.peek()
        if current.kind in {"gt", "lt", "gte", "lte"}:
            op_token = self.advance()
            value_token = self.expect("term")
            op_map: dict[str, t.Literal["gt", "lt", "gte", "lte"]] = {
                "gt": "gt",
                "lt": "lt",
                "gte": "gte",
                "lte": "lte",
            }
            return FieldCmpNode(
                field=spec.name,
                op=op_map[op_token.kind],
                value=value_token.value,
            )
        if current.kind == "lbracket" or current.kind == "lbrace":
            return self._parse_range(spec.name)
        if current.kind == "term":
            value_token = self.advance()
            return FieldEqNode(field=spec.name, value=value_token.value)
        message = (
            f"expected value, comparison, or range after {spec.name}:; "
            f"got {current.kind} ({current.value!r})"
        )
        raise QueryParseError(message, position=current.start)

    def _parse_range(self, field: str) -> FieldRangeNode:
        """Parse the ``[a TO b]`` or ``{a TO b}`` range tail."""
        open_token = self.advance()
        inclusive_lo = open_token.kind == "lbracket"
        lo = self.expect("term").value
        _ = self.expect("to")
        hi = self.expect("term").value
        close_token = self.peek()
        if open_token.kind == "lbracket":
            _ = self.expect("rbracket")
            inclusive_hi = True
        else:
            _ = self.expect("rbrace")
            inclusive_hi = False
        # Use close_token to keep the variable referenced (helps
        # readers see we validated the matching bracket); ruff
        # doesn't flag this because the conditional above consumed
        # it, but the read above makes the intent explicit.
        _ = close_token
        return FieldRangeNode(
            field=field,
            lo=lo,
            hi=hi,
            inclusive_lo=inclusive_lo,
            inclusive_hi=inclusive_hi,
        )

    @staticmethod
    def _starts_primary(token: Token) -> bool:
        """Return whether ``token`` could begin a fresh primary expression.

        Used by the implicit-AND detection in :meth:`parse_conjunction`:
        if the next token can start a primary, we treat the position
        as ``X Y`` (implicit AND) rather than the end of the
        conjunction.
        """
        return token.kind in {
            "term",
            "ident",
            "lparen",
            "minus",
            "plus",
            "not",
        }
