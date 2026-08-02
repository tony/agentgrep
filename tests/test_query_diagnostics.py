"""CLI surface for unregistered-field-predicate diagnostics (agentgrep#153).

``tests/test_query_gate.py`` proves the detection itself
(:func:`agentgrep._query_gate.unregistered_field_predicates`) and that a lone
unregistered predicate stays a literal search. This module proves that
diagnostic actually reaches the CLI's stderr, JSON, and NDJSON output — the
warning a user or script needs to notice a typo'd field name, rather than
silently searching for the wrong thing.
"""

from __future__ import annotations

import json

import pytest

from agentgrep import FindArgs, GrepArgs, SearchArgs, parse_args
from agentgrep.cli.render import run_find_command, run_grep_command, run_search_command

_WARNING_TEXT = (
    "'bogusfield:xyz' looks like a field predicate, but 'bogusfield' is not "
    "a registered query field; searching for the literal text instead"
)


def _run_search(argv: list[str]) -> None:
    """Parse ``argv`` and run it as ``agentgrep search``."""
    args = parse_args(argv)
    assert isinstance(args, SearchArgs)
    run_search_command(args)


def _run_grep(argv: list[str]) -> None:
    """Parse ``argv`` and run it as ``agentgrep grep``."""
    args = parse_args(argv)
    assert isinstance(args, GrepArgs)
    run_grep_command(args)


def _run_find(argv: list[str]) -> None:
    """Parse ``argv`` and run it as ``agentgrep find``."""
    args = parse_args(argv)
    assert isinstance(args, FindArgs)
    run_find_command(args)


@pytest.mark.parametrize(
    ("command", "argv"),
    [
        ("search", ["search", "bogusfield:xyz"]),
        ("grep", ["grep", "bogusfield:xyz"]),
        ("find", ["find", "bogusfield:xyz"]),
    ],
)
def test_stderr_warns_for_an_unregistered_field_predicate(
    command: str,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every non-``--ui`` command prints the same warning shape on stderr."""
    {"search": _run_search, "grep": _run_grep, "find": _run_find}[command](argv)

    assert f"warning: {_WARNING_TEXT}" in capsys.readouterr().err


def test_search_json_carries_a_warnings_key(capsys: pytest.CaptureFixture[str]) -> None:
    """The JSON envelope's ``warnings`` key matches the CLI serializer shape."""
    _run_search(["search", "bogusfield:xyz", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"] == [
        {
            "code": "unregistered_field_predicate",
            "field": "bogusfield",
            "token": "bogusfield:xyz",
            "suggestion": None,
            "message": _WARNING_TEXT,
        },
    ]


def test_search_ndjson_summary_line_carries_warnings(capsys: pytest.CaptureFixture[str]) -> None:
    """The terminal NDJSON ``summary`` event also carries the warning."""
    _run_search(["search", "bogusfield:xyz", "--ndjson"])

    lines = capsys.readouterr().out.strip().splitlines()
    summary = json.loads(lines[-1])
    assert summary["type"] == "summary"
    assert summary["warnings"][0]["field"] == "bogusfield"


def test_grep_json_carries_a_warnings_key(capsys: pytest.CaptureFixture[str]) -> None:
    """``grep --json`` carries the same warning shape as ``search``."""
    _run_grep(["grep", "bogusfield:xyz", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"][0]["field"] == "bogusfield"


def test_find_json_carries_a_warnings_key(capsys: pytest.CaptureFixture[str]) -> None:
    """``find --json`` carries the same warning shape as ``search``."""
    _run_find(["find", "bogusfield:xyz", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"][0]["field"] == "bogusfield"


def test_a_typo_close_to_a_real_field_gets_a_suggestion(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A near-miss typo is pointed at the field it probably meant."""
    _run_search(["search", "agnet:codex"])

    assert "(did you mean 'agent'?)" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["search", "Note: fix this"],
        ["search", "https://example.com"],
    ],
)
def test_no_warning_for_tokens_the_gate_excludes(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Prose and URLs never trip the warning — see ``test_query_gate.py``."""
    _run_search(argv)

    assert "warning:" not in capsys.readouterr().err


def test_registered_field_predicate_gets_no_warning(capsys: pytest.CaptureFixture[str]) -> None:
    """A real, registered field predicate needs no diagnostic."""
    _run_search(["search", "kind:prompt"])

    assert "warning:" not in capsys.readouterr().err
