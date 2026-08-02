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

agentgrep#156 traced two further defects to the same module, both rooted in
the same mismatch: the detection regex scanned the whole input string for
*any* ``ident:`` shape, rather than being anchored to how
:func:`agentgrep.query.parser.tokenize` actually decides field-predicate
shape (a token's own leading prefix, once). A URL's port
(``http://localhost:8080/api``) was independently re-examined as its own
candidate after the scheme was exempted, and a hyphenated word
(``sub-path:x``) matched a registered field's suffix even though
``tokenize()`` never splits it. The tests below extending the URI-exemption
and gate-baseline sections prove both are fixed.
"""

from __future__ import annotations

import typing as t

import pytest

from agentgrep import GrepArgs, SearchArgs, parse_args
from agentgrep._query_gate import (
    _IDENT_RE as _GATE_IDENT_RE,
    _WORD_RE as _GATE_WORD_RE,
    QUERYABLE_FIELD_NAMES,
    has_query_syntax,
    unregistered_field_predicates,
)
from agentgrep.query import build_query_from_input, compose_query_ast, default_registry
from agentgrep.query.ast import TermNode
from agentgrep.query.parser import (
    _IDENT_RE as _TOKENIZER_IDENT_RE,
    _WORD_RE as _TOKENIZER_WORD_RE,
    tokenize,
)
from agentgrep.records import SearchQuery

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
        # A bare parenthesis carries no syntax of its own for the gate to
        # recognize (ADR 0007's Decision section documents this exactly):
        # grouping only engages alongside a boolean keyword, quote, or
        # registered predicate.
        ("(ruff uv)", False),
        ("(ruff AND uv)", True),
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


def test_has_query_syntax_agrees_with_the_tokenizer_for_a_hyphenated_word() -> None:
    """agentgrep#156: the gate and the real tokenizer must decide identically.

    ``path`` is registered, but ``sub-path`` is not a valid identifier
    (``_IDENT_RE`` excludes ``-``), so ``tokenize()`` never splits
    ``sub-path:x`` into an ``ident`` + ``colon`` pair — it stays one
    literal ``term`` token. The gate's own docstring claims this can never
    disagree; this test is the proof, not a restatement of the claim.
    """
    assert has_query_syntax("sub-path:x", known_field_names=frozenset({"path"})) is False

    tokens = tokenize("sub-path:x")
    kinds = [token.kind for token in tokens if token.kind != "eof"]
    assert kinds == ["term"]
    assert tokens[0].value == "sub-path:x"


@pytest.mark.parametrize(
    "text",
    [
        # A registered field name immediately after a ``.`` or ``/`` is
        # not a predicate either — the tokenizer's own ``_IDENT_RE`` check
        # runs against the run's *whole* prefix up to the first ``:``, and
        # a ``.`` or ``/`` in that prefix fails it the same way ``-`` does.
        "a.timestamp:x",
        "a/model:x",
    ],
)
def test_has_query_syntax_agrees_with_the_tokenizer_for_a_prefixed_field_name(
    text: str,
) -> None:
    """A registered field name is only a predicate at the run's own start."""
    assert has_query_syntax(text) is False

    tokens = tokenize(text)
    kinds = [token.kind for token in tokens if token.kind != "eof"]
    assert kinds == ["term"]


# ---------------------------------------------------------------------------
# unregistered_field_predicates: diagnostic-only detection, never gates
# whether the parser engages.
# ---------------------------------------------------------------------------


def test_unregistered_field_predicates_finds_a_lone_typo() -> None:
    """The concrete regression this half of the fix closes."""
    (found,) = unregistered_field_predicates("bogusfield:xyz")
    assert found.token == "bogusfield:xyz"
    assert found.field == "bogusfield"


@pytest.mark.parametrize(
    "text",
    [
        "bogusfield:xyz,",
        "(bogusfield:xyz)",
    ],
)
def test_unregistered_field_predicates_token_stops_at_the_word_boundary(
    text: str,
) -> None:
    """Trailing punctuation outside ``_WORD_RE`` never joins the token.

    The token is the matched word run itself, not everything up to the
    next whitespace — a comma or a closing paren glued on with no space
    was never part of the field-predicate shape.
    """
    (found,) = unregistered_field_predicates(text)
    assert found.token == "bogusfield:xyz"


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
        # agentgrep#156: a port (or any second ``:``) in the URI's
        # remainder must not be independently re-examined as its own
        # field-predicate candidate once the scheme itself is exempted.
        "http://localhost:8080/api",
        "https://example.com:443/path",
        "redis://foo:6379",
    ],
)
def test_unregistered_field_predicates_skips_uri_schemes(text: str) -> None:
    """A URL scheme is never mistaken for a typo'd field name."""
    assert unregistered_field_predicates(text) == ()


def test_unregistered_field_predicates_never_rescans_a_uris_remainder() -> None:
    """The whole URI is one ``_WORD_RE`` run, checked once, not per ``:``.

    Before agentgrep#156's fix, the scheme exemption only checked the
    matched scheme's own trailing characters, so a second ``ident:`` shape
    later in the same URI (``localhost:8080``) was found and flagged
    independently. Credentials (``user:pass@host``) are the same shape and
    must be equally unexamined, and a second URL in the same input is its
    own independent run, exempted on its own terms.
    """
    assert unregistered_field_predicates("redis://user:pass@host:6379") == ()
    found = unregistered_field_predicates(
        "http://localhost:8080/api https://example.com:443/path",
    )
    assert found == ()


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
# build_query_from_input's warning field — the TUI search-box surface.
# See tests/test_tui_query_diagnostics.py for the mounted-app, submit-only
# contract (a live keystroke must never fire a notification).
# ---------------------------------------------------------------------------


def _base_query() -> SearchQuery:
    """Build one minimal base query for ``build_query_from_input``."""
    return SearchQuery(
        terms=(),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )


def test_build_query_from_input_warns_for_an_unregistered_field_predicate() -> None:
    """The search-box path attaches the same warning as the CLI/MCP paths."""
    result = build_query_from_input("bogusfield:xyz", _base_query(), default_registry())

    assert result.error is None
    assert result.query is not None
    assert result.query.terms == ("bogusfield:xyz",)
    assert result.warning is not None
    assert "bogusfield" in result.warning


def test_build_query_from_input_no_warning_for_a_registered_field_predicate() -> None:
    """A real field predicate needs no diagnostic."""
    result = build_query_from_input("kind:prompt", _base_query(), default_registry())

    assert result.warning is None


def test_build_query_from_input_no_warning_for_a_url_literal() -> None:
    """A URL stays a silent literal, not a flagged predicate."""
    result = build_query_from_input("https://example.com", _base_query(), default_registry())

    assert result.warning is None


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


def test_query_gate_word_and_ident_regexes_mirror_the_tokenizer() -> None:
    """agentgrep#156: the gate's duplicated regexes must track the real ones.

    ``agentgrep._query_gate`` keeps its own copies of
    ``agentgrep.query.parser``'s ``_WORD_RE``/``_IDENT_RE`` instead of
    importing the module (see ``_query_gate.py``'s cold-start rationale).
    A drift here would silently reopen the exact disagreement agentgrep#156
    fixed — the gate deciding field-predicate shape by a rule the real
    tokenizer no longer uses.
    """
    assert _GATE_WORD_RE.pattern == _TOKENIZER_WORD_RE.pattern
    assert _GATE_IDENT_RE.pattern == _TOKENIZER_IDENT_RE.pattern
