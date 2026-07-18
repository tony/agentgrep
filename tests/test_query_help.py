"""Tests for the registry-driven query-language help renderer.

The renderer is the single source of truth for query-language docs
shown in MCP tool descriptions, the MCP query-language resource, and
the server instructions. These tests guard against drift between the
rendered field list and :func:`agentgrep.query.default_registry`.
"""

from __future__ import annotations

from agentgrep.query import (
    default_registry,
    parse_query,
    scope_widened_for_ast,
)
from agentgrep.query.help import (
    query_language_fields,
    query_language_operators,
    query_language_summary,
)


def test_fields_mirror_the_registry() -> None:
    """Every registry field (canonical name) appears once in the field docs."""
    registry = default_registry()
    rendered = query_language_fields(registry)
    assert tuple(doc.name for doc in rendered) == registry.known_names()


def test_field_docs_carry_registry_metadata() -> None:
    """Field docs reflect kind/layer/enum metadata from the registry."""
    docs = {doc.name: doc for doc in query_language_fields()}
    assert docs["agent"].kind == "enum"
    assert docs["agent"].layer == "source"
    assert "codex" in docs["agent"].enum_values
    assert docs["timestamp"].supports_range is True
    assert "date" in docs["timestamp"].aliases


def test_summary_names_every_field() -> None:
    """The compact summary names each queryable field for discoverability."""
    summary = query_language_summary()
    for name in default_registry().known_names():
        assert name in summary


def test_summary_documents_core_operators() -> None:
    """The summary advertises the operators a user would otherwise miss."""
    summary = query_language_summary()
    for marker in ("OR", "NOT", '"', "field:*", "TO"):
        assert marker in summary


def test_operators_include_phrase_exists_and_wildcard() -> None:
    """Operator docs cover the features ADR 0007 added."""
    syntaxes = " ".join(op.syntax for op in query_language_operators())
    for marker in ('"', "field:*", "*"):
        assert marker in syntaxes


def test_model_wildcard_example_opts_into_conversations() -> None:
    """Copying the model wildcard example cannot silently search prompts only."""
    wildcard = next(op for op in query_language_operators() if op.syntax == "field:glob*")
    assert wildcard.example == "scope:all model:gpt*"


def test_exists_example_works_in_the_default_prompt_scope() -> None:
    """Copying the exists example cannot target conversation-only metadata."""
    exists = next(op for op in query_language_operators() if op.syntax == "field:*")
    ast = parse_query(exists.example, default_registry())

    assert exists.example == "agent:*"
    assert scope_widened_for_ast(ast, "prompts") == "prompts"
