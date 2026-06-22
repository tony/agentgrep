"""Query compilation errors (ADR 0010 leaf module).

See ADR 0010 (module boundaries and the facade re-export contract).
"""

from __future__ import annotations


class QueryCompileError(ValueError):
    """Raised when a query AST can't be compiled.

    Distinct from :class:`agentgrep.query.parser.QueryParseError` —
    parse errors are syntactic, compile errors are semantic
    (e.g. comparing against a string field, range against an enum).
    """


__all__ = ("QueryCompileError",)
