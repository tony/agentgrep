"""Lucene-style query language for agentgrep.

This package defines a small query parser plus a compiler that splits
the parsed AST into two layers of predicates:

- **source-level predicates** that prune sources before any file is
  opened (`agent:`, `path:`, `store:`, `mtime:`).
- **record-level predicates** that filter parsed records
  (`type:`, `timestamp:`, `model:`, `role:`, plain text terms).

The legacy code path is preserved exactly: when a query contains no
``:`` field separators, no parsing or compilation happens — the
existing :class:`agentgrep.SearchQuery` interface keeps its behavior
and timing.

Examples
--------
Parse a query string with the default field registry::

    from agentgrep.query import parse_query, default_registry

    ast = parse_query("agent:codex bliss", default_registry())

See Also
--------
:mod:`agentgrep.query.ast` — AST node types.
:mod:`agentgrep.query.parser` — tokenizer + recursive-descent parser.
"""

from __future__ import annotations

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
from agentgrep.query.parser import QueryParseError, parse_query, tokenize
from agentgrep.query.registry import (
    FieldKind,
    FieldLayer,
    FieldRegistry,
    FieldSpec,
    default_registry,
)

__all__ = [
    "AndNode",
    "FieldCmpNode",
    "FieldEqNode",
    "FieldKind",
    "FieldLayer",
    "FieldRangeNode",
    "FieldRegistry",
    "FieldSpec",
    "NotNode",
    "OrNode",
    "QueryNode",
    "QueryParseError",
    "TermNode",
    "Token",
    "TokenKind",
    "default_registry",
    "parse_query",
    "tokenize",
]
