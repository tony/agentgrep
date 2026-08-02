"""``kind:`` field contract (agentgrep#153).

``SearchRecord.kind`` (``"prompt"`` / ``"history"``) is rendered on every
frontend but was not a registered queryable field. This module proves it
parses, compiles, and evaluates correctly end to end, and that a mistyped
enum value produces a clean compile error rather than a silent zero-match
search — independent of how an *unregistered* field name is handled (see
``tests/test_query_gate.py`` for that half of agentgrep#153).
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

from agentgrep import GrepArgs, SearchArgs, parse_args
from agentgrep.query import (
    QueryCompileError,
    build_query_from_input,
    compile_query,
    compose_query_ast,
    default_registry,
    parse_query,
)
from agentgrep.records import SearchQuery, SearchRecord


def _record(kind: t.Literal["prompt", "history"]) -> SearchRecord:
    """Build one minimal record of the given ``kind``."""
    return SearchRecord(
        kind=kind,
        agent="codex",
        store="codex.history",
        adapter_id="codex.history_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="deploy the service",
    )


PROMPT_RECORD = _record("prompt")
HISTORY_RECORD = _record("history")


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


def test_kind_field_is_registered() -> None:
    """The concrete regression this half of the fix closes."""
    registry = default_registry()
    spec = registry.get("kind")

    assert "kind" in registry.known_names()
    assert spec is not None
    assert spec.enum_values == ("prompt", "history")


@pytest.mark.parametrize(
    ("query_text", "expect_prompt", "expect_history"),
    [
        ("kind:prompt", True, False),
        ("kind:history", False, True),
    ],
)
def test_kind_field_evaluates_directly(
    query_text: str,
    expect_prompt: bool,
    expect_history: bool,
) -> None:
    """T1(a): parse_query + compile_query + record_predicate, no frontend."""
    registry = default_registry()
    ast = parse_query(query_text, registry)
    compiled = compile_query(ast, registry)

    assert compiled.record_predicate is not None
    assert compiled.record_predicate(PROMPT_RECORD) is expect_prompt
    assert compiled.record_predicate(HISTORY_RECORD) is expect_history


@pytest.mark.parametrize("command", ["search", "grep"])
@pytest.mark.parametrize(
    ("query_text", "expect_prompt", "expect_history"),
    [
        ("kind:prompt", True, False),
        ("kind:history", False, True),
    ],
)
def test_kind_field_works_through_the_cli_path(
    command: str,
    query_text: str,
    expect_prompt: bool,
    expect_history: bool,
) -> None:
    """T1(b): the CLI path (parse_args -> SearchArgs/GrepArgs.compiled).

    ``grep`` rejects a query that carries no text pattern at all (field
    predicates alone can't drive line-level matching), so its positional
    also carries a bare ``deploy`` term. Both fixture records' text
    already contains "deploy", so the extra AND clause does not change
    which record the ``kind:`` predicate should admit.
    """
    argv = [command, query_text] if command == "search" else [command, query_text, "deploy"]
    parsed = parse_args(argv)

    assert isinstance(parsed, SearchArgs | GrepArgs)
    assert parsed.compiled is not None
    assert parsed.compiled.record_predicate is not None
    assert parsed.compiled.record_predicate(PROMPT_RECORD) is expect_prompt
    assert parsed.compiled.record_predicate(HISTORY_RECORD) is expect_history


@pytest.mark.parametrize(
    ("query_text", "expect_prompt", "expect_history"),
    [
        ("kind:prompt", True, False),
        ("kind:history", False, True),
    ],
)
def test_kind_field_works_through_compose_query_ast(
    query_text: str,
    expect_prompt: bool,
    expect_history: bool,
) -> None:
    """T1(c), half one: the MCP path (compose_query_ast + compile_query)."""
    registry = default_registry()
    ast, user_ast = compose_query_ast([query_text], (), registry)

    assert user_ast is not None
    compiled = compile_query(ast, registry)
    assert compiled.record_predicate is not None
    assert compiled.record_predicate(PROMPT_RECORD) is expect_prompt
    assert compiled.record_predicate(HISTORY_RECORD) is expect_history


@pytest.mark.parametrize(
    ("query_text", "expect_prompt", "expect_history"),
    [
        ("kind:prompt", True, False),
        ("kind:history", False, True),
    ],
)
def test_kind_field_works_through_build_query_from_input(
    query_text: str,
    expect_prompt: bool,
    expect_history: bool,
) -> None:
    """T1(c), half two: the TUI search-box path (build_query_from_input)."""
    registry = default_registry()
    result = build_query_from_input(query_text, _base_query(), registry)

    assert result.error is None
    assert result.query is not None
    compiled = result.query.compiled
    assert compiled is not None
    assert compiled.record_predicate is not None
    assert compiled.record_predicate(PROMPT_RECORD) is expect_prompt
    assert compiled.record_predicate(HISTORY_RECORD) is expect_history


_INVALID_KIND_MESSAGE = "invalid kind value 'promt'; valid choices: prompt, history"


@pytest.mark.parametrize("command", ["search", "grep"])
def test_kind_typo_errors_on_the_cli(
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T2, CLI: a typo'd enum value exits non-zero with the compile message."""
    with pytest.raises(SystemExit):
        parse_args([command, "kind:promt"])

    assert _INVALID_KIND_MESSAGE in capsys.readouterr().err


def test_kind_typo_errors_through_build_query_from_input() -> None:
    """T2, TUI: same QueryCompileError message, surfaced as ``result.error``."""
    result = build_query_from_input("kind:promt", _base_query(), default_registry())

    assert result.query is None
    assert result.error == _INVALID_KIND_MESSAGE


def test_kind_typo_errors_through_compose_query_ast() -> None:
    """T2, MCP: same QueryCompileError message, raised from compile_query."""
    registry = default_registry()
    ast, _user_ast = compose_query_ast(["kind:promt"], (), registry)

    with pytest.raises(QueryCompileError, match="invalid kind value 'promt'"):
        compile_query(ast, registry)
