"""AST node definitions for the agentgrep query language.

Every node carries a ``kind`` literal discriminator so the union below
participates in pydantic's discriminated-union narrowing — the same
pattern :mod:`agentgrep.events` uses for the engine event stream.

The grammar is documented in the package docstring; in brief:

- :class:`TermNode` — a bare positional term (`bliss`).
- :class:`FieldEqNode` — `field:value` (substring / enum / path match).
- :class:`FieldCmpNode` — `field:>value`, `field:<=value` (comparison).
- :class:`FieldRangeNode` — `field:[a TO b]` (inclusive) or
  `field:{a TO b}` (exclusive).
- :class:`NotNode` — `NOT child` or `-child`.
- :class:`AndNode` — `left AND right` (n-ary chain).
- :class:`OrNode` — `left OR right` (n-ary chain).

The :data:`QueryNode` union is the type every public query function
takes or returns; consumers narrow with ``isinstance``.
"""

from __future__ import annotations

import typing as t

import pydantic

TokenKind = t.Literal[
    "term",  # bare word or quoted string (positional term value)
    "ident",  # field name on the left side of ``:``
    "colon",  # the ``:`` between a field name and its value
    "minus",  # leading ``-`` shorthand for NOT on next primary
    "plus",  # leading ``+`` shorthand for required (effectively a no-op)
    "and",  # explicit ``AND`` keyword
    "or",  # explicit ``OR`` keyword
    "not",  # explicit ``NOT`` keyword
    "lparen",  # ``(``
    "rparen",  # ``)``
    "lbracket",  # ``[`` (inclusive range start)
    "rbracket",  # ``]``
    "lbrace",  # ``{`` (exclusive range start)
    "rbrace",  # ``}``
    "to",  # the ``TO`` keyword inside a range
    "gt",  # ``>`` inside a comparison value
    "lt",  # ``<``
    "gte",  # ``>=``
    "lte",  # ``<=``
    "eof",  # synthetic end-of-stream marker emitted last
]
"""Discriminator literal for each token shape the lexer produces."""


class Token(pydantic.BaseModel):
    """One token in the lexed query stream.

    The tokenizer emits a flat sequence of these for the parser to
    consume. Each token carries its kind (see :data:`TokenKind`), the
    raw source value, and the start offset in the original query
    string — useful for error-message pointers and for re-tokenising
    sub-expressions inside ranges.
    """

    model_config: t.ClassVar[pydantic.ConfigDict] = pydantic.ConfigDict(
        frozen=True,
        extra="forbid",
    )

    kind: TokenKind
    value: str
    start: int


class _BaseNode(pydantic.BaseModel):
    """Frozen base for every AST node.

    Subclasses set a ``kind`` literal that participates in the
    discriminated-union narrowing in :data:`QueryNode`. Nodes are
    frozen so transformations always produce fresh trees instead of
    mutating shared sub-expressions.
    """

    model_config: t.ClassVar[pydantic.ConfigDict] = pydantic.ConfigDict(
        frozen=True,
        extra="forbid",
    )


class TermNode(_BaseNode):
    """A bare positional term — what `bliss` parses to.

    Bare terms route to the implicit ``text`` field at compile time,
    so a sequence of them becomes the existing text-only fast path.
    """

    kind: t.Literal["term"] = "term"
    value: str


class FieldEqNode(_BaseNode):
    """A ``field:value`` predicate.

    The ``value`` is the raw source text; the compiler interprets it
    according to the field's registered :class:`FieldSpec` (enum
    membership, substring match, path glob, …).
    """

    kind: t.Literal["field_eq"] = "field_eq"
    field: str
    value: str


class FieldCmpNode(_BaseNode):
    """A ``field:>value`` / ``field:<=value`` predicate.

    Comparison only applies to ordered fields (date, number); the
    compiler errors on comparison against a string / enum / path
    field.
    """

    kind: t.Literal["field_cmp"] = "field_cmp"
    field: str
    op: t.Literal["gt", "lt", "gte", "lte"]
    value: str


class FieldRangeNode(_BaseNode):
    """A ``field:[a TO b]`` / ``field:{a TO b}`` predicate.

    Brackets denote inclusive bounds (Lucene convention); braces
    denote exclusive. Either bound can be the literal ``*`` for
    "unbounded on this side" — the compiler treats ``*`` as the field
    type's natural minimum / maximum.
    """

    kind: t.Literal["field_range"] = "field_range"
    field: str
    lo: str
    hi: str
    inclusive_lo: bool
    inclusive_hi: bool


class NotNode(_BaseNode):
    """Boolean negation of a child node.

    Both ``NOT child`` (keyword) and ``-child`` (sigil) parse into
    this. The compiler propagates negation through the AST so the
    rest of the compiler can stay positive-only.
    """

    kind: t.Literal["not"] = "not"
    child: QueryNode


class AndNode(_BaseNode):
    """Conjunction of two or more children.

    Stored as an n-ary list rather than a binary tree so a long
    implicit-AND chain (`a b c d e`) lays out flatly. The compiler
    treats AND as commutative and associative for layer-splitting.
    """

    kind: t.Literal["and"] = "and"
    children: tuple[QueryNode, ...]


class OrNode(_BaseNode):
    """Disjunction of two or more children.

    Same n-ary representation as :class:`AndNode`. The compiler
    treats OR as commutative and associative; mixed-layer ORs fall
    back to record-level evaluation per the package docs.
    """

    kind: t.Literal["or"] = "or"
    children: tuple[QueryNode, ...]


QueryNode = t.Annotated[
    TermNode | FieldEqNode | FieldCmpNode | FieldRangeNode | NotNode | AndNode | OrNode,
    pydantic.Field(discriminator="kind"),
]
"""Discriminated union of every AST node :func:`parse_query` may emit.

Tagged on the ``kind`` literal. Use ``isinstance(node, FieldEqNode)``
to narrow inside a visitor; pydantic's discriminator metadata lets
``ty`` understand the narrowing.
"""


NotNode.model_rebuild()
AndNode.model_rebuild()
OrNode.model_rebuild()
