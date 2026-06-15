"""Registry-driven query-language documentation.

Single source of truth for the query-language help shown in MCP tool
descriptions, the ``agentgrep://query-language`` resource, and the MCP
server instructions. Field docs derive from
:func:`agentgrep.query.default_registry` so they cannot drift from what
the compiler actually accepts. Operator docs live here because the
operator grammar is owned by the parser, not the registry.

The CLI ``--help`` example blocks deliberately use literal strings (see
:mod:`agentgrep`) rather than this renderer so the root help path stays
cold-start cheap; this module is consumed by the MCP layer, which is not
cold-start sensitive.
"""

from __future__ import annotations

import dataclasses

from agentgrep.query.registry import FieldRegistry, default_registry


@dataclasses.dataclass(slots=True, frozen=True)
class FieldDoc:
    """One queryable field, rendered from its :class:`FieldSpec`."""

    name: str
    kind: str
    layer: str
    aliases: tuple[str, ...]
    enum_values: tuple[str, ...]
    supports_comparison: bool
    supports_range: bool


@dataclasses.dataclass(slots=True, frozen=True)
class OperatorDoc:
    """One query-language operator with a copy-pasteable example."""

    syntax: str
    description: str
    example: str


_OPERATORS: tuple[OperatorDoc, ...] = (
    OperatorDoc(
        syntax="term term",
        description="Bare terms are case-insensitive substrings, AND-combined.",
        example="ruff uv",
    ),
    OperatorDoc(
        syntax="AND / OR / NOT",
        description="Boolean composition; keywords are uppercase.",
        example="ruff OR uv",
    ),
    OperatorDoc(
        syntax="- / +",
        description="Shorthand for NOT and required (required is a no-op).",
        example="ruff -tmux",
    ),
    OperatorDoc(
        syntax="( )",
        description="Grouping to control precedence.",
        example="(ruff OR uv) tmux",
    ),
    OperatorDoc(
        syntax='"phrase"',
        description="Exact adjacent words, matched as one substring.",
        example='"deploy v1"',
    ),
    OperatorDoc(
        syntax="field:value",
        description="Field predicate (substring, enum, or path glob).",
        example="agent:codex",
    ),
    OperatorDoc(
        syntax="field:*",
        description="Field is present and non-empty.",
        example="model:*",
    ),
    OperatorDoc(
        syntax="field:glob*",
        description="Wildcard (* / ?) on text and string fields, anchored.",
        example="model:gpt*",
    ),
    OperatorDoc(
        syntax="field:>v / field:<=v",
        description="Comparison on date fields (timestamp, mtime).",
        example="timestamp:>2026-01-01",
    ),
    OperatorDoc(
        syntax="field:[a TO b] / {a TO b}",
        description="Inclusive / exclusive range on date fields.",
        example="timestamp:[2026-01 TO 2026-06]",
    ),
)


def query_language_fields(
    registry: FieldRegistry | None = None,
) -> tuple[FieldDoc, ...]:
    """Return one :class:`FieldDoc` per registered field, in registry order.

    Parameters
    ----------
    registry : FieldRegistry or None
        Registry to render. Defaults to :func:`default_registry`.

    Returns
    -------
    tuple[FieldDoc, ...]
        Structured field documentation derived from the registry.
    """
    reg = registry if registry is not None else default_registry()
    return tuple(
        FieldDoc(
            name=spec.name,
            kind=spec.kind,
            layer=spec.layer,
            aliases=spec.aliases,
            enum_values=spec.enum_values,
            supports_comparison=spec.supports_comparison,
            supports_range=spec.supports_range,
        )
        for spec in reg.specs
    )


def query_language_operators() -> tuple[OperatorDoc, ...]:
    """Return the documented query-language operators."""
    return _OPERATORS


def query_language_summary(registry: FieldRegistry | None = None) -> str:
    """Return a compact one-paragraph summary of the query language.

    Suitable for an MCP tool description or server-instruction segment.
    Names every queryable field so an agent can discover the vocabulary
    without a round-trip to the resource.

    Parameters
    ----------
    registry : FieldRegistry or None
        Registry whose field names appear in the summary. Defaults to
        :func:`default_registry`.

    Returns
    -------
    str
        A single paragraph naming the fields and core operators.
    """
    names = ", ".join(doc.name for doc in query_language_fields(registry))
    return (
        "Terms are case-insensitive substrings, AND-combined; compose with "
        'OR / NOT / ( ). Quote "exact phrases". Fields: '
        f"{names}. Use field:value, field:* (exists), field:glob* (wildcard), "
        "date comparisons (timestamp:>2026-01-01), and ranges "
        "(timestamp:[2026-01 TO 2026-06]). Bare terms stay literal substrings."
    )
