# ruff: noqa: D102, D103
"""Functional tests for the ``agentgrep`` CLI package."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import inspect
import io
import itertools
import json
import os
import pathlib
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import typing as t
import urllib.parse

import pydantic
import pytest

import agentgrep as _agentgrep_module
import agentgrep._engine.orchestration as _rm_orch
import agentgrep._engine.planning as _rm_planning
import agentgrep._engine.scanning as _rm_scanning
import agentgrep.readers as _rm_readers
from agentgrep._engine import orchestration
from agentgrep.records import RecordOrigin, SourceOriginSummary
from agentgrep.store_catalog import CATALOG

if t.TYPE_CHECKING:
    import collections.abc as cabc

pytestmark = pytest.mark.legacy

AgentName = t.Literal[
    "codex",
    "claude",
    "cursor-cli",
    "cursor-ide",
    "gemini",
    "antigravity-cli",
    "antigravity-ide",
    "grok",
    "pi",
    "opencode",
]
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class BackendSelectionLike(t.Protocol):
    """Structural type for backend selection values."""

    find_tool: str | None
    grep_tool: str | None
    json_tool: str | None


class SearchRecordLike(t.Protocol):
    """Structural type for search results used in tests."""

    kind: str
    agent: str
    text: str
    timestamp: str | None
    session_id: str | None
    conversation_id: str | None
    model: str | None
    path: pathlib.Path


class FindRecordLike(t.Protocol):
    """Structural type for find results used in tests."""

    agent: str
    path: pathlib.Path


class SourceHandleLike(t.Protocol):
    """Structural type for discovered sources used in tests."""

    path: pathlib.Path


class SearchQueryFactory(t.Protocol):
    """Factory protocol for query construction."""

    def __call__(
        self,
        *,
        terms: tuple[str, ...],
        scope: str,
        any_term: bool,
        regex: bool,
        case_sensitive: bool,
        agents: tuple[AgentName, ...],
        limit: int | None,
        match_surface: str = ...,
    ) -> object: ...


class SearchRecordFactory(t.Protocol):
    """Factory protocol for constructing search records."""

    def __call__(
        self,
        *,
        kind: str,
        agent: str,
        store: str,
        adapter_id: str,
        path: pathlib.Path,
        text: str,
    ) -> SearchRecordLike: ...


class BackendSelectionFactory(t.Protocol):
    """Factory protocol for backend selection construction."""

    def __call__(
        self,
        find_tool: str | None,
        grep_tool: str | None,
        json_tool: str | None,
    ) -> BackendSelectionLike: ...


class ShutilLike(t.Protocol):
    """Minimal shutil surface used by tests."""

    def which(self, name: str) -> str | None: ...


class ImportlibLike(t.Protocol):
    """Minimal importlib surface used by tests."""

    def import_module(self, name: str) -> object: ...


class AgentGrepModule(t.Protocol):
    """Structural type for the loaded standalone module."""

    shutil: ShutilLike
    importlib: ImportlibLike
    SearchQuery: SearchQueryFactory
    SearchRecord: SearchRecordFactory
    BackendSelection: BackendSelectionFactory

    def select_backends(self) -> BackendSelectionLike: ...

    def discover_sources(
        self,
        home: pathlib.Path,
        agents: tuple[AgentName, ...],
        backends: BackendSelectionLike,
    ) -> list[SourceHandleLike]: ...

    def search_sources(
        self,
        query: object,
        sources: cabc.Sequence[SourceHandleLike],
        backends: BackendSelectionLike,
    ) -> list[SearchRecordLike]: ...

    def run_search_query(
        self,
        home: pathlib.Path,
        query: object,
        *,
        backends: BackendSelectionLike | None = None,
        progress: object | None = None,
    ) -> list[SearchRecordLike]: ...

    def plan_search_sources(
        self,
        query: object,
        sources: list[SourceHandleLike],
        backends: BackendSelectionLike,
    ) -> list[SourceHandleLike]: ...

    def find_sources(
        self,
        pattern: str | None,
        sources: cabc.Sequence[SourceHandleLike],
        limit: int | None,
    ) -> list[FindRecordLike]: ...

    def parse_args(self, argv: cabc.Sequence[str] | None = None) -> object | None: ...

    def build_docs_parser(self) -> argparse.ArgumentParser: ...


def load_agentgrep_module() -> AgentGrepModule:
    """Return the installed ``agentgrep`` package."""
    return t.cast("AgentGrepModule", t.cast("object", _agentgrep_module))


def write_jsonl(path: pathlib.Path, rows: cabc.Sequence[object]) -> None:
    """Write JSONL rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def strip_ansi(text: str) -> str:
    """Return ``text`` without ANSI escape sequences."""
    return ANSI_RE.sub("", text)


def run_agentgrep_cli(
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the installed CLI in a subprocess via ``python -m agentgrep``."""
    command = [sys.executable, "-m", "agentgrep", *args]
    merged_env = os.environ.copy()
    if env is not None:
        merged_env.update(env)
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=merged_env,
    )


def test_select_backends_prefers_first_available(monkeypatch: pytest.MonkeyPatch) -> None:
    agentgrep = load_agentgrep_module()

    def fake_which(name: str) -> str | None:
        mapping = {
            "fd": "/tmp/fd",
            "rg": "/tmp/rg",
            "jq": "/tmp/jq",
        }
        return mapping.get(name)

    monkeypatch.setattr(agentgrep.shutil, "which", fake_which)
    backends = agentgrep.select_backends()

    assert backends.find_tool == "/tmp/fd"
    assert backends.grep_tool == "/tmp/rg"
    assert backends.json_tool == "/tmp/jq"


def test_list_files_matching_ignores_gitignore(tmp_path: pathlib.Path) -> None:
    """Agent stores under ``$HOME`` must be discovered through ``.gitignore``.

    Dotfile-managed setups whose root has ``.gitignore: *`` would otherwise
    silently mask every session file.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    _ = (tmp_path / ".gitignore").write_text("*\n!keep.json\n", encoding="utf-8")
    for name in ("a.jsonl", "b.jsonl"):
        _ = (tmp_path / name).write_text("{}", encoding="utf-8")

    backends = agentgrep.select_backends()

    paths = agentgrep.list_files_matching(tmp_path, "*.jsonl", backends.find_tool)

    assert {p.name for p in paths} == {"a.jsonl", "b.jsonl"}


class PathGlobCase(t.NamedTuple):
    """One path-qualified glob shape for source discovery."""

    test_id: str
    pattern: str
    files: tuple[str, ...]
    expected: tuple[str, ...]


PATH_GLOB_CASES: tuple[PathGlobCase, ...] = (
    PathGlobCase(
        test_id="cursor-workspace-state",
        pattern="*/state.vscdb",
        files=("project/state.vscdb", "project/nested/state.vscdb"),
        expected=("project/state.vscdb",),
    ),
    PathGlobCase(
        test_id="cursor-cli-store-db",
        pattern="*/*/store.db",
        files=("scope/thread/store.db", "scope/store.db", "scope/thread/nested/store.db"),
        expected=("scope/thread/store.db",),
    ),
)


@pytest.mark.parametrize(
    "case",
    PATH_GLOB_CASES,
    ids=[case.test_id for case in PATH_GLOB_CASES],
)
def test_list_files_matching_path_qualified_globs_skip_fd(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: PathGlobCase,
) -> None:
    """Path-qualified discovery globs use bounded relative matching."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    for relative_path in case.files:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("{}", encoding="utf-8")

    def run_readonly_command(_command: list[str]) -> subprocess.CompletedProcess[str]:
        message = "path-qualified globs should not spawn fd"
        raise AssertionError(message)

    monkeypatch.setattr(_rm_orch, "run_readonly_command", run_readonly_command)

    paths = agentgrep.list_files_matching(tmp_path, case.pattern, "fd")

    assert tuple(str(path.relative_to(tmp_path)) for path in paths) == case.expected


def test_cli_without_subcommand_prints_main_help() -> None:
    completed = run_agentgrep_cli()

    assert completed.returncode == 0
    assert "usage: agentgrep" in completed.stdout
    assert "find examples:" in completed.stdout


def test_find_without_pattern_lists_every_source(tmp_path: pathlib.Path) -> None:
    """``agentgrep find`` with no pattern lists every discovered source (fd parity)."""
    session_dir = tmp_path / ".codex" / "sessions" / "2026" / "05"
    session_dir.mkdir(parents=True)
    (session_dir / "alpha.jsonl").write_text(
        '{"type":"response_item","payload":{"role":"user","content":"hi"}}\n',
    )
    (session_dir / "beta.jsonl").write_text(
        '{"type":"response_item","payload":{"role":"user","content":"hi"}}\n',
    )
    completed = run_agentgrep_cli("find", "--no-progress", env={"HOME": str(tmp_path)})

    assert completed.returncode == 0
    assert "alpha.jsonl" in completed.stdout
    assert "beta.jsonl" in completed.stdout
    # No help banner.
    assert "usage: agentgrep find" not in completed.stdout


class FindRejectCase(t.NamedTuple):
    """A find query that cannot be faithfully evaluated, and an error fragment."""

    test_id: str
    argv: tuple[str, ...]
    fragment: str


FIND_REJECT_CASES: tuple[FindRejectCase, ...] = (
    FindRejectCase("boolean-or-text", ("find", "codex OR claude"), "OR / NOT over text"),
    FindRejectCase("not-text", ("find", "NOT codex"), "OR / NOT over text"),
    FindRejectCase("record-field-model", ("find", "model:gpt*"), "model: field filters records"),
    FindRejectCase(
        "record-field-scope",
        ("find", "scope:conversations"),
        "scope: field filters records",
    ),
)


@pytest.mark.parametrize("case", FIND_REJECT_CASES, ids=[c.test_id for c in FIND_REJECT_CASES])
def test_find_rejects_unevaluable_query(
    case: FindRejectCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``find`` errors (exit 2) on queries it cannot honor, instead of mis-searching."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    with pytest.raises(SystemExit) as exc_info:
        agentgrep.parse_args(case.argv)

    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert case.fragment in err
    assert "Traceback" not in err


def test_find_allows_source_predicate_with_text() -> None:
    """``find`` still accepts source-level predicates plus a flat text pattern."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    parsed = agentgrep.parse_args(("find", "agent:codex bliss"))

    assert isinstance(parsed, agentgrep.FindArgs)


def test_help_examples_are_present_for_help_flags() -> None:
    root_help = run_agentgrep_cli("--help")
    find_help = run_agentgrep_cli("find", "--help")

    assert root_help.returncode == 0
    assert find_help.returncode == 0
    assert "agentgrep find cursor-cli --json" in find_help.stdout


def test_query_language_examples_present_in_search_and_grep_help() -> None:
    """search/grep help advertises the query language so it is discoverable."""
    search_help = run_agentgrep_cli("search", "--help")
    grep_help = run_agentgrep_cli("grep", "--help")

    assert search_help.returncode == 0
    assert grep_help.returncode == 0
    assert "query language examples:" in search_help.stdout
    assert "agent:codex" in search_help.stdout
    assert "query language examples:" in grep_help.stdout


def test_bare_search_prints_help() -> None:
    """``agentgrep search`` with no terms shows help+examples, not a full-store dump."""
    completed = run_agentgrep_cli("search")

    assert completed.returncode == 0
    assert "examples:" in completed.stdout
    assert "query language examples:" in completed.stdout
    assert "agentgrep search 'ruff OR uv'" in completed.stdout


def test_parse_args_bare_search_returns_none_and_prints_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``parse_args(["search"])`` prints the search help and returns None."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    args = agentgrep.parse_args(["search"])

    assert args is None
    captured = capsys.readouterr().out
    assert "examples:" in captured
    assert "agentgrep search 'ruff OR uv'" in captured


def test_parse_args_bare_search_with_ui_does_not_print_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``agentgrep search --ui`` keeps launching the explorer; no help banner."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    args = agentgrep.parse_args(["search", "--ui"])

    assert isinstance(args, agentgrep.SearchArgs)
    captured = capsys.readouterr().out
    assert "examples:" not in captured


def test_colorize_inline_code_strips_backticks_without_theme() -> None:
    """RST ``code`` spans lose their backticks even with no theme bound."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    out = agentgrep.AgentGrepHelpFormatter._colorize_inline_code(
        "pick ``search`` or ``grep``",
        theme=None,
    )

    assert out == "pick search or grep"
    assert "`" not in out


def test_colorize_inline_code_colors_with_theme() -> None:
    """With a theme, ``code`` spans are colored and the backticks removed."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    theme = agentgrep.AnsiHelpTheme.default()

    out = agentgrep.AgentGrepHelpFormatter._colorize_inline_code(
        "pick ``search``",
        theme=theme,
    )

    assert "``" not in out
    assert f"{theme.inline_code}search{theme.reset}" in out


def test_help_has_no_literal_double_backticks() -> None:
    """Help output strips RST inline-code backticks on every surface."""
    for argv in (["--help"], ["grep", "--help"], ["search", "--help"]):
        completed = run_agentgrep_cli(*argv)
        assert completed.returncode == 0
        assert "``" not in completed.stdout


def test_help_colorizes_query_language_tokens() -> None:
    """Forced-color search help highlights query tokens down to their parts."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    theme = agentgrep.AnsiHelpTheme.default()
    completed = run_agentgrep_cli(
        "--color",
        "always",
        "search",
        "--help",
        env={"FORCE_COLOR": "1", "NO_COLOR": ""},
    )

    assert completed.returncode == 0
    out = completed.stdout
    # The bare boolean OR in `agentgrep search 'ruff OR uv'` is keyword-colored.
    assert f"{theme.query_keyword}OR{theme.reset}" in out
    # `model:gpt*` splits into field / colon / value / wildcard spans.
    assert f"{theme.query_field}model{theme.reset}" in out
    assert f"{theme.query_punct}:{theme.reset}" in out
    assert f"{theme.query_wildcard}*{theme.reset}" in out
    # Inline-code in the description renders with the inline_code color.
    assert theme.inline_code in out


def test_colorize_query_argument_splits_field_colon_value_wildcard() -> None:
    """A quoted `field:value*` arg is colored field / colon / value / wildcard."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    theme = agentgrep.AnsiHelpTheme.default()

    out = agentgrep.AgentGrepHelpFormatter._colorize_query_argument(
        "'model:gpt*'",
        theme=theme,
    )

    assert out.startswith("'") and out.endswith("'")  # outer shell quotes stay plain
    assert f"{theme.query_field}model{theme.reset}" in out
    assert f"{theme.query_punct}:{theme.reset}" in out
    assert f"{theme.query_value}gpt{theme.reset}" in out
    assert f"{theme.query_wildcard}*{theme.reset}" in out


def test_colorize_query_expression_comparison_and_negation() -> None:
    """Comparison ops use the operator color; the `-` sigil uses the negation color."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    theme = agentgrep.AnsiHelpTheme.default()

    ts = agentgrep.AgentGrepHelpFormatter._colorize_query_expression(
        "timestamp:>2026-01-01",
        theme=theme,
    )
    assert f"{theme.query_field}timestamp{theme.reset}" in ts
    assert f"{theme.query_punct}:{theme.reset}" in ts
    assert f"{theme.query_operator}>{theme.reset}" in ts
    assert f"{theme.query_value}2026-01-01{theme.reset}" in ts

    neg = agentgrep.AgentGrepHelpFormatter._colorize_query_expression(
        "-agent:cursor-cli",
        theme=theme,
    )
    assert neg.startswith(f"{theme.query_negation}-{theme.reset}")
    assert f"{theme.query_field}agent{theme.reset}" in neg


def test_colorize_query_argument_keyword_and_bare_term() -> None:
    """Boolean keywords are keyword-colored; bare terms get the value color."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    theme = agentgrep.AnsiHelpTheme.default()

    out = agentgrep.AgentGrepHelpFormatter._colorize_query_argument(
        "'ruff OR uv'",
        theme=theme,
    )
    assert f"{theme.query_keyword}OR{theme.reset}" in out
    assert f"{theme.query_value}ruff{theme.reset}" in out
    assert f"{theme.query_value}uv{theme.reset}" in out


def test_build_docs_parser_returns_root_parser() -> None:
    """Adapter for ``sphinx-autodoc-argparse`` exposes the root parser."""
    agentgrep = load_agentgrep_module()
    parser = agentgrep.build_docs_parser()

    assert isinstance(parser, argparse.ArgumentParser)
    assert parser.prog == "agentgrep"


def test_parse_args_ui_subcommand_returns_ui_args() -> None:
    """``agentgrep ui`` parses to a ``UIArgs`` with empty initial query."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    args = agentgrep.parse_args(["ui"])

    assert isinstance(args, agentgrep.UIArgs)
    assert args.initial_query == ""


def test_parse_args_ui_subcommand_with_initial_query() -> None:
    """``agentgrep ui bliss`` populates ``initial_query``."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    args = agentgrep.parse_args(["ui", "bliss"])

    assert isinstance(args, agentgrep.UIArgs)
    assert args.initial_query == "bliss"


def test_parse_args_empty_argv_returns_none_and_prints_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``parse_args([])`` prints the directory-of-choices help and returns None."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    args = agentgrep.parse_args([])

    assert args is None
    captured = capsys.readouterr().out
    assert "agentgrep" in captured
    assert "{grep,search,find,ui}" in captured or "grep" in captured


def test_main_with_empty_argv_prints_root_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main([])`` prints the themed directory of choices and exits 0.

    The vcspull/tmuxp-style banner must surface every subcommand's
    example block — assert on the stable per-block headers rather than
    on the full rendered text so wording tweaks don't churn this test.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    exit_code = agentgrep.main([])

    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "grep examples:" in captured
    assert "find examples:" in captured
    assert "ui examples:" in captured


def test_main_with_unknown_positional_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main(['bliss'])`` exits 2 with argparse 'invalid choice' (vcspull parity).

    Locks in the deliberate removal of the implicit-search shorthand:
    ``agentgrep bliss`` no longer becomes ``agentgrep search bliss``.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.main(["bliss"])

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "invalid choice" in captured.err
    assert "bliss" in captured.err


def test_root_help_not_rewritten_by_default_verb() -> None:
    completed = run_agentgrep_cli("--help")

    assert completed.returncode == 0
    assert "find examples:" in completed.stdout


def test_force_color_colorizes_help_output() -> None:
    completed = run_agentgrep_cli(
        "--color",
        "always",
        "find",
        "--help",
        env={"FORCE_COLOR": "1", "NO_COLOR": ""},
    )

    assert completed.returncode == 0
    assert "\x1b[" in completed.stdout


def test_no_color_overrides_color_always() -> None:
    completed = run_agentgrep_cli(
        "--color",
        "always",
        "find",
        "--help",
        env={"NO_COLOR": "1"},
    )

    assert completed.returncode == 0
    assert "\x1b[" not in completed.stdout


def test_search_codex_prompt_match_returns_full_prompt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "rollout.jsonl"
    write_jsonl(
        session_path,
        [
            {
                "type": "session_meta",
                "payload": {"id": "session-1", "model_provider": "openai"},
            },
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "A serenity prompt with bliss and detail.",
                        },
                    ],
                },
            },
            {
                "timestamp": "2026-01-01T00:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Assistant reply"}],
                },
            },
        ],
    )

    query = agentgrep.SearchQuery(
        terms=("serenity", "bliss"),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    sources = agentgrep.discover_sources(
        home,
        ("codex",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 1
    assert records[0].kind == "prompt"
    assert records[0].text == "A serenity prompt with bliss and detail."
    assert records[0].session_id == "session-1"


def test_limited_codex_session_search_preserves_session_model(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Limited searches keep the session_meta model on Codex records.

    Regression guard: bounded newest-first scans would read the trailing
    records before the leading ``session_meta`` line, so limited haystack
    searches for a model name returned no Codex records at all.
    """
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "rollout.jsonl"
    write_jsonl(
        session_path,
        [
            {
                "type": "session_meta",
                "payload": {"id": "session-1", "model": "gpt-test-o5"},
            },
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "first prompt"}],
                },
            },
            {
                "timestamp": "2026-01-01T00:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "second prompt"}],
                },
            },
        ],
    )

    query = agentgrep.SearchQuery(
        terms=("gpt-test-o5",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=1,
    )
    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_sources(home, ("codex",), backends)
    records = agentgrep.search_sources(query, sources, backends)

    assert len(records) == 1
    assert records[0].model == "gpt-test-o5"


def test_limited_pi_session_search_preserves_conversation_id(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Limited searches keep the session-header cwd on pi records.

    Regression guard: bounded newest-first scans would read message lines
    before the leading ``session`` header, dropping ``conversation_id``.
    """
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    session_file = home / ".pi" / "agent" / "sessions" / "--home-user-proj--" / "sess.jsonl"
    write_jsonl(
        session_file,
        [
            _pi_session_header(cwd="/home/user/proj"),
            {
                "type": "message",
                "id": "u1",
                "parentId": None,
                "timestamp": "2026-05-30T12:00:02.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "explain the streaming design"}],
                    "timestamp": 1780228802000,
                },
            },
        ],
    )

    query = agentgrep.SearchQuery(
        terms=("streaming",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("pi",),
        limit=1,
    )
    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_sources(home, ("pi",), backends)
    records = agentgrep.search_sources(query, sources, backends)

    assert len(records) == 1
    assert records[0].conversation_id == "/home/user/proj"


def test_text_search_codex_session_preserves_session_metadata(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text-surface searches keep canonical Codex session metadata.

    Regression guard: the raw text prefilter dropped the session_meta
    header before decode, so grep-style matches carried model=None and the
    file stem instead of the canonical session id.
    """
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "rollout-abc.jsonl"
    write_jsonl(
        session_path,
        [
            {
                "type": "session_meta",
                "payload": {"id": "canonical-session-id", "model": "gpt-test-o5"},
            },
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "needle prompt"}],
                },
            },
        ],
    )

    query = agentgrep.SearchQuery(
        terms=("needle",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
        match_surface="text",
    )
    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_sources(home, ("codex",), backends)
    records = agentgrep.search_sources(query, sources, backends)

    assert len(records) == 1
    assert records[0].model == "gpt-test-o5"
    assert records[0].session_id == "canonical-session-id"


def test_text_search_pi_session_preserves_conversation_id(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text-surface searches keep canonical pi session metadata.

    Regression guard: the raw text prefilter dropped the session header
    before decode, so grep-style matches lost the cwd and carried the file
    stem instead of the canonical session id.
    """
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    session_file = home / ".pi" / "agent" / "sessions" / "--home-user-proj--" / "sess.jsonl"
    write_jsonl(
        session_file,
        [
            _pi_session_header(cwd="/home/user/proj"),
            {
                "type": "message",
                "id": "u1",
                "parentId": None,
                "timestamp": "2026-05-30T12:00:02.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "needle prompt"}],
                    "timestamp": 1780228802000,
                },
            },
        ],
    )

    query = agentgrep.SearchQuery(
        terms=("needle",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("pi",),
        limit=None,
        match_surface="text",
    )
    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_sources(home, ("pi",), backends)
    records = agentgrep.search_sources(query, sources, backends)

    assert len(records) == 1
    assert records[0].session_id == "019e0000-0000-7000-8000-000000000abc"
    assert records[0].conversation_id == "/home/user/proj"


def test_unbounded_haystack_search_finds_path_only_matches(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Project-name searches find conversations whose content lacks the term.

    Regression guard: content-only root prefiltering dropped sources whose
    haystack match lived in the file path, so unlimited searches for a
    project directory name returned nothing.
    """
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    session_path = home / ".claude" / "projects" / "-home-user-tmux-proj" / "session-1.jsonl"
    write_jsonl(
        session_path,
        [
            {
                "type": "user",
                "sessionId": "session-1",
                "message": {"role": "user", "content": "unrelated words only"},
            },
        ],
    )

    def grep_root_paths(
        _root: pathlib.Path,
        _query: t.Any,
        _grep_program: str,
        *,
        control: t.Any = None,
    ) -> set[pathlib.Path]:
        _ = control
        return set()

    monkeypatch.setattr(_rm_orch, "grep_root_paths", grep_root_paths)

    query = agentgrep.SearchQuery(
        terms=("tmux-proj",),
        scope="conversations",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("claude",),
        limit=None,
        match_surface="haystack",
    )
    backends = agentgrep.BackendSelection(None, "rg", None)
    sources = agentgrep.discover_sources(home, ("claude",), backends)
    records = agentgrep.search_sources(query, sources, backends)

    assert [record.text for record in records] == ["unrelated words only"]


def test_search_reports_source_and_match_progress(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    first = home / ".codex" / "sessions" / "2026" / "01" / "01" / "first.jsonl"
    second = home / ".codex" / "sessions" / "2026" / "01" / "01" / "second.jsonl"
    write_jsonl(
        first,
        [{"type": "response_item", "payload": {"role": "user", "content": "bliss"}}],
    )
    write_jsonl(
        second,
        [{"type": "response_item", "payload": {"role": "user", "content": "other"}}],
    )

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    class RecordingProgress:
        def __init__(self) -> None:
            self.events: list[tuple[str, int | str]] = []

        def start(self, query: object) -> None:
            self.events.append(("start", 0))

        def sources_discovered(self, count: int) -> None:
            self.events.append(("discovered", count))

        def prefilter_started(self, root: pathlib.Path) -> None:
            self.events.append(("prefilter", root.name))

        def sources_planned(self, planned: int, total: int) -> None:
            self.events.append(("planned", planned))
            self.events.append(("total", total))

        def source_started(self, index: int, total: int, source: object) -> None:
            self.events.append(("source_started", index))

        def source_finished(
            self,
            index: int,
            total: int,
            source: object,
            records: int,
            matches: int,
        ) -> None:
            self.events.append(("source_finished", matches))

        def result_added(self, count: int) -> None:
            self.events.append(("result_added", count))

        def record_added(self, record: object) -> None:
            self.events.append(("record_added", getattr(record, "kind", "?")))

        def finish(self, result_count: int) -> None:
            self.events.append(("finish", result_count))

        def close(self) -> None:
            self.events.append(("close", 0))

    progress = RecordingProgress()
    records = agentgrep.run_search_query(
        home,
        query,
        backends=agentgrep.BackendSelection(None, None, None),
        progress=progress,
    )

    assert len(records) == 1
    assert ("discovered", 2) in progress.events
    assert ("planned", 2) in progress.events
    assert ("source_started", 1) in progress.events
    assert ("source_started", 2) in progress.events
    assert ("source_finished", 1) in progress.events
    assert ("result_added", 1) in progress.events
    assert progress.events[-2:] == [("finish", 1), ("close", 0)]


def test_collect_search_records_calls_record_added_with_each_unique_record(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "session.jsonl",
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=source.path,
        text="bliss",
        session_id="abc",
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    class CapturingProgress:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.counts: list[int] = []

        def start(self, query: object) -> None: ...
        def sources_discovered(self, count: int) -> None: ...
        def prefilter_started(self, root: pathlib.Path) -> None: ...
        def sources_planned(self, planned: int, total: int) -> None: ...
        def source_started(self, index: int, total: int, source: object) -> None: ...
        def source_finished(
            self,
            index: int,
            total: int,
            source: object,
            records: int,
            matches: int,
        ) -> None: ...
        def result_added(self, count: int) -> None:
            self.counts.append(count)

        def record_added(self, record: object) -> None:
            self.added.append(record)

        def finish(self, result_count: int) -> None: ...
        def close(self) -> None: ...

    def iter_records(source: object) -> cabc.Iterator[object]:
        yield record
        yield record  # same dedupe key — second insert must not fire record_added

    monkeypatch.setattr(_rm_scanning, "iter_source_records", iter_records)
    progress = CapturingProgress()

    records = agentgrep.collect_search_records(query, [source], progress=progress)

    assert records == [record]
    assert progress.added == [record]
    assert progress.counts == [1]


def test_collect_search_records_reports_in_source_progress_and_yields_gil(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large source scans report parser progress and cooperatively yield."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "session.jsonl",
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
        dedupe=False,
    )

    class CapturingProgress:
        def __init__(self) -> None:
            self.source_progress_events: list[tuple[int, int, int, int]] = []

        def source_started(self, index: int, total: int, source: object) -> None: ...
        def source_finished(
            self,
            index: int,
            total: int,
            source: object,
            records: int,
            matches: int,
        ) -> None: ...
        def result_added(self, count: int) -> None: ...
        def record_added(self, record: object) -> None: ...

        def source_progress(
            self,
            index: int,
            total: int,
            source: object,
            records: int,
            matches: int,
        ) -> None:
            self.source_progress_events.append((index, total, records, matches))

    def iter_records(source: object) -> cabc.Iterator[object]:
        for index in range(agentgrep._SOURCE_PROGRESS_RECORD_INTERVAL + 1):
            yield agentgrep.SearchRecord(
                kind="prompt",
                agent="codex",
                store="codex.sessions",
                adapter_id="codex.sessions_jsonl.v1",
                path=tmp_path / "session.jsonl",
                text=f"bliss {index}",
            )

    sleep_calls: list[float] = []
    monkeypatch.setattr(_rm_scanning, "iter_source_records", iter_records)
    monkeypatch.setattr(agentgrep.time, "sleep", sleep_calls.append)
    progress = CapturingProgress()

    _ = agentgrep.collect_search_records(query, [source], progress=progress)

    assert progress.source_progress_events == [
        (
            1,
            1,
            agentgrep._SOURCE_PROGRESS_RECORD_INTERVAL,
            agentgrep._SOURCE_PROGRESS_RECORD_INTERVAL,
        ),
    ]
    assert sleep_calls == [0]


def test_iter_jsonl_cooperatively_yields_during_large_files(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSONL parsing yields cooperatively once the wall-clock interval elapses."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "events.jsonl"
    line_count = 300
    lines = [json.dumps({"type": "noise", "index": index}) for index in range(line_count)]
    path.write_text("\n".join(lines), encoding="utf-8")
    # Advance the clock past the yield interval on every read so each line
    # crosses the wall-clock deadline and yields.
    ticks = itertools.count(0.0, agentgrep._JSONL_YIELD_INTERVAL_SECONDS * 2)
    monkeypatch.setattr(agentgrep.time, "perf_counter", lambda: next(ticks))
    sleep_calls: list[float] = []
    monkeypatch.setattr(agentgrep.time, "sleep", sleep_calls.append)

    parsed = list(agentgrep.iter_jsonl(path))

    assert len(parsed) == line_count
    assert sleep_calls  # cooperative yields fired as wall time advanced
    assert set(sleep_calls) == {0}


class PeriodicYieldCase(t.NamedTuple):
    """One :class:`agentgrep._PeriodicYield` gating scenario."""

    test_id: str
    perf_values: tuple[float, ...]
    """``perf_counter`` returns: index 0 seeds the deadline, the rest drive calls."""
    expected_sleeps: int


_PERIODIC_YIELD_CASES = (
    PeriodicYieldCase("within_interval_never_yields", (0.0, 0.001, 0.002, 0.009), 0),
    PeriodicYieldCase("each_call_past_interval_yields", (0.0, 0.02, 0.04, 0.06), 3),
    PeriodicYieldCase("yields_only_after_interval", (0.0, 0.005, 0.011, 0.012, 0.025), 2),
)


@pytest.mark.parametrize("case", _PERIODIC_YIELD_CASES, ids=lambda case: case.test_id)
def test_periodic_yield_gates_on_wall_clock(
    case: PeriodicYieldCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_PeriodicYield`` sleeps only when the wall-clock interval has elapsed."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    ticks = iter(case.perf_values)
    monkeypatch.setattr(agentgrep.time, "perf_counter", lambda: next(ticks))
    sleep_calls: list[float] = []
    monkeypatch.setattr(agentgrep.time, "sleep", sleep_calls.append)

    yield_now = agentgrep._PeriodicYield()
    for _ in case.perf_values[1:]:
        yield_now()

    assert len(sleep_calls) == case.expected_sleeps
    assert set(sleep_calls) <= {0}


@pytest.fixture(params=["accelerated", "stdlib"])
def loads_impl(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> t.Callable[[str], object]:
    """Yield ``_loads`` under both the orjson and forced-stdlib paths.

    The ``stdlib`` param forces ``_orjson`` absent so the pure-Python
    fallback runs even where orjson is installed; ``accelerated`` skips when
    orjson is missing. Mirrors the shared-implementation fixture in ADR 0002.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    if request.param == "stdlib":
        monkeypatch.setattr(_rm_readers, "_orjson", None)
    elif agentgrep._orjson is None:
        pytest.skip("orjson accelerator is not installed")
    return t.cast("t.Callable[[str], object]", agentgrep._loads)


class LoadsCase(t.NamedTuple):
    """One ``_loads`` decode input shared by both implementations."""

    test_id: str
    text: str


_LOADS_CASES = (
    LoadsCase("object", '{"a": 1, "b": [2, 3]}'),
    LoadsCase("array", "[1, 2, 3]"),
    LoadsCase("string_with_escape", '"hello \\u00e9 world"'),
    LoadsCase("nested", '{"x": {"y": [true, false, null]}}'),
    LoadsCase("unicode", '{"emoji": "\U0001f3af", "accent": "café"}'),
    LoadsCase("float", '{"pi": 3.14159, "t": -273.15}'),
    LoadsCase("scalar_int", "42"),
    LoadsCase("scalar_null", "null"),
    LoadsCase("large_int_within_64bit", "9007199254740993"),
    # orjson rejects NaN/Infinity (stdlib json — and thus Python's json.dumps
    # — accepts them); _loads falls back to stdlib so both paths agree.
    LoadsCase("positive_infinity", "Infinity"),
    LoadsCase("negative_infinity", "-Infinity"),
)


@pytest.mark.parametrize("case", _LOADS_CASES, ids=lambda case: case.test_id)
def test_loads_matches_stdlib_json(
    case: LoadsCase,
    loads_impl: t.Callable[[str], object],
) -> None:
    """``_loads`` returns the same value as ``json.loads`` on both paths."""
    assert loads_impl(case.text) == json.loads(case.text)


@pytest.mark.parametrize(
    "bad",
    ['{"a":}', "not json", "{unterminated", ""],
    ids=["bad_value", "bare_word", "unterminated", "empty"],
)
def test_loads_raises_json_decode_error_on_invalid(
    bad: str,
    loads_impl: t.Callable[[str], object],
) -> None:
    """Invalid input raises ``json.JSONDecodeError`` regardless of backend."""
    with pytest.raises(json.JSONDecodeError):
        loads_impl(bad)


class MessageCandidateCase(t.NamedTuple):
    """One ``iter_message_candidates`` walk and its expected candidate roles."""

    test_id: str
    value: object
    expected_roles: tuple[str, ...]


_MESSAGE_CANDIDATE_CASES = (
    MessageCandidateCase("message_dict", {"role": "user", "content": "hi"}, ("user",)),
    MessageCandidateCase("roleless_with_content", {"content": "hi"}, ()),
    MessageCandidateCase("role_without_text", {"role": "user"}, ()),
    MessageCandidateCase(
        "nested_message", {"a": {"role": "assistant", "text": "yo"}}, ("assistant",)
    ),
    MessageCandidateCase(
        "list_of_messages",
        [{"role": "user", "text": "a"}, {"role": "assistant", "text": "b"}],
        ("user", "assistant"),
    ),
)


@pytest.mark.parametrize("case", _MESSAGE_CANDIDATE_CASES, ids=lambda case: case.test_id)
def test_iter_message_candidates_yields_expected_roles(case: MessageCandidateCase) -> None:
    """Skipping text extraction for role-less nodes leaves the candidates unchanged."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    candidates = list(agentgrep.iter_message_candidates(case.value))
    assert tuple(candidate.role for candidate in candidates) == case.expected_roles


def test_iter_message_candidates_skips_text_extraction_without_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extract_message_text`` runs only for nodes that carry a role."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    calls: list[object] = []
    real = agentgrep.adapters._extract.extract_message_text
    monkeypatch.setattr(
        agentgrep.adapters._extract,
        "extract_message_text",
        lambda mapping: calls.append(mapping) or real(mapping),
    )

    list(agentgrep.iter_message_candidates({"content": "hi", "nested": {"x": "y"}}))
    assert calls == []  # no role anywhere -> never extracted

    list(agentgrep.iter_message_candidates({"role": "user", "content": "hi"}))
    assert len(calls) == 1  # the single role-bearing node


class MessageCandidateOriginCase(t.NamedTuple):
    """One ``iter_message_candidates`` walk and its expected origin cwd."""

    test_id: str
    value: object
    expected_cwd: str | None


_MESSAGE_CANDIDATE_ORIGIN_CASES = (
    MessageCandidateOriginCase(
        test_id="uuid_workspace_not_a_path",
        value={"workspace": "a1b2-uuid", "messages": [{"role": "user", "text": "hi"}]},
        expected_cwd=None,
    ),
    MessageCandidateOriginCase(
        test_id="bare_token_directory_not_a_path",
        value={"directory": "sidebar", "messages": [{"role": "user", "text": "hi"}]},
        expected_cwd=None,
    ),
    MessageCandidateOriginCase(
        test_id="path_workspace_extracted",
        value={"workspace": "/work/proj", "messages": [{"role": "user", "text": "hi"}]},
        expected_cwd="/work/proj",
    ),
    MessageCandidateOriginCase(
        test_id="home_prefixed_cwd_extracted",
        value={"cwd": "~/work/proj", "messages": [{"role": "user", "text": "hi"}]},
        expected_cwd="~/work/proj",
    ),
)


@pytest.mark.parametrize(
    "case",
    _MESSAGE_CANDIDATE_ORIGIN_CASES,
    ids=lambda case: case.test_id,
)
def test_iter_message_candidates_requires_path_like_origin_values(
    case: MessageCandidateOriginCase,
) -> None:
    """Bare tokens under path-named keys never become origin paths."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    candidates = list(agentgrep.iter_message_candidates(case.value))
    assert len(candidates) == 1
    origin = candidates[0].origin
    if case.expected_cwd is None:
        assert origin is None
    else:
        assert origin is not None
        assert origin.cwd == case.expected_cwd


class MessageCandidateBranchCase(t.NamedTuple):
    """One ``iter_message_candidates`` walk and its expected origin branch."""

    test_id: str
    value: object
    expected_branch: str | None


_MESSAGE_CANDIDATE_BRANCH_CASES = (
    MessageCandidateBranchCase(
        test_id="bare_branch_without_evidence_dropped",
        value={
            "branch": "left",
            "panels": {},
            "messages": [{"role": "user", "text": "hi"}],
        },
        expected_branch=None,
    ),
    MessageCandidateBranchCase(
        test_id="bare_branch_with_path_evidence_kept",
        value={
            "branch": "main",
            "cwd": "/work/proj",
            "messages": [{"role": "user", "text": "hi"}],
        },
        expected_branch="main",
    ),
    MessageCandidateBranchCase(
        test_id="git_branch_key_always_kept",
        value={"gitBranch": "main", "messages": [{"role": "user", "text": "hi"}]},
        expected_branch="main",
    ),
    MessageCandidateBranchCase(
        test_id="git_submapping_branch_kept",
        value={"git": {"branch": "main"}, "messages": [{"role": "user", "text": "hi"}]},
        expected_branch="main",
    ),
)


@pytest.mark.parametrize(
    "case",
    _MESSAGE_CANDIDATE_BRANCH_CASES,
    ids=lambda case: case.test_id,
)
def test_iter_message_candidates_gates_bare_branch_keys(
    case: MessageCandidateBranchCase,
) -> None:
    """Bare branch keys need git or path evidence to become origins."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    candidates = list(agentgrep.iter_message_candidates(case.value))
    assert len(candidates) == 1
    origin = candidates[0].origin
    if case.expected_branch is None:
        assert origin is None
    else:
        assert origin is not None
        assert origin.branch == case.expected_branch


def test_origin_mapping_keys_cover_extractor() -> None:
    """The walk-guard key set lists every key _origin_from_mapping reads."""
    extract = importlib.import_module("agentgrep.adapters._extract")
    source = inspect.getsource(extract._origin_from_mapping)
    read_keys = set(re.findall(r'(?<!git_)mapping\.get\("([^"]+)"\)', source))
    assert read_keys
    assert read_keys <= extract._ORIGIN_MAPPING_KEYS


class CodexNoiseLineCase(t.NamedTuple):
    """One Codex JSONL line and whether it is a function-call-output record."""

    test_id: str
    line: str
    expected: bool


_CODEX_NOISE_CASES = (
    CodexNoiseLineCase(
        "compact_noise",
        '{"type":"response_item","payload":{"type":"function_call_output","output":"x"}}',
        True,
    ),
    CodexNoiseLineCase(
        "spaced_noise",
        '{"type": "response_item", "payload": {"type": "function_call_output", "output": "x"}}',
        True,
    ),
    CodexNoiseLineCase(
        "real_message",
        '{"type":"response_item","payload":{"type":"message","role":"user","content":"hi"}}',
        False,
    ),
    CodexNoiseLineCase("non_codex", '{"role":"assistant","text":"hello"}', False),
)


@pytest.mark.parametrize("case", _CODEX_NOISE_CASES, ids=lambda case: case.test_id)
def test_is_codex_function_call_output_line(case: CodexNoiseLineCase) -> None:
    """Noise detection tolerates JSON spacing without normalizing each line."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep._is_codex_function_call_output_line(case.line) is case.expected


class ReverseJsonlCase(t.NamedTuple):
    """One reverse JSONL parsing shape."""

    test_id: str
    rows: tuple[object, ...]
    trailing_newline: bool
    chunk_bytes: int
    expected_indexes: tuple[int, ...]


REVERSE_JSONL_CASES: tuple[ReverseJsonlCase, ...] = (
    ReverseJsonlCase(
        test_id="trailing-newline",
        rows=({"index": 0}, {"index": 1}, {"index": 2}),
        trailing_newline=True,
        chunk_bytes=11,
        expected_indexes=(2, 1, 0),
    ),
    ReverseJsonlCase(
        test_id="no-trailing-newline",
        rows=({"index": 0}, {"index": 1}, {"index": 2}),
        trailing_newline=False,
        chunk_bytes=13,
        expected_indexes=(2, 1, 0),
    ),
)


@pytest.mark.parametrize(
    "case",
    REVERSE_JSONL_CASES,
    ids=[c.test_id for c in REVERSE_JSONL_CASES],
)
def test_iter_jsonl_reverse_reads_newest_lines_first(
    case: ReverseJsonlCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private reverse JSONL parsing yields valid rows from file end to start."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "events.jsonl"
    text = "\n".join(json.dumps(row) for row in case.rows)
    if case.trailing_newline:
        text += "\n"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(agentgrep.readers, "_JSONL_REVERSE_CHUNK_BYTES", case.chunk_bytes)

    parsed = list(agentgrep._iter_jsonl(path, reverse=True))

    assert [row["index"] for row in parsed if isinstance(row, dict)] == list(
        case.expected_indexes,
    )


def test_iter_jsonl_reverse_raw_skip_avoids_decoding_skipped_lines(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reverse raw-line filtering skips lines before JSON decode."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            (
                '{"index":0,"text":"skip me"}',
                '{"index":1,"text":"keep me"}',
                '{"index":2,"text":"skip me too"}',
            ),
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(agentgrep.readers, "_JSONL_REVERSE_CHUNK_BYTES", 9)
    decoded_inputs: list[str] = []
    original_loads = agentgrep._loads

    def loads_with_capture(payload: str) -> object:
        decoded_inputs.append(payload)
        return t.cast("object", original_loads(payload))

    monkeypatch.setattr(agentgrep.readers, "_loads", loads_with_capture)

    parsed = list(
        agentgrep._iter_jsonl(
            path,
            skip_line=lambda raw_line: "skip" in raw_line,
            skip_line_mode="line",
            reverse=True,
        ),
    )

    assert [row["index"] for row in parsed if isinstance(row, dict)] == [1]
    assert decoded_inputs == ['{"index":1,"text":"keep me"}']


def test_parse_codex_session_skips_function_call_output_before_json_decode(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex tool-output lines cannot become prompt records and stay unparsed."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "session.jsonl"
    tool_output_line = json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "bliss" + ("x" * agentgrep._CODEX_RAW_SKIP_MIN_BYTES),
            },
        },
    )
    message_line = json.dumps(
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {"role": "user", "content": "bliss prompt"},
        },
    )
    path.write_text(f"{tool_output_line}\n{message_line}\n", encoding="utf-8")
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )
    decoded_payloads: list[str] = []
    original_loads = agentgrep._loads

    def tracking_loads(payload: str) -> object:
        decoded_payloads.append(payload)
        return original_loads(payload)

    monkeypatch.setattr(agentgrep.readers, "_loads", tracking_loads)

    records = list(agentgrep.parse_codex_session_file(source))

    assert [record.text for record in records] == ["bliss prompt"]
    assert decoded_payloads == [message_line]


def test_iter_jsonl_prefix_skip_with_full_line_predicate(
    tmp_path: pathlib.Path,
) -> None:
    """Prefix skips stay cheap while full-line predicates see whole lines."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    prefix_bytes = int(agentgrep._JSONL_PREFIX_BYTES)
    skip_target = json.dumps({"type": "skipme", "data": "x" * (prefix_bytes * 2)})
    keep_target = json.dumps(
        {"type": "keep", "pad": "y" * (prefix_bytes * 2), "marker": "needle-far"},
    )
    drop_target = json.dumps({"type": "keep", "marker": "drop-me"})
    path = tmp_path / "lines.jsonl"
    path.write_text(f"{skip_target}\n{keep_target}\n{drop_target}\n", encoding="utf-8")

    prefix_calls: list[str] = []
    full_calls: list[str] = []

    def prefix_skip(line: str) -> bool:
        prefix_calls.append(line)
        return '"skipme"' in line

    def full_skip(line: str) -> bool:
        full_calls.append(line)
        return "drop-me" in line

    values = list(
        agentgrep._iter_jsonl(
            path,
            skip_line=prefix_skip,
            skip_line_mode="prefix",
            full_line_skip=full_skip,
        ),
    )

    assert values == [json.loads(keep_target)]
    assert all(len(call) <= prefix_bytes for call in prefix_calls)
    assert all('"skipme"' not in call for call in full_calls)
    assert any("needle-far" in call for call in full_calls)


class FirstJsonlRecordCase(t.NamedTuple):
    """One top-level discriminator accepted after a nested marker decoy."""

    test_id: str
    marker: str
    record_type: str


FIRST_JSONL_RECORD_CASES: tuple[FirstJsonlRecordCase, ...] = (
    FirstJsonlRecordCase(
        test_id="codex-session-meta",
        marker='"type":"session_meta"',
        record_type="session_meta",
    ),
    FirstJsonlRecordCase(
        test_id="codex-turn-context",
        marker='"type":"turn_context"',
        record_type="turn_context",
    ),
    FirstJsonlRecordCase(
        test_id="pi-session",
        marker='"type":"session"',
        record_type="session",
    ),
)


@pytest.mark.parametrize(
    FirstJsonlRecordCase._fields,
    [pytest.param(*case, id=case.test_id) for case in FIRST_JSONL_RECORD_CASES],
)
def test_read_first_matching_jsonl_record_requires_an_accepted_top_level_type(
    test_id: str,
    marker: str,
    record_type: str,
    tmp_path: pathlib.Path,
) -> None:
    """Prefix markers nominate candidates; decoded record types decide acceptance."""
    _ = test_id
    path = tmp_path / "records.jsonl"
    write_jsonl(
        path,
        [
            {
                "type": "noise",
                "payload": {"type": record_type, "value": "decoy"},
            },
            {"type": record_type, "value": "canonical"},
        ],
    )

    record = _rm_readers._read_first_matching_jsonl_record(
        path,
        marker,
        accept_record=lambda candidate: candidate.get("type") == record_type,
    )

    assert record == {"type": record_type, "value": "canonical"}


class TwoStageSkipCase(t.NamedTuple):
    """One Codex session layout and its expected raw-prefilter skip path."""

    test_id: str
    oversize_tool_output: bool
    expected_discard: bool


TWO_STAGE_SKIP_CASES: tuple[TwoStageSkipCase, ...] = (
    TwoStageSkipCase(
        test_id="oversized-session-keeps-chunked-prefix-discard",
        oversize_tool_output=True,
        expected_discard=True,
    ),
    TwoStageSkipCase(
        test_id="small-session-keeps-full-line-mode",
        oversize_tool_output=False,
        expected_discard=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    TWO_STAGE_SKIP_CASES,
    ids=[c.test_id for c in TWO_STAGE_SKIP_CASES],
)
def test_codex_session_raw_prefilter_keeps_prefix_tool_output_skip(
    case: TwoStageSkipCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw text prefilters do not disable the chunked Codex tool-output skip."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "session.jsonl"
    output_pad = int(agentgrep._CODEX_RAW_SKIP_MIN_BYTES) if case.oversize_tool_output else 64
    tool_output_line = json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "bliss" + ("x" * output_pad),
            },
        },
    )
    message_line = json.dumps(
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {"role": "user", "content": "bliss prompt"},
        },
    )
    decoy_line = json.dumps(
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "response_item",
            "payload": {"role": "user", "content": "other prompt"},
        },
    )
    path.write_text(
        f"{tool_output_line}\n{message_line}\n{decoy_line}\n",
        encoding="utf-8",
    )
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )
    discard_calls: list[bytes] = []
    original_discard = agentgrep._discard_rest_of_line

    def tracking_discard(handle: t.BinaryIO, prefix: bytes) -> None:
        discard_calls.append(prefix)
        original_discard(handle, prefix)

    monkeypatch.setattr(agentgrep.readers, "_discard_rest_of_line", tracking_discard)

    def raw_skip_line(line: str) -> bool:
        return "bliss" not in line

    records = list(
        agentgrep.parse_codex_session_file(source, raw_skip_line=raw_skip_line),
    )

    assert [record.text for record in records] == ["bliss prompt"]
    assert bool(discard_calls) is case.expected_discard


class CodexHeaderPrefilterCase(t.NamedTuple):
    """One Codex session layout for header preservation under raw prefilters."""

    test_id: str
    oversize_tool_output: bool


CODEX_HEADER_PREFILTER_CASES: tuple[CodexHeaderPrefilterCase, ...] = (
    CodexHeaderPrefilterCase(
        test_id="small-session-line-mode",
        oversize_tool_output=False,
    ),
    CodexHeaderPrefilterCase(
        test_id="oversized-session-prefix-mode",
        oversize_tool_output=True,
    ),
)


@pytest.mark.parametrize(
    "case",
    CODEX_HEADER_PREFILTER_CASES,
    ids=[c.test_id for c in CODEX_HEADER_PREFILTER_CASES],
)
def test_parse_codex_session_raw_prefilter_preserves_header(
    case: CodexHeaderPrefilterCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw text prefilters never drop the session_meta header.

    Regression guard: the header rarely contains the search term, so the
    prefilter skipped it before decode and matching records emitted with
    model=None and the file stem as session_id.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "session.jsonl"
    meta_line = json.dumps(
        {
            "type": "session_meta",
            "payload": {"id": "canonical-session-id", "model": "gpt-test-o5"},
        },
    )
    lines = [meta_line]
    if case.oversize_tool_output:
        lines.append(
            json.dumps(
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "bliss" + ("x" * int(agentgrep._CODEX_RAW_SKIP_MIN_BYTES)),
                    },
                },
            ),
        )
    miss_line = json.dumps(
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {"role": "user", "content": "other prompt"},
        },
    )
    match_line = json.dumps(
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "response_item",
            "payload": {"role": "user", "content": "bliss prompt"},
        },
    )
    lines.extend((miss_line, match_line))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )
    decoded_payloads: list[str] = []
    original_loads = agentgrep._loads

    def tracking_loads(payload: str) -> object:
        decoded_payloads.append(payload)
        return original_loads(payload)

    monkeypatch.setattr(agentgrep.readers, "_loads", tracking_loads)

    def raw_skip_line(line: str) -> bool:
        return "bliss" not in line

    records = list(
        agentgrep.parse_codex_session_file(source, raw_skip_line=raw_skip_line),
    )

    assert [record.text for record in records] == ["bliss prompt"]
    assert records[0].model == "gpt-test-o5"
    assert records[0].session_id == "canonical-session-id"
    assert decoded_payloads == [meta_line, match_line]


def test_parse_pi_session_raw_prefilter_preserves_header(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw text prefilters never drop the pi session header.

    Regression guard: skipping the header before decode emitted records
    with conversation_id=None and the file stem as session_id.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "sess.jsonl"
    header_line = json.dumps(
        {
            "type": "session",
            "id": "pi-sess-1",
            "timestamp": "2026-05-30T12:00:00.000Z",
            "cwd": "/home/user/proj",
            "version": 3,
        },
    )
    miss_line = json.dumps(
        {
            "type": "message",
            "id": "u0",
            "parentId": None,
            "timestamp": "2026-05-30T12:00:01.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "other prompt"}],
                "timestamp": 1780228801000,
            },
        },
    )
    match_line = json.dumps(
        {
            "type": "message",
            "id": "u1",
            "parentId": "u0",
            "timestamp": "2026-05-30T12:00:02.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "bliss prompt"}],
                "timestamp": 1780228802000,
            },
        },
    )
    path.write_text("\n".join((header_line, miss_line, match_line)) + "\n", encoding="utf-8")
    source = agentgrep.SourceHandle(
        agent="pi",
        store="pi.sessions",
        adapter_id="pi.sessions_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )
    decoded_payloads: list[str] = []
    original_loads = agentgrep._loads

    def tracking_loads(payload: str) -> object:
        decoded_payloads.append(payload)
        return original_loads(payload)

    monkeypatch.setattr(agentgrep.readers, "_loads", tracking_loads)

    def raw_skip_line(line: str) -> bool:
        return "bliss" not in line

    records = list(
        agentgrep.parse_pi_session_file(source, raw_skip_line=raw_skip_line),
    )

    assert [record.text for record in records] == ["bliss prompt"]
    assert records[0].session_id == "pi-sess-1"
    assert records[0].conversation_id == "/home/user/proj"
    assert decoded_payloads == [header_line, match_line]


class PiBashExecutionCase(t.NamedTuple):
    """A pi ``bashExecution`` message and the searchable text it should yield."""

    test_id: str
    message: dict[str, object]
    expected_text: str | None


PI_BASH_EXECUTION_CASES: tuple[PiBashExecutionCase, ...] = (
    PiBashExecutionCase(
        test_id="command-and-output-joined",
        message={
            "role": "bashExecution",
            "command": "ls -la",
            "output": "total 0",
            "excludeFromContext": False,
        },
        expected_text="ls -la\ntotal 0",
    ),
    PiBashExecutionCase(
        test_id="command-only",
        message={"role": "bashExecution", "command": "pwd", "output": ""},
        expected_text="pwd",
    ),
    PiBashExecutionCase(
        test_id="empty-yields-nothing",
        message={"role": "bashExecution", "command": "", "output": ""},
        expected_text=None,
    ),
)


@pytest.mark.parametrize(
    "case",
    PI_BASH_EXECUTION_CASES,
    ids=[c.test_id for c in PI_BASH_EXECUTION_CASES],
)
def test_parse_pi_session_extracts_bash_execution(
    case: PiBashExecutionCase,
    tmp_path: pathlib.Path,
) -> None:
    """`bashExecution` turns surface their joined command/output as history."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "sess.jsonl"
    header = json.dumps(
        {
            "type": "session",
            "id": "pi-sess-1",
            "timestamp": "2026-05-30T12:00:00.000Z",
            "cwd": "/home/user/proj",
            "version": 3,
        },
    )
    entry = json.dumps(
        {
            "type": "message",
            "id": "b1",
            "parentId": None,
            "timestamp": "2026-05-30T12:00:01.000Z",
            "message": case.message,
        },
    )
    _ = path.write_text("\n".join((header, entry)) + "\n", encoding="utf-8")
    source = agentgrep.SourceHandle(
        agent="pi",
        store="pi.sessions",
        adapter_id="pi.sessions_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )

    texts = [record.text for record in agentgrep.iter_source_records(source)]

    assert texts == ([] if case.expected_text is None else [case.expected_text])


def test_parse_codex_session_reverse_preserves_header(
    tmp_path: pathlib.Path,
) -> None:
    """Manual reverse parses still carry canonical session metadata.

    Regression guard: reverse iteration reads the leading session_meta
    header last, so direct ``reverse=True`` callers received records with
    model=None and the file stem as session_id.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "rollout-abc.jsonl"
    write_jsonl(
        path,
        [
            {
                "type": "session_meta",
                "payload": {"id": "canonical-session-id", "model": "gpt-test-o5"},
            },
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {"role": "user", "content": "first prompt"},
            },
            {
                "timestamp": "2026-01-01T00:01:00Z",
                "type": "response_item",
                "payload": {"role": "user", "content": "second prompt"},
            },
        ],
    )
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )

    records = list(agentgrep.parse_codex_session_file(source, reverse=True))

    assert [record.text for record in records] == ["second prompt", "first prompt"]
    assert {record.model for record in records} == {"gpt-test-o5"}
    assert {record.session_id for record in records} == {"canonical-session-id"}


class CodexTurnContextCase(t.NamedTuple):
    """One read path through a Codex rollout."""

    test_id: str
    reverse: bool
    prefilter: bool
    position: t.Literal["before", "straddle", "beyond"]


CODEX_TURN_CONTEXT_CASES: tuple[CodexTurnContextCase, ...] = (
    CodexTurnContextCase(
        test_id="before-forward",
        reverse=False,
        prefilter=False,
        position="before",
    ),
    CodexTurnContextCase(
        test_id="before-reverse",
        reverse=True,
        prefilter=False,
        position="before",
    ),
    CodexTurnContextCase(
        test_id="before-prefilter",
        reverse=False,
        prefilter=True,
        position="before",
    ),
    CodexTurnContextCase(
        test_id="straddle-forward",
        reverse=False,
        prefilter=False,
        position="straddle",
    ),
    CodexTurnContextCase(
        test_id="beyond-forward",
        reverse=False,
        prefilter=False,
        position="beyond",
    ),
    CodexTurnContextCase(
        test_id="beyond-reverse",
        reverse=True,
        prefilter=False,
        position="beyond",
    ),
    CodexTurnContextCase(
        test_id="beyond-prefilter",
        reverse=False,
        prefilter=True,
        position="beyond",
    ),
    CodexTurnContextCase(
        test_id="beyond-prefilter-reverse",
        reverse=True,
        prefilter=True,
        position="beyond",
    ),
)


@pytest.mark.parametrize(
    CodexTurnContextCase._fields,
    [pytest.param(*case, id=case.test_id) for case in CODEX_TURN_CONTEXT_CASES],
)
def test_parse_codex_session_reads_model_from_turn_context(
    test_id: str,
    reverse: bool,
    prefilter: bool,
    position: t.Literal["before", "straddle", "beyond"],
    tmp_path: pathlib.Path,
) -> None:
    """The Codex model slug comes from ``turn_context``, on every read path.

    ``session_meta`` names only ``model_provider`` — the provider id — so
    reading it as the model labelled every Codex record ``openai``.

    Cases cover forward, reverse, and prefiltered reads while moving the first
    complete ``turn_context`` record before, across, and beyond the former
    64-KiB head boundary. The unrelated padding line must be discarded without
    materializing it as a decoded JSON object.
    """
    _ = test_id
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "rollout-abc.jsonl"
    session_meta = json.dumps(
        {
            "type": "session_meta",
            "payload": {"id": "session-1", "model_provider": "openai"},
        },
    )
    turn_context = json.dumps(
        {
            "type": "turn_context",
            "payload": {"model": "gpt-5.4-codex", "cwd": "/work/demo"},
        },
    )
    prompts = tuple(
        json.dumps(
            {
                "timestamp": f"2026-01-01T00:0{index}:00Z",
                "type": "response_item",
                "payload": {"role": "user", "content": text},
            },
        )
        for index, text in enumerate(("bliss prompt", "bliss second"))
    )
    prefix = f"{session_meta}\n"
    if position != "before":
        target = 65536 - 8 if position == "straddle" else 65536 + 1024
        empty_noise = json.dumps({"type": "noise", "payload": {"padding": ""}})
        padding_length = target - len(prefix.encode()) - len(empty_noise.encode()) - 1
        noise = json.dumps(
            {"type": "noise", "payload": {"padding": "x" * padding_length}},
        )
        prefix = f"{prefix}{noise}\n"
        assert len(prefix.encode()) == target
    _ = path.write_text(
        "\n".join((f"{prefix}{turn_context}", *prompts)),
        encoding="utf-8",
    )
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )

    def raw_skip_line(line: str) -> bool:
        return "bliss" not in line

    records = list(
        agentgrep.parse_codex_session_file(
            source,
            raw_skip_line=raw_skip_line if prefilter else None,
            reverse=reverse,
        ),
    )

    assert len(records) == 2
    assert {record.model for record in records} == {"gpt-5.4-codex"}
    assert {record.session_id for record in records} == {"session-1"}


class CodexFirstValidContextCase(t.NamedTuple):
    """One invalid context record preceding a valid model record."""

    test_id: str
    invalid_line: str


CODEX_FIRST_VALID_CONTEXT_CASES: tuple[CodexFirstValidContextCase, ...] = (
    CodexFirstValidContextCase(
        test_id="nested-context-marker",
        invalid_line=json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "turn_context",
                    "model": "wrong-model",
                },
            },
        ),
    ),
    CodexFirstValidContextCase(
        test_id="malformed-context",
        invalid_line='{"type":"turn_context","payload":',
    ),
    CodexFirstValidContextCase(
        test_id="context-without-model",
        invalid_line=json.dumps({"type": "turn_context", "payload": {"cwd": "/work"}}),
    ),
)


@pytest.mark.parametrize(
    CodexFirstValidContextCase._fields,
    [pytest.param(*case, id=case.test_id) for case in CODEX_FIRST_VALID_CONTEXT_CASES],
)
def test_parse_codex_session_uses_first_valid_turn_context(
    test_id: str,
    invalid_line: str,
    tmp_path: pathlib.Path,
) -> None:
    """Malformed or model-free contexts do not hide the first valid model."""
    _ = test_id
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "rollout-first-valid.jsonl"
    rows = (
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "session-1", "model_provider": "openai"},
            },
        ),
        invalid_line,
        json.dumps({"type": "noise", "payload": {"padding": "x" * 65536}}),
        json.dumps(
            {"type": "turn_context", "payload": {"model": "gpt-first-valid"}},
        ),
        json.dumps(
            {
                "type": "response_item",
                "payload": {"role": "user", "content": "bliss prompt"},
            },
        ),
    )
    _ = path.write_text("\n".join(rows), encoding="utf-8")
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )

    records = list(agentgrep.parse_codex_session_file(source))

    assert {record.model for record in records} == {"gpt-first-valid"}


def test_parse_codex_session_model_falls_back_to_session_meta(
    tmp_path: pathlib.Path,
) -> None:
    """A rollout with no ``turn_context`` keeps whatever ``session_meta`` names."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "rollout-abc.jsonl"
    write_jsonl(
        path,
        [
            {
                "type": "session_meta",
                "payload": {"id": "session-1", "model": "gpt-test-o5"},
            },
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {"role": "user", "content": "first prompt"},
            },
        ],
    )
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )

    records = list(agentgrep.parse_codex_session_file(source))

    assert [record.model for record in records] == ["gpt-test-o5"]


def test_parse_pi_session_reverse_preserves_header(
    tmp_path: pathlib.Path,
) -> None:
    """Manual reverse parses still carry the pi session header state."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "sess.jsonl"
    write_jsonl(
        path,
        [
            {
                "type": "session",
                "id": "pi-sess-1",
                "timestamp": "2026-05-30T12:00:00.000Z",
                "cwd": "/home/user/proj",
                "version": 3,
            },
            {
                "type": "message",
                "id": "u1",
                "parentId": None,
                "timestamp": "2026-05-30T12:00:02.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "needle prompt"}],
                    "timestamp": 1780228802000,
                },
            },
        ],
    )
    source = agentgrep.SourceHandle(
        agent="pi",
        store="pi.sessions",
        adapter_id="pi.sessions_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )

    records = list(agentgrep.parse_pi_session_file(source, reverse=True))

    assert [record.text for record in records] == ["needle prompt"]
    assert records[0].session_id == "pi-sess-1"
    assert records[0].conversation_id == "/home/user/proj"


def test_streaming_search_progress_buffers_and_flushes_records(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    # Freeze the clock so the self-pacing auto-flush threshold (50 ms) never
    # fires during the explicit-flush sequence; the test exercises the
    # buffer/explicit-flush surface only.
    monkeypatch.setattr(agentgrep.time, "monotonic", lambda: 0.0)
    emitted: list[object] = []
    progress = agentgrep.StreamingSearchProgress(emit=emitted.append)
    record_a = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="a",
    )
    record_b = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "b.jsonl",
        text="b",
    )

    progress.record_added(record_a)
    progress.record_added(record_b)
    progress.result_added(2)
    progress.flush()

    batches = [e for e in emitted if isinstance(e, agentgrep.StreamingRecordsBatch)]
    assert len(batches) == 1
    assert batches[0].records == (record_a, record_b)
    assert batches[0].total == 2

    progress.flush()
    assert sum(1 for e in emitted if isinstance(e, agentgrep.StreamingRecordsBatch)) == 1


def test_streaming_search_progress_self_paces_flush(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``record_added`` auto-flushes once the 50 ms batching window elapses."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    clock = {"now": 0.0}
    monkeypatch.setattr(agentgrep.time, "monotonic", lambda: clock["now"])
    emitted: list[object] = []
    progress = agentgrep.StreamingSearchProgress(emit=emitted.append)
    record_a = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="a",
    )
    record_b = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "b.jsonl",
        text="b",
    )

    progress.record_added(record_a)
    assert [e for e in emitted if isinstance(e, agentgrep.StreamingRecordsBatch)] == []

    clock["now"] = agentgrep.StreamingSearchProgress._FLUSH_INTERVAL_SECONDS + 0.01
    progress.record_added(record_b)

    batches = [e for e in emitted if isinstance(e, agentgrep.StreamingRecordsBatch)]
    assert len(batches) == 1
    assert batches[0].records == (record_a, record_b)


def test_streaming_search_progress_translates_progress_callbacks(
    tmp_path: pathlib.Path,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    emitted: list[object] = []
    progress = agentgrep.StreamingSearchProgress(emit=emitted.append)
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "session.jsonl",
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )

    progress.start(query)
    progress.sources_discovered(10)
    progress.sources_planned(7, 10)
    progress.source_started(1, 7, source)
    progress.source_progress(1, 7, source, records=128, matches=3)
    progress.source_finished(1, 7, source, records=256, matches=2)
    progress.result_added(2)
    progress.finish(2)

    snapshots = [e for e in emitted if isinstance(e, agentgrep.ProgressSnapshot)]
    finished = [e for e in emitted if isinstance(e, agentgrep.StreamingSearchFinished)]

    assert len(snapshots) == 6
    assert snapshots[0].phase == "discovering"
    assert snapshots[0].query_label == "bliss"
    assert snapshots[1].phase == "discovered"
    assert snapshots[1].detail == "10 sources"
    assert snapshots[2].phase == "planning"
    assert snapshots[2].current == 7
    assert snapshots[2].total == 10
    assert snapshots[3].phase == "scanning"
    assert snapshots[3].current == 1
    assert snapshots[3].total == 7
    assert snapshots[3].detail == "session.jsonl"
    assert snapshots[3].source_records_seen == 0
    assert snapshots[4].phase == "scanning"
    assert snapshots[4].detail == "128 records, 3 source matches"
    assert snapshots[4].source_records_seen == 128
    assert snapshots[5].phase == "scanning"
    assert snapshots[5].detail is not None
    assert "matches" in snapshots[5].detail
    assert snapshots[5].source_records_seen == 256

    assert len(finished) == 1
    assert finished[0].outcome == "complete"
    assert finished[0].total == 2
    assert finished[0].elapsed >= 0.0


def test_compute_filter_matches_returns_substring_matches(
    tmp_path: pathlib.Path,
) -> None:
    """The filter worker's pure helper matches by case-folded substring."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    blissful = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "bliss.jsonl",
        text="serene BLISS abounds",
    )
    other = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "other.jsonl",
        text="unrelated text",
    )

    matches = agentgrep.compute_filter_matches([blissful, other], "bliss")
    assert matches == (blissful,)

    no_matches = agentgrep.compute_filter_matches([blissful, other], "xyz")
    assert no_matches == ()


def test_compute_filter_matches_empty_text_returns_all(tmp_path: pathlib.Path) -> None:
    """Whitespace-only or empty filter text returns every record unchanged."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="anything",
    )
    assert agentgrep.compute_filter_matches([record], "") == (record,)
    assert agentgrep.compute_filter_matches([record], "   ") == (record,)


def test_cached_haystack_memoizes_per_record(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cached_haystack`` calls ``build_search_haystack`` once per record."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    agentgrep.clear_haystack_cache()
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="serene bliss",
    )
    call_count = 0
    real_build_search_haystack = agentgrep.build_search_haystack

    def counting_build(rec: object) -> str:
        nonlocal call_count
        call_count += 1
        return t.cast("str", real_build_search_haystack(rec))

    monkeypatch.setattr(orchestration, "build_search_haystack", counting_build)
    first = agentgrep.cached_haystack(record)
    second = agentgrep.cached_haystack(record)
    assert first == second
    assert first == "serene bliss\n" + str(record.path)
    assert call_count == 1
    agentgrep.clear_haystack_cache()
    # Cache cleared — next call rebuilds.
    _ = agentgrep.cached_haystack(record)
    assert call_count == 2


def test_compute_filter_matches_uses_cached_haystack(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filter uses the cache: ``build_search_haystack`` not called once cached."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    agentgrep.clear_haystack_cache()
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx} alpha",
        )
        for idx in range(3)
    ]
    # Warm the cache.
    for record in records:
        agentgrep.cached_haystack(record)

    def raise_if_called(_record: object) -> str:
        msg = "build_search_haystack must not run after cache is warm"
        raise RuntimeError(msg)

    monkeypatch.setattr(orchestration, "build_search_haystack", raise_if_called)
    matches = agentgrep.compute_filter_matches(records, "alpha")
    assert len(matches) == 3


def test_pydantic_payloads_reject_wrong_types(tmp_path: pathlib.Path) -> None:
    """Payload models validate field types at construction time."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="hi",
    )

    # Happy path constructs cleanly
    rap = agentgrep.RecordsAppendedPayload(records=(record,), total=1)
    assert rap.records == (record,)
    assert rap.total == 1

    sfp = agentgrep.SearchFinishedPayload(outcome="complete", total=1, elapsed=0.5)
    assert sfp.error_message is None

    fcp = agentgrep.FilterCompletedPayload(text="abc", matching=(record,))
    assert fcp.text == "abc"

    # Wrong types raise ValidationError
    with pytest.raises(pydantic.ValidationError):
        agentgrep.SearchFinishedPayload(outcome="not-a-valid-outcome", total=0, elapsed=0.0)
    with pytest.raises(pydantic.ValidationError):
        agentgrep.FilterRequestedPayload(text=None)  # type: ignore[arg-type]


def test_collect_search_records_returns_partial_results_on_answer_now(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "session.jsonl",
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )
    first = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=source.path,
        text="first bliss",
    )
    second = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=source.path,
        text="second bliss",
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    control = agentgrep.SearchControl()

    def iter_records(source: object) -> cabc.Iterator[object]:
        yield first
        control.request_answer_now()
        yield second

    monkeypatch.setattr(_rm_scanning, "iter_source_records", iter_records)

    records = agentgrep.collect_search_records(query, [source], control=control)

    assert records == [first]
    assert control.answer_now_requested()


def test_run_search_query_interrupts_progress_on_keyboard_interrupt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "first.jsonl"
    write_jsonl(
        session_path,
        [{"type": "response_item", "payload": {"role": "user", "content": "bliss"}}],
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    class RecordingProgress:
        def __init__(self) -> None:
            self.events: list[str] = []

        def start(self, query: object) -> None:
            self.events.append("start")

        def sources_discovered(self, count: int) -> None:
            self.events.append("sources_discovered")

        def prefilter_started(self, root: pathlib.Path) -> None:
            self.events.append("prefilter_started")

        def sources_planned(self, planned: int, total: int) -> None:
            self.events.append("sources_planned")

        def source_started(self, index: int, total: int, source: object) -> None:
            self.events.append("source_started")

        def source_finished(
            self,
            index: int,
            total: int,
            source: object,
            records: int,
            matches: int,
        ) -> None:
            self.events.append("source_finished")

        def result_added(self, count: int) -> None:
            self.events.append("result_added")

        def record_added(self, record: object) -> None:
            self.events.append("record_added")

        def finish(self, result_count: int) -> None:
            self.events.append("finish")

        def interrupt(self) -> None:
            self.events.append("interrupt")

        def close(self) -> None:
            self.events.append("close")

    def raise_interrupt(source: object) -> cabc.Iterator[object]:
        raise KeyboardInterrupt
        yield source

    progress = RecordingProgress()
    monkeypatch.setattr(_rm_scanning, "iter_source_records", raise_interrupt)

    with pytest.raises(KeyboardInterrupt):
        agentgrep.run_search_query(
            home,
            query,
            backends=agentgrep.BackendSelection(None, None, None),
            progress=progress,
        )

    assert progress.events[-1] == "interrupt"
    assert "close" not in progress.events
    assert "finish" not in progress.events


def test_plan_search_sources_prefilters_one_root_once(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    first = home / ".codex" / "sessions" / "2026" / "01" / "01" / "one.jsonl"
    second = home / ".codex" / "sessions" / "2026" / "01" / "01" / "two.jsonl"
    write_jsonl(
        first,
        [{"type": "response_item", "payload": {"role": "user", "content": "bliss"}}],
    )
    write_jsonl(
        second,
        [{"type": "response_item", "payload": {"role": "user", "content": "other"}}],
    )

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    sources = agentgrep.discover_sources(
        home,
        ("codex",),
        agentgrep.BackendSelection(None, None, None),
    )
    calls: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        control: object | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, f"{first}\n", "")

    monkeypatch.setattr(orchestration, "run_readonly_command", fake_run)
    planned = agentgrep.plan_search_sources(
        query,
        sources,
        agentgrep.BackendSelection(None, "/fake/rg", None),
    )

    assert len(calls) == 1
    assert [source.path for source in planned] == [first]


class ClaudeCompactCase(t.NamedTuple):
    """One Claude project record and whether it should yield a search record."""

    test_id: str
    record: dict[str, object]
    emits: bool


CLAUDE_COMPACT_CASES: tuple[ClaudeCompactCase, ...] = (
    ClaudeCompactCase(
        test_id="normal-user-turn-emitted",
        record={
            "type": "user",
            "uuid": "u1",
            "sessionId": "s",
            "timestamp": "2026-05-25T10:00:00Z",
            "message": {"role": "user", "content": "a real user prompt"},
        },
        emits=True,
    ),
    ClaudeCompactCase(
        test_id="compact-summary-skipped",
        record={
            "type": "user",
            "isCompactSummary": True,
            "uuid": "u2",
            "sessionId": "s",
            "timestamp": "2026-05-25T10:00:00Z",
            "message": {"role": "user", "content": "a /compact machine recap"},
        },
        emits=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    CLAUDE_COMPACT_CASES,
    ids=[c.test_id for c in CLAUDE_COMPACT_CASES],
)
def test_parse_claude_project_skips_compact_summaries(
    case: ClaudeCompactCase,
    tmp_path: pathlib.Path,
) -> None:
    """`isCompactSummary: true` records are dropped; normal user turns are kept."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "session.jsonl"
    _ = path.write_text(json.dumps(case.record) + "\n", encoding="utf-8")
    source = agentgrep.SourceHandle(
        agent="claude",
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )

    records = list(agentgrep.iter_source_records(source))

    assert bool(records) is case.emits


def test_plan_search_sources_prunes_chat_sources_from_prompt_scope(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt-scope planning skips Claude transcript files before parsing."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    history = agentgrep.SourceHandle(
        agent="claude",
        store="claude.history",
        adapter_id="claude.history_jsonl.v1",
        path=tmp_path / "history.jsonl",
        path_kind="history_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=2,
    )
    transcript = agentgrep.SourceHandle(
        agent="claude",
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
        path=tmp_path / "projects" / "session.jsonl",
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )
    query = agentgrep.SearchQuery(
        terms=("biome",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("claude",),
        limit=None,
    )
    checked: list[str] = []

    def direct_source_matches(
        source: object,
        query: object,
        backends: object,
        control: object | None = None,
    ) -> bool:
        checked.append(t.cast("t.Any", source).store)
        return True

    monkeypatch.setattr(_rm_planning, "direct_source_matches", direct_source_matches)

    planned = agentgrep.plan_search_sources(
        query,
        [history, transcript],
        agentgrep.BackendSelection(None, None, None),
    )

    assert [source.store for source in planned] == ["claude.history"]
    assert checked == ["claude.history"]


def test_plan_search_sources_skips_root_prefilter_for_sqlite_sources(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite sources bypass binary root grep and stay parse candidates."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    root = tmp_path / "cursor-workspaces"
    source = agentgrep.SourceHandle(
        agent="cursor-ide",
        store="cursor-ide.workspace_state",
        adapter_id="cursor_ide.state_vscdb_modern.v1",
        path=root / "project" / "state.vscdb",
        path_kind="sqlite_db",
        source_kind="sqlite",
        search_root=root,
        mtime_ns=1,
    )
    query = agentgrep.SearchQuery(
        terms=("serenity",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("cursor-ide",),
        limit=None,
    )
    grep_calls: list[tuple[pathlib.Path, object]] = []

    def grep_root_paths(
        search_root: pathlib.Path,
        query: object,
        grep_program: str,
        *,
        control: object | None = None,
    ) -> set[pathlib.Path]:
        _ = grep_program, control
        grep_calls.append((search_root, query))
        return set()

    monkeypatch.setattr(_rm_orch, "grep_root_paths", grep_root_paths)

    planned = agentgrep.plan_search_sources(
        query,
        [source],
        agentgrep.BackendSelection(None, "/fake/rg", None),
    )

    assert planned == [source]
    assert grep_calls == []


def test_search_prefers_newer_sources_when_limiting(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    older = home / ".codex" / "sessions" / "2026" / "01" / "01" / "a-old.jsonl"
    newer = home / ".codex" / "sessions" / "2026" / "01" / "01" / "z-new.jsonl"
    rows = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "bliss appears here"}],
            },
        },
    ]
    write_jsonl(older, rows)
    write_jsonl(newer, rows)
    older_mtime_ns = 1_700_000_000_000_000_000
    newer_mtime_ns = older_mtime_ns + 1000
    os.utime(older, ns=(older_mtime_ns, older_mtime_ns))
    os.utime(newer, ns=(newer_mtime_ns, newer_mtime_ns))

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=1,
    )
    sources = agentgrep.discover_sources(
        home,
        ("codex",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 1
    assert records[0].path == newer


def test_search_dedupes_identical_prompts_within_session(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "dupes.jsonl"
    write_jsonl(
        session_path,
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "bliss prompt"}],
                },
            },
            {
                "timestamp": "2026-01-01T00:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "bliss prompt"}],
                },
            },
        ],
    )

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    sources = agentgrep.discover_sources(
        home,
        ("codex",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 1
    assert records[0].timestamp == "2026-01-01T00:01:00Z"


def test_search_keeps_identical_prompts_across_sessions(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    first = home / ".codex" / "sessions" / "2026" / "01" / "01" / "first.jsonl"
    second = home / ".codex" / "sessions" / "2026" / "01" / "01" / "second.jsonl"
    rows = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "bliss prompt"}],
            },
        },
    ]
    write_jsonl(first, rows)
    write_jsonl(second, rows)

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    sources = agentgrep.discover_sources(
        home,
        ("codex",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 2
    assert {record.path for record in records} == {first, second}


def test_search_limit_applies_to_unique_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "limit.jsonl"
    write_jsonl(
        session_path,
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "bliss prompt"}],
                },
            },
            {
                "timestamp": "2026-01-01T00:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "bliss prompt"}],
                },
            },
            {
                "timestamp": "2026-01-01T00:02:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "bliss second"}],
                },
            },
        ],
    )

    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=2,
    )
    sources = agentgrep.discover_sources(
        home,
        ("codex",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 2
    assert [record.text for record in records] == ["bliss second", "bliss prompt"]


def test_search_codex_history_json_returns_history_record(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    history_path = home / ".codex" / "history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    _ = history_path.write_text(
        json.dumps(
            [
                {
                    "command": "serenity command example",
                    "timestamp": "2026-01-01T00:00:00Z",
                },
            ],
        ),
        encoding="utf-8",
    )

    query = agentgrep.SearchQuery(
        terms=("serenity",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    sources = agentgrep.discover_sources(
        home,
        ("codex",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 1
    assert records[0].kind == "prompt"
    assert records[0].text == "serenity command example"


def test_search_codex_history_json_keeps_millisecond_timestamp(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy ``history.json`` writes ``timestamp`` as a number, not a string.

    A string-only accessor dropped it, and the legacy entry carries no ``ts``
    key to fall back on, so every record from this store surfaced with no time
    at all.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    history_path = home / ".codex" / "history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    _ = history_path.write_text(
        json.dumps(
            [{"command": "serenity command example", "timestamp": 1780000000000}],
        ),
        encoding="utf-8",
    )

    query = agentgrep.SearchQuery(
        terms=("serenity",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_sources(home, ("codex",), backends)
    records = agentgrep.search_sources(query, sources, backends)

    assert len(records) == 1
    assert records[0].timestamp == "2026-05-28T20:26:40Z"
    # This store is a flat prompt log: no cwd, branch, or model on disk.
    assert records[0].origin is None
    assert records[0].model is None


def test_cursor_ai_tracking_summary_is_exposed_as_history(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    db_path = home / ".cursor" / "ai-tracking" / "ai-code-tracking.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    _ = connection.execute(
        """
        CREATE TABLE conversation_summaries (
            conversationId TEXT,
            title TEXT,
            tldr TEXT,
            overview TEXT,
            summaryBullets TEXT,
            model TEXT,
            mode TEXT,
            updatedAt TEXT
        )
        """,
    )
    _ = connection.execute(
        """
        INSERT INTO conversation_summaries
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "conv-1",
            "Serenity Session",
            "bliss summary",
            "overview text",
            json.dumps(["bullet one", "bullet two"]),
            "gpt-5",
            "chat",
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()

    query = agentgrep.SearchQuery(
        terms=("serenity", "bliss"),
        scope="conversations",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("cursor-cli",),
        limit=None,
    )
    sources = agentgrep.discover_sources(
        home,
        ("cursor-cli",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 1
    assert records[0].agent == "cursor-cli"
    assert records[0].kind == "history"
    assert "bliss summary" in records[0].text


def test_cursor_state_itemtable_extracts_prompt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    db_path = home / ".cursor" / "state.vscdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    _ = connection.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
    payload = {
        "messages": [
            {"role": "user", "content": "serenity and bliss live here"},
            {"role": "assistant", "content": "response"},
        ],
    }
    _ = connection.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("workbench.panel.chat.composerData", json.dumps(payload)),
    )
    connection.commit()
    connection.close()

    query = agentgrep.SearchQuery(
        terms=("serenity", "bliss"),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("cursor-ide",),
        limit=None,
    )
    sources = agentgrep.discover_sources(
        home,
        ("cursor-ide",),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.search_sources(query, sources, agentgrep.BackendSelection(None, None, None))

    assert len(records) == 1
    assert records[0].kind == "prompt"
    assert records[0].text == "serenity and bliss live here"


class CursorStateTwoStageCase(t.NamedTuple):
    """Parametrized case for the two-stage Cursor state key/value read."""

    test_id: str
    table: str
    rows: tuple[tuple[str, str], ...]
    expected_rows: tuple[tuple[str, str], ...]
    expected_value_fetches: int


_CURSOR_STATE_TWO_STAGE_CASES: tuple[CursorStateTwoStageCase, ...] = (
    CursorStateTwoStageCase(
        test_id="legacy-itemtable",
        table="ItemTable",
        rows=(
            ("workbench.panel.chat.composerData", "matched"),
            ("extension.unrelated.largeCache", "ignored"),
        ),
        expected_rows=(("workbench.panel.chat.composerData", "matched"),),
        expected_value_fetches=1,
    ),
    CursorStateTwoStageCase(
        test_id="modern-cursor-disk-kv",
        table="cursorDiskKV",
        rows=(
            ("aiService.prompts", "matched"),
            ("workbench.colorTheme", "ignored"),
        ),
        expected_rows=(("aiService.prompts", "matched"),),
        expected_value_fetches=1,
    ),
    CursorStateTwoStageCase(
        test_id="case-insensitive-key",
        table="cursorDiskKV",
        rows=(("AISERVICE.PROMPTS", "matched"),),
        expected_rows=(("AISERVICE.PROMPTS", "matched"),),
        expected_value_fetches=1,
    ),
    CursorStateTwoStageCase(
        test_id="dynamic-aichat-prefix",
        table="ItemTable",
        rows=(
            ("workbench.panel.aichat.view.abc.prompts", "matched"),
            ("workbench.panel.explorer.view.cache", "ignored"),
        ),
        expected_rows=(("workbench.panel.aichat.view.abc.prompts", "matched"),),
        expected_value_fetches=1,
    ),
    CursorStateTwoStageCase(
        test_id="duplicate-key-no-pk",
        table="ItemTable",
        rows=(
            ("aiService.prompts", "first"),
            ("aiService.prompts", "second"),
        ),
        expected_rows=(
            ("aiService.prompts", "first"),
            ("aiService.prompts", "second"),
        ),
        expected_value_fetches=1,
    ),
)


class CursorWorkspaceOriginCase(t.NamedTuple):
    """Parametrized case for Cursor workspace source-origin summaries."""

    test_id: str
    workspace_dir: str
    workspace_payload: dict[str, object] | None
    expected_summary: SourceOriginSummary | None


_CURSOR_WORKSPACE_DIGEST = "9b2a1f0c4d3e5a6b7c8d9e0f1a2b3c4d"
"""A Cursor ``workspaceStorage`` directory name: md5 of the workspace path."""

_CURSOR_WORKSPACE_ORIGIN_CASES: tuple[CursorWorkspaceOriginCase, ...] = (
    CursorWorkspaceOriginCase(
        test_id="folder-uri-adds-cwd-and-hash",
        workspace_dir=_CURSOR_WORKSPACE_DIGEST,
        workspace_payload={"folder": "vscode-remote://wsl+Ubuntu/home/u/work/proj"},
        expected_summary=SourceOriginSummary(
            # `cwd` is a fact, not a pruning claim: a composerData bubble can
            # carry a cwd of its own, so only `cwd_hash` is complete.
            origins=(RecordOrigin(cwd="/home/u/work/proj", cwd_hash=_CURSOR_WORKSPACE_DIGEST),),
            complete_fields=frozenset({"cwd_hash"}),
        ),
    ),
    CursorWorkspaceOriginCase(
        test_id="missing-workspace-json-keeps-cwd-unknown",
        workspace_dir=_CURSOR_WORKSPACE_DIGEST,
        workspace_payload=None,
        expected_summary=SourceOriginSummary(
            origins=(RecordOrigin(cwd_hash=_CURSOR_WORKSPACE_DIGEST),),
            complete_fields=frozenset({"cwd_hash"}),
        ),
    ),
    CursorWorkspaceOriginCase(
        test_id="non-digest-directory-has-no-workspace-hash",
        workspace_dir="wshash",
        workspace_payload=None,
        expected_summary=None,
    ),
)


@pytest.mark.parametrize(
    CursorStateTwoStageCase._fields,
    _CURSOR_STATE_TWO_STAGE_CASES,
    ids=[case.test_id for case in _CURSOR_STATE_TWO_STAGE_CASES],
)
def test_iter_key_value_rows_reads_values_only_for_matched_keys(
    test_id: str,
    table: str,
    rows: tuple[tuple[str, str], ...],
    expected_rows: tuple[tuple[str, str], ...],
    expected_value_fetches: int,
) -> None:
    """The key/value iterator scans keys first and point-fetches values."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    connection = sqlite3.connect(":memory:")
    _ = connection.execute(f"CREATE TABLE {table} (key TEXT, value TEXT)")
    for key, value in rows:
        _ = connection.execute(f"INSERT INTO {table} VALUES (?, ?)", (key, value))
    connection.commit()
    traces: list[str] = []
    connection.set_trace_callback(traces.append)

    fetched = list(
        agentgrep.iter_key_value_rows(
            connection,
            table,
            exact_keys=("aiService.prompts", "workbench.panel.chat.composerData"),
            key_prefixes=("workbench.panel.aichat.view",),
        ),
    )

    assert fetched == list(expected_rows)
    key_scans = [trace for trace in traces if trace.upper().startswith("SELECT KEY FROM")]
    assert key_scans
    assert " WHERE " in key_scans[-1].upper()
    assert " LIKE " not in key_scans[-1].upper()
    assert "COLLATE NOCASE" in key_scans[-1].upper()
    assert "VALUE" not in key_scans[-1].upper()
    value_fetches = [trace for trace in traces if trace.upper().startswith("SELECT VALUE FROM")]
    assert len(value_fetches) == expected_value_fetches
    assert all(" WHERE KEY = " in trace.upper() for trace in value_fetches)


def test_cursor_state_parser_skips_irrelevant_blob_values(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large non-matching ``cursorDiskKV`` blobs are never fetched by the parser.

    The fixture mirrors a real Cursor database where a few small chat and
    prompt keys sit beside many large unrelated blobs; the traced SQL
    proves value reads stay keyed to the matching rows.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    db_path = home / ".cursor" / "state.vscdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    _ = connection.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    irrelevant_keys = [f"editor.cache.{index}" for index in range(20)] + [
        f"telemetry.blob.{index}" for index in range(20)
    ]
    large_blob = b"x" * (256 * 1024)
    for key in irrelevant_keys:
        _ = connection.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (key, large_blob))
    matching_payloads = {
        "aiService.prompts": json.dumps(
            {"prompts": [{"text": "serenity blob prompt", "commandType": 1}]},
        ),
        "workbench.panel.chat.composerData": json.dumps(
            {"messages": [{"role": "user", "content": "bliss blob prompt"}]},
        ),
    }
    for key, value in matching_payloads.items():
        _ = connection.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (key, value))
    connection.commit()
    connection.close()

    traces: list[str] = []
    original_open_readonly_sqlite = agentgrep.adapters.cursor_ide.open_readonly_sqlite

    def traced_open_readonly_sqlite(path: pathlib.Path) -> sqlite3.Connection:
        traced_connection = t.cast("sqlite3.Connection", original_open_readonly_sqlite(path))
        traced_connection.set_trace_callback(traces.append)
        return traced_connection

    monkeypatch.setattr(
        agentgrep.adapters.cursor_ide,
        "open_readonly_sqlite",
        traced_open_readonly_sqlite,
    )

    sources = agentgrep.discover_sources(
        home,
        ("cursor-ide",),
        agentgrep.BackendSelection(None, None, None),
    )
    state_sources = [source for source in sources if source.path == db_path]
    assert len(state_sources) == 1
    records = list(agentgrep.iter_source_records(state_sources[0]))

    assert sorted(record.text for record in records) == [
        "bliss blob prompt",
        "serenity blob prompt",
    ]
    value_fetches = [trace for trace in traces if trace.upper().startswith("SELECT VALUE FROM")]
    assert len(value_fetches) == len(matching_payloads)
    for trace in value_fetches:
        assert all(key not in trace for key in irrelevant_keys)
    assert not [trace for trace in traces if " LIKE " in trace.upper()]


class ProtobufTextCase(t.NamedTuple):
    """Parametrized case for :func:`agentgrep.iter_protobuf_text_fields`."""

    test_id: str
    data: bytes
    min_length: int
    expected: list[str]


_PROTOBUF_TEXT_CASES: tuple[ProtobufTextCase, ...] = (
    ProtobufTextCase("leaf-text", b"\x0a\x05hello", 2, ["hello"]),
    ProtobufTextCase("nested-message-recurses", b"\x0a\x07\x0a\x05world", 2, ["world"]),
    ProtobufTextCase("varint-field-skipped", b"\x08\x96\x01", 2, []),
    ProtobufTextCase("two-text-fields", b"\x0a\x05alpha\x12\x04beta", 2, ["alpha", "beta"]),
    ProtobufTextCase("min-length-filters-short", b"\x0a\x05hello", 8, []),
    ProtobufTextCase("truncated-length-stops", b"\x0a\x05hel", 2, []),
    ProtobufTextCase("empty-input", b"", 2, []),
)


@pytest.mark.parametrize(
    ProtobufTextCase._fields,
    _PROTOBUF_TEXT_CASES,
    ids=[case.test_id for case in _PROTOBUF_TEXT_CASES],
)
def test_iter_protobuf_text_fields(
    test_id: str,
    data: bytes,
    min_length: int,
    expected: list[str],
) -> None:
    """The schema-less protobuf walker recovers text and skips non-text fields."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert list(agentgrep.iter_protobuf_text_fields(data, min_length=min_length)) == expected


class CursorPromptShapeCase(t.NamedTuple):
    """Parametrized case for :func:`agentgrep.iter_cursor_prompt_candidates`."""

    test_id: str
    value: object
    expected: list[str]


_CURSOR_PROMPT_SHAPE_CASES: tuple[CursorPromptShapeCase, ...] = (
    CursorPromptShapeCase(
        "prompts-wrapper",
        {"prompts": [{"text": "first prompt", "commandType": 1}]},
        ["first prompt"],
    ),
    CursorPromptShapeCase(
        "bare-list-with-marker",
        [{"text": "second prompt", "commandType": 2}],
        ["second prompt"],
    ),
    CursorPromptShapeCase(
        "bare-list-without-marker-ignored",
        [{"text": "not a prompt"}],
        [],
    ),
    CursorPromptShapeCase(
        "empty-text-skipped",
        {"prompts": [{"text": "", "commandType": 1}]},
        [],
    ),
    CursorPromptShapeCase(
        "messages-shape-ignored",
        {"messages": [{"role": "user", "content": "hi"}]},
        [],
    ),
)


@pytest.mark.parametrize(
    CursorPromptShapeCase._fields,
    _CURSOR_PROMPT_SHAPE_CASES,
    ids=[case.test_id for case in _CURSOR_PROMPT_SHAPE_CASES],
)
def test_iter_cursor_prompt_candidates(
    test_id: str,
    value: object,
    expected: list[str],
) -> None:
    """Cursor ``aiService.prompts`` entries surface as user prompts."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    candidates = list(agentgrep.iter_cursor_prompt_candidates(value))
    assert [candidate.text for candidate in candidates] == expected
    assert all(candidate.role == "user" for candidate in candidates)


def test_cursor_cli_prompt_history_surfaces_user_prompts(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``~/.config/cursor/prompt_history.json`` becomes cursor-cli prompt records."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    history = home / ".config" / "cursor" / "prompt_history.json"
    history.parent.mkdir(parents=True)
    _ = history.write_text(
        json.dumps(["serenity prompt", "bliss prompt", "serenity prompt"]),
        encoding="utf-8",
    )

    sources = agentgrep.discover_sources(
        home,
        ("cursor-cli",),
        agentgrep.BackendSelection(None, None, None),
    )
    history_sources = [s for s in sources if s.store == "cursor-cli.prompt_history"]
    assert len(history_sources) == 1

    records = list(agentgrep.iter_source_records(history_sources[0]))
    assert [r.text for r in records] == ["serenity prompt", "bliss prompt"]
    assert all(r.role == "user" and r.agent == "cursor-cli" for r in records)
    assert records[0].timestamp is not None


def test_cursor_cli_chats_db_is_opt_in_and_extracts_protobuf_text(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chats ``store.db`` is inspectable-only and yields readable blob text."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    db_path = home / ".config" / "cursor" / "chats" / "phash" / "sess-1234" / "store.db"
    db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(db_path)
    _ = connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    _ = connection.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    message = "Reviewing the engine lazy imports for merge readiness"
    inner = b"\x0a" + bytes([len(message)]) + message.encode("utf-8")
    blob = b"\x0a" + bytes([len(inner)]) + inner
    _ = connection.execute("INSERT INTO blobs VALUES (?, ?)", ("h1", blob))
    connection.commit()
    connection.close()

    backends = agentgrep.BackendSelection(None, None, None)
    default_sources = agentgrep.discover_sources(home, ("cursor-cli",), backends)
    assert not any(s.store == "cursor-cli.chats" for s in default_sources)

    inventory = agentgrep.discover_sources(
        home,
        ("cursor-cli",),
        backends,
        include_non_default=True,
    )
    chat_sources = [s for s in inventory if s.store == "cursor-cli.chats"]
    assert len(chat_sources) == 1

    records = list(agentgrep.iter_source_records(chat_sources[0]))
    assert any("lazy imports" in r.text for r in records)
    assert records[0].session_id == "sess-1234"


_CURSOR_CLI_CHAT_TEXT = "Reviewing the engine lazy imports for merge readiness"
"""One chat blob's text, long enough to clear the protobuf minimum run length."""

_CURSOR_CLI_CHATS_DIGEST = "1a2b3c4d5e6f708192a3b4c5d6e7f809"
"""A Cursor CLI ``chats`` directory name: md5 of the workspace path."""

_CURSOR_CLI_META_SCHEMA = "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)"


def _cursor_cli_meta_value(payload: dict[str, object]) -> str:
    """Encode session metadata the way Cursor CLI stores it: hex-encoded JSON."""
    return json.dumps(payload).encode("utf-8").hex()


def _write_cursor_cli_chats_db(
    path: pathlib.Path,
    *,
    meta_schema: str,
    meta_rows: tuple[tuple[str, ...], ...],
) -> None:
    """Create one Cursor CLI ``chats/*/store.db`` with a single protobuf blob."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        _ = connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        payload = _CURSOR_CLI_CHAT_TEXT.encode("utf-8")
        _ = connection.execute(
            "INSERT INTO blobs VALUES (?, ?)",
            ("h1", b"\x0a" + bytes([len(payload)]) + payload),
        )
        if meta_schema:
            _ = connection.execute(meta_schema)
        for row in meta_rows:
            placeholders = ", ".join("?" * len(row))
            _ = connection.execute(f"INSERT INTO meta VALUES ({placeholders})", row)
        connection.commit()
    finally:
        connection.close()


def _cursor_cli_chat_records(
    agentgrep: t.Any,
    home: pathlib.Path,
) -> list[t.Any]:
    """Discover the opt-in chats store and parse it, as an inventory caller would."""
    sources = agentgrep.discover_sources(
        home,
        ("cursor-cli",),
        agentgrep.BackendSelection(None, None, None),
        include_non_default=True,
    )
    chat_sources = [source for source in sources if source.store == "cursor-cli.chats"]
    assert len(chat_sources) == 1
    return list(agentgrep.iter_source_records(chat_sources[0]))


class CursorCliChatsMetaCase(t.NamedTuple):
    """One ``meta`` table shape and the model its chat records may carry."""

    test_id: str
    meta_schema: str
    meta_rows: tuple[tuple[str, ...], ...]
    expected_model: str | None


_CURSOR_CLI_CHATS_META_CASES: tuple[CursorCliChatsMetaCase, ...] = (
    CursorCliChatsMetaCase(
        test_id="hex-json-names-the-model",
        meta_schema=_CURSOR_CLI_META_SCHEMA,
        meta_rows=(("0", _cursor_cli_meta_value({"lastUsedModel": "claude-4.5-sonnet"})),),
        expected_model="claude-4.5-sonnet",
    ),
    CursorCliChatsMetaCase(
        test_id="default-sentinel-is-not-a-model",
        meta_schema=_CURSOR_CLI_META_SCHEMA,
        meta_rows=(("0", _cursor_cli_meta_value({"lastUsedModel": "default"})),),
        expected_model=None,
    ),
    CursorCliChatsMetaCase(
        test_id="session-metadata-without-a-model",
        meta_schema=_CURSOR_CLI_META_SCHEMA,
        meta_rows=(("0", _cursor_cli_meta_value({"agentId": "agent-1"})),),
        expected_model=None,
    ),
    CursorCliChatsMetaCase(
        test_id="value-is-not-hex",
        meta_schema=_CURSOR_CLI_META_SCHEMA,
        meta_rows=(("0", "not hex at all"),),
        expected_model=None,
    ),
    CursorCliChatsMetaCase(
        test_id="no-row-keyed-zero",
        meta_schema=_CURSOR_CLI_META_SCHEMA,
        meta_rows=(("1", _cursor_cli_meta_value({"lastUsedModel": "claude-4.5-sonnet"})),),
        expected_model=None,
    ),
    # The store is unofficial and migrated in place. A `SELECT value` against a
    # `meta` that has no such column raises into the scan's own
    # `except sqlite3.DatabaseError`, which would not fail loudly — it would
    # turn every chat record in the store into nothing.
    CursorCliChatsMetaCase(
        test_id="meta-without-a-value-column-keeps-the-records",
        meta_schema="CREATE TABLE meta (key TEXT PRIMARY KEY)",
        meta_rows=(("0",),),
        expected_model=None,
    ),
    CursorCliChatsMetaCase(
        test_id="no-meta-table-keeps-the-records",
        meta_schema="",
        meta_rows=(),
        expected_model=None,
    ),
)


@pytest.mark.parametrize(
    CursorCliChatsMetaCase._fields,
    _CURSOR_CLI_CHATS_META_CASES,
    ids=[case.test_id for case in _CURSOR_CLI_CHATS_META_CASES],
)
def test_cursor_cli_chats_model_comes_from_the_meta_row(
    test_id: str,
    meta_schema: str,
    meta_rows: tuple[tuple[str, ...], ...],
    expected_model: str | None,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session model is the ``meta`` row's ``lastUsedModel``, or nothing."""
    _ = test_id
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _write_cursor_cli_chats_db(
        home / ".config" / "cursor" / "chats" / _CURSOR_CLI_CHATS_DIGEST / "sess-1" / "store.db",
        meta_schema=meta_schema,
        meta_rows=meta_rows,
    )

    records = _cursor_cli_chat_records(agentgrep, home)

    assert [record.text for record in records] == [_CURSOR_CLI_CHAT_TEXT]
    assert {record.model for record in records} == {expected_model}


class CursorCliChatsDigestCase(t.NamedTuple):
    """One ``chats/<project_hash>/`` directory name and the hash it may report."""

    test_id: str
    project_dir: str
    expected_cwd_hash: str | None


_CURSOR_CLI_CHATS_DIGEST_CASES: tuple[CursorCliChatsDigestCase, ...] = (
    CursorCliChatsDigestCase(
        test_id="digest-directory-is-a-cwd-hash",
        project_dir=_CURSOR_CLI_CHATS_DIGEST,
        expected_cwd_hash=_CURSOR_CLI_CHATS_DIGEST,
    ),
    CursorCliChatsDigestCase(
        test_id="non-digest-directory-is-not-a-cwd-hash",
        project_dir="phash",
        expected_cwd_hash=None,
    ),
)


@pytest.mark.parametrize(
    CursorCliChatsDigestCase._fields,
    _CURSOR_CLI_CHATS_DIGEST_CASES,
    ids=[case.test_id for case in _CURSOR_CLI_CHATS_DIGEST_CASES],
)
def test_cursor_cli_chats_cwd_hash_needs_a_digest(
    test_id: str,
    project_dir: str,
    expected_cwd_hash: str | None,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chats directory becomes a ``cwd_hash`` only when it has a digest's shape.

    The literal workspace path is nowhere in this store, so ``cwd`` stays unset
    however the directory is named — agentgrep does not hash a path it recovered
    elsewhere to manufacture an identity Cursor never wrote, and it does not
    promote a sibling directory's name to one either.
    """
    _ = test_id
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _write_cursor_cli_chats_db(
        home / ".config" / "cursor" / "chats" / project_dir / "sess-1" / "store.db",
        meta_schema=_CURSOR_CLI_META_SCHEMA,
        meta_rows=(),
    )

    records = _cursor_cli_chat_records(agentgrep, home)

    assert records
    assert {None if record.origin is None else record.origin.cwd_hash for record in records} == {
        expected_cwd_hash
    }
    assert all(record.origin is None or record.origin.cwd is None for record in records)


def test_cursor_ide_workspace_state_extracts_aiservice_prompts(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-workspace ``state.vscdb`` surfaces its ``aiService.prompts`` history."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    workspace_root = agentgrep._cursor_ide_workspace_root(home)
    db_path = workspace_root / "wshash" / "state.vscdb"
    db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(db_path)
    _ = connection.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
    _ = connection.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        (
            "aiService.prompts",
            json.dumps({"prompts": [{"text": "serenity workspace prompt", "commandType": 1}]}),
        ),
    )
    connection.commit()
    connection.close()

    sources = agentgrep.discover_sources(
        home,
        ("cursor-ide",),
        agentgrep.BackendSelection(None, None, None),
    )
    workspace_sources = [s for s in sources if s.store == "cursor-ide.workspace_state"]
    assert len(workspace_sources) == 1

    records = list(agentgrep.iter_source_records(workspace_sources[0]))
    assert [r.text for r in records] == ["serenity workspace prompt"]
    assert records[0].role == "user"
    assert records[0].agent == "cursor-ide"


@pytest.mark.parametrize(
    CursorWorkspaceOriginCase._fields,
    _CURSOR_WORKSPACE_ORIGIN_CASES,
    ids=[case.test_id for case in _CURSOR_WORKSPACE_ORIGIN_CASES],
)
def test_cursor_ide_workspace_state_has_origin_summary(
    test_id: str,
    workspace_dir: str,
    workspace_payload: dict[str, object] | None,
    expected_summary: SourceOriginSummary | None,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-workspace Cursor state sources expose conservative origin summaries."""
    _ = test_id
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    workspace_root = agentgrep._cursor_ide_workspace_root(home)
    workspace_path = workspace_root / workspace_dir
    db_path = workspace_path / "state.vscdb"
    if workspace_payload is not None:
        workspace_path.mkdir(parents=True)
        _ = (workspace_path / "workspace.json").write_text(
            json.dumps(workspace_payload),
            encoding="utf-8",
        )
    _write_cursor_state_db(db_path)

    sources = agentgrep.discover_sources(
        home,
        ("cursor-ide",),
        agentgrep.BackendSelection(None, None, None),
    )
    workspace_sources = [s for s in sources if s.store == "cursor-ide.workspace_state"]

    assert len(workspace_sources) == 1
    assert workspace_sources[0].origin_summary == expected_summary


def _write_cursor_state_db(path: pathlib.Path) -> None:
    """Create a minimal valid Cursor IDE ``state.vscdb`` (empty ItemTable)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        _ = connection.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        connection.commit()
    finally:
        connection.close()


def test_discover_cursor_ide_finds_native_global_state(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The global state.vscdb is discovered via the ide_global root on the native path."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    global_db = agentgrep._cursor_ide_workspace_root(home).parent / "globalStorage" / "state.vscdb"
    _write_cursor_state_db(global_db)

    sources = agentgrep.discover_sources(
        home,
        ("cursor-ide",),
        agentgrep.BackendSelection(None, None, None),
    )

    global_sources = [
        s for s in sources if s.store == "cursor-ide.state_vscdb" and s.path == global_db
    ]
    assert [s.adapter_id for s in global_sources] == ["cursor_ide.state_vscdb_modern.v1"]


def test_discover_cursor_ide_wsl_bridge_probes_windows_mount(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On WSL, cursor-ide discovery reaches the Windows-host state.vscdb databases."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # Force the WSL branch and point the users-mount root at a fake Windows tree.
    monkeypatch.setattr(agentgrep.discovery, "_is_wsl", lambda: True)
    users_root = tmp_path / "mnt-c-users"
    monkeypatch.setenv("AGENTGREP_WSL_USERS_ROOT", str(users_root))
    cursor_user = users_root / "winuser" / "AppData" / "Roaming" / "Cursor" / "User"
    global_db = cursor_user / "globalStorage" / "state.vscdb"
    workspace_db = cursor_user / "workspaceStorage" / "h" / "state.vscdb"
    _write_cursor_state_db(global_db)
    _write_cursor_state_db(workspace_db)

    sources = agentgrep.discover_sources(
        home,
        ("cursor-ide",),
        agentgrep.BackendSelection(None, None, None),
    )

    stores_by_path = {s.path: s.store for s in sources}
    assert stores_by_path.get(global_db) == "cursor-ide.state_vscdb"
    assert stores_by_path.get(workspace_db) == "cursor-ide.workspace_state"


def test_find_discovers_sources_and_filters_pattern(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    codex_history = home / ".codex" / "history.json"
    codex_history.parent.mkdir(parents=True, exist_ok=True)
    _ = codex_history.write_text("[]", encoding="utf-8")

    cursor_db = home / ".cursor" / "state.vscdb"
    cursor_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(cursor_db)
    connection.close()

    sources = agentgrep.discover_sources(
        home,
        ("codex", "cursor-ide"),
        agentgrep.BackendSelection(None, None, None),
    )
    records = agentgrep.find_sources("state", sources, None)

    assert len(records) == 1
    assert records[0].agent == "cursor-ide"
    assert records[0].path.name == "state.vscdb"


def test_display_path_collapses_home_and_marks_directories(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    assert agentgrep.format_display_path(home / ".codex" / "sessions", directory=True) == (
        "~/.codex/sessions/"
    )
    assert (
        agentgrep.format_display_path(
            home / ".codex" / "sessions" / "rollout.jsonl",
        )
        == "~/.codex/sessions/rollout.jsonl"
    )
    assert agentgrep.format_display_path(home, directory=True) == "~/"
    assert (
        agentgrep.format_display_path(
            pathlib.Path("~/.codex/sessions"),
            directory=True,
        )
        == "~/.codex/sessions/"
    )
    assert (
        agentgrep.format_display_path(
            pathlib.Path(f"{home}-other") / "sessions",
            directory=True,
        )
        == f"{home}-other/sessions/"
    )


def test_search_record_serialization_uses_private_paths(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=home / ".codex" / "sessions" / "rollout.jsonl",
        text="serenity and bliss",
    )

    payload = agentgrep.serialize_search_record(record)

    assert payload["path"] == "~/.codex/sessions/rollout.jsonl"
    assert str(home) not in json.dumps(payload)


def test_text_outputs_use_private_paths(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    find_record = agentgrep.FindRecord(
        kind="find",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=home / ".codex" / "sessions",
        path_kind="session_file",
    )
    find_args = agentgrep.FindArgs(
        pattern="sessions",
        agents=("codex",),
        limit=None,
        output_mode="text",
        color_mode="auto",
    )

    find_buffer = io.StringIO()
    with contextlib.redirect_stdout(find_buffer):
        agentgrep.print_find_results([find_record], find_args)

    assert "~/.codex/sessions" in find_buffer.getvalue()
    assert str(home) not in find_buffer.getvalue()


def test_source_handle_serialization_uses_private_paths(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=home / ".codex" / "sessions" / "rollout.jsonl",
        path_kind="session_file",
        source_kind="jsonl",
        search_root=home / ".codex" / "sessions",
        mtime_ns=123,
    )

    payload = agentgrep.serialize_source_handle(source)

    assert payload["path"] == "~/.codex/sessions/rollout.jsonl"
    assert payload["search_root"] == "~/.codex/sessions/"
    assert str(home) not in json.dumps(payload)


def test_find_record_serialization_uses_private_paths(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    record = agentgrep.FindRecord(
        kind="find",
        agent="codex",
        store="codex.history",
        adapter_id="codex.history_json.v1",
        path=home / ".codex" / "history.json",
        path_kind="history_file",
    )

    payload = agentgrep.serialize_find_record(record)

    assert payload["path"] == "~/.codex/history.json"
    assert str(home) not in json.dumps(payload)


def test_json_output_falls_back_without_pydantic(monkeypatch: pytest.MonkeyPatch) -> None:
    """``maybe_build_pydantic`` selects the pure-Python serializers.

    This covers the serializer *selection* branch only. It cannot observe the
    import boundary: ``agentgrep`` is already imported by the time it runs, so
    every module-scope ``import pydantic`` is already satisfied and patching
    ``importlib`` cannot un-satisfy it. See
    ``test_cli_json_survives_missing_pydantic`` for the boundary test.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/example.jsonl"),
        text="serenity and bliss",
    )

    original_import_module = agentgrep.importlib.import_module

    def fake_import_module(name: str, package: str | None = None) -> object:
        if name == "pydantic":
            raise ImportError
        return original_import_module(name)

    monkeypatch.setattr(agentgrep.importlib, "import_module", fake_import_module)
    serialize_search, _, serialize_envelope = agentgrep.maybe_build_pydantic()
    serialized = serialize_search(record)
    envelope = serialize_envelope(
        "grep",
        {"patterns": ["serenity"]},
        [serialized],
    )

    assert envelope["schema_version"] == "agentgrep.v1"
    results = t.cast("list[dict[str, object]]", envelope["results"])
    assert results[0]["text"] == "serenity and bliss"


NO_PYDANTIC_RUNNER = '''\
"""Run ``python -m agentgrep`` with ``pydantic`` made un-importable."""

from __future__ import annotations

import runpy
import sys


class PydanticBlocker:
    """Meta-path finder that hides ``pydantic`` from the import system."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "pydantic" or fullname.startswith("pydantic."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


sys.meta_path.insert(0, PydanticBlocker())
sys.argv = ["agentgrep", *sys.argv[1:]]
runpy.run_module("agentgrep", run_name="__main__")
'''


class PydanticFreeCase(t.NamedTuple):
    """A CLI output mode that must survive a pydantic-free interpreter."""

    test_id: str
    flag: str


PYDANTIC_FREE_CASES = [
    PydanticFreeCase(test_id="json", flag="--json"),
    PydanticFreeCase(test_id="ndjson", flag="--ndjson"),
]


@pytest.mark.xfail(
    reason=(
        "The pydantic-free JSON fallback is not reachable from the CLI. "
        "agentgrep.stores, agentgrep.progress, and agentgrep.query.ast each "
        "subclass pydantic.BaseModel at module scope and are all imported on "
        "the search path, so `python -m agentgrep search` raises ImportError "
        "before maybe_use_pydantic() ever picks a serializer. Restoring the "
        "fallback means giving those three modules a pydantic-free "
        "definition; this test pins the gap so it cannot be silently lost."
    ),
    raises=AssertionError,
)
@pytest.mark.parametrize(
    "case",
    PYDANTIC_FREE_CASES,
    ids=[c.test_id for c in PYDANTIC_FREE_CASES],
)
def test_cli_json_survives_missing_pydantic(
    case: PydanticFreeCase,
    tmp_path: pathlib.Path,
) -> None:
    """The CLI emits JSON when ``pydantic`` cannot be imported.

    Runs the real ``python -m agentgrep`` entry point in a subprocess whose
    import system refuses to load ``pydantic``. A subprocess is the only way
    to observe this boundary: patching ``importlib`` inside an already-imported
    ``agentgrep`` leaves every module-scope ``import pydantic`` satisfied, so
    an in-process test passes whether or not the fallback exists.
    """
    home = tmp_path / "home"
    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "rollout.jsonl"
    write_jsonl(
        session_path,
        [{"type": "response_item", "payload": {"role": "user", "content": "bliss"}}],
    )
    runner = tmp_path / "run_without_pydantic.py"
    runner.write_text(NO_PYDANTIC_RUNNER, encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(runner), "search", "bliss", "--agent", "codex", case.flag],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "HOME": str(home), "NO_COLOR": "1"},
    )

    assert completed.returncode == 0, completed.stderr
    payloads = (
        [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        if case.flag == "--ndjson"
        else [json.loads(completed.stdout)]
    )
    assert payloads
    assert "pydantic" not in completed.stderr


def test_json_output_default_does_not_emit_progress(tmp_path: pathlib.Path) -> None:
    home = tmp_path / "home"
    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "rollout.jsonl"
    write_jsonl(
        session_path,
        [{"type": "response_item", "payload": {"role": "user", "content": "bliss"}}],
    )

    completed = run_agentgrep_cli("grep", "bliss", "--json", env={"HOME": str(home)})

    assert completed.returncode == 0
    payload = t.cast("dict[str, object]", json.loads(completed.stdout))
    assert payload["command"] == "grep"
    assert completed.stderr == ""


def test_json_output_progress_always_writes_stderr_only(tmp_path: pathlib.Path) -> None:
    home = tmp_path / "home"
    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "rollout.jsonl"
    write_jsonl(
        session_path,
        [{"type": "response_item", "payload": {"role": "user", "content": "bliss"}}],
    )

    completed = run_agentgrep_cli(
        "grep",
        "bliss",
        "--json",
        "--progress",
        "always",
        env={"HOME": str(home)},
    )

    assert completed.returncode == 0
    payload = t.cast("dict[str, object]", json.loads(completed.stdout))
    assert payload["command"] == "grep"
    assert "Searching bliss" in completed.stderr
    assert "Search complete: 1 match" in completed.stderr


def test_json_output_progress_color_always_colours_only_stderr(
    tmp_path: pathlib.Path,
) -> None:
    home = tmp_path / "home"
    session_path = home / ".codex" / "sessions" / "2026" / "01" / "01" / "rollout.jsonl"
    write_jsonl(
        session_path,
        [{"type": "response_item", "payload": {"role": "user", "content": "bliss"}}],
    )

    completed = run_agentgrep_cli(
        "--color",
        "always",
        "grep",
        "bliss",
        "--json",
        "--progress",
        "always",
        env={
            "HOME": str(home),
            "NO_COLOR": "",
            "FORCE_COLOR": "",
        },
    )

    assert completed.returncode == 0
    payload = t.cast("dict[str, object]", json.loads(completed.stdout))
    assert payload["command"] == "grep"
    assert "\x1b[" not in completed.stdout
    assert "\x1b[" in completed.stderr
    assert "Search complete:" in strip_ansi(completed.stderr)


def test_progress_no_color_overrides_color_always(monkeypatch: pytest.MonkeyPatch) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    monkeypatch.setenv("NO_COLOR", "1")
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=False,
        color_mode="always",
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    progress.finish(1)
    progress.close()

    out = stream.getvalue()
    assert "\x1b[" not in out
    assert "Search complete: 1 match" in out


def test_progress_force_color_enables_auto_for_non_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=False,
        color_mode="auto",
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    progress.finish(1)
    progress.close()

    out = stream.getvalue()
    assert "\x1b[" in out
    assert "Searching bliss" in strip_ansi(out)


def test_auto_color_disables_dumb_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto mode does not emit SGR escapes to a terminal with no color support."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    class DumbTerminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")

    colors = agentgrep.AnsiColors.for_stream("auto", DumbTerminal())

    assert not colors.enabled


class ProgressLineCase(t.NamedTuple):
    """Formatting case for single-line search progress."""

    test_id: str
    snapshot: object
    expected: str


def _progress_line_cases() -> tuple[ProgressLineCase, ...]:
    """Build progress-line cases after importing the runtime module."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    return (
        ProgressLineCase(
            test_id="source-count-with-detail",
            snapshot=agentgrep.ProgressSnapshot(
                query_label="bliss",
                phase="scanning",
                current=5,
                total=9,
                detail="128 records, 3 source matches",
                matches=10,
                elapsed=1.5,
            ),
            expected=(
                "Searching bliss | scanning 5/9 sources | "
                "128 records, 3 source matches | 10 matches | 1.5s"
            ),
        ),
        ProgressLineCase(
            test_id="detail-without-source-count",
            snapshot=agentgrep.ProgressSnapshot(
                query_label="bliss",
                phase="prefiltering",
                current=None,
                total=None,
                detail="~/.codex/sessions/",
                matches=0,
                elapsed=0.5,
            ),
            expected="Searching bliss | prefiltering ~/.codex/sessions/ | 0 matches | 0.5s",
        ),
    )


_PROGRESS_LINE_CASES = _progress_line_cases()


@pytest.mark.parametrize(
    "case",
    _PROGRESS_LINE_CASES,
    ids=[c.test_id for c in _PROGRESS_LINE_CASES],
)
def test_format_search_progress_line_includes_detail(case: ProgressLineCase) -> None:
    """Current source detail stays visible alongside source counters."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    line = agentgrep.format_search_progress_line(
        case.snapshot,
        colors=agentgrep.AnsiColors.for_stream("never", io.StringIO()),
    )

    assert line == case.expected


def test_non_tty_progress_emits_start_heartbeat_and_finish() -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=False,
        heartbeat_interval=10.0,
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    progress.set_status("scanning", current=1, total=3, detail="one.jsonl")
    progress._last_heartbeat_at -= 30.0
    progress.set_status("scanning", current=2, total=3, detail="two.jsonl")
    progress.finish(4)
    progress.close()

    out = stream.getvalue()
    assert "Searching bliss" in out
    assert "... still searching bliss" in out
    assert "scanning 2/3 sources" in out
    assert "Search complete: 4 matches" in out


def test_answer_now_input_listener_requests_on_blank_enter() -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    class TtyInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    control = agentgrep.SearchControl()
    listener = agentgrep.AnswerNowInputListener(control, stream=TtyInput("\n"))

    listener.start()
    deadline = time.monotonic() + 1.0
    while not control.answer_now_requested() and time.monotonic() < deadline:
        time.sleep(0.01)
    listener.stop()

    assert control.answer_now_requested()


def test_answer_now_input_listener_ignores_nonblank_input() -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())

    class TtyInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    control = agentgrep.SearchControl()
    listener = agentgrep.AnswerNowInputListener(control, stream=TtyInput("not yet\n"))

    listener.start()
    time.sleep(0.01)
    listener.stop()

    assert not control.answer_now_requested()


def test_run_readonly_command_terminates_when_answer_now_requested() -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    control = agentgrep.SearchControl()
    timer = threading.Timer(0.05, control.request_answer_now)
    timer.start()
    try:
        completed = agentgrep.run_readonly_command(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            ],
            control=control,
        )
    finally:
        timer.cancel()

    assert control.answer_now_requested()
    assert completed.returncode != 0


def test_tty_progress_renders_spinner_and_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    monkeypatch.delenv("NO_COLOR", raising=False)
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=True,
        color_mode="always",
        refresh_interval=0.01,
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    progress.set_status("scanning", current=1, total=2, detail="one.jsonl")
    progress.result_added(1)
    time.sleep(0.03)
    progress.finish(1)
    progress.close()

    out = stream.getvalue()
    plain = strip_ansi(out)
    assert "Searching bliss" in plain
    assert "scanning 1/2 sources" in plain
    assert any(f"\x1b[36m{frame}\x1b[0m" in out for frame in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
    assert "\x1b[35mbliss\x1b[0m" in out
    assert "\x1b[33m1 match\x1b[0m" in out
    assert "\r\x1b[2K" in out


def test_tty_progress_renders_answer_now_hint() -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=True,
        color_mode="never",
        refresh_interval=0.01,
        answer_now_hint=True,
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    time.sleep(0.03)
    progress.answer_now(3)

    out = stream.getvalue()
    assert "[Press enter, answer now]" in out
    assert "Answering now: 3 matches" in out
    assert out.endswith("\n")


def test_tty_progress_render_fits_terminal_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY progress renders must not wrap into uncleared terminal rows."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    columns = 72
    monkeypatch.setattr(
        agentgrep.shutil,
        "get_terminal_size",
        lambda fallback: os.terminal_size((columns, 24)),
    )
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=True,
        color_mode="never",
        refresh_interval=100.0,
        answer_now_hint=True,
    )
    query = agentgrep.SearchQuery(
        terms=("libtmux",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    progress._stop_tty_thread()
    progress.set_status(
        "scanning",
        current=8,
        total=3807,
        detail="128 records, 0 source matches",
    )
    progress.result_added(76)
    progress._render_tty("⠋")

    rendered = stream.getvalue().split("\r\033[2K")[-1]
    assert "\n" not in rendered
    assert len(strip_ansi(rendered)) <= columns


def test_tty_progress_answer_now_hint_is_white(monkeypatch: pytest.MonkeyPatch) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    monkeypatch.delenv("NO_COLOR", raising=False)
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=True,
        color_mode="always",
        refresh_interval=0.01,
        answer_now_hint=True,
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    time.sleep(0.03)
    progress.answer_now(1)

    assert "\x1b[37m[Press enter, answer now]\x1b[0m" in stream.getvalue()


def test_tty_progress_interrupt_preserves_current_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    monkeypatch.delenv("NO_COLOR", raising=False)
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=True,
        color_mode="never",
        refresh_interval=0.01,
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    progress.set_status("scanning", current=118, total=126, detail="rollout.jsonl")
    progress.result_added(109)
    time.sleep(0.03)
    progress.interrupt()

    out = stream.getvalue()
    assert "Searching bliss | scanning 118/126 sources | rollout.jsonl | 109 matches" in out
    assert out.endswith("\n")
    assert "\r\x1b[2KSearching bliss | scanning 118/126 sources | rollout.jsonl" in out


def test_tty_progress_prefilter_uses_private_directory_path(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=True,
        color_mode="never",
        refresh_interval=0.01,
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    progress.prefilter_started(home / ".codex" / "sessions")
    time.sleep(0.03)
    progress.interrupt()

    out = stream.getvalue()
    assert "prefiltering ~/.codex/sessions/" in out
    assert str(home) not in out


def test_non_tty_progress_interrupt_emits_current_summary() -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    stream = io.StringIO()
    progress = agentgrep.ConsoleSearchProgress(
        enabled=True,
        stream=stream,
        tty=False,
        color_mode="never",
        heartbeat_interval=10.0,
    )
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )

    progress.start(query)
    progress.set_status("scanning", current=118, total=126, detail="rollout.jsonl")
    progress.result_added(109)
    progress.interrupt()

    out = stream.getvalue()
    assert "Searching bliss\n" in out
    assert "Searching bliss | scanning 118/126 sources | rollout.jsonl | 109 matches" in out


def test_main_handles_keyboard_interrupt_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    args = agentgrep.GrepArgs(
        patterns=("bliss",),
        agents=("codex",),
        scope="prompts",
        case_mode="smart",
        pattern_mode="regex",
        invert_match=False,
        count_only=False,
        files_with_matches=False,
        only_matching=False,
        no_dedupe=False,
        line_number=None,
        heading=None,
        limit=None,
        vimgrep=False,
        column=False,
        output_mode="text",
        color_mode="never",
        progress_mode="auto",
    )

    def parse_args(argv: cabc.Sequence[str] | None = None) -> object:
        return args

    def run_grep_command(args: object) -> int:
        raise KeyboardInterrupt

    def exit_on_sigint() -> t.NoReturn:
        raise SystemExit(130)

    monkeypatch.setattr(agentgrep, "parse_args", parse_args)
    monkeypatch.setattr(agentgrep, "run_grep_command", run_grep_command)
    monkeypatch.setattr(agentgrep, "_exit_on_sigint", exit_on_sigint)

    with pytest.raises(SystemExit) as excinfo:
        agentgrep.main(["grep", "bliss"])

    assert excinfo.value.code == 130
    captured = capsys.readouterr()
    assert "Interrupted by user." in captured.err
    assert "Traceback" not in captured.err


def test_exit_on_sigint_posix_installs_default_handler_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(sys, "platform", "linux")
    calls: list[tuple[str, int, object]] = []

    def signal_handler(sig: int, handler: object) -> object:
        calls.append(("signal", sig, handler))
        return None

    def raise_signal(sig: int) -> None:
        calls.append(("raise_signal", sig, None))
        raise SystemExit(130)

    monkeypatch.setattr(signal, "signal", signal_handler)
    monkeypatch.setattr(signal, "raise_signal", raise_signal)

    with pytest.raises(SystemExit) as excinfo:
        agentgrep._exit_on_sigint()

    assert excinfo.value.code == 130
    assert calls == [
        ("signal", signal.SIGINT, signal.SIG_IGN),
        ("signal", signal.SIGINT, signal.SIG_DFL),
        ("raise_signal", signal.SIGINT, None),
    ]


def test_exit_on_sigint_windows_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(sys, "platform", "win32")

    def fail_if_called(*args: object) -> None:
        msg = "signal APIs should not be called on Windows fallback"
        raise AssertionError(msg)

    monkeypatch.setattr(signal, "signal", fail_if_called)
    monkeypatch.setattr(signal, "raise_signal", fail_if_called)

    with pytest.raises(SystemExit) as excinfo:
        agentgrep._exit_on_sigint()

    assert excinfo.value.code == 130


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX signal semantics only; Windows uses exit-code 130",
)
def test_exit_on_sigint_produces_wifsignaled_sigint() -> None:
    runner = (
        "import signal\n"
        "signal.signal(signal.SIGINT, signal.default_int_handler)\n"
        "from agentgrep import _exit_on_sigint\n"
        "try:\n"
        "    signal.raise_signal(signal.SIGINT)\n"
        "except KeyboardInterrupt:\n"
        "    _exit_on_sigint()\n"
    )
    src_dir = pathlib.Path(__file__).resolve().parents[1] / "src"
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(
            p for p in (str(src_dir), os.environ.get("PYTHONPATH", "")) if p
        ),
    }

    completed = subprocess.run(
        [sys.executable, "-c", runner],
        env=env,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == -signal.SIGINT, (
        f"expected WIFSIGNALED(SIGINT) (-{int(signal.SIGINT)}), "
        f"got returncode={completed.returncode}; stderr={completed.stderr!r}"
    )


def _make_query(agentgrep: object, agents: tuple[AgentName, ...], terms: tuple[str, ...]) -> object:
    """Build a basic SearchQuery for adapter tests."""
    mod = t.cast("t.Any", agentgrep)
    return mod.SearchQuery(
        terms=terms,
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agents,
        limit=None,
    )


def test_discover_codex_sources_honours_codex_home_env(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CODEX_HOME`` overrides ``${HOME}/.codex`` per the catalogue contract."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    decoy_home = tmp_path / "home"
    alt_root = tmp_path / "elsewhere"
    monkeypatch.setenv("HOME", str(decoy_home))
    monkeypatch.setenv("CODEX_HOME", str(alt_root))
    # Decoy entry under ${HOME}/.codex that should NOT be discovered.
    decoy_history = decoy_home / ".codex" / "history.jsonl"
    decoy_history.parent.mkdir(parents=True, exist_ok=True)
    _ = decoy_history.write_text(
        '{"session_id":"x","ts":1,"text":"decoy"}\n',
        encoding="utf-8",
    )
    # Real entry under ${CODEX_HOME}.
    history_path = alt_root / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    _ = history_path.write_text(
        '{"session_id":"s","ts":1,"text":"libtmux from env"}\n',
        encoding="utf-8",
    )

    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_codex_sources(decoy_home, backends)

    paths = {s.path for s in sources}
    assert history_path in paths
    assert decoy_history not in paths


def test_discover_codex_sqlite_sources_honours_codex_sqlite_home_env(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CODEX_SQLITE_HOME`` points SQLite-backed stores away from ``CODEX_HOME``."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    codex_root = tmp_path / "codex-home"
    sqlite_root = tmp_path / "codex-sqlite"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_root))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(sqlite_root))

    decoy_state = codex_root / "state_5.sqlite"
    decoy_state.parent.mkdir(parents=True, exist_ok=True)
    decoy_state.touch()
    real_state = sqlite_root / "state_5.sqlite"
    real_state.parent.mkdir(parents=True, exist_ok=True)
    real_state.touch()

    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_codex_sources(
        home,
        backends,
        include_non_default=True,
    )

    paths = {source.path for source in sources}
    assert real_state in paths
    assert decoy_state not in paths


def test_search_codex_history_jsonl_uses_modern_text_schema(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current Codex ``history.jsonl`` stores prompts as ``text`` with Unix ``ts``."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    history_path = home / ".codex" / "history.jsonl"
    write_jsonl(
        history_path,
        [
            {
                "session_id": "session-jsonl-1",
                "ts": 1_700_000_000,
                "text": "modern codex prompt schema",
            },
        ],
    )

    backends = agentgrep.BackendSelection(None, None, None)
    query = agentgrep.SearchQuery(
        terms=("modern",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    sources = agentgrep.discover_sources(home, ("codex",), backends)
    records = agentgrep.search_sources(query, sources, backends)

    assert len(records) == 1
    record = records[0]
    assert record.text == "modern codex prompt schema"
    assert record.adapter_id == "codex.history_jsonl.v1"
    assert record.timestamp == "2023-11-14T22:13:20Z"
    assert record.session_id == "session-jsonl-1"
    assert record.conversation_id == "session-jsonl-1"
    assert "version_detection" not in agentgrep.serialize_search_record(record)


def test_search_codex_legacy_root_rollout_json_session(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy root ``rollout-*.json`` sessions are parsed as primary chat."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    session_path = home / ".codex" / "sessions" / "rollout-2025-04-21-abc.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    _ = session_path.write_text(
        json.dumps(
            {
                "session": {
                    "id": "legacy-session-1",
                    "timestamp": "2025-04-21T00:00:00Z",
                    "model": "legacy-model",
                },
                "items": [
                    {
                        "role": "user",
                        "type": "message",
                        "content": "legacy codex prompt",
                    },
                    {
                        "role": "assistant",
                        "type": "message",
                        "content": "legacy codex answer",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    backends = agentgrep.BackendSelection(None, None, None)
    query = agentgrep.SearchQuery(
        terms=("legacy",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    sources = agentgrep.discover_sources(home, ("codex",), backends)
    records = agentgrep.search_sources(query, sources, backends)

    assert [record.text for record in records] == [
        "legacy codex prompt",
        "legacy codex answer",
    ]
    assert {record.adapter_id for record in records} == {"codex.sessions_legacy_json.v1"}
    assert records[0].session_id == "legacy-session-1"
    assert records[0].model == "legacy-model"


def test_source_payload_exposes_codex_history_data_versions(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex history detection follows the concrete file shape, not app freshness."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    codex_home = home / ".codex"
    _ = (codex_home / "version.json").parent.mkdir(parents=True, exist_ok=True)
    _ = (codex_home / "version.json").write_text(
        json.dumps({"latest_version": "9.9.9"}),
        encoding="utf-8",
    )
    _ = (codex_home / "history.json").write_text(
        json.dumps([{"command": "legacy prompt", "timestamp": "2026-01-01T00:00:00Z"}]),
        encoding="utf-8",
    )
    write_jsonl(
        codex_home / "history.jsonl",
        [{"session_id": "session-jsonl-1", "ts": 1_700_000_000, "text": "modern prompt"}],
    )

    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_sources(home, ("codex",), backends)
    payloads = {
        pathlib.Path(source.path).name: agentgrep.serialize_source_handle(source)
        for source in sources
        if source.store == "codex.history"
    }

    current = payloads["history.jsonl"]["version_detection"]
    legacy = payloads["history.json"]["version_detection"]
    assert current == {
        "app_version": None,
        "data_version": "codex.history_jsonl.current",
        "strategy": "shape_inference",
        "confidence": "high",
        "evidence": "history.jsonl object keys include session_id, ts, text",
    }
    assert legacy == {
        "app_version": None,
        "data_version": "codex.history_json.legacy",
        "strategy": "shape_inference",
        "confidence": "high",
        "evidence": "history.json array object keys include command, timestamp",
    }


def test_source_payload_exposes_codex_legacy_session_data_version(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy Codex session JSON reports its concrete legacy data shape."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    session_path = home / ".codex" / "sessions" / "rollout-2025-04-21-abc.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    _ = session_path.write_text(
        json.dumps(
            {
                "session": {"id": "legacy-session-1"},
                "items": [{"role": "user", "content": "legacy prompt"}],
            },
        ),
        encoding="utf-8",
    )

    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_sources(home, ("codex",), backends)
    payload = next(
        agentgrep.serialize_source_handle(source)
        for source in sources
        if source.adapter_id == "codex.sessions_legacy_json.v1"
    )

    assert payload["version_detection"] == {
        "app_version": None,
        "data_version": "codex.sessions.legacy_json.v1",
        "strategy": "shape_inference",
        "confidence": "high",
        "evidence": "legacy session JSON object keys include session, items",
    }


def test_codex_source_version_detection_uses_safe_client_hints(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex uses local metadata files and embedded session metadata without spawning."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    _ = (codex_home / "models_cache.json").write_text(
        json.dumps({"client_version": "0.135.0"}),
        encoding="utf-8",
    )
    write_jsonl(
        codex_home / "history.jsonl",
        [{"session_id": "session-jsonl-1", "ts": 1_700_000_000, "text": "modern prompt"}],
    )
    write_jsonl(
        codex_home / "sessions" / "2026" / "01" / "01" / "rollout.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {"id": "session-1", "cli_version": "0.134.0"},
            },
        ],
    )

    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_sources(home, ("codex",), backends)
    payloads = [agentgrep.serialize_source_handle(source) for source in sources]
    history = next(item for item in payloads if item["adapter_id"] == "codex.history_jsonl.v1")
    session = next(item for item in payloads if item["adapter_id"] == "codex.sessions_jsonl.v1")

    assert history["version_detection"]["app_version"] == "0.135.0"
    assert history["version_detection"]["strategy"] == "shape_inference"
    assert session["version_detection"] == {
        "app_version": "0.134.0",
        "data_version": "codex.sessions.rollout.v1",
        "strategy": "embedded_metadata",
        "confidence": "high",
        "evidence": "session_meta.payload keys include cli_version",
    }


def test_codex_sqlite_source_versions_derive_from_filename_suffix(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex SQLite store suffixes are schema-version evidence."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    codex_home = home / ".codex"
    for filename in ("state_5.sqlite", "memories_1.sqlite", "goals_1.sqlite"):
        path = codex_home / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_codex_sources(home, backends, include_non_default=True)
    payloads = {
        pathlib.Path(source.path).name: agentgrep.serialize_source_handle(source)
        for source in sources
    }

    assert payloads["state_5.sqlite"]["version_detection"]["data_version"] == (
        "codex.state.sqlite.v5"
    )
    assert payloads["memories_1.sqlite"]["version_detection"]["data_version"] == (
        "codex.memories.sqlite.v1"
    )
    assert payloads["goals_1.sqlite"]["version_detection"]["data_version"] == (
        "codex.goals.sqlite.v1"
    )


def test_discover_codex_memory_workspace_in_non_default_inventory(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex markdown memory files are inspectable inventory sources."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    memory_path = home / ".codex" / "memories" / "MEMORY.md"
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    _ = memory_path.write_text("workspace memory note", encoding="utf-8")
    git_path = home / ".codex" / "memories" / ".git" / "ignored.md"
    git_path.parent.mkdir(parents=True, exist_ok=True)
    _ = git_path.write_text("ignored git internals", encoding="utf-8")

    backends = agentgrep.BackendSelection(None, None, None)
    default_sources = agentgrep.discover_codex_sources(home, backends)
    inventory_sources = agentgrep.discover_codex_sources(
        home,
        backends,
        include_non_default=True,
    )

    assert memory_path not in {source.path for source in default_sources}
    memory_sources = [
        source for source in inventory_sources if source.adapter_id == "codex.memories_text.v1"
    ]
    assert [source.path for source in memory_sources] == [memory_path]
    assert memory_sources[0].coverage.value == "inspectable"


def test_claude_source_version_detection_infers_history_and_project_versions(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude detection combines history shape and transcript embedded versions."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    claude_home = home / ".claude"
    write_jsonl(
        claude_home / "history.jsonl",
        [
            {
                "display": "claude prompt",
                "timestamp": 1_700_000_000_000,
                "project": "/tmp/project",
                "sessionId": "session-1",
                "pastedContents": {},
            },
        ],
    )
    write_jsonl(
        claude_home / "projects" / "-tmp-project" / "session-1.jsonl",
        [
            {
                "type": "user",
                "sessionId": "session-1",
                "version": "2.1.157",
                "message": {"role": "user", "content": "project prompt"},
            },
        ],
    )

    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_sources(home, ("claude",), backends)
    payloads = [agentgrep.serialize_source_handle(source) for source in sources]
    history = next(item for item in payloads if item["adapter_id"] == "claude.history_jsonl.v1")
    project = next(item for item in payloads if item["adapter_id"] == "claude.projects_jsonl.v1")

    assert history["version_detection"] == {
        "app_version": None,
        "data_version": "claude.history_jsonl.log_entry.v1",
        "strategy": "shape_inference",
        "confidence": "high",
        "evidence": "history.jsonl object keys include display, timestamp, project",
    }
    assert project["version_detection"] == {
        "app_version": "2.1.157",
        "data_version": "claude.projects_jsonl.message.v1",
        "strategy": "embedded_metadata",
        "confidence": "high",
        "evidence": "project transcript keys include version",
    }


def test_discover_claude_sources_honours_claude_config_dir_env(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CLAUDE_CONFIG_DIR`` overrides the default ``${HOME}/.claude`` root."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    decoy_home = tmp_path / "home"
    alt_root = tmp_path / "claude-config"
    monkeypatch.setenv("HOME", str(decoy_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alt_root))

    decoy_history = decoy_home / ".claude" / "history.jsonl"
    write_jsonl(decoy_history, [{"display": "decoy", "timestamp": 1}])
    history_path = alt_root / "history.jsonl"
    write_jsonl(history_path, [{"display": "real", "timestamp": 1}])

    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_claude_sources(decoy_home, backends)

    paths = {source.path for source in sources}
    assert history_path in paths
    assert decoy_history not in paths


def test_parse_claude_store_db_returns_message_samples(tmp_path: pathlib.Path) -> None:
    """Claude ``__store.db`` inspection surfaces message-table text."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    db_path = tmp_path / "__store.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE base_messages (
                uuid TEXT PRIMARY KEY,
                session_id TEXT
            );
            CREATE TABLE user_messages (
                uuid TEXT PRIMARY KEY,
                message TEXT,
                timestamp TEXT
            );
            CREATE TABLE assistant_messages (
                uuid TEXT PRIMARY KEY,
                message TEXT,
                timestamp TEXT,
                model TEXT
            );
            CREATE TABLE conversation_summaries (
                leaf_uuid TEXT,
                summary TEXT,
                updated_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO base_messages(uuid, session_id) VALUES (?, ?)",
            ("u1", "session-db-1"),
        )
        connection.execute(
            "INSERT INTO user_messages(uuid, message, timestamp) VALUES (?, ?, ?)",
            ("u1", "sqlite user prompt", "2026-05-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO assistant_messages(uuid, message, timestamp, model) VALUES (?, ?, ?, ?)",
            ("a1", "sqlite assistant answer", "2026-05-01T00:00:01Z", "claude-test"),
        )
        connection.execute(
            "INSERT INTO conversation_summaries(leaf_uuid, summary, updated_at) VALUES (?, ?, ?)",
            ("u1", "sqlite summary", "2026-05-01T00:00:02Z"),
        )
        connection.commit()
    finally:
        connection.close()

    source = agentgrep.SourceHandle(
        agent="claude",
        store="claude.store_db",
        adapter_id="claude.store_sqlite.v1",
        path=db_path,
        path_kind="sqlite_db",
        source_kind="sqlite",
        search_root=None,
        mtime_ns=0,
    )

    records = list(agentgrep.iter_source_records(source))

    assert [record.text for record in records] == [
        "sqlite user prompt",
        "sqlite assistant answer",
        "sqlite summary",
    ]
    assert records[0].kind == "prompt"
    assert records[0].session_id == "session-db-1"
    assert records[1].model == "claude-test"


def test_parse_codex_state_db_returns_thread_and_job_samples(
    tmp_path: pathlib.Path,
) -> None:
    """Codex ``state_5.sqlite`` inspection surfaces prompt-bearing fields."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    db_path = tmp_path / "state_5.sqlite"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                first_user_message TEXT,
                preview TEXT,
                title TEXT,
                updated_at_ms INTEGER
            );
            CREATE TABLE agent_jobs (
                id TEXT PRIMARY KEY,
                thread_id TEXT,
                instruction TEXT,
                updated_at_ms INTEGER
            );
            """
        )
        connection.execute(
            """
            INSERT INTO threads(id, first_user_message, preview, title, updated_at_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "thread-1",
                "codex sqlite prompt",
                "codex sqlite preview",
                "Codex DB",
                1_700_000_000_000,
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_jobs(id, thread_id, instruction, updated_at_ms)
            VALUES (?, ?, ?, ?)
            """,
            ("job-1", "thread-1", "codex job instruction", 1_700_000_000_001),
        )
        connection.commit()
    finally:
        connection.close()

    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.state_db",
        adapter_id="codex.state_sqlite.v1",
        path=db_path,
        path_kind="sqlite_db",
        source_kind="sqlite",
        search_root=None,
        mtime_ns=0,
    )

    records = list(agentgrep.iter_source_records(source))

    assert [record.text for record in records] == [
        "codex sqlite prompt",
        "codex sqlite preview",
        "codex job instruction",
    ]
    assert records[0].kind == "prompt"
    assert records[0].conversation_id == "thread-1"
    assert records[2].metadata["job_id"] == "job-1"


def test_parse_codex_session_index_returns_thread_name_samples(
    tmp_path: pathlib.Path,
) -> None:
    """Codex ``session_index.jsonl`` inspection surfaces thread names."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    index_path = tmp_path / "session_index.jsonl"
    write_jsonl(
        index_path,
        [
            {
                "id": "thread-1",
                "thread_name": "Storage coverage plan",
                "updated_at": "2026-05-30T12:00:00Z",
            },
        ],
    )
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.session_index",
        adapter_id="codex.session_index_jsonl.v1",
        path=index_path,
        path_kind="store_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=0,
        coverage=agentgrep.StoreCoverage.INSPECTABLE,
    )

    records = list(agentgrep.iter_source_records(source))

    assert len(records) == 1
    assert records[0].text == "Storage coverage plan"
    assert records[0].session_id == "thread-1"
    assert records[0].timestamp == "2026-05-30T12:00:00Z"


def test_parse_codex_logs_db_returns_feedback_log_samples(
    tmp_path: pathlib.Path,
) -> None:
    """Codex ``logs_2.sqlite`` inspection surfaces structured log payload text."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    db_path = tmp_path / "logs_2.sqlite"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY,
                ts TEXT,
                level TEXT,
                target TEXT,
                feedback_log_body TEXT,
                thread_id TEXT
            );
            """
        )
        connection.execute(
            """
            INSERT INTO logs(id, ts, level, target, feedback_log_body, thread_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "2026-05-30T12:00:00Z",
                "INFO",
                "codex_core",
                '{"message":"feedback body text"}',
                "thread-1",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.logs_db",
        adapter_id="codex.logs_sqlite.v1",
        path=db_path,
        path_kind="sqlite_db",
        source_kind="sqlite",
        search_root=None,
        mtime_ns=0,
        coverage=agentgrep.StoreCoverage.CATALOG_ONLY,
    )

    records = list(agentgrep.iter_source_records(source))

    assert len(records) == 1
    assert records[0].text == '{"message":"feedback body text"}'
    assert records[0].timestamp == "2026-05-30T12:00:00Z"
    assert records[0].metadata == {"level": "INFO", "target": "codex_core"}


def test_parse_codex_external_imports_returns_ledger_samples(
    tmp_path: pathlib.Path,
) -> None:
    """Codex external-agent import ledger inspection surfaces imported thread ids."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    ledger_path = tmp_path / "external_agent_session_imports.json"
    _ = ledger_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "source_path": "/tmp/source.jsonl",
                        "content_hash": "abc123",
                        "imported_thread_id": "thread-1",
                        "imported_at": "2026-05-30T12:00:00Z",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    source = agentgrep.SourceHandle(
        agent="codex",
        store="codex.external_agent_imports",
        adapter_id="codex.external_imports_json.v1",
        path=ledger_path,
        path_kind="store_file",
        source_kind="json",
        search_root=None,
        mtime_ns=0,
        coverage=agentgrep.StoreCoverage.CATALOG_ONLY,
    )

    records = list(agentgrep.iter_source_records(source))

    assert len(records) == 1
    assert records[0].text == "Imported external agent session thread-1"
    assert records[0].conversation_id == "thread-1"
    assert records[0].metadata == {
        "content_hash": "abc123",
        "source_name": "source.jsonl",
    }


def test_parse_claude_settings_returns_key_summary_sample(tmp_path: pathlib.Path) -> None:
    """Claude settings inspection summarizes keys without indexing raw config values."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    settings_path = tmp_path / "settings.json"
    _ = settings_path.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Read"]},
                "env": {"SECRET": "do-not-index"},
            },
        ),
        encoding="utf-8",
    )
    source = agentgrep.SourceHandle(
        agent="claude",
        store="claude.settings",
        adapter_id="claude.settings_json.v1",
        path=settings_path,
        path_kind="store_file",
        source_kind="json",
        search_root=None,
        mtime_ns=0,
        coverage=agentgrep.StoreCoverage.CATALOG_ONLY,
    )

    records = list(agentgrep.iter_source_records(source))

    assert len(records) == 1
    assert records[0].text == "Claude settings keys: env, permissions"
    assert "do-not-index" not in records[0].text


def test_parse_claude_task_json_returns_task_sample(tmp_path: pathlib.Path) -> None:
    """Claude task JSON inspection surfaces task prose and status metadata."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    task_path = tmp_path / "tasks" / "team" / "1.json"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    _ = task_path.write_text(
        json.dumps(
            {
                "id": "1",
                "subject": "Ship storage coverage",
                "description": "Handle Claude and Codex storage gaps",
                "status": "in_progress",
                "blocks": ["2"],
                "blockedBy": ["0"],
                "metadata": {"source": "test"},
            },
        ),
        encoding="utf-8",
    )
    source = agentgrep.SourceHandle(
        agent="claude",
        store="claude.tasks",
        adapter_id="claude.tasks_json.v1",
        path=task_path,
        path_kind="store_file",
        source_kind="json",
        search_root=None,
        mtime_ns=0,
        coverage=agentgrep.StoreCoverage.INSPECTABLE,
    )

    records = list(agentgrep.iter_source_records(source))

    assert len(records) == 1
    assert records[0].text == "Ship storage coverage\n\nHandle Claude and Codex storage gaps"
    assert records[0].title == "Ship storage coverage"
    assert records[0].metadata == {
        "status": "in_progress",
        "task_id": "1",
        "blocks": ["2"],
        "blocked_by": ["0"],
    }


def test_parse_vscode_chat_session_emits_prompt_and_assistant() -> None:
    """VS Code chat parsing yields paired user/assistant records with tool metadata."""
    from tests.conftest import fixture_path

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = fixture_path("vscode.chat_sessions", "example.json")
    source = agentgrep.SourceHandle(
        agent="vscode",
        store="vscode.chat_sessions",
        adapter_id="vscode.chat_sessions_json.v1",
        path=path,
        path_kind="session_file",
        source_kind="json",
        search_root=path.parent,
        mtime_ns=0,
    )

    records = list(agentgrep.iter_source_records(source))

    assert [(r.kind, r.role) for r in records] == [
        ("prompt", "user"),
        ("history", "assistant"),
        ("prompt", "user"),
        ("history", "assistant"),
    ]
    assert records[0].text == "Summarize the build pipeline in this repository"
    # Assistant prose is the no-`kind` response parts joined; the
    # toolInvocationSerialized part between them is skipped.
    assert records[1].text == (
        "The pipeline builds, then tests, then publishes. "
        "See the workflow file for the exact steps."
    )
    assert records[1].metadata["tools"] == ["copilot_readFile"]
    assert records[3].metadata["tools"] == ["copilot_searchWorkspace"]
    assert all(r.session_id == "00000000-0000-4000-8000-000000000000" for r in records)
    assert records[0].timestamp is not None


def test_parse_vscode_chat_session_tolerates_empty_and_draft_turns(
    tmp_path: pathlib.Path,
) -> None:
    """Empty request lists, missing messages, and empty responses don't crash parsing."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "draft.json"
    _ = path.write_text(
        json.dumps(
            {
                "sessionId": "sess-1",
                "requests": [
                    {"timestamp": 1779999665000},
                    {
                        "message": {"text": "only a prompt"},
                        "response": [],
                        "timestamp": 1779999666000,
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    source = agentgrep.SourceHandle(
        agent="vscode",
        store="vscode.chat_sessions",
        adapter_id="vscode.chat_sessions_json.v1",
        path=path,
        path_kind="session_file",
        source_kind="json",
        search_root=path.parent,
        mtime_ns=0,
    )

    records = list(agentgrep.iter_source_records(source))

    assert [(r.kind, r.text) for r in records] == [("prompt", "only a prompt")]


class VscodeJsonlCase(t.NamedTuple):
    """One VS Code ``.jsonl`` mutation-log session and its expected records."""

    test_id: str
    lines: tuple[str, ...]
    expected: tuple[tuple[str, str, str], ...]


VSCODE_JSONL_CASES: tuple[VscodeJsonlCase, ...] = (
    VscodeJsonlCase(
        test_id="snapshot-only-single-turn",
        lines=(
            json.dumps(
                {
                    "kind": 0,
                    "v": {
                        "sessionId": "s",
                        "requests": [
                            {
                                "message": {"text": "hello"},
                                "response": [{"value": "hi there"}],
                                "timestamp": 1779999665000,
                            },
                        ],
                    },
                },
            ),
        ),
        expected=(
            ("prompt", "user", "hello"),
            ("history", "assistant", "hi there"),
        ),
    ),
    VscodeJsonlCase(
        test_id="event-appends-second-turn",
        lines=(
            json.dumps(
                {
                    "kind": 0,
                    "v": {
                        "sessionId": "s",
                        "requests": [
                            {
                                "message": {"text": "first"},
                                "response": [{"value": "r1"}],
                                "timestamp": 1,
                            },
                        ],
                    },
                },
            ),
            json.dumps(
                {
                    "kind": 2,
                    "k": ["requests"],
                    "i": 1,
                    "v": [
                        {
                            "message": {"text": "second"},
                            "response": [{"value": "r2"}],
                            "timestamp": 2,
                        },
                    ],
                },
            ),
        ),
        expected=(
            ("prompt", "user", "first"),
            ("history", "assistant", "r1"),
            ("prompt", "user", "second"),
            ("history", "assistant", "r2"),
        ),
    ),
    VscodeJsonlCase(
        test_id="event-streams-response-parts",
        lines=(
            json.dumps(
                {
                    "kind": 0,
                    "v": {
                        "sessionId": "s",
                        "requests": [
                            {"message": {"text": "q"}, "response": [], "timestamp": 1},
                        ],
                    },
                },
            ),
            json.dumps(
                {"kind": 2, "k": ["requests", 0, "response"], "i": 0, "v": [{"value": "part1 "}]},
            ),
            json.dumps(
                {"kind": 2, "k": ["requests", 0, "response"], "i": 1, "v": [{"value": "part2"}]},
            ),
        ),
        expected=(
            ("prompt", "user", "q"),
            ("history", "assistant", "part1 part2"),
        ),
    ),
    VscodeJsonlCase(
        test_id="skips-non-object-lines",
        lines=(
            json.dumps([]),
            json.dumps(
                {
                    "kind": 0,
                    "v": {
                        "sessionId": "s",
                        "requests": [{"message": {"text": "ok"}, "timestamp": 1}],
                    },
                },
            ),
        ),
        expected=(("prompt", "user", "ok"),),
    ),
    VscodeJsonlCase(
        test_id="truncate-replaces-response-tail",
        lines=(
            json.dumps(
                {
                    "kind": 0,
                    "v": {
                        "sessionId": "s",
                        "requests": [
                            {
                                "message": {"text": "q"},
                                "response": [{"value": "kept "}, {"value": "REPLACED"}],
                                "timestamp": 1,
                            },
                        ],
                    },
                },
            ),
            json.dumps(
                {"kind": 2, "k": ["requests", 0, "response"], "i": 1, "v": [{"value": "new"}]},
            ),
        ),
        expected=(
            ("prompt", "user", "q"),
            ("history", "assistant", "kept new"),
        ),
    ),
    VscodeJsonlCase(
        test_id="no-v-truncates-response",
        lines=(
            json.dumps(
                {
                    "kind": 0,
                    "v": {
                        "sessionId": "s",
                        "requests": [
                            {
                                "message": {"text": "q"},
                                "response": [{"value": "keep"}, {"value": "drop"}],
                                "timestamp": 1,
                            },
                        ],
                    },
                },
            ),
            json.dumps({"kind": 2, "k": ["requests", 0, "response"], "i": 1}),
        ),
        expected=(
            ("prompt", "user", "q"),
            ("history", "assistant", "keep"),
        ),
    ),
    VscodeJsonlCase(
        test_id="requests-replace-drops-stale-turn",
        lines=(
            json.dumps(
                {
                    "kind": 0,
                    "v": {
                        "sessionId": "s",
                        "requests": [{"message": {"text": "stale prompt"}, "timestamp": 1}],
                    },
                },
            ),
            json.dumps(
                {
                    "kind": 2,
                    "k": ["requests"],
                    "i": 0,
                    "v": [{"message": {"text": "current prompt"}, "timestamp": 2}],
                },
            ),
        ),
        expected=(("prompt", "user", "current prompt"),),
    ),
)


@pytest.mark.parametrize(
    "case",
    VSCODE_JSONL_CASES,
    ids=[c.test_id for c in VSCODE_JSONL_CASES],
)
def test_parse_vscode_chat_session_jsonl_event_log(
    case: VscodeJsonlCase,
    tmp_path: pathlib.Path,
) -> None:
    """The ``.jsonl`` mutation log rebuilds turns before the shared extraction runs."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "session.jsonl"
    _ = path.write_text("\n".join(case.lines) + "\n", encoding="utf-8")
    source = agentgrep.SourceHandle(
        agent="vscode",
        store="vscode.chat_sessions",
        adapter_id="vscode.chat_sessions_json.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=path.parent,
        mtime_ns=0,
    )

    records = list(agentgrep.iter_source_records(source))

    assert [(r.kind, r.role, r.text) for r in records] == list(case.expected)


class VscodeDiscoveryCase(t.NamedTuple):
    """A chat-session filename and the on-disk content to seed for discovery."""

    test_id: str
    filename: str
    content: str


VSCODE_DISCOVERY_CASES: tuple[VscodeDiscoveryCase, ...] = (
    VscodeDiscoveryCase(
        test_id="legacy-json-object",
        filename="s.json",
        content=json.dumps({"sessionId": "x", "requests": []}),
    ),
    VscodeDiscoveryCase(
        test_id="current-jsonl-log",
        filename="s.jsonl",
        content=json.dumps({"kind": 0, "v": {"sessionId": "x", "requests": []}}),
    ),
)


@pytest.mark.parametrize(
    "case",
    VSCODE_DISCOVERY_CASES,
    ids=[c.test_id for c in VSCODE_DISCOVERY_CASES],
)
def test_discover_vscode_finds_json_and_jsonl_sessions(
    case: VscodeDiscoveryCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both the legacy ``.json`` and current ``.jsonl`` chat sessions are discovered."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    roaming = tmp_path / "Roaming"
    monkeypatch.setenv("VSCODE_APPDATA", str(roaming))
    session = (
        roaming / "Code" / "User" / "workspaceStorage" / "hash1" / "chatSessions" / case.filename
    )
    session.parent.mkdir(parents=True)
    _ = session.write_text(case.content, encoding="utf-8")

    sources = agentgrep.discover_sources(
        home,
        ("vscode",),
        agentgrep.BackendSelection(None, None, None),
    )

    assert session in {s.path for s in sources}


def test_vscode_workspace_cwd_resolves_wsl_remote(tmp_path: pathlib.Path) -> None:
    """A sibling workspace.json maps a WSL remote folder URI to the Linux path."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    workspace = tmp_path / "workspaceStorage" / "abc123"
    sessions = workspace / "chatSessions"
    sessions.mkdir(parents=True)
    _ = (workspace / "workspace.json").write_text(
        json.dumps({"folder": "vscode-remote://wsl+Ubuntu/home/u/work/proj"}),
        encoding="utf-8",
    )

    assert agentgrep._vscode_workspace_cwd(sessions / "s.json") == "/home/u/work/proj"

    # A windowless session has no sibling workspace.json -> no cwd.
    lonely = tmp_path / "globalStorage" / "emptyWindowChatSessions" / "w.json"
    lonely.parent.mkdir(parents=True)
    assert agentgrep._vscode_workspace_cwd(lonely) is None


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("vscode-remote://wsl+Ubuntu/home/u/proj", "/home/u/proj"),
        ("vscode-remote://wsl%2BUbuntu/home/u/proj", "/home/u/proj"),
        ("file:///home/u/proj", "/home/u/proj"),
        ("file:///home/u/with%20space", "/home/u/with space"),
        ("vscode-remote://ssh-remote+host/srv/code", "/srv/code"),
        ("untitled:Untitled-1", None),
    ],
)
def test_vscode_uri_to_path_variants(uri: str, expected: str | None) -> None:
    """Folder URIs map to local paths; non-file/remote schemes return None."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep._vscode_uri_to_path(uri) == expected


def test_discover_vscode_finds_workspace_chat_sessions(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workspace chat transcripts under an edition's User dir are discovered."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    roaming = tmp_path / "Roaming"
    monkeypatch.setenv("VSCODE_APPDATA", str(roaming))
    session = roaming / "Code" / "User" / "workspaceStorage" / "hash1" / "chatSessions" / "s.json"
    session.parent.mkdir(parents=True)
    _ = session.write_text(
        json.dumps({"sessionId": "x", "requests": []}),
        encoding="utf-8",
    )

    sources = agentgrep.discover_sources(
        home,
        ("vscode",),
        agentgrep.BackendSelection(None, None, None),
    )

    assert session in {s.path for s in sources}


def test_discover_vscode_wsl_bridge_probes_windows_mount(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On WSL, discovery reaches Windows-host chat under the users mount root."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("VSCODE_APPDATA", raising=False)
    # Force the WSL branch so the bridge is exercised on any host, and point
    # the users-mount root at a fake Windows profile tree.
    monkeypatch.setattr(agentgrep.discovery, "_is_wsl", lambda: True)
    users_root = tmp_path / "mnt-c-users"
    monkeypatch.setenv("AGENTGREP_WSL_USERS_ROOT", str(users_root))
    session = (
        users_root
        / "winuser"
        / "AppData"
        / "Roaming"
        / "Code"
        / "User"
        / "workspaceStorage"
        / "h"
        / "chatSessions"
        / "s.json"
    )
    session.parent.mkdir(parents=True)
    _ = session.write_text(
        json.dumps({"sessionId": "x", "requests": []}),
        encoding="utf-8",
    )

    sources = agentgrep.discover_sources(
        home,
        ("vscode",),
        agentgrep.BackendSelection(None, None, None),
    )

    assert session in {s.path for s in sources}


def test_vscode_inline_history_discovers_and_extracts_prompts(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The global state.vscdb inline-chat-history key yields one prompt per entry."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    roaming = tmp_path / "Roaming"
    monkeypatch.setenv("VSCODE_APPDATA", str(roaming))
    global_storage = roaming / "Code" / "User" / "globalStorage"
    global_storage.mkdir(parents=True)
    connection = sqlite3.connect(global_storage / "state.vscdb")
    _ = connection.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
    _ = connection.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("inline-chat-history", json.dumps(["rename this symbol", "add a docstring"])),
    )
    # An auth-shaped key in the same db must never be read.
    _ = connection.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("secret://github-copilot/token", "do-not-index"),
    )
    connection.commit()
    connection.close()

    sources = agentgrep.discover_sources(
        home,
        ("vscode",),
        agentgrep.BackendSelection(None, None, None),
    )
    inline = [s for s in sources if s.adapter_id == "vscode.inline_history_sqlite.v1"]
    assert len(inline) == 1

    records = list(agentgrep.iter_source_records(inline[0]))
    texts = [r.text for r in records]
    assert texts == ["rename this symbol", "add a docstring"]
    assert all(r.kind == "prompt" and r.role == "user" for r in records)
    assert "do-not-index" not in " ".join(texts)


def test_discover_remaining_claude_inventory_sources(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude non-default inventory discovers memory, instruction, and app-state stores."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    claude_home = home / ".claude"
    project_root = home / "work" / "repo"
    project_session = claude_home / "projects" / "repo" / "session.jsonl"
    write_jsonl(project_session, [{"type": "system", "cwd": str(project_root)}])

    paths = [
        claude_home / "CLAUDE.md",
        claude_home / "projects" / "-repo" / "memory" / "MEMORY.md",
        claude_home / "todos" / "agent.json",
        claude_home / "skills" / "review.md",
        claude_home / "skills" / "audit" / "SKILL.md",
        claude_home / "commands" / "ship.md",
        claude_home / "teams" / "storage" / "config.json",
        claude_home / "stats-cache.json",
        claude_home / "sessions" / "session.json",
        claude_home / "context-mode" / "state.json",
        claude_home / "ide" / "bridge.json",
        claude_home / ".last-update-result.json",
        claude_home / "plugins" / "cache" / "example" / ".claude-plugin" / "plugin.json",
        claude_home / "plugins" / "cache" / "example" / "commands" / "ship.md",
        claude_home / "plugins" / "cache" / "example" / "agents" / "reviewer.md",
        claude_home / "plugins" / "cache" / "example" / "skills" / "audit" / "SKILL.md",
        claude_home / "plugins" / "cache" / "example" / "hooks" / "hooks.json",
        claude_home / "chrome" / "native-host.json",
        claude_home / "local" / "install-state.json",
        claude_home / "jobs" / "job.json",
        claude_home / "debug" / "claude.log",
        claude_home / "shell-snapshots" / "snapshot.sh",
        project_root / "CLAUDE.md",
        project_root / ".claude" / "commands" / "project.md",
        project_root / ".claude" / "agents" / "reviewer.md",
        project_root / ".claude" / "skills" / "audit" / "SKILL.md",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("{}" if path.suffix == ".json" else "sample", encoding="utf-8")

    backends = agentgrep.BackendSelection(None, None, None)
    default_sources = agentgrep.discover_claude_sources(home, backends)
    inventory_sources = agentgrep.discover_claude_sources(
        home,
        backends,
        include_non_default=True,
    )

    default_paths = {source.path for source in default_sources}
    assert not default_paths.intersection(paths)
    adapter_ids = {source.adapter_id for source in inventory_sources}
    assert {
        "claude.projects_memory_text.v1",
        "claude.memory_text.v1",
        "claude.project_instruction_text.v1",
        "claude.todos_json.v1",
        "claude.skills_text.v1",
        "claude.commands_text.v1",
        "claude.teams_json.v1",
        "claude.plugin_manifest_json.v1",
        "claude.plugin_instruction_text.v1",
        "claude.plugin_hooks_json.v1",
        "claude.app_state_json_summary.v1",
        "claude.file_metadata_summary.v1",
    } <= adapter_ids

    inventory_paths = {source.path for source in inventory_sources}
    assert project_root / "CLAUDE.md" in inventory_paths
    assert project_root / ".claude" / "commands" / "project.md" in inventory_paths


def test_claude_private_inventory_stays_unenumerated(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude private files are documented but not discovered from disk."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    claude_home = home / ".claude"
    private_paths = [
        claude_home / ".credentials.json",
        claude_home / "security_warnings_state_repo.json",
        claude_home / "session-env" / "session.json",
    ]
    for path in private_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("secret", encoding="utf-8")

    sources = agentgrep.discover_claude_sources(
        home,
        agentgrep.BackendSelection(None, None, None),
        include_non_default=True,
    )

    assert not {source.path for source in sources}.intersection(private_paths)


def test_parse_remaining_claude_inventory_samples(tmp_path: pathlib.Path) -> None:
    """Claude inventory parsers expose prompt-adjacent text and redact app state."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    todo_path = tmp_path / "todos" / "agent.json"
    todo_path.parent.mkdir(parents=True, exist_ok=True)
    _ = todo_path.write_text(
        json.dumps(
            {
                "todos": [
                    {
                        "id": "todo-1",
                        "content": "Fix storage parser",
                        "status": "pending",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    team_path = tmp_path / "teams" / "storage" / "config.json"
    team_path.parent.mkdir(parents=True, exist_ok=True)
    _ = team_path.write_text(
        json.dumps(
            {
                "name": "Storage Team",
                "description": "Coordinates storage coverage",
                "createdAt": 1_700_000_000_000,
                "members": [
                    {
                        "name": "worker",
                        "prompt": "Audit non-default storage",
                        "joinedAt": 1_700_000_000_001,
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    app_path = tmp_path / "stats-cache.json"
    _ = app_path.write_text(
        json.dumps({"version": 3, "secret": "do-not-index", "dailyActivity": [1, 2]}),
        encoding="utf-8",
    )
    plugin_manifest_path = tmp_path / "plugin.json"
    _ = plugin_manifest_path.write_text(
        json.dumps({"name": "secret-plugin", "description": "private description"}),
        encoding="utf-8",
    )
    hook_path = tmp_path / "hooks.json"
    _ = hook_path.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"command": "echo do-not-index"}]}}),
        encoding="utf-8",
    )
    raw_path = tmp_path / "debug.log"
    _ = raw_path.write_text("do-not-index\nsecond line\n", encoding="utf-8")

    sources = [
        agentgrep.SourceHandle(
            agent="claude",
            store="claude.todos",
            adapter_id="claude.todos_json.v1",
            path=todo_path,
            path_kind="store_file",
            source_kind="json",
            search_root=None,
            mtime_ns=0,
            coverage=agentgrep.StoreCoverage.INSPECTABLE,
        ),
        agentgrep.SourceHandle(
            agent="claude",
            store="claude.teams",
            adapter_id="claude.teams_json.v1",
            path=team_path,
            path_kind="store_file",
            source_kind="json",
            search_root=None,
            mtime_ns=0,
            coverage=agentgrep.StoreCoverage.INSPECTABLE,
        ),
        agentgrep.SourceHandle(
            agent="claude",
            store="claude.stats_cache",
            adapter_id="claude.app_state_json_summary.v1",
            path=app_path,
            path_kind="store_file",
            source_kind="json",
            search_root=None,
            mtime_ns=0,
            coverage=agentgrep.StoreCoverage.CATALOG_ONLY,
        ),
        agentgrep.SourceHandle(
            agent="claude",
            store="claude.plugins_cache",
            adapter_id="claude.plugin_manifest_json.v1",
            path=plugin_manifest_path,
            path_kind="store_file",
            source_kind="json",
            search_root=None,
            mtime_ns=0,
            coverage=agentgrep.StoreCoverage.INSPECTABLE,
        ),
        agentgrep.SourceHandle(
            agent="claude",
            store="claude.plugins_cache",
            adapter_id="claude.plugin_hooks_json.v1",
            path=hook_path,
            path_kind="store_file",
            source_kind="json",
            search_root=None,
            mtime_ns=0,
            coverage=agentgrep.StoreCoverage.INSPECTABLE,
        ),
        agentgrep.SourceHandle(
            agent="claude",
            store="claude.debug_logs",
            adapter_id="claude.file_metadata_summary.v1",
            path=raw_path,
            path_kind="store_file",
            source_kind="text",
            search_root=None,
            mtime_ns=0,
            coverage=agentgrep.StoreCoverage.CATALOG_ONLY,
        ),
    ]

    records = [record for source in sources for record in agentgrep.iter_source_records(source)]

    assert records[0].text == "Fix storage parser"
    assert records[0].metadata["status"] == "pending"
    assert "Storage Team" in records[1].text
    assert "Audit non-default storage" in records[1].text
    assert "do-not-index" not in records[2].text
    assert records[2].metadata == {"key_count": 3}
    assert "secret-plugin" not in records[3].text
    assert "description" in records[3].text
    assert "echo do-not-index" not in records[4].text
    assert "PreToolUse" in records[4].text
    assert "do-not-index" not in records[5].text
    assert records[5].metadata["line_count"] == 2


def test_discover_remaining_codex_inventory_sources(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex non-default inventory discovers instructions, config, app state, and plugins."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    codex_home = home / ".codex"
    project_root = home / "work" / "repo"
    session_path = codex_home / "sessions" / "2026" / "05" / "30" / "rollout-1.jsonl"
    write_jsonl(
        session_path,
        [{"type": "session_meta", "payload": {"id": "s1", "cwd": str(project_root)}}],
    )

    paths = [
        codex_home / "skills" / "review" / "SKILL.md",
        codex_home / "rules" / "default.rules",
        codex_home / "config.toml",
        codex_home / "config.toml.bak",
        codex_home / "hooks.json",
        codex_home / "managed_config.toml",
        codex_home / "environments.toml",
        codex_home / "update-check.json",
        codex_home / "version.json",
        codex_home / ".personality_migration",
        codex_home / "models_cache.json",
        codex_home / "internal_storage.json",
        codex_home / "process_manager" / "chat_processes.json",
        codex_home / "tmp" / "arg0" / "state.json",
        codex_home / "log" / "codex.log",
        codex_home / "shell_snapshots" / "snapshot.sh",
        codex_home / "plugins" / "cache" / "example" / ".codex-plugin" / "plugin.json",
        codex_home / "plugins" / "cache" / "example" / ".claude-plugin" / "plugin.json",
        codex_home / "plugins" / "cache" / "example" / ".agents" / "plugins" / "marketplace.json",
        codex_home / "plugins" / "cache" / "example" / "commands" / "ship.md",
        codex_home / "plugins" / "cache" / "example" / "agents" / "reviewer.md",
        codex_home / "plugins" / "cache" / "example" / "skills" / "audit" / "SKILL.md",
        codex_home / "plugins" / "cache" / "example" / "custom-skills" / "audit" / "SKILL.md",
        codex_home / "plugins" / "cache" / "example" / "hooks" / "hooks.json",
        project_root / ".codex" / "config.toml",
        project_root / ".codex" / "hooks.json",
        project_root / ".codex" / "skills" / "audit" / "SKILL.md",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".json":
            text = "{}"
        elif path.suffix == ".toml" or ".toml." in path.name:
            text = "model = 'gpt-5'\n"
        else:
            text = "instruction text"
        _ = path.write_text(text, encoding="utf-8")

    backends = agentgrep.BackendSelection(None, None, None)
    default_sources = agentgrep.discover_codex_sources(home, backends)
    inventory_sources = agentgrep.discover_codex_sources(
        home,
        backends,
        include_non_default=True,
    )

    default_paths = {source.path for source in default_sources}
    assert not default_paths.intersection(paths)
    adapter_ids = {source.adapter_id for source in inventory_sources}
    assert {
        "codex.skills_text.v1",
        "codex.rules_text.v1",
        "codex.config_toml.v1",
        "codex.config_backup_toml.v1",
        "codex.project_config_toml.v1",
        "codex.project_skill_text.v1",
        "codex.hooks_json.v1",
        "codex.app_state_json_summary.v1",
        "codex.plugin_manifest_json.v1",
        "codex.plugin_marketplace_json.v1",
        "codex.plugin_hooks_json.v1",
        "codex.plugin_instruction_text.v1",
        "codex.file_metadata_summary.v1",
    } <= adapter_ids

    inventory_paths = {source.path for source in inventory_sources}
    assert project_root / ".codex" / "config.toml" in inventory_paths
    assert project_root / ".codex" / "skills" / "audit" / "SKILL.md" in inventory_paths


def test_codex_private_inventory_stays_unenumerated(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex private files are documented but not discovered from disk."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    codex_home = home / ".codex"
    private_paths = [
        codex_home / "auth.json",
        codex_home / "installation_id",
        codex_home / "policy" / "policy.json",
        codex_home / "secrets" / "token",
        codex_home / ".env",
    ]
    for path in private_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("secret", encoding="utf-8")

    sources = agentgrep.discover_codex_sources(
        home,
        agentgrep.BackendSelection(None, None, None),
        include_non_default=True,
    )

    assert not {source.path for source in sources}.intersection(private_paths)


def test_parse_codex_inventory_safe_samples(tmp_path: pathlib.Path) -> None:
    """Codex config and app-state samples summarize structure without raw values."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    config_path = tmp_path / "config.toml"
    _ = config_path.write_text(
        "model = 'secret-model'\n[projects]\n'/private/repo' = { trust_level = 'trusted' }\n",
        encoding="utf-8",
    )
    app_path = tmp_path / "version.json"
    _ = app_path.write_text(
        json.dumps({"latest_version": "9.9.9", "dismissed_version": None}),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "plugin.json"
    _ = manifest_path.write_text(
        json.dumps({"name": "secret-plugin", "description": "private description"}),
        encoding="utf-8",
    )
    marketplace_path = tmp_path / "marketplace.json"
    _ = marketplace_path.write_text(
        json.dumps({"plugins": [{"name": "private-plugin", "repo": "secret"}]}),
        encoding="utf-8",
    )
    hook_path = tmp_path / "hooks.json"
    _ = hook_path.write_text(
        json.dumps({"hooks": {"PostToolUse": [{"command": "echo do-not-index"}]}}),
        encoding="utf-8",
    )
    raw_path = tmp_path / "codex.log"
    _ = raw_path.write_text("do-not-index\nsecond line\n", encoding="utf-8")

    sources = [
        agentgrep.SourceHandle(
            agent="codex",
            store="codex.config",
            adapter_id="codex.config_toml.v1",
            path=config_path,
            path_kind="store_file",
            source_kind="text",
            search_root=None,
            mtime_ns=0,
            coverage=agentgrep.StoreCoverage.CATALOG_ONLY,
        ),
        agentgrep.SourceHandle(
            agent="codex",
            store="codex.version_file",
            adapter_id="codex.app_state_json_summary.v1",
            path=app_path,
            path_kind="store_file",
            source_kind="json",
            search_root=None,
            mtime_ns=0,
            coverage=agentgrep.StoreCoverage.CATALOG_ONLY,
        ),
        agentgrep.SourceHandle(
            agent="codex",
            store="codex.plugins",
            adapter_id="codex.plugin_manifest_json.v1",
            path=manifest_path,
            path_kind="store_file",
            source_kind="json",
            search_root=None,
            mtime_ns=0,
            coverage=agentgrep.StoreCoverage.INSPECTABLE,
        ),
        agentgrep.SourceHandle(
            agent="codex",
            store="codex.plugin_marketplace",
            adapter_id="codex.plugin_marketplace_json.v1",
            path=marketplace_path,
            path_kind="store_file",
            source_kind="json",
            search_root=None,
            mtime_ns=0,
            coverage=agentgrep.StoreCoverage.INSPECTABLE,
        ),
        agentgrep.SourceHandle(
            agent="codex",
            store="codex.plugins",
            adapter_id="codex.plugin_hooks_json.v1",
            path=hook_path,
            path_kind="store_file",
            source_kind="json",
            search_root=None,
            mtime_ns=0,
            coverage=agentgrep.StoreCoverage.INSPECTABLE,
        ),
        agentgrep.SourceHandle(
            agent="codex",
            store="codex.log_files",
            adapter_id="codex.file_metadata_summary.v1",
            path=raw_path,
            path_kind="store_file",
            source_kind="text",
            search_root=None,
            mtime_ns=0,
            coverage=agentgrep.StoreCoverage.CATALOG_ONLY,
        ),
    ]

    records = [record for source in sources for record in agentgrep.iter_source_records(source)]

    assert "secret-model" not in records[0].text
    assert "/private/repo" not in records[0].text
    assert "projects" in records[0].text
    assert "9.9.9" not in records[1].text
    assert "latest_version" in records[1].text
    assert "secret-plugin" not in records[2].text
    assert "description" in records[2].text
    assert "private-plugin" not in records[3].text
    assert "plugins" in records[3].text
    assert "echo do-not-index" not in records[4].text
    assert "PostToolUse" in records[4].text
    assert "do-not-index" not in records[5].text
    assert records[5].metadata["line_count"] == 2


def test_search_claude_history_expands_external_pasted_text(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude global prompt history resolves content-addressed pasted text."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    paste_hash = "0123456789abcdef"
    paste_path = home / ".claude" / "paste-cache" / f"{paste_hash}.txt"
    paste_path.parent.mkdir(parents=True, exist_ok=True)
    _ = paste_path.write_text("external bliss paste", encoding="utf-8")
    history_path = home / ".claude" / "history.jsonl"
    write_jsonl(
        history_path,
        [
            {
                "display": "Review [Pasted text #1] and [Pasted text #2 +1 lines]",
                "pastedContents": {
                    "1": {
                        "id": 1,
                        "type": "text",
                        "content": "inline serenity paste",
                    },
                    "2": {
                        "id": 2,
                        "type": "text",
                        "contentHash": paste_hash,
                    },
                },
                "timestamp": 1_700_000_000_000,
                "project": "/synthetic/project",
                "sessionId": "session-1",
            },
        ],
    )

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    query = t.cast("t.Any", agentgrep).SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("claude",),
        limit=None,
    )
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("claude",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    assert len(records) == 1
    record = records[0]
    assert record.agent == "claude"
    assert record.store == "claude.history"
    assert record.adapter_id == "claude.history_jsonl.v1"
    assert record.kind == "prompt"
    assert record.role == "user"
    assert record.timestamp == "2023-11-14T22:13:20Z"
    assert record.session_id == "session-1"
    assert record.conversation_id == "session-1"
    assert "inline serenity paste" in record.text
    assert "external bliss paste" in record.text
    assert "[Pasted text" not in record.text


def test_paste_cache_only_terms_survive_grep_backends(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terms living only in paste-cache files still match with a grep backend.

    Pins the unconditional Claude history admission: content grep over
    history.jsonl cannot see paste-cache expansions, so source admission
    must never depend on it. The grep helper is stubbed to report a miss
    so any future conditional admission fails here.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    paste_hash = "0123456789abcdef"
    paste_path = home / ".claude" / "paste-cache" / f"{paste_hash}.txt"
    paste_path.parent.mkdir(parents=True, exist_ok=True)
    _ = paste_path.write_text("hidden serenity needle", encoding="utf-8")
    history_path = home / ".claude" / "history.jsonl"
    write_jsonl(
        history_path,
        [
            {
                "display": "Review [Pasted text #1]",
                "pastedContents": {
                    "1": {"id": 1, "type": "text", "contentHash": paste_hash},
                },
                "timestamp": 1_700_000_000_000,
                "project": "/synthetic/project",
                "sessionId": "session-1",
            },
        ],
    )

    def grep_misses(*_args: t.Any, **_kwargs: t.Any) -> bool:
        return False

    monkeypatch.setattr(_rm_orch, "grep_file_matches", grep_misses)

    query = agentgrep.SearchQuery(
        terms=("needle",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("claude",),
        limit=None,
    )
    backends = agentgrep.BackendSelection(None, "rg", None)
    sources = agentgrep.discover_sources(home, ("claude",), backends)
    records = agentgrep.search_sources(query, sources, backends)

    assert any("hidden serenity needle" in record.text for record in records)


def test_prompt_scope_excludes_claude_project_user_turns_when_history_exists(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default prompt scope uses Claude's prompt history, not transcript replay."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    claude_home = home / ".claude"
    write_jsonl(
        claude_home / "history.jsonl",
        [
            {
                "display": "biome from prompt history",
                "timestamp": 1_700_000_000_000,
                "project": "/synthetic/project",
                "sessionId": "session-1",
                "pastedContents": {},
            },
        ],
    )
    write_jsonl(
        claude_home / "projects" / "-synthetic-project" / "session-1.jsonl",
        [
            {
                "type": "user",
                "sessionId": "session-1",
                "version": "2.1.157",
                "message": {"role": "user", "content": "biome from transcript"},
            },
        ],
    )

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    query = t.cast("t.Any", agentgrep).SearchQuery(
        terms=("biome",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("claude",),
        limit=None,
    )
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("claude",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    assert [(record.store, record.text) for record in records] == [
        ("claude.history", "biome from prompt history"),
    ]


def test_search_claude_history_tolerates_missing_paste_cache(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing Claude paste-cache entries keep history search resilient."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    history_path = home / ".claude" / "history.jsonl"
    write_jsonl(
        history_path,
        [
            {
                "display": "missing paste marker [Pasted text #1]",
                "pastedContents": {
                    "1": {
                        "id": 1,
                        "type": "text",
                        "contentHash": "fedcba9876543210",
                    },
                },
                "timestamp": 1_700_000_000_000,
                "project": "/synthetic/project",
                "sessionId": "session-1",
            },
        ],
    )

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    query = t.cast("t.Any", agentgrep).SearchQuery(
        terms=("missing",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("claude",),
        limit=None,
    )
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("claude",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    assert len(records) == 1
    assert records[0].text == "missing paste marker [Pasted text #1]"


def test_discover_gemini_sources_honours_gemini_cli_home_env(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GEMINI_CLI_HOME`` overrides ``${HOME}/.gemini`` per the catalogue contract."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    decoy_home = tmp_path / "home"
    alt_root = tmp_path / "elsewhere"
    monkeypatch.setenv("HOME", str(decoy_home))
    monkeypatch.setenv("GEMINI_CLI_HOME", str(alt_root))
    decoy_session = decoy_home / ".gemini" / "tmp" / "h0" / "chats" / "session-decoy.jsonl"
    write_jsonl(
        decoy_session,
        [
            {
                "sessionId": "decoy",
                "projectHash": "h0",
                "startTime": "2026-05-17T12:00:00Z",
                "lastUpdated": "2026-05-17T12:00:00Z",
                "kind": "main",
            },
        ],
    )
    session = alt_root / "tmp" / "h0" / "chats" / "session-real.jsonl"
    write_jsonl(
        session,
        [
            {
                "sessionId": "real",
                "projectHash": "h0",
                "startTime": "2026-05-17T12:00:00Z",
                "lastUpdated": "2026-05-17T12:00:00Z",
                "kind": "main",
            },
        ],
    )

    backends = agentgrep.BackendSelection(None, None, None)
    sources = agentgrep.discover_gemini_sources(decoy_home, backends)

    paths = {s.path for s in sources}
    assert session in paths
    assert decoy_session not in paths


def test_resolve_env_root_warns_on_missing_path(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An env override set to a non-existent path logs a warning and falls back.

    Structured ``extra=`` is checked via ``caplog.records`` per the project's
    logging convention (no string matching).
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    default_path = tmp_path / "fallback"
    default_path.mkdir()
    bad_path = tmp_path / "does-not-exist"
    monkeypatch.setenv("CODEX_HOME", str(bad_path))

    with caplog.at_level("WARNING", logger="agentgrep"):
        result = agentgrep.resolve_env_root("CODEX_HOME", default_path)

    assert result == default_path
    relevant = [r for r in caplog.records if getattr(r, "agentgrep_env_var", None) == "CODEX_HOME"]
    assert relevant, "expected warning record with agentgrep_env_var"
    assert relevant[0].levelname == "WARNING"
    assert getattr(relevant[0], "agentgrep_env_path", None) == str(bad_path)
    assert getattr(relevant[0], "agentgrep_env_path_status", None) == "not_found"


def test_resolve_env_root_warns_when_env_path_is_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An env override pointing at a regular file logs ``not_a_directory``.

    Distinguishing the file case from the missing case lets operators tell
    a typo apart from an env var pointed at the wrong kind of inode.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    default_path = tmp_path / "fallback"
    default_path.mkdir()
    file_path = tmp_path / "i-am-a-file"
    _ = file_path.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(file_path))

    with caplog.at_level("WARNING", logger="agentgrep"):
        result = agentgrep.resolve_env_root("CODEX_HOME", default_path)

    assert result == default_path
    relevant = [r for r in caplog.records if getattr(r, "agentgrep_env_var", None) == "CODEX_HOME"]
    assert relevant, "expected warning record with agentgrep_env_var"
    assert getattr(relevant[0], "agentgrep_env_path_status", None) == "not_a_directory"
    assert getattr(relevant[0], "agentgrep_env_path", None) == str(file_path)


def test_resolve_env_root_returns_default_when_env_unset(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset env var returns the default without warning."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    default_path = tmp_path / "fallback"
    default_path.mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)

    result = agentgrep.resolve_env_root("CODEX_HOME", default_path)

    assert result == default_path


def test_search_cursor_cli_transcript_user_prompt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor CLI agent transcripts: user-turn text is surfaced."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    transcript = home / ".cursor" / "projects" / "p" / "agent-transcripts" / "u" / "u.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "role": "user",
                "message": {
                    "content": [
                        {"type": "text", "text": "<user_query>libtmux list windows</user_query>"},
                    ],
                },
            },
        ],
    )

    query = _make_query(agentgrep, ("cursor-cli",), ("libtmux",))
    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("cursor-cli",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    assert any(r.agent == "cursor-cli" and "libtmux" in r.text for r in records)
    cursor_records = [r for r in records if r.agent == "cursor-cli"]
    assert cursor_records[0].timestamp is not None  # mtime-derived fallback


def test_search_cursor_cli_transcript_assistant_text(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor CLI: assistant text turns surface as records too."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    transcript = home / ".cursor" / "projects" / "p" / "agent-transcripts" / "u" / "u.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "role": "user",
                "message": {"content": [{"type": "text", "text": "ping libtmux"}]},
            },
            {
                "role": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Here's the libtmux output."}],
                },
            },
        ],
    )

    query = _make_query(agentgrep, ("cursor-cli",), ("libtmux",))
    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("cursor-cli",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    roles = {r.role for r in records if r.agent == "cursor-cli"}
    assert "user" in roles
    assert "assistant" in roles


def test_search_cursor_cli_transcript_ignores_tool_use_blocks(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor CLI: ``tool_use`` blocks have no ``text`` payload and must not crash."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    transcript = home / ".cursor" / "projects" / "p" / "agent-transcripts" / "u" / "u.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "role": "user",
                "message": {"content": [{"type": "text", "text": "libtmux ping"}]},
            },
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp_libtmux_list_windows",
                            "input": {},
                        },
                    ],
                },
            },
        ],
    )

    query = _make_query(agentgrep, ("cursor-cli",), ("libtmux",))
    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("cursor-cli",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    assert all(r.text.strip() for r in records)  # no empty-text records leak through


def test_search_gemini_chat_legacy_json_session(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-Feb 2026 .json single-file session: messages[] surface to search."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    legacy_session = home / ".gemini" / "tmp" / "h0" / "chats" / "session-legacy.json"
    legacy_session.parent.mkdir(parents=True, exist_ok=True)
    _ = legacy_session.write_text(
        json.dumps(
            {
                "sessionId": "legacy-sess",
                "projectHash": "h0",
                "startTime": "2026-02-01T00:00:00Z",
                "lastUpdated": "2026-02-01T00:01:00Z",
                "messages": [
                    {
                        "id": "m1",
                        "timestamp": "2026-02-01T00:00:30Z",
                        "type": "user",
                        "content": [{"text": "legacy libtmux trace"}],
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    query = _make_query(agentgrep, ("gemini",), ("libtmux",))
    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("gemini",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    legacy = [r for r in records if r.store == "gemini.tmp_chats_legacy"]
    assert legacy, "expected at least one gemini.tmp_chats_legacy record"
    assert legacy[0].text == "legacy libtmux trace"
    assert legacy[0].session_id == "legacy-sess"
    assert legacy[0].role == "user"


def test_search_gemini_chat_session_user_prompt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini chat JSONL: user MessageRecord is surfaced with timestamp + sessionId."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    session = home / ".gemini" / "tmp" / "h0" / "chats" / "session-x.jsonl"
    write_jsonl(
        session,
        [
            {
                "sessionId": "sess-1",
                "projectHash": "h0",
                "startTime": "2026-05-17T12:00:00Z",
                "lastUpdated": "2026-05-17T12:00:00Z",
                "kind": "main",
            },
            {
                "id": "m1",
                "timestamp": "2026-05-17T12:00:05Z",
                "type": "user",
                "content": [{"text": "remind me about libtmux"}],
            },
        ],
    )

    query = _make_query(agentgrep, ("gemini",), ("libtmux",))
    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("gemini",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    assert any(r.agent == "gemini" and "libtmux" in r.text for r in records)
    chat_records = [r for r in records if r.store == "gemini.tmp_chats"]
    assert chat_records, "expected at least one gemini.tmp_chats record"
    assert chat_records[0].session_id == "sess-1"
    assert chat_records[0].timestamp == "2026-05-17T12:00:05Z"


def test_search_gemini_chat_session_drops_textless_records(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``$set`` updates and content-free gemini records produce no record.

    A gemini-typed record with empty ``content`` AND no ``thoughts`` or
    ``toolCalls`` carries no searchable text — it should be skipped. A
    gemini-typed record WITH ``thoughts`` or ``toolCalls`` is handled by
    ``test_search_gemini_chat_session_surfaces_thoughts``.
    """
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    session = home / ".gemini" / "tmp" / "h0" / "chats" / "session-x.jsonl"
    write_jsonl(
        session,
        [
            {
                "sessionId": "sess-1",
                "projectHash": "h0",
                "startTime": "2026-05-17T12:00:00Z",
                "lastUpdated": "2026-05-17T12:00:00Z",
                "kind": "main",
            },
            {
                "id": "m1",
                "timestamp": "2026-05-17T12:00:05Z",
                "type": "user",
                "content": [{"text": "libtmux ping"}],
            },
            {"$set": {"lastUpdated": "2026-05-17T12:00:10Z"}},
            {
                "id": "m2",
                "timestamp": "2026-05-17T12:00:11Z",
                "type": "gemini",
                "content": "",
                "model": "gemini-3-flash-preview",
            },
        ],
    )

    query = _make_query(agentgrep, ("gemini",), ("libtmux",))
    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("gemini",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    chat_records = [r for r in records if r.store == "gemini.tmp_chats"]
    assert len(chat_records) == 1
    assert chat_records[0].role == "user"


class GeminiRoleGateCase(t.NamedTuple):
    """A Gemini record ``type`` and whether it should surface a search record."""

    test_id: str
    record_type: str
    emitted: bool


GEMINI_ROLE_GATE_CASES: tuple[GeminiRoleGateCase, ...] = (
    GeminiRoleGateCase("user-emitted", "user", True),
    GeminiRoleGateCase("gemini-emitted", "gemini", True),
    GeminiRoleGateCase("info-dropped", "info", False),
    GeminiRoleGateCase("error-dropped", "error", False),
    GeminiRoleGateCase("warning-dropped", "warning", False),
)


@pytest.mark.parametrize(
    "case",
    GEMINI_ROLE_GATE_CASES,
    ids=[c.test_id for c in GEMINI_ROLE_GATE_CASES],
)
def test_parse_gemini_chat_gates_system_records(
    case: GeminiRoleGateCase,
    tmp_path: pathlib.Path,
) -> None:
    """Only `user`/`gemini` turns surface; `info`/`error`/`warning` are skipped."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "session-x.jsonl"
    write_jsonl(
        path,
        [
            {
                "sessionId": "s",
                "projectHash": "h",
                "startTime": "2026-05-17T12:00:00Z",
                "lastUpdated": "2026-05-17T12:00:00Z",
                "kind": "main",
            },
            {
                "id": "m1",
                "timestamp": "2026-05-17T12:00:05Z",
                "type": case.record_type,
                "content": "searchable text",
            },
        ],
    )
    source = agentgrep.SourceHandle(
        agent="gemini",
        store="gemini.tmp_chats",
        adapter_id="gemini.tmp_chats_jsonl.v1",
        path=path,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )

    records = list(agentgrep.iter_source_records(source))

    assert bool(records) is case.emitted


def test_search_gemini_chat_session_surfaces_thoughts_and_tool_calls(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini turns with empty ``content`` are surfaced via ``thoughts`` and ``toolCalls``."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    session = home / ".gemini" / "tmp" / "h0" / "chats" / "session-x.jsonl"
    write_jsonl(
        session,
        [
            {
                "sessionId": "sess-1",
                "projectHash": "h0",
                "startTime": "2026-05-17T12:00:00Z",
                "lastUpdated": "2026-05-17T12:00:00Z",
                "kind": "main",
            },
            {
                "id": "thought-turn",
                "timestamp": "2026-05-17T12:00:11Z",
                "type": "gemini",
                "content": "",
                "thoughts": [
                    {
                        "subject": "Analysing libtmux",
                        "description": "The user wants the libtmux helper.",
                        "timestamp": "2026-05-17T12:00:10Z",
                    },
                ],
                "model": "gemini-3-flash-preview",
            },
            {
                "id": "tool-turn",
                "timestamp": "2026-05-17T12:00:13Z",
                "type": "gemini",
                "content": "",
                "toolCalls": [
                    {
                        "id": "call_0",
                        "name": "run_shell_command",
                        "args": {"command": "rg libtmux"},
                        "description": "Invoke a libtmux-related shell helper.",
                    },
                ],
                "model": "gemini-3-flash-preview",
            },
        ],
    )

    query = _make_query(agentgrep, ("gemini",), ("libtmux",))
    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("gemini",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    chat_records = [r for r in records if r.store == "gemini.tmp_chats"]
    assert len(chat_records) == 2  # one thought turn + one tool-call turn
    by_role = {r.role: r for r in chat_records}
    assert "gemini" in by_role
    texts = "\n".join(r.text for r in chat_records)
    assert "Analysing libtmux" in texts
    assert "libtmux helper" in texts
    assert "run_shell_command" in texts
    assert "libtmux-related shell helper" in texts


def test_search_gemini_chat_session_metadata_with_future_type_field(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SessionMetadataRecord with a hypothetical ``type`` field stays classified.

    Upstream discriminates metadata records by ``kind``; agentgrep's parser
    must too, so a future Gemini schema that adds a ``type`` field to the
    session-metadata line cannot silently misclassify it as a turn.
    """
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    session = home / ".gemini" / "tmp" / "h0" / "chats" / "session-x.jsonl"
    write_jsonl(
        session,
        [
            {
                "sessionId": "sess-1",
                "projectHash": "h0",
                "startTime": "2026-05-17T12:00:00Z",
                "lastUpdated": "2026-05-17T12:00:00Z",
                "kind": "main",
                # Hypothetical forward-compat: upstream adds `type`.
                "type": "session_meta",
            },
            {
                "id": "m1",
                "timestamp": "2026-05-17T12:00:05Z",
                "type": "user",
                "content": [{"text": "remind me about libtmux"}],
            },
        ],
    )

    query = _make_query(agentgrep, ("gemini",), ("libtmux",))
    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("gemini",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    chat_records = [r for r in records if r.store == "gemini.tmp_chats"]
    assert len(chat_records) == 1
    assert chat_records[0].role == "user"
    assert chat_records[0].session_id == "sess-1"


def test_search_gemini_logs_returns_user_message(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini logs.json: flat LogEntry array yields prompt-history records."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    logs = home / ".gemini" / "tmp" / "h0" / "logs.json"
    logs.parent.mkdir(parents=True, exist_ok=True)
    _ = logs.write_text(
        json.dumps(
            [
                {
                    "sessionId": "sess-1",
                    "messageId": 0,
                    "type": "user",
                    "message": "libtmux trace",
                    "timestamp": "2026-05-17T12:00:05Z",
                },
            ],
        ),
        encoding="utf-8",
    )

    query = _make_query(agentgrep, ("gemini",), ("libtmux",))
    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("gemini",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    log_records = [r for r in records if r.store == "gemini.tmp_logs"]
    assert log_records, "expected at least one gemini.tmp_logs record"
    assert log_records[0].text == "libtmux trace"
    assert log_records[0].role == "user"
    assert log_records[0].kind == "prompt"
    assert log_records[0].timestamp == "2026-05-17T12:00:05Z"
    assert log_records[0].session_id == "sess-1"


# ─── Grok backend tests ──────────────────────────────────────────────────


def test_discover_grok_sources_honours_grok_home_env(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GROK_HOME`` overrides ``${HOME}/.grok`` per the catalogue contract."""
    agentgrep = load_agentgrep_module()
    decoy_home = tmp_path / "home"
    alt_root = tmp_path / "elsewhere"
    monkeypatch.setenv("HOME", str(decoy_home))
    monkeypatch.setenv("GROK_HOME", str(alt_root))
    decoy_prompt = decoy_home / ".grok" / "sessions" / "%2Ftmp%2Fdecoy" / "prompt_history.jsonl"
    write_jsonl(
        decoy_prompt,
        [
            {
                "timestamp": "2026-05-25T10:00:00Z",
                "session_id": "s1",
                "prompt": "hi",
                "is_bash": False,
            },
        ],
    )
    real_prompt = alt_root / "sessions" / "%2Ftmp%2Freal" / "prompt_history.jsonl"
    write_jsonl(
        real_prompt,
        [
            {
                "timestamp": "2026-05-25T10:00:00Z",
                "session_id": "s2",
                "prompt": "yo",
                "is_bash": False,
            },
        ],
    )

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_grok_sources(decoy_home, backends)

    paths = {s.path for s in sources}
    assert real_prompt in paths
    assert decoy_prompt not in paths


def test_search_grok_prompt_history(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grok prompt_history.jsonl records surface as kind=prompt, role=user."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GROK_HOME", raising=False)
    prompt_file = home / ".grok" / "sessions" / "%2Ftmp%2Fproj" / "prompt_history.jsonl"
    write_jsonl(
        prompt_file,
        [
            {
                "timestamp": "2026-05-25T10:00:00.000000000Z",
                "session_id": "019729a0-0000-7000-8000-000000000001",
                "prompt": "summarise the codebase",
                "is_bash": False,
            },
            {
                "timestamp": "2026-05-25T10:01:30.000000000Z",
                "session_id": "019729a0-0000-7000-8000-000000000001",
                "prompt": "uv run pytest",
                "is_bash": True,
            },
        ],
    )

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    query = t.cast("t.Any", agentgrep).SearchQuery(
        terms=("summarise",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("grok",),
        limit=None,
    )
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("grok",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    assert records, "expected at least one grok prompt history record"
    assert records[0].kind == "prompt"
    assert records[0].role == "user"
    assert records[0].agent == "grok"
    assert "summarise" in records[0].text
    assert records[0].session_id == "019729a0-0000-7000-8000-000000000001"


def test_search_grok_chat_history_session(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grok chat_history.jsonl yields user and assistant records."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GROK_HOME", raising=False)
    session_uuid = "019729a0-0000-7000-8000-aabbccddeeff"
    chat_file = home / ".grok" / "sessions" / "%2Ftmp%2Fproj" / session_uuid / "chat_history.jsonl"
    write_jsonl(
        chat_file,
        [
            {"type": "system", "content": "You are Grok.", "timestamp": "2026-05-25T10:00:00Z"},
            {"type": "user", "content": "explain the design", "timestamp": "2026-05-25T10:00:01Z"},
            {
                "type": "assistant",
                "content": "The design uses an event-driven architecture.",
                "timestamp": "2026-05-25T10:00:03Z",
            },
        ],
    )

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    query = t.cast("t.Any", agentgrep).SearchQuery(
        terms=("design",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("grok",),
        limit=None,
    )
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("grok",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    assert len(records) >= 2, "expected user + assistant records"
    roles = {r.role for r in records}
    assert "user" in roles
    assert "assistant" in roles
    for record in records:
        assert record.conversation_id == session_uuid
        assert record.agent == "grok"


def test_search_grok_chat_history_drops_empty_content(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Records with empty content are not emitted."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GROK_HOME", raising=False)
    chat_file = home / ".grok" / "sessions" / "%2Ftmp%2Fproj" / "sess-1" / "chat_history.jsonl"
    write_jsonl(
        chat_file,
        [
            {"type": "user", "content": "", "timestamp": "2026-05-25T10:00:01Z"},
            {"type": "assistant", "content": None, "timestamp": "2026-05-25T10:00:03Z"},
            {"type": "tool_result", "content": "   ", "timestamp": "2026-05-25T10:00:04Z"},
        ],
    )

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    query = t.cast("t.Any", agentgrep).SearchQuery(
        terms=(),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("grok",),
        limit=None,
    )
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("grok",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    chat_records = [r for r in records if r.store == "grok.sessions"]
    assert chat_records == []


def test_search_grok_session_search_db(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grok session_search.sqlite yields titled records."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GROK_HOME", raising=False)
    db_path = home / ".grok" / "sessions" / "session_search.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE session_docs ("
        "  session_id TEXT PRIMARY KEY,"
        "  cwd TEXT NOT NULL,"
        "  updated_at INTEGER NOT NULL,"
        "  title TEXT NOT NULL,"
        "  content TEXT NOT NULL,"
        "  content_hash TEXT NOT NULL,"
        "  last_indexed_offset INTEGER NOT NULL DEFAULT 0"
        ")",
    )
    conn.execute(
        "INSERT INTO session_docs VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "019729a0-0000-7000-8000-000000000099",
            "/tmp/proj",
            1779750000,
            "Refactor auth middleware",
            "The auth middleware was refactored to use JWT tokens.",
            "abc123",
            0,
        ),
    )
    conn.commit()
    conn.close()

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    query = t.cast("t.Any", agentgrep).SearchQuery(
        terms=("middleware",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("grok",),
        limit=None,
    )
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("grok",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    db_records = [r for r in records if r.store == "grok.session_search"]
    assert db_records, "expected at least one session_search record"
    assert db_records[0].title == "Refactor auth middleware"
    assert "JWT tokens" in db_records[0].text
    assert db_records[0].session_id == "019729a0-0000-7000-8000-000000000099"
    assert db_records[0].timestamp is not None
    assert db_records[0].timestamp.startswith("2026-")


def test_search_grok_session_search_db_without_cwd_column(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grok databases predating the cwd column still yield records."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GROK_HOME", raising=False)
    db_path = home / ".grok" / "sessions" / "session_search.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE session_docs ("
        "  session_id TEXT PRIMARY KEY,"
        "  updated_at INTEGER NOT NULL,"
        "  title TEXT NOT NULL,"
        "  content TEXT NOT NULL,"
        "  content_hash TEXT NOT NULL"
        ")",
    )
    conn.execute(
        "INSERT INTO session_docs VALUES (?, ?, ?, ?, ?)",
        (
            "019729a0-0000-7000-8000-000000000042",
            1779750000,
            "Refactor auth middleware",
            "The auth middleware was refactored to use JWT tokens.",
            "abc123",
        ),
    )
    conn.commit()
    conn.close()

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    query = t.cast("t.Any", agentgrep).SearchQuery(
        terms=("middleware",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("grok",),
        limit=None,
    )
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("grok",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    db_records = [r for r in records if r.store == "grok.session_search"]
    assert db_records, "expected at least one session_search record"
    assert db_records[0].title == "Refactor auth middleware"
    assert db_records[0].session_id == "019729a0-0000-7000-8000-000000000042"
    assert db_records[0].origin is None


def _pi_session_header(
    *, cwd: str = "/home/user/project", version: int | None = 3
) -> dict[str, object]:
    """Build a pi session-header line; ``version=None`` omits the field (v1)."""
    header: dict[str, object] = {
        "type": "session",
        "id": "019e0000-0000-7000-8000-000000000abc",
        "timestamp": "2026-05-30T12:00:00.000Z",
        "cwd": cwd,
    }
    if version is not None:
        header["version"] = version
    return header


def _parse_pi_entries(
    agentgrep: AgentGrepModule,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    entries: list[dict[str, object]],
    *,
    version: int | None = 3,
) -> list[t.Any]:
    """Write a nested pi session of ``entries`` and return its parsed records."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    session_file = home / ".pi" / "agent" / "sessions" / "--home-user-project--" / "sess.jsonl"
    write_jsonl(session_file, [_pi_session_header(version=version), *entries])
    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("pi",), backends)
    records: list[t.Any] = []
    for source in sources:
        if source.store == "pi.sessions":
            records.extend(t.cast("t.Any", agentgrep).iter_source_records(source))
    return records


def test_discover_pi_sources_honours_pi_coding_agent_dir(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PI_CODING_AGENT_DIR`` is used verbatim, overriding ``${HOME}/.pi/agent``."""
    agentgrep = load_agentgrep_module()
    decoy_home = tmp_path / "home"
    alt_dir = tmp_path / "elsewhere" / "agent"
    monkeypatch.setenv("HOME", str(decoy_home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(alt_dir))
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    decoy = decoy_home / ".pi" / "agent" / "sessions" / "--decoy--" / "d.jsonl"
    write_jsonl(decoy, [_pi_session_header(cwd="/decoy")])
    real = alt_dir / "sessions" / "--real--" / "r.jsonl"
    write_jsonl(real, [_pi_session_header(cwd="/real")])

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_pi_sources(decoy_home, backends)

    paths = {s.path for s in sources}
    assert real in paths
    assert decoy not in paths


def test_discover_pi_sources_session_dir_override_is_flat(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PI_CODING_AGENT_SESSION_DIR`` holds session files flat; cwd comes from the header."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    flat_dir = tmp_path / "pi-sessions"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(flat_dir))
    session_file = flat_dir / "2026-05-30T12-00-00-000Z_019e0000-0000-7000-8000-0000000000aa.jsonl"
    write_jsonl(
        session_file,
        [
            _pi_session_header(cwd="/srv/work/app"),
            {
                "type": "message",
                "id": "u1",
                "parentId": None,
                "timestamp": "2026-05-30T12:00:02.000Z",
                "message": {
                    "role": "user",
                    "content": "flat layout prompt",
                    "timestamp": 1780228802000,
                },
            },
        ],
    )

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("pi",), backends)
    pi_sources = [s for s in sources if s.store == "pi.sessions"]

    assert any(s.path == session_file for s in pi_sources)
    records: list[t.Any] = []
    for source in pi_sources:
        records.extend(t.cast("t.Any", agentgrep).iter_source_records(source))
    assert records, "expected the flat-layout session to parse"
    assert records[0].conversation_id == "/srv/work/app"


def test_search_pi_sessions(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pi sessions yield user prompts and assistant history carrying the model."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    session_file = home / ".pi" / "agent" / "sessions" / "--home-user-proj--" / "sess.jsonl"
    write_jsonl(
        session_file,
        [
            _pi_session_header(cwd="/home/user/proj"),
            {
                "type": "message",
                "id": "u1",
                "parentId": None,
                "timestamp": "2026-05-30T12:00:02.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "explain the streaming design"}],
                    "timestamp": 1780228802000,
                },
            },
            {
                "type": "message",
                "id": "a1",
                "parentId": "u1",
                "timestamp": "2026-05-30T12:00:03.000Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "The streaming design is event-driven."}],
                    "provider": "openrouter",
                    "model": "example/model",
                    "timestamp": 1780228803000,
                },
            },
        ],
    )

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    query = t.cast("t.Any", agentgrep).SearchQuery(
        terms=("streaming",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("pi",),
        limit=None,
    )
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("pi",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    assert len(records) >= 2, "expected user + assistant records"
    by_role = {r.role: r for r in records}
    assert by_role["user"].kind == "prompt"
    assert by_role["user"].agent == "pi"
    assert by_role["user"].conversation_id == "/home/user/proj"
    assert by_role["assistant"].kind == "history"
    assert by_role["assistant"].model == "example/model"


def test_parse_pi_session_v1_uses_unix_ms_timestamp_fallback(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v1 session (no version) with no entry timestamp falls back to inner unix-ms."""
    agentgrep = load_agentgrep_module()
    records = _parse_pi_entries(
        agentgrep,
        tmp_path,
        monkeypatch,
        [
            {
                "type": "message",
                "id": "u1",
                "parentId": None,
                "message": {"role": "user", "content": "v1 prompt", "timestamp": 1700000000000},
            },
        ],
        version=None,
    )

    assert len(records) == 1
    assert records[0].kind == "prompt"
    assert records[0].timestamp == "2023-11-14T22:13:20Z"


class PiEntryCase(t.NamedTuple):
    """Parametrized case for one pi session entry through the parser."""

    test_id: str
    entry: dict[str, object]
    expected_count: int
    expected_kind: str | None
    expected_role: str | None
    expected_text_contains: str | None
    expected_model: str | None


PI_ENTRY_CASES: tuple[PiEntryCase, ...] = (
    PiEntryCase(
        "user-message-is-prompt",
        {
            "type": "message",
            "id": "u1",
            "timestamp": "2026-05-30T12:00:02.000Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "design question"}]},
        },
        1,
        "prompt",
        "user",
        "design question",
        None,
    ),
    PiEntryCase(
        "assistant-message-is-history-with-model",
        {
            "type": "message",
            "id": "a1",
            "timestamp": "2026-05-30T12:00:03.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "an answer"}],
                "model": "example/model",
            },
        },
        1,
        "history",
        "assistant",
        "an answer",
        "example/model",
    ),
    PiEntryCase(
        "tool-result-is-history",
        {
            "type": "message",
            "id": "t1",
            "timestamp": "2026-05-30T12:00:04.000Z",
            "message": {
                "role": "toolResult",
                "toolName": "read",
                "content": [{"type": "text", "text": "tool output"}],
                "isError": False,
            },
        },
        1,
        "history",
        "toolResult",
        "tool output",
        None,
    ),
    PiEntryCase(
        "compaction-summary-is-history",
        {
            "type": "compaction",
            "id": "c1",
            "timestamp": "2026-05-30T12:00:05.000Z",
            "summary": "compacted summary text",
        },
        1,
        "history",
        "compaction",
        "compacted summary text",
        None,
    ),
    PiEntryCase(
        "branch-summary-is-history",
        {
            "type": "branch_summary",
            "id": "b1",
            "timestamp": "2026-05-30T12:00:06.000Z",
            "fromId": "u1",
            "summary": "branch summary text",
        },
        1,
        "history",
        "branch_summary",
        "branch summary text",
        None,
    ),
    PiEntryCase(
        "session-info-name-is-history",
        {
            "type": "session_info",
            "id": "s1",
            "timestamp": "2026-05-30T12:00:07.000Z",
            "name": "Session title",
        },
        1,
        "history",
        "session_info",
        "Session title",
        None,
    ),
    PiEntryCase(
        "model-change-is-skipped",
        {
            "type": "model_change",
            "id": "m1",
            "timestamp": "2026-05-30T12:00:01.000Z",
            "provider": "openrouter",
            "modelId": "example/model",
        },
        0,
        None,
        None,
        None,
        None,
    ),
    PiEntryCase(
        "thinking-level-change-is-skipped",
        {
            "type": "thinking_level_change",
            "id": "tl1",
            "timestamp": "2026-05-30T12:00:01.500Z",
            "thinkingLevel": "high",
        },
        0,
        None,
        None,
        None,
        None,
    ),
    PiEntryCase(
        "empty-user-content-is-skipped",
        {
            "type": "message",
            "id": "u2",
            "timestamp": "2026-05-30T12:00:02.000Z",
            "message": {"role": "user", "content": []},
        },
        0,
        None,
        None,
        None,
        None,
    ),
    PiEntryCase(
        "assistant-thinking-only-is-skipped",
        {
            "type": "message",
            "id": "a2",
            "timestamp": "2026-05-30T12:00:03.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "internal reasoning"}],
            },
        },
        0,
        None,
        None,
        None,
        None,
    ),
)


@pytest.mark.parametrize(
    PiEntryCase._fields,
    PI_ENTRY_CASES,
    ids=[case.test_id for case in PI_ENTRY_CASES],
)
def test_parse_pi_session_entry(
    test_id: str,
    entry: dict[str, object],
    expected_count: int,
    expected_kind: str | None,
    expected_role: str | None,
    expected_text_contains: str | None,
    expected_model: str | None,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each pi session entry type maps to the expected record (or is skipped)."""
    _ = test_id
    agentgrep = load_agentgrep_module()
    records = _parse_pi_entries(agentgrep, tmp_path, monkeypatch, [entry])

    assert len(records) == expected_count
    if expected_count:
        record = records[0]
        assert record.agent == "pi"
        assert record.kind == expected_kind
        assert record.role == expected_role
        assert record.model == expected_model
        if expected_text_contains is not None:
            assert expected_text_contains in record.text


class UnixToIsoCase(t.NamedTuple):
    """Parametrized case for _unix_to_isoformat edge cases."""

    test_id: str
    value: object
    expected: str | None


UNIX_TO_ISO_CASES: tuple[UnixToIsoCase, ...] = (
    UnixToIsoCase(
        test_id="valid-unix-seconds",
        value=1779750000,
        expected="2026-05-25",
    ),
    UnixToIsoCase(
        test_id="zero-returns-none",
        value=0,
        expected=None,
    ),
    UnixToIsoCase(
        test_id="negative-returns-none",
        value=-1,
        expected=None,
    ),
    UnixToIsoCase(
        test_id="nan-returns-none",
        value=float("nan"),
        expected=None,
    ),
    UnixToIsoCase(
        test_id="inf-returns-none",
        value=float("inf"),
        expected=None,
    ),
    UnixToIsoCase(
        test_id="negative-inf-returns-none",
        value=float("-inf"),
        expected=None,
    ),
    UnixToIsoCase(
        test_id="extreme-int-returns-none",
        value=9999999999999,
        expected=None,
    ),
    UnixToIsoCase(
        test_id="bool-true-returns-none",
        value=True,
        expected=None,
    ),
    UnixToIsoCase(
        test_id="none-returns-none",
        value=None,
        expected=None,
    ),
    UnixToIsoCase(
        test_id="string-returns-none",
        value="1779750000",
        expected=None,
    ),
)


def _build_opencode_db(
    db_path: pathlib.Path,
    *,
    messages: list[tuple[str, list[dict[str, object]]]],
    session_title: str = "Test session",
    directory: str = "/work/proj",
    model: str = "example/model",
    created: int = 1780000000000,
) -> None:
    """Build a minimal OpenCode ``opencode.db`` with session/message/part rows."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, directory TEXT)")
        conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT)")
        conn.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT)",
        )
        conn.execute("INSERT INTO session VALUES (?, ?, ?)", ("ses_1", session_title, directory))
        part_index = 0
        for message_index, (role, parts) in enumerate(messages):
            message_id = f"msg_{message_index}"
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?)",
                (
                    message_id,
                    "ses_1",
                    json.dumps({"role": role, "time": {"created": created}, "modelID": model}),
                ),
            )
            for part in parts:
                conn.execute(
                    "INSERT INTO part VALUES (?, ?, ?, ?)",
                    (f"prt_{part_index}", message_id, "ses_1", json.dumps(part)),
                )
                part_index += 1
        conn.commit()
    finally:
        conn.close()


def _protobuf_field(text: str) -> bytes:
    """Encode one length-delimited protobuf string field for tests."""
    raw = text.encode("utf-8")
    length = len(raw)
    varint = bytearray()
    while True:
        to_write = length & 0x7F
        length >>= 7
        if length:
            varint.append(to_write | 0x80)
            continue
        varint.append(to_write)
        break
    return b"\x0a" + bytes(varint) + raw


def _build_antigravity_steps_db(
    db_path: pathlib.Path,
    *,
    text: str,
    metadata_table: str | None = None,
    metadata_text: str | None = None,
) -> None:
    """Build a minimal Antigravity CLI conversation database.

    ``metadata_table`` adds one protobuf metadata row beside ``steps``. Leaving
    it unset reproduces a database written before Antigravity shipped those
    tables: ``steps`` and nothing else.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_payload BLOB, step_format INTEGER)",
        )
        conn.execute(
            "INSERT INTO steps VALUES (?, ?, ?)",
            (1, _protobuf_field(text), 1),
        )
        if metadata_table is not None:
            conn.execute(f"CREATE TABLE {metadata_table} (idx INTEGER PRIMARY KEY, data BLOB)")
            payload = None if metadata_text is None else _protobuf_field(metadata_text)
            conn.execute(f"INSERT INTO {metadata_table} VALUES (?, ?)", (1, payload))
        conn.commit()
    finally:
        conn.close()


class AntigravityHistoryCase(t.NamedTuple):
    """Parametrized case for Antigravity CLI prompt-history entries."""

    test_id: str
    entry: dict[str, object]
    query_term: str
    expected_text: str
    expected_timestamp: str
    expected_session_id: str | None
    expected_workspace: str


ANTIGRAVITY_HISTORY_CASES: tuple[AntigravityHistoryCase, ...] = (
    AntigravityHistoryCase(
        test_id="display-with-conversation",
        entry={
            "display": "ship the antigravity cli adapter",
            "timestamp": 1780142400000,
            "type": "prompt",
            "workspace": "/workspace/demo",
            "conversationId": "5cd92cd1-6f86-42de-8f7e-81ebb47f36dd",
        },
        query_term="adapter",
        expected_text="ship the antigravity cli adapter",
        expected_timestamp="2026-05-30T12:00:00Z",
        expected_session_id="5cd92cd1-6f86-42de-8f7e-81ebb47f36dd",
        expected_workspace="/workspace/demo",
    ),
    AntigravityHistoryCase(
        test_id="display-without-conversation",
        entry={
            "display": "search antigravity prompt recall",
            "timestamp": 1780142460000,
            "workspace": "/workspace/demo",
        },
        query_term="recall",
        expected_text="search antigravity prompt recall",
        expected_timestamp="2026-05-30T12:01:00Z",
        expected_session_id=None,
        expected_workspace="/workspace/demo",
    ),
)


@pytest.mark.parametrize(
    AntigravityHistoryCase._fields,
    ANTIGRAVITY_HISTORY_CASES,
    ids=[case.test_id for case in ANTIGRAVITY_HISTORY_CASES],
)
def test_search_antigravity_cli_history(
    test_id: str,
    entry: dict[str, object],
    query_term: str,
    expected_text: str,
    expected_timestamp: str,
    expected_session_id: str | None,
    expected_workspace: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Antigravity CLI ``history.jsonl`` is searched as prompt history."""
    _ = test_id
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    history_path = home / ".gemini" / "antigravity-cli" / "history.jsonl"
    write_jsonl(history_path, [entry])

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    agents: tuple[AgentName, ...] = ("antigravity-cli",)
    query = t.cast("t.Any", agentgrep).SearchQuery(
        terms=(query_term,),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agents,
        limit=None,
    )

    sources = t.cast("t.Any", agentgrep).discover_sources(home, agents, backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    assert len(records) == 1
    record = records[0]
    assert record.kind == "prompt"
    assert record.agent == "antigravity-cli"
    assert record.store == "antigravity-cli.history"
    assert record.adapter_id == "antigravity_cli.history_jsonl.v1"
    assert record.role == "user"
    assert record.text == expected_text
    assert record.timestamp == expected_timestamp
    assert record.session_id == expected_session_id
    assert record.conversation_id == expected_session_id
    assert record.metadata == {"workspace": expected_workspace, "type": entry.get("type", "")}


def _encrypted_pb_bytes(length: int = 4096) -> bytes:
    """Return high-entropy bytes with no valid protobuf framing.

    The real loose ``.pb`` artifacts are encrypted: ~8.0 bits/byte of entropy,
    no printable run long enough for the extractor's 16-byte gate, and no
    protobuf field varint at the head. The superseded fixture wrote *fabricated
    plaintext protobuf* into these paths and then asserted it could be read back
    — which is how three stores shipped advertising readable prompt strings they
    never contained.

    Parameters
    ----------
    length : int
        Number of bytes to synthesize.

    Returns
    -------
    bytes
        A deterministic byte stream standing in for an encrypted payload.
    """
    return bytes((index * 167 + 13) % 256 for index in range(length))


class AntigravityEncryptedCase(t.NamedTuple):
    """Parametrized case for an Antigravity store whose payloads are encrypted."""

    test_id: str
    agent: AgentName
    relative_path: pathlib.Path
    store: str


ANTIGRAVITY_ENCRYPTED_CASES: tuple[AntigravityEncryptedCase, ...] = (
    AntigravityEncryptedCase(
        test_id="cli-implicit-pb",
        agent="antigravity-cli",
        relative_path=pathlib.Path(".gemini/antigravity-cli/implicit/implicit-1.pb"),
        store="antigravity-cli.implicit",
    ),
    AntigravityEncryptedCase(
        test_id="ide-conversation-pb",
        agent="antigravity-ide",
        relative_path=pathlib.Path(".gemini/antigravity/conversations/ide-1.pb"),
        store="antigravity-ide.conversations",
    ),
    AntigravityEncryptedCase(
        test_id="ide-implicit-pb",
        agent="antigravity-ide",
        relative_path=pathlib.Path(".gemini/antigravity/implicit/implicit-1.pb"),
        store="antigravity-ide.implicit",
    ),
)


@pytest.mark.parametrize(
    AntigravityEncryptedCase._fields,
    ANTIGRAVITY_ENCRYPTED_CASES,
    ids=[case.test_id for case in ANTIGRAVITY_ENCRYPTED_CASES],
)
def test_antigravity_encrypted_pb_stores_are_catalog_only(
    test_id: str,
    agent: AgentName,
    relative_path: pathlib.Path,
    store: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Encrypted Antigravity ``.pb`` stores are catalogued, never enumerated."""
    del test_id

    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    source_path = home / relative_path
    source_path.parent.mkdir(parents=True, exist_ok=True)
    _ = source_path.write_bytes(_encrypted_pb_bytes())

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    agents: tuple[AgentName, ...] = (agent,)
    default_sources = t.cast("t.Any", agentgrep).discover_sources(home, agents, backends)
    inventory_sources = t.cast("t.Any", agentgrep).discover_sources(
        home,
        agents,
        backends,
        include_non_default=True,
    )

    descriptor = CATALOG.by_id(store)

    assert descriptor.coverage_level.value == "catalog_only"
    assert descriptor.discovery == ()
    assert descriptor.sample_record is None
    assert source_path not in {source.path for source in default_sources}
    assert source_path not in {source.path for source in inventory_sources}


def test_antigravity_cli_conversation_db_decodes_sqlite_protobuf(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Antigravity CLI conversation SQLite blobs *are* plaintext protobuf.

    This is the store the generic extractor genuinely reads: the encryption
    boundary is the loose ``.pb`` file, not the protobuf format.
    """
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    source_path = home / ".gemini/antigravity-cli/conversations/conv-1.db"
    text = "inspectable antigravity conversation text"
    _build_antigravity_steps_db(source_path, text=text)

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    agents: tuple[AgentName, ...] = ("antigravity-cli",)
    default_sources = t.cast("t.Any", agentgrep).discover_sources(home, agents, backends)
    inventory_sources = t.cast("t.Any", agentgrep).discover_sources(
        home,
        agents,
        backends,
        include_non_default=True,
    )

    assert source_path not in {source.path for source in default_sources}
    source = next(source for source in inventory_sources if source.path == source_path)

    assert source.store == "antigravity-cli.conversations"
    assert source.adapter_id == "antigravity_cli.conversations_sqlite_protobuf.v1"
    assert source.coverage.value == "inspectable"

    records = list(t.cast("t.Any", agentgrep).iter_source_records(source))

    assert len(records) == 1
    assert records[0].kind == "history"
    assert records[0].text == text
    assert records[0].session_id == source_path.stem


def test_antigravity_cli_conversation_db_rejects_encrypted_blobs(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Encrypted-shaped step blobs yield zero records instead of decoding to noise."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    source_path = home / ".gemini/antigravity-cli/conversations/conv-enc.db"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(source_path))
    try:
        _ = conn.execute(
            "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_payload BLOB, step_format INTEGER)",
        )
        _ = conn.execute(
            "INSERT INTO steps VALUES (?, ?, ?)",
            (1, _encrypted_pb_bytes(), 1),
        )
        conn.commit()
    finally:
        conn.close()

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    agents: tuple[AgentName, ...] = ("antigravity-cli",)
    inventory_sources = t.cast("t.Any", agentgrep).discover_sources(
        home,
        agents,
        backends,
        include_non_default=True,
    )
    source = next(source for source in inventory_sources if source.path == source_path)

    assert list(t.cast("t.Any", agentgrep).iter_source_records(source)) == []


class AntigravityModelCase(t.NamedTuple):
    """Parametrized case for the model on an Antigravity CLI conversation."""

    test_id: str
    metadata_table: str | None
    metadata_text: str | None
    expected_model: str | None


ANTIGRAVITY_MODEL_CASES: tuple[AntigravityModelCase, ...] = (
    AntigravityModelCase(
        test_id="gen-metadata",
        metadata_table="gen_metadata",
        metadata_text="gemini-pro-agent",
        expected_model="gemini-pro-agent",
    ),
    AntigravityModelCase(
        test_id="executor-metadata-fallback",
        metadata_table="executor_metadata",
        metadata_text="gemini-pro-agent",
        expected_model="gemini-pro-agent",
    ),
    AntigravityModelCase(
        test_id="no-metadata-table",
        metadata_table=None,
        metadata_text=None,
        expected_model=None,
    ),
    AntigravityModelCase(
        test_id="null-metadata-blob",
        metadata_table="gen_metadata",
        metadata_text=None,
        expected_model=None,
    ),
    AntigravityModelCase(
        test_id="metadata-names-no-model",
        metadata_table="gen_metadata",
        metadata_text="running_tasks_reminder",
        expected_model=None,
    ),
)


@pytest.mark.parametrize(
    AntigravityModelCase._fields,
    ANTIGRAVITY_MODEL_CASES,
    ids=[case.test_id for case in ANTIGRAVITY_MODEL_CASES],
)
def test_antigravity_cli_conversation_db_model(
    test_id: str,
    metadata_table: str | None,
    metadata_text: str | None,
    expected_model: str | None,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The conversation model comes from the metadata tables, never from ``steps``.

    A database that predates those tables — and one whose metadata names no
    model — still yields its step records, with no model rather than no records.
    """
    del test_id

    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    source_path = home / ".gemini/antigravity-cli/conversations/conv-model.db"
    text = "antigravity conversation step text"
    _build_antigravity_steps_db(
        source_path,
        text=text,
        metadata_table=metadata_table,
        metadata_text=metadata_text,
    )

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    agents: tuple[AgentName, ...] = ("antigravity-cli",)
    inventory_sources = t.cast("t.Any", agentgrep).discover_sources(
        home,
        agents,
        backends,
        include_non_default=True,
    )
    source = next(source for source in inventory_sources if source.path == source_path)
    records = list(t.cast("t.Any", agentgrep).iter_source_records(source))

    assert [record.text for record in records] == [text]
    assert records[0].model == expected_model


def _parse_opencode_records(
    agentgrep: AgentGrepModule,
    home: pathlib.Path,
) -> list[t.Any]:
    """Discover and parse every ``opencode.db`` record under ``home``."""
    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("opencode",), backends)
    records: list[t.Any] = []
    for source in sources:
        if source.store == "opencode.db":
            records.extend(t.cast("t.Any", agentgrep).iter_source_records(source))
    return records


def test_discover_opencode_sources_default_xdg_location(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """opencode.db under the default ``~/.local/share/opencode`` is discovered."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    db_path = home / ".local" / "share" / "opencode" / "opencode.db"
    _build_opencode_db(db_path, messages=[("user", [{"type": "text", "text": "hi"}])])

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_opencode_sources(home, backends)

    assert db_path in {s.path for s in sources}


def test_discover_opencode_sources_honours_xdg_data_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``XDG_DATA_HOME`` relocates the opencode data directory."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    alt = tmp_path / "xdg"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(alt))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    decoy = home / ".local" / "share" / "opencode" / "opencode.db"
    _build_opencode_db(decoy, messages=[("user", [{"type": "text", "text": "decoy"}])])
    real = alt / "opencode" / "opencode.db"
    _build_opencode_db(real, messages=[("user", [{"type": "text", "text": "real"}])])

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    paths = {s.path for s in t.cast("t.Any", agentgrep).discover_opencode_sources(home, backends)}

    assert real in paths
    assert decoy not in paths


class OpencodeOverrideCase(t.NamedTuple):
    """Parametrized case for the ``OPENCODE_DB`` absolute-path override."""

    test_id: str
    db_filename: str


OPENCODE_OVERRIDE_CASES: tuple[OpencodeOverrideCase, ...] = (
    OpencodeOverrideCase("default-name", "opencode.db"),
    OpencodeOverrideCase("custom-name", "my-sessions.db"),
    OpencodeOverrideCase("channel-name", "opencode-canary.db"),
)


@pytest.mark.parametrize(
    OpencodeOverrideCase._fields,
    OPENCODE_OVERRIDE_CASES,
    ids=[case.test_id for case in OPENCODE_OVERRIDE_CASES],
)
def test_discover_opencode_sources_honours_opencode_db_override(
    test_id: str,
    db_filename: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absolute ``OPENCODE_DB`` is discovered as that exact file, any filename."""
    _ = test_id
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    custom = tmp_path / "custom" / db_filename
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("OPENCODE_DB", str(custom))
    _build_opencode_db(custom, messages=[("user", [{"type": "text", "text": "custom"}])])

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    sources = t.cast("t.Any", agentgrep).discover_opencode_sources(home, backends)

    assert {s.path for s in sources} == {custom}
    assert sources[0].store == "opencode.db"


def test_search_opencode_sessions(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """opencode.db yields user prompts and assistant history carrying the model."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    db_path = home / ".local" / "share" / "opencode" / "opencode.db"
    _build_opencode_db(
        db_path,
        messages=[
            ("user", [{"type": "text", "text": "explain the streaming design"}]),
            ("assistant", [{"type": "text", "text": "the streaming design is event-driven"}]),
        ],
    )

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    query = t.cast("t.Any", agentgrep).SearchQuery(
        terms=("streaming",),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("opencode",),
        limit=None,
    )
    sources = t.cast("t.Any", agentgrep).discover_sources(home, ("opencode",), backends)
    records = t.cast("t.Any", agentgrep).search_sources(query, sources, backends)

    assert len(records) >= 2, "expected user + assistant records"
    by_role = {r.role: r for r in records}
    assert by_role["user"].kind == "prompt"
    assert by_role["user"].agent == "opencode"
    assert by_role["user"].metadata.get("directory") == "/work/proj"
    assert by_role["assistant"].kind == "history"
    assert by_role["assistant"].model == "example/model"


class OpencodePartCase(t.NamedTuple):
    """Parametrized case for one OpenCode message part through the parser."""

    test_id: str
    message_role: str
    part: dict[str, object]
    expected_count: int
    expected_kind: str | None
    expected_text_contains: str | None


OPENCODE_PART_CASES: tuple[OpencodePartCase, ...] = (
    OpencodePartCase(
        "user-text-is-prompt",
        "user",
        {"type": "text", "text": "a design question"},
        1,
        "prompt",
        "a design question",
    ),
    OpencodePartCase(
        "assistant-text-is-history",
        "assistant",
        {"type": "text", "text": "an answer"},
        1,
        "history",
        "an answer",
    ),
    OpencodePartCase(
        "reasoning-is-history",
        "assistant",
        {"type": "reasoning", "text": "internal thinking"},
        1,
        "history",
        "internal thinking",
    ),
    OpencodePartCase(
        "subtask-prompt-is-searchable",
        "assistant",
        {"type": "subtask", "prompt": "spawn a search subtask", "description": "desc"},
        1,
        "history",
        "spawn a search subtask",
    ),
    OpencodePartCase(
        "tool-part-is-skipped",
        "assistant",
        {"type": "tool", "tool": "read", "state": {"status": "completed", "output": "x"}},
        0,
        None,
        None,
    ),
    OpencodePartCase(
        "file-part-is-skipped",
        "assistant",
        {"type": "file", "mime": "text/plain", "url": "file://x"},
        0,
        None,
        None,
    ),
    OpencodePartCase(
        "step-start-is-skipped",
        "assistant",
        {"type": "step-start"},
        0,
        None,
        None,
    ),
    OpencodePartCase(
        "empty-text-is-skipped",
        "user",
        {"type": "text", "text": ""},
        0,
        None,
        None,
    ),
)


@pytest.mark.parametrize(
    OpencodePartCase._fields,
    OPENCODE_PART_CASES,
    ids=[case.test_id for case in OPENCODE_PART_CASES],
)
def test_parse_opencode_part(
    test_id: str,
    message_role: str,
    part: dict[str, object],
    expected_count: int,
    expected_kind: str | None,
    expected_text_contains: str | None,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each OpenCode part type maps to the expected record (or is skipped)."""
    _ = test_id
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    db_path = home / ".local" / "share" / "opencode" / "opencode.db"
    _build_opencode_db(db_path, messages=[(message_role, [part])])

    records = _parse_opencode_records(agentgrep, home)

    assert len(records) == expected_count
    if expected_count:
        record = records[0]
        assert record.agent == "opencode"
        assert record.kind == expected_kind
        if expected_text_contains is not None:
            assert expected_text_contains in record.text


@pytest.mark.parametrize(
    UnixToIsoCase._fields,
    UNIX_TO_ISO_CASES,
    ids=[c.test_id for c in UNIX_TO_ISO_CASES],
)
def test_unix_to_isoformat_edge_cases(
    test_id: str,
    value: object,
    expected: str | None,
) -> None:
    """_unix_to_isoformat handles edge cases without crashing."""
    agentgrep = load_agentgrep_module()
    result = t.cast("t.Any", agentgrep).adapters._common._unix_to_isoformat(value)
    if expected is None:
        assert result is None, f"{test_id}: expected None, got {result!r}"
    else:
        assert result is not None, f"{test_id}: expected timestamp, got None"
        assert result.startswith(expected), f"{test_id}: {result!r}"


class OpencodeModelCase(t.NamedTuple):
    """An OpenCode message.data shape and the model id it should surface."""

    test_id: str
    message_data: dict[str, object]
    expected_model: str | None


OPENCODE_MODEL_CASES: tuple[OpencodeModelCase, ...] = (
    OpencodeModelCase(
        test_id="assistant-top-level-modelid",
        message_data={
            "role": "assistant",
            "time": {"created": 1780000000000},
            "modelID": "anthropic/opus",
        },
        expected_model="anthropic/opus",
    ),
    OpencodeModelCase(
        test_id="user-nested-model-modelid",
        message_data={
            "role": "user",
            "time": {"created": 1780000000000},
            "model": {"providerID": "google", "modelID": "gemma-4"},
        },
        expected_model="gemma-4",
    ),
    OpencodeModelCase(
        test_id="no-model-yields-none",
        message_data={"role": "user", "time": {"created": 1780000000000}},
        expected_model=None,
    ),
)


@pytest.mark.parametrize(
    "case",
    OPENCODE_MODEL_CASES,
    ids=[c.test_id for c in OPENCODE_MODEL_CASES],
)
def test_parse_opencode_db_message_model(
    case: OpencodeModelCase,
    tmp_path: pathlib.Path,
) -> None:
    """User-message model comes from nested data.model.modelID; assistant top-level."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, directory TEXT)")
        conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT)")
        conn.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT)",
        )
        conn.execute("INSERT INTO session VALUES (?, ?, ?)", ("ses_1", "T", "/w"))
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?)",
            ("msg_1", "ses_1", json.dumps(case.message_data)),
        )
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?)",
            ("prt_1", "msg_1", "ses_1", json.dumps({"type": "text", "text": "hi"})),
        )
        conn.commit()
    finally:
        conn.close()
    source = agentgrep.SourceHandle(
        agent="opencode",
        store="opencode.db",
        adapter_id="opencode.db_sqlite.v1",
        path=db_path,
        path_kind="sqlite_db",
        source_kind="sqlite",
        search_root=None,
        mtime_ns=1,
    )

    records = list(agentgrep.iter_source_records(source))

    assert len(records) == 1
    assert records[0].model == case.expected_model


def test_parse_grok_subagents_emits_dispatch_prompt() -> None:
    """grok.subagents meta.json yields the delegated prompt as one record."""
    from tests.conftest import fixture_path

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    source = agentgrep.SourceHandle(
        agent="grok",
        store="grok.subagents",
        adapter_id="grok.subagents_json.v1",
        path=fixture_path("grok.subagents", "meta.json"),
        path_kind="session_file",
        source_kind="json",
        search_root=None,
        mtime_ns=1,
    )
    records = list(agentgrep.iter_source_records(source))
    assert len(records) == 1
    record = records[0]
    assert record.kind == "prompt"
    assert record.role == "user"
    assert "login sessions are issued" in record.text
    assert record.title == "Map the authentication module"
    assert record.metadata.get("subagent_type") == "code-explorer"


def test_parse_gemini_memory_emits_markdown(tmp_path: pathlib.Path) -> None:
    """gemini.memory (GEMINI.md) is parsed as one inspectable text record."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    md = tmp_path / "GEMINI.md"
    md.write_text("# Project memory\n\nAlways prefer ripgrep.\n", encoding="utf-8")
    source = agentgrep.SourceHandle(
        agent="gemini",
        store="gemini.memory",
        adapter_id="gemini.memory_text.v1",
        path=md,
        path_kind="store_file",
        source_kind="text",
        search_root=None,
        mtime_ns=1,
    )
    records = list(agentgrep.iter_source_records(source))
    assert len(records) == 1
    assert "prefer ripgrep" in records[0].text
    assert records[0].kind == "history"


def test_parse_antigravity_cli_transcript_emits_turns(tmp_path: pathlib.Path) -> None:
    """antigravity-cli transcript yields readable turns; null content is skipped."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    logs = tmp_path / "brain" / "uuid-1" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    transcript = logs / "transcript_full.jsonl"
    transcript.write_text(
        '{"type":"USER_INPUT","source":"USER_EXPLICIT",'
        '"created_at":"2026-06-21T00:00:00Z","content":"add a retry helper"}\n'
        '{"type":"CONVERSATION_HISTORY","source":"SYSTEM","content":null}\n'
        '{"type":"ASSISTANT","content":"here is the helper"}\n',
        encoding="utf-8",
    )
    source = agentgrep.SourceHandle(
        agent="antigravity-cli",
        store="antigravity-cli.transcript",
        adapter_id="antigravity_cli.transcript_jsonl.v1",
        path=transcript,
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )
    records = list(agentgrep.iter_source_records(source))
    assert len(records) == 2
    assert records[0].kind == "prompt"
    assert records[0].role == "user"
    assert "retry helper" in records[0].text
    assert records[0].conversation_id == "uuid-1"
    assert records[1].kind == "history"


def test_parse_claude_usage_facet_joins_nl_fields(tmp_path: pathlib.Path) -> None:
    """claude.usage_data facet emits the natural-language fields as one record."""
    import json as _json

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    facet = tmp_path / "abc.json"
    facet.write_text(
        _json.dumps(
            {
                "session_id": "abc",
                "brief_summary": "Refactored the parser",
                "underlying_goal": "make discovery faster",
                "friction_detail": "flaky fixture",
                "goal_categories": ["perf"],
            },
        ),
        encoding="utf-8",
    )
    source = agentgrep.SourceHandle(
        agent="claude",
        store="claude.usage_data",
        adapter_id="claude.usage_facets_json.v1",
        path=facet,
        path_kind="store_file",
        source_kind="json",
        search_root=None,
        mtime_ns=1,
    )
    records = list(agentgrep.iter_source_records(source))
    assert len(records) == 1
    assert "Refactored the parser" in records[0].text
    assert "make discovery faster" in records[0].text
    assert records[0].kind == "history"


def test_parse_pi_context_mode_db_emits_events(tmp_path: pathlib.Path) -> None:
    """pi.context_mode_db emits session_events payloads as records."""
    import sqlite3 as _sqlite3

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    db = tmp_path / "abc.db"
    conn = _sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE session_events (id INTEGER PRIMARY KEY, session_id TEXT, "
        "type TEXT, data TEXT, created_at TEXT)",
    )
    conn.execute(
        "INSERT INTO session_events (session_id, type, data, created_at) VALUES (?, ?, ?, ?)",
        ("s1", "tool_call", '{"tool":"rg","params":{"q":"login"}}', "2026-06-21"),
    )
    conn.commit()
    conn.close()
    source = agentgrep.SourceHandle(
        agent="pi",
        store="pi.context_mode_db",
        adapter_id="pi.context_mode_sqlite.v1",
        path=db,
        path_kind="sqlite_db",
        source_kind="sqlite",
        search_root=None,
        mtime_ns=1,
    )
    records = list(agentgrep.iter_source_records(source))
    assert len(records) == 1
    assert "login" in records[0].text
    assert records[0].role == "tool_call"
    assert records[0].kind == "history"


class PiContextModeOriginCase(t.NamedTuple):
    """One context-mode database shape and the origin its records must carry."""

    test_id: str

    stem_is_digest: bool
    """Whether the file is named ``sha256(project_dir)[:16]``, as Pi names it."""

    has_project_dir_column: bool
    """``False`` reproduces a database from before the ``project_dir`` migration."""

    writes_project_dir: bool
    """``False`` leaves the column at the empty string the shipped schema defaults to."""

    expect_cwd: bool
    expect_cwd_hash: bool


PI_CONTEXT_MODE_ORIGIN_CASES: tuple[PiContextModeOriginCase, ...] = (
    PiContextModeOriginCase(
        test_id="digest-stem-with-project-dir",
        stem_is_digest=True,
        has_project_dir_column=True,
        writes_project_dir=True,
        expect_cwd=True,
        expect_cwd_hash=True,
    ),
    PiContextModeOriginCase(
        test_id="empty-project-dir-keeps-the-digest",
        stem_is_digest=True,
        has_project_dir_column=True,
        writes_project_dir=False,
        expect_cwd=False,
        expect_cwd_hash=True,
    ),
    PiContextModeOriginCase(
        test_id="pre-migration-schema-keeps-the-digest",
        stem_is_digest=True,
        has_project_dir_column=False,
        writes_project_dir=False,
        expect_cwd=False,
        expect_cwd_hash=True,
    ),
    PiContextModeOriginCase(
        test_id="non-digest-stem-is-no-digest",
        stem_is_digest=False,
        has_project_dir_column=True,
        writes_project_dir=True,
        expect_cwd=True,
        expect_cwd_hash=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    PI_CONTEXT_MODE_ORIGIN_CASES,
    ids=[c.test_id for c in PI_CONTEXT_MODE_ORIGIN_CASES],
)
def test_parse_pi_context_mode_db_origin(
    case: PiContextModeOriginCase,
    tmp_path: pathlib.Path,
) -> None:
    """A context-mode record carries the cwd its row names and the digest its file does.

    Both are the same fact in two encodings: Pi names the database
    ``sha256(project_dir)[:16]`` and repeats the absolute ``project_dir`` on
    every row, so the round trip is free to check and this test checks it —
    hashing the recovered ``cwd`` has to reproduce the stem the store chose.
    That is a check, never a construction: the ``cwd_hash`` agentgrep reports is
    read off the file name, so a file whose stem is not a digest reports no
    ``cwd_hash`` rather than one agentgrep computed for it.

    A database that predates the ``project_dir`` migration still yields its
    records; the missing column reads back as ``NULL``, not as an
    ``OperationalError`` that would zero the store.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    project_dir = str(tmp_path / "work" / "agentgrep")
    digest = hashlib.sha256(project_dir.encode("utf-8")).hexdigest()[:16]
    stem = digest if case.stem_is_digest else "context-mode-backup"
    db = tmp_path / f"{stem}.db"

    columns = "id INTEGER PRIMARY KEY, session_id TEXT, type TEXT, data TEXT, created_at TEXT"
    if case.has_project_dir_column:
        columns = f"{columns}, project_dir TEXT NOT NULL DEFAULT ''"
    connection = sqlite3.connect(db)
    try:
        _ = connection.execute(f"CREATE TABLE session_events ({columns})")
        values: tuple[object, ...] = ("s1", "decision", '{"note":"login"}', "2026-06-21")
        if case.has_project_dir_column:
            _ = connection.execute(
                "INSERT INTO session_events "
                "(session_id, type, data, created_at, project_dir) VALUES (?, ?, ?, ?, ?)",
                (*values, project_dir if case.writes_project_dir else ""),
            )
        else:
            _ = connection.execute(
                "INSERT INTO session_events "
                "(session_id, type, data, created_at) VALUES (?, ?, ?, ?)",
                values,
            )
        connection.commit()
    finally:
        connection.close()

    source = agentgrep.SourceHandle(
        agent="pi",
        store="pi.context_mode_db",
        adapter_id="pi.context_mode_sqlite.v1",
        path=db,
        path_kind="sqlite_db",
        source_kind="sqlite",
        search_root=None,
        mtime_ns=1,
    )

    records = list(agentgrep.iter_source_records(source))

    assert len(records) == 1
    origin = t.cast("RecordOrigin | None", records[0].origin)
    expected_cwd = project_dir if case.expect_cwd else None
    expected_cwd_hash = digest if case.expect_cwd_hash else None
    assert origin is not None
    assert origin.cwd == expected_cwd
    assert origin.cwd_hash == expected_cwd_hash
    if origin.cwd is not None and origin.cwd_hash is not None:
        # The free round trip: the directory the row names hashes to the digest
        # the file is named after, so the two encodings agree.
        assert hashlib.sha256(origin.cwd.encode("utf-8")).hexdigest()[:16] == origin.cwd_hash


GROK_PAIRED_TERM = "authflow"
"""The one term every record of the paired Grok fixture contains."""

GROK_PAIRED_SESSION = "019729a0-0000-7000-8000-0000000000aa"


def _seed_grok_project(
    home: pathlib.Path,
    *,
    project_dir_name: str,
    session_id: str = GROK_PAIRED_SESSION,
) -> None:
    """Seed one Grok project directory with a transcript and a prompt log.

    Both stores hang off the same directory name at different depths —
    ``sessions/<name>/prompt_history.jsonl`` and
    ``sessions/<name>/<session>/chat_history.jsonl`` — so seeding them together
    is what makes the parent/grandparent decode disagree loudly if it ever does.
    """
    project = home / ".grok" / "sessions" / project_dir_name
    write_jsonl(
        project / session_id / "chat_history.jsonl",
        [
            {
                "type": "user",
                "content": f"trace the {GROK_PAIRED_TERM} redirect",
                "timestamp": "2026-05-25T10:00:00.000000000Z",
            },
            {
                "type": "assistant",
                "content": f"the {GROK_PAIRED_TERM} redirect is signed server-side",
                "model_id": "grok-4-fast",
                "timestamp": "2026-05-25T10:00:05.000000000Z",
            },
        ],
    )
    write_jsonl(
        project / "prompt_history.jsonl",
        [
            {
                "timestamp": "2026-05-25T10:00:00.000000000Z",
                "session_id": session_id,
                "prompt": f"trace the {GROK_PAIRED_TERM} redirect",
                "is_bash": False,
            },
        ],
    )


def _seed_grok_session_search(
    home: pathlib.Path,
    *,
    cwd: str,
    session_id: str = GROK_PAIRED_SESSION,
) -> None:
    """Seed the Grok FTS index with one row for ``session_id``."""
    db_path = home / ".grok" / "sessions" / "session_search.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        _ = conn.execute(
            "CREATE TABLE session_docs ("
            "  session_id TEXT PRIMARY KEY,"
            "  cwd TEXT NOT NULL,"
            "  updated_at INTEGER NOT NULL,"
            "  title TEXT NOT NULL,"
            "  content TEXT NOT NULL,"
            "  content_hash TEXT NOT NULL"
            ")",
        )
        _ = conn.execute(
            "INSERT INTO session_docs VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                cwd,
                1779750000,
                "Auth redirect",
                f"the {GROK_PAIRED_TERM} redirect is signed server-side",
                "abc123",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _grok_record_cwd(record: t.Any) -> str | None:
    """Return the working directory one Grok record reports, if any."""
    origin = t.cast("RecordOrigin | None", record.origin)
    return origin.cwd if origin is not None else None


def _search_grok(
    home: pathlib.Path,
    term: str,
) -> list[t.Any]:
    """Search Grok through the public discovery and execution surface."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    backends = agentgrep.BackendSelection(None, None, None)
    query = agentgrep.SearchQuery(
        terms=(term,),
        scope="all",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("grok",),
        limit=None,
        dedupe=False,
    )
    sources = agentgrep.discover_sources(home, ("grok",), backends)
    return t.cast("list[t.Any]", agentgrep.search_sources(query, sources, backends))


class GrokProjectOriginCase(t.NamedTuple):
    """One Grok project-directory name and the ``cwd`` it may yield."""

    test_id: str

    project_dir_name: str
    """The directory name Grok wrote under ``sessions/``."""

    expect_cwd: str | None
    """The decoded working directory, or ``None`` when the name is not one."""


GROK_PROJECT_ORIGIN_CASES: tuple[GrokProjectOriginCase, ...] = (
    GrokProjectOriginCase(
        test_id="url-encoded-project-path",
        project_dir_name=urllib.parse.quote("/work/python/agentgrep", safe=""),
        expect_cwd="/work/python/agentgrep",
    ),
    GrokProjectOriginCase(
        test_id="url-encoded-path-with-a-space",
        project_dir_name=urllib.parse.quote("/work/my proj", safe=""),
        expect_cwd="/work/my proj",
    ),
    GrokProjectOriginCase(
        test_id="name-that-is-not-a-path",
        project_dir_name="session-1234",
        expect_cwd=None,
    ),
)


@pytest.mark.parametrize(
    "case",
    GROK_PROJECT_ORIGIN_CASES,
    ids=[c.test_id for c in GROK_PROJECT_ORIGIN_CASES],
)
def test_grok_jsonl_stores_decode_the_project_directory(
    case: GrokProjectOriginCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both Grok JSONL stores recover the working directory from the same name.

    ``%2F`` is a lossless escape, so decoding it is a recovery. A directory
    whose name does not decode to a path is not a project directory, and gets no
    ``cwd`` rather than a plausible-looking one: a fabricated working directory
    does not merely omit a result, it makes a repo-scoped filter silently skip
    the user's own project.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GROK_HOME", raising=False)
    _seed_grok_project(home, project_dir_name=case.project_dir_name)

    records = _search_grok(home, GROK_PAIRED_TERM)

    stores = {record.store for record in records}
    assert stores == {"grok.sessions", "grok.prompt_history"}
    for record in records:
        cwd = _grok_record_cwd(record)
        assert cwd == case.expect_cwd, f"{record.store} decoded {cwd!r}"


def test_grok_chat_history_reads_the_assistant_model_id(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``model_id`` names the model that answered; a user turn names none.

    Grok spells the slug ``model_id`` where the other stores spell it ``model``,
    and only an assistant line carries it. Reading the key in the Grok parser
    keeps the shared ``extract_model`` helper from applying a Grok-specific
    spelling to every other store's payload.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GROK_HOME", raising=False)
    _seed_grok_project(home, project_dir_name=urllib.parse.quote("/work/proj", safe=""))

    records = [
        record for record in _search_grok(home, GROK_PAIRED_TERM) if record.store == "grok.sessions"
    ]

    assert records
    models = {record.role: record.model for record in records}
    assert models == {"user": None, "assistant": "grok-4-fast"}


def test_grok_stores_agree_on_the_session_cwd(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One Grok session reports one working directory across all three stores.

    ``grok.session_search`` reads the ``cwd`` Grok recorded in
    ``session_docs``; the two JSONL stores decode it from the directory name
    Grok filed the session under. They are two encodings of the same fact, and a
    search that returned both a literal and a decoded path for one session would
    answer a single ``cwd:`` filter with two working directories — matching the
    session through the index and missing it through the transcript.
    """
    project_dir = "/work/python/agentgrep"
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GROK_HOME", raising=False)
    _seed_grok_project(home, project_dir_name=urllib.parse.quote(project_dir, safe=""))
    _seed_grok_session_search(home, cwd=project_dir)

    records = _search_grok(home, GROK_PAIRED_TERM)

    session_records = [record for record in records if record.session_id == GROK_PAIRED_SESSION]
    stores = {record.store for record in session_records}
    assert stores == {"grok.sessions", "grok.prompt_history", "grok.session_search"}
    assert {_grok_record_cwd(record) for record in session_records} == {project_dir}
