"""Exceptions raised while compiling a parsed query into predicates."""

from __future__ import annotations


class QueryCompileError(ValueError):
    """Raised when a query AST can't be compiled.

    Distinct from :class:`agentgrep.query.parser.QueryParseError` —
    parse errors are syntactic, compile errors are semantic
    (e.g. comparing against a string field, range against an enum).
    """


__all__ = ("QueryCompileError",)
