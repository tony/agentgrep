r"""Shared query-syntax gate contract (agentgrep#153).

Two defects, one shared root cause: the CLI's cold-start positional scan
and the query compiler's live scan each independently decided whether a
colon-bearing token looked like query syntax, and both only engaged the
parser when the identifier before ``:`` was *already a registered field
name*. An unregistered predicate (a typo, or ``kind:`` before it was
registered) silently degraded to a zero-signal literal search instead of
the parser's existing "unknown field" error — with no signal at all.

This module proves the fix: one shared gate
(:mod:`agentgrep._query_gate`) closes the drift between the CLI and
compiler scans, keeps engaging the parser for a registered predicate only
(so a plausible literal like ``Note: fix this`` or ``C:\\Users\\foo``
never turns into a hard "unknown field" error), and separately surfaces an
unregistered field-predicate-shaped token as a non-fatal, suggestible
diagnostic rather than silence. See ``tests/test_query_kind_field.py`` for
the ``kind:`` field's own contract, and ``tests/test_query_diagnostics.py``
for how the diagnostic reaches the CLI/MCP/TUI output surfaces.
"""

from __future__ import annotations

import typing as t

import pytest

from agentgrep import GrepArgs, SearchArgs, parse_args
from agentgrep._query_gate import (
    QUERYABLE_FIELD_NAMES,
    has_query_syntax,
    unregistered_field_predicates,
)
from agentgrep.query import compose_query_ast, default_registry
from agentgrep.query.ast import TermNode

if t.TYPE_CHECKING:
    from agentgrep.query.registry import FieldRegistry


# ---------------------------------------------------------------------------
# has_query_syntax: engages only for a registered predicate, a standalone
# boolean keyword, or a leading quote.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", False),
        ("ruff uv tmux", False),
        ("12:30", False),
        ('"phrase here"', True),
        ("ruff AND uv", True),
        ("ruff and uv", False),
        ("agent:claude", True),
        ("kind:prompt", True),
    ],
)
def test_has_query_syntax_baseline_shapes(text: str, expected: bool) -> None:
    """Registered predicates, phrases, and boolean keywords still engage."""
    assert has_query_syntax(text) is expected


@pytest.mark.parametrize(
    "text",
    [
        "bogusfield:xyz",
        "foo:bar",
        "agnet:codex",
        "Note: fix this",
        "https://example.com",
        r"C:\Users\foo",
    ],
)
def test_has_query_syntax_does_not_engage_for_an_unregistered_token_alone(
    text: str,
) -> None:
    """An unregistered field-predicate-shaped token, alone, stays literal.

    This is the fix's central tradeoff: a blanket "any ``ident:`` shape is
    a predicate" rule would make every one of these hard-error instead
    (verified against a rejected earlier design of this same module).
    """
    assert has_query_syntax(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "bogusfield:xyz OR ruff",
        "ruff NOT bogusfield:xyz",
        "agent:claude bogusfield:xyz",
    ],
)
def test_has_query_syntax_still_engages_when_combined_with_known_syntax(
    text: str,
) -> None:
    """An unregistered predicate alongside a boolean keyword or a registered predicate.

    Still engages the parser for the whole positional — the graceful
    literal fallback only covers a lone unregistered token, a documented,
    deliberate scope boundary (see ADR 0007's Risks).
    """
    assert has_query_syntax(text) is True


def test_a_registered_field_name_is_never_uri_exempted() -> None:
    """A real field name before ``//`` is still a predicate.

    ``agent://codex`` looks URI-shaped, but ``agent`` is a registered
    field, so it engages the parser regardless of what follows the colon.
    """
    assert has_query_syntax("agent://codex", known_field_names=frozenset({"agent"})) is True


# ---------------------------------------------------------------------------
# unregistered_field_predicates: diagnostic-only detection, never gates
# whether the parser engages.
# ---------------------------------------------------------------------------


def test_unregistered_field_predicates_finds_a_lone_typo() -> None:
    """The concrete regression this half of the fix closes."""
    (found,) = unregistered_field_predicates("bogusfield:xyz")
    assert found.token == "bogusfield:xyz"
    assert found.field == "bogusfield"


def test_unregistered_field_predicates_suggests_a_close_registered_name() -> None:
    """A near-miss typo gets pointed at the field it probably meant."""
    (found,) = unregistered_field_predicates("agnet:codex")
    assert found.field == "agnet"
    assert found.suggestion == "agent"


def test_unregistered_field_predicates_has_no_suggestion_when_nothing_is_close() -> None:
    """An unrelated identifier gets no suggestion rather than a bad guess."""
    (found,) = unregistered_field_predicates("bogusfield:xyz")
    assert found.suggestion is None


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com",
        "ftp://example.com",
        "git://example.com/repo.git",
    ],
)
def test_unregistered_field_predicates_skips_uri_schemes(text: str) -> None:
    """A URL scheme is never mistaken for a typo'd field name."""
    assert unregistered_field_predicates(text) == ()


@pytest.mark.parametrize(
    "text",
    [
        "Note: fix this",
        "TODO:fix",
        r"C:\Users\foo",
    ],
)
def test_unregistered_field_predicates_skips_non_lowercase_identifiers(text: str) -> None:
    """Every registered field name is lowercase.

    So a capitalized or mixed-case token before a colon reads as prose or
    a path, not a typo.
    """
    assert unregistered_field_predicates(text) == ()


def test_unregistered_field_predicates_skips_registered_fields() -> None:
    """A field that resolved via ``has_query_syntax`` needs no diagnostic."""
    assert unregistered_field_predicates("kind:prompt") == ()


def test_unregistered_field_predicates_finds_every_match_in_order() -> None:
    """Multiple typo'd predicates in one positional are all reported."""
    found = unregistered_field_predicates("bogusfield:xyz otherbad:1")
    assert [entry.field for entry in found] == ["bogusfield", "otherbad"]


# ---------------------------------------------------------------------------
# End-to-end: the CLI and MCP paths agree with has_query_syntax, and a
# literal search still runs (not an error) for a lone unregistered token.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["search", "grep"])
def test_unregistered_field_predicate_runs_as_a_literal_search_on_the_cli(
    command: str,
) -> None:
    """A lone unregistered field-shaped token compiles to nothing.

    It survives as a residual literal term — not a parse error.
    """
    parsed = parse_args([command, "bogusfield:xyz"])

    assert isinstance(parsed, SearchArgs | GrepArgs)
    assert parsed.compiled is None
    residual = parsed.terms if isinstance(parsed, SearchArgs) else parsed.patterns
    assert residual == ("bogusfield:xyz",)


@pytest.mark.parametrize("command", ["search", "grep"])
def test_unregistered_field_predicate_combined_with_boolean_still_errors_on_the_cli(
    command: str,
) -> None:
    """The documented scope boundary: composed with OR/NOT, it still hard-errors."""
    with pytest.raises(SystemExit):
        parse_args([command, "bogusfield:xyz OR ruff"])


def test_url_literal_stays_a_literal_search_through_compose_query_ast() -> None:
    """Same allowlist decision reached through the MCP path."""
    registry = default_registry()
    ast, user_ast = compose_query_ast(["https://example.com"], (), registry)

    assert user_ast is None
    assert isinstance(ast, TermNode)
    assert ast.value == "https://example.com"


def test_unregistered_field_predicate_stays_literal_through_compose_query_ast() -> None:
    """Same fallback reached through the MCP path, not just the CLI."""
    registry = default_registry()
    ast, user_ast = compose_query_ast(["bogusfield:xyz"], (), registry)

    assert user_ast is None
    assert isinstance(ast, TermNode)
    assert ast.value == "bogusfield:xyz"


# ---------------------------------------------------------------------------
# Drift guard.
# ---------------------------------------------------------------------------


def _live_registry_field_names(registry: FieldRegistry) -> frozenset[str]:
    return frozenset(name for spec in registry.specs for name in (spec.name, *spec.aliases))


def test_cli_query_field_names_mirror_the_registry() -> None:
    """The promised, previously-missing drift guard (see ``_query_gate.py``).

    Unlike the CLI mirror's original purpose, a drift here reproduces the
    exact silent-literal defect agentgrep#153 reports for the CLI path —
    a freshly registered field would not engage the parser via
    ``agentgrep.cli.parser._query_syntax_present`` until this mirror is
    updated too.
    """
    live_field_names = _live_registry_field_names(default_registry())

    assert live_field_names == QUERYABLE_FIELD_NAMES
