"""Validate that every shipped query-language example parses and compiles.

The query examples that ship in the CLI ``--help`` text (Python string
constants assembled by :func:`agentgrep.build_description`) and in the
documentation are harvested here and run through the real query parser and
compiler. A stale field name, operator, or enum value in any example fails
loudly instead of shipping.

The documentation console examples are already executed by the
``pytest_documentation`` runner against a seeded sandbox, but the CLI help
examples are never executed — so without this tester a typo like
``agnet:codex`` in the help text would drift silently. The query string is
pulled from each example with the real argparse subparsers, so flag/value
separation matches the CLI exactly and cannot drift from it.
"""

from __future__ import annotations

import pathlib
import re
import shlex
import typing as t

import pytest

import agentgrep
from agentgrep.cli.parser import create_parser
from agentgrep.query import (
    compile_query,
    default_registry,
    parse_query,
    scope_widened_for_ast,
)

pytestmark = pytest.mark.documentation

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_QUERY_SUBCOMMANDS = frozenset({"search", "grep", "find"})
# An example command line, with the optional ``$ `` console prompt and an
# optional ``uv run `` prefix stripped from the capture group.
_COMMAND_LINE_RE = re.compile(r"^\s*(?:\$ )?((?:uv run )?agentgrep .+?)\s*$")


class QueryExample(t.NamedTuple):
    """One harvested example command and where it came from."""

    test_id: str
    source: str
    command: str


def _slug(text: str) -> str:
    """Return a readable, id-safe slug for a command string."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


def _command_lines(text: str) -> t.Iterator[str]:
    """Yield ``agentgrep ...`` command lines from help text or a transcript."""
    for line in text.splitlines():
        match = _COMMAND_LINE_RE.match(line)
        if match is not None:
            yield match.group(1)


def _harvest_cli_examples(seen: set[str]) -> list[QueryExample]:
    """Harvest example commands from the CLI ``--help`` description constants."""
    constants = {
        "cli": agentgrep.CLI_DESCRIPTION,
        "search": agentgrep.SEARCH_DESCRIPTION,
        "grep": agentgrep.GREP_DESCRIPTION,
        "find": agentgrep.FIND_DESCRIPTION,
    }
    examples: list[QueryExample] = []
    for name, text in constants.items():
        for command in _command_lines(text):
            if command in seen:
                continue
            seen.add(command)
            examples.append(
                QueryExample(
                    test_id=f"cli-{name}-{_slug(command)}",
                    source=f"cli:{name}",
                    command=command,
                ),
            )
    return examples


def _harvest_docs_examples(seen: set[str]) -> list[QueryExample]:
    """Harvest example commands from the documentation console blocks.

    Reuses the ``pytest_documentation`` collector so the markdown is parsed
    once, the same way the doc-test runner parses it. Console blocks that
    demonstrate a *rejected* invocation (their output contains ``error:``) are
    skipped — those exist to show the CLI refusing bad input, not to model a
    valid query.
    """
    from pytest_documentation import MarkdownFenceCollector, collect_examples

    pages = [_REPO_ROOT / "docs" / "library" / "query-language.md"]
    collected = collect_examples(
        pages,
        collectors=[MarkdownFenceCollector(languages={"console"})],
        project_root=_REPO_ROOT,
    )
    examples: list[QueryExample] = []
    for example in collected:
        source_text = example.source
        if "error:" in source_text.lower():
            continue
        for command in _command_lines(source_text):
            if command in seen:
                continue
            seen.add(command)
            examples.append(
                QueryExample(
                    test_id=f"docs-{_slug(command)}",
                    source="docs:query-language.md",
                    command=command,
                ),
            )
    return examples


def _harvest_query_examples() -> list[QueryExample]:
    """Harvest every query example from the CLI help and the documentation."""
    seen: set[str] = set()
    return _harvest_cli_examples(seen) + _harvest_docs_examples(seen)


def _extract_query(command: str) -> str | None:
    """Return the query string ``command`` would compile, or ``None``.

    Parses the command with the real argparse subparsers and reads the
    positional (``terms`` / ``patterns`` / ``pattern``), so flags and their
    values are dropped exactly as the CLI drops them. ``None`` means the
    command is not a query-bearing ``search`` / ``grep`` / ``find``.
    """
    tokens = shlex.split(command)
    if tokens[:2] == ["uv", "run"]:
        tokens = tokens[2:]
    if not tokens or tokens[0] != "agentgrep":
        return None
    argv = tokens[1:]
    if not argv or argv[0] not in _QUERY_SUBCOMMANDS:
        return None
    bundle = create_parser("never")
    try:
        namespace = bundle.parser.parse_args(argv)
    except SystemExit as exc:  # pragma: no cover - only on a broken example
        message = f"{command!r} is not a valid agentgrep invocation (argparse exit {exc.code})"
        raise AssertionError(message) from exc
    command_name = t.cast("str", namespace.command)
    if command_name == "search":
        positionals = t.cast("list[str]", namespace.terms)
    elif command_name == "grep":
        positionals = t.cast("list[str]", namespace.patterns)
    else:  # find
        raw_pattern = t.cast("str | None", namespace.pattern)
        positionals = [raw_pattern] if raw_pattern else []
    return " ".join(part for part in positionals if part)


_QUERY_EXAMPLES = _harvest_query_examples()
_MODEL_QUERY_EXAMPLES = tuple(
    example for example in _QUERY_EXAMPLES if "model:" in (_extract_query(example.command) or "")
)


def test_query_examples_were_harvested() -> None:
    """The harvest found a meaningful number of examples (guard against silent breakage)."""
    assert len(_QUERY_EXAMPLES) >= 10
    assert any(example.source.startswith("cli:") for example in _QUERY_EXAMPLES)
    assert any(example.source.startswith("docs:") for example in _QUERY_EXAMPLES)


@pytest.mark.parametrize(
    "example",
    _QUERY_EXAMPLES,
    ids=[example.test_id for example in _QUERY_EXAMPLES],
)
def test_query_example_parses_and_compiles(example: QueryExample) -> None:
    """Every shipped query example parses and compiles without error."""
    query = _extract_query(example.command)
    if not query:
        pytest.skip(f"{example.command!r} carries no query to validate")
    registry = default_registry()
    ast = parse_query(query, registry)
    compiled = compile_query(ast, registry)
    assert compiled is not None


@pytest.mark.parametrize(
    "example",
    _MODEL_QUERY_EXAMPLES,
    ids=[example.test_id for example in _MODEL_QUERY_EXAMPLES],
)
def test_model_examples_discover_conversations(example: QueryExample) -> None:
    """Shipped model examples widen the prompt-default discovery scope."""
    query = _extract_query(example.command)
    assert query is not None
    ast = parse_query(query, default_registry())
    assert scope_widened_for_ast(ast, "prompts") == "all"


def test_extract_query_reads_positional_dropping_flags() -> None:
    """``_extract_query`` recovers the query and drops flags + their values."""
    assert _extract_query("agentgrep search 'ruff OR uv'") == "ruff OR uv"
    assert _extract_query("agentgrep search --threshold 70 migration") == "migration"
    assert _extract_query("agentgrep grep -F --scope conversations TODO") == "TODO"
    assert _extract_query("agentgrep find sessions --agent codex") == "sessions"
    # Non-query commands carry nothing to validate.
    assert _extract_query("agentgrep ui bliss") is None


def test_query_tester_rejects_unknown_field() -> None:
    """A mistyped field in an example is caught (proves the tester has teeth)."""
    from agentgrep.query import QueryParseError

    query = _extract_query("agentgrep search 'agnet:codex'")
    assert query == "agnet:codex"
    with pytest.raises(QueryParseError):
        parse_query(query, default_registry())
