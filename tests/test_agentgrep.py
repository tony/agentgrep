# ruff: noqa: D102, D103
"""Functional tests for the ``agentgrep`` CLI package."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import io
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

import pytest

import agentgrep as _agentgrep_module

if t.TYPE_CHECKING:
    import collections.abc as cabc

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

    monkeypatch.setattr(agentgrep, "run_readonly_command", run_readonly_command)

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

    monkeypatch.setattr(agentgrep, "grep_root_paths", grep_root_paths)

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

    monkeypatch.setattr(agentgrep, "iter_source_records", iter_records)
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
    monkeypatch.setattr(agentgrep, "iter_source_records", iter_records)
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
    """JSONL parsing yields even before search records are produced."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    path = tmp_path / "events.jsonl"
    lines = [
        json.dumps({"type": "noise", "index": index})
        for index in range(agentgrep._JSONL_YIELD_LINE_INTERVAL + 1)
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    sleep_calls: list[float] = []
    monkeypatch.setattr(agentgrep.time, "sleep", sleep_calls.append)

    parsed = list(agentgrep.iter_jsonl(path))

    assert len(parsed) == agentgrep._JSONL_YIELD_LINE_INTERVAL + 1
    assert sleep_calls == [0]


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
    monkeypatch.setattr(agentgrep, "_JSONL_REVERSE_CHUNK_BYTES", case.chunk_bytes)

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
    monkeypatch.setattr(agentgrep, "_JSONL_REVERSE_CHUNK_BYTES", 9)
    decoded_inputs: list[str] = []
    original_loads = agentgrep.json.loads

    def loads_with_capture(payload: str) -> object:
        decoded_inputs.append(payload)
        return t.cast("object", original_loads(payload))

    monkeypatch.setattr(agentgrep.json, "loads", loads_with_capture)

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
    original_loads = agentgrep.json.loads

    def tracking_loads(payload: str) -> object:
        decoded_payloads.append(payload)
        return original_loads(payload)

    monkeypatch.setattr(agentgrep.json, "loads", tracking_loads)

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

    monkeypatch.setattr(agentgrep, "_discard_rest_of_line", tracking_discard)

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
    original_loads = agentgrep.json.loads

    def tracking_loads(payload: str) -> object:
        decoded_payloads.append(payload)
        return original_loads(payload)

    monkeypatch.setattr(agentgrep.json, "loads", tracking_loads)

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
    original_loads = agentgrep.json.loads

    def tracking_loads(payload: str) -> object:
        decoded_payloads.append(payload)
        return original_loads(payload)

    monkeypatch.setattr(agentgrep.json, "loads", tracking_loads)

    def raw_skip_line(line: str) -> bool:
        return "bliss" not in line

    records = list(
        agentgrep.parse_pi_session_file(source, raw_skip_line=raw_skip_line),
    )

    assert [record.text for record in records] == ["bliss prompt"]
    assert records[0].session_id == "pi-sess-1"
    assert records[0].conversation_id == "/home/user/proj"
    assert decoded_payloads == [header_line, match_line]


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
    progress.source_finished(1, 7, source, records=5, matches=2)
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
    assert snapshots[4].phase == "scanning"
    assert snapshots[4].detail == "128 records, 3 source matches"
    assert snapshots[5].phase == "scanning"
    assert snapshots[5].detail is not None
    assert "matches" in snapshots[5].detail

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

    monkeypatch.setattr(agentgrep, "build_search_haystack", counting_build)
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

    monkeypatch.setattr(agentgrep, "build_search_haystack", raise_if_called)
    matches = agentgrep.compute_filter_matches(records, "alpha")
    assert len(matches) == 3


def _build_empty_ui_app(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> t.Any:
    """Build a streaming UI app with the search worker stubbed to a no-op."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *args, **kwargs: [],
    )
    query = agentgrep.SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    return agentgrep.build_streaming_ui_app(home, query, control=agentgrep.SearchControl())


async def test_streaming_ui_app_mounts_cleanly(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot the Textual app via ``Pilot`` to surface CSS / mount errors in CI.

    Also asserts the results widget is in the screen's focus chain — the
    Textual API requires ``can_focus=True`` as a class keyword (not a class
    attribute), and that detail is easy to get wrong on a dynamic-base
    subclass.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    # Wide enough for the side-by-side layout — below the split breakpoint
    # the detail pane collapses (display: none) and leaves the focus chain.
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        focus_chain_ids = {getattr(w, "id", None) for w in app.screen.focus_chain}
        assert "results" in focus_chain_ids, f"#results not in focus chain; chain={focus_chain_ids}"
        # Both inputs and the detail pane should be focusable too.
        assert {"search", "filter", "detail-scroll"}.issubset(focus_chain_ids)


async def test_streaming_ui_app_wires_inline_completion(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The search and filter inputs carry working inline-completion suggesters."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        filter_input = app.screen.query_one("#filter")
        assert search.suggester is not None
        assert filter_input.suggester is not None
        # The query suggester completes a bare field-name prefix.
        suggestion = await search.suggester.get_suggestion("age")
        assert suggestion == "agent:"


async def test_streaming_ui_app_enum_dropdown_opens_and_closes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typing an enum field predicate opens the value dropdown; other text hides it."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        dropdown = app.screen.query_one("#enum-dropdown")

        # An enum field token opens the dropdown with one option per value.
        search.value = "scope:"
        await pilot.pause()
        assert dropdown.display is True
        assert dropdown.option_count == 3  # prompts, conversations, all

        # A partial filters the values.
        search.value = "agent:cu"
        await pilot.pause()
        assert dropdown.display is True
        assert dropdown.option_count == 2  # cursor-cli, cursor-ide

        # The dropdown tracks the input cursor: a long prefix pushes it right.
        search.value = "ruff codex review notes scope:"
        search.cursor_position = len(search.value)
        await pilot.pause()
        assert dropdown.display is True
        # Left edge is anchored near the cursor column, not pinned at 0.
        cursor_x = search.cursor_screen_offset.x
        assert abs(dropdown.region.x - (cursor_x - 1)) <= 1
        assert dropdown.region.x > 10

        # Non-enum / bare text hides it.
        search.value = "ruff"
        await pilot.pause()
        assert dropdown.display is False


async def test_streaming_ui_app_filter_dropdown_and_query_aware(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filter box gets a keyword dropdown and a query-aware matcher."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        filter_input = app.screen.query_one("#filter")
        dropdown = app.screen.query_one("#filter-dropdown")

        # A bare token lists field-name keywords (no record vocabulary).
        filter_input.value = "agent"
        filter_input.cursor_position = len("agent")
        await pilot.pause()
        assert dropdown.display is True
        assert app._filter_dropdown_values[0] == "agent:"

        # A field token lists the enum values.
        filter_input.value = "scope:"
        filter_input.cursor_position = len("scope:")
        await pilot.pause()
        assert app._filter_dropdown_values == ("prompts", "conversations", "all")

        # The filter executes the query language: a predicate compiles to a
        # matcher; empty/whitespace yields no matcher (all records pass).
        assert app._build_filter_matcher("agent:codex") is not None
        assert app._build_filter_matcher("   ") is None

        # A free-text term that isn't a keyword shows no dropdown.
        filter_input.value = "zzznomatch"
        filter_input.cursor_position = len("zzznomatch")
        await pilot.pause()
        assert dropdown.display is False


async def test_dropdown_accept_leaves_cursor_at_end_without_selecting(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepting a dropdown choice places the cursor at the end, not select-all."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        search.value = "agent:co"
        search.cursor_position = len("agent:co")
        await pilot.pause()
        assert app._enum_values == ("codex",)

        app._accept_dropdown_choice(search, app._enum_dropdown, app._enum_values, 0)
        await pilot.pause()

        assert search.value == "agent:codex"
        assert search.cursor_position == len("agent:codex")
        assert search.selection.is_empty


async def test_detail_pane_highlights_filter_terms_distinctly(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filter terms are highlighted in the detail body in a distinct style."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app._filter_terms = ("mobx",)
        body = "use biome and mobx here"
        renderable, _ = app._build_detail_body(body, ("biome",))

        spans = [(s.start, s.end, str(s.style)) for s in renderable.spans]
        biome = body.index("biome")
        mobx = body.index("mobx")
        # Search term keeps the yellow highlight; filter term gets its own.
        assert any(
            s == biome and e == biome + len("biome") and "yellow" in style for s, e, style in spans
        )
        assert any(
            s == mobx and e == mobx + len("mobx") and "cyan" in style for s, e, style in spans
        )


async def test_dropdown_dismissal_keys_close_without_accepting(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Esc, Enter, and Ctrl+C dismiss an open dropdown without auto-accepting."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        dropdown = app.screen.query_one("#enum-dropdown")
        search.focus()
        await pilot.pause()

        # Each block uses a distinct value so the reactive fires Changed and
        # the dropdown reopens.
        #
        # Esc dismisses and keeps focus in the input (still editing).
        search.value = "agent:"
        search.cursor_position = len(search.value)
        await pilot.pause()
        assert dropdown.display is True
        await pilot.press("escape")
        await pilot.pause()
        assert dropdown.display is False
        assert app.focused is search

        # Enter closes the dropdown without accepting a value.
        search.value = "scope:"
        search.cursor_position = len(search.value)
        await pilot.pause()
        assert dropdown.display is True
        await pilot.press("enter")
        await pilot.pause()
        assert dropdown.display is False
        assert search.value == "scope:"

        # Ctrl+C dismisses the dropdown instead of quitting the app.
        search.value = "agent:cu"
        search.cursor_position = len(search.value)
        await pilot.pause()
        assert dropdown.display is True
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert dropdown.display is False
        # The app is still running (Ctrl+C was consumed by the dropdown).
        assert app.screen.query_one("#search") is search


async def test_empty_query_focuses_search_input_and_marks_search_done(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no initial query, the search bar takes focus and chrome is idle."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "search"
        assert app._search_done is True


def test_streaming_ui_app_passes_runtime_to_search_worker(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TUI owns one runtime and passes it to backend searches."""
    from agentgrep.ui import app as ui_app

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    runtimes: list[object] = []

    def record_runtime(*_args: object, **kwargs: object) -> list[object]:
        runtimes.append(kwargs.get("runtime"))
        return []

    monkeypatch.setattr(ui_app, "run_search_query", record_runtime)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    app = agentgrep.build_streaming_ui_app(home, query, control=agentgrep.SearchControl())

    app._reset_search_chrome()
    app._run_search()
    app._run_search()

    assert len(runtimes) == 2
    assert isinstance(runtimes[0], agentgrep.SearchRuntime)
    assert runtimes[0] is runtimes[1]
    assert runtimes[0].source_scan_cache is not None


async def test_search_input_posts_search_requested_only_on_enter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typing alone posts nothing; pressing Enter posts one ``SearchRequested``.

    The ``SearchRequested`` class lives inside the streaming-app factory
    closure, so the test sniffs every posted message and filters to ones
    whose payload type matches :class:`SearchRequestedPayload`.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    posts: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app._search_input.focus()
        await pilot.pause()
        original_post_message = app._search_input.post_message

        def capture(message: object) -> bool:
            payload = getattr(message, "payload", None)
            if isinstance(payload, agentgrep.SearchRequestedPayload):
                posts.append(payload.text)
            return original_post_message(message)

        monkeypatch.setattr(app._search_input, "post_message", capture)
        await pilot.press("b")
        await pilot.press("l")
        await pilot.press("i")
        await pilot.pause(0.4)
        assert posts == [], f"keystrokes should not auto-post; got {posts}"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert posts == ["bli"], f"expected one post on Enter, got {posts}"


async def test_search_input_dispatch_spawns_search_group_worker(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing Enter on a non-empty search bar spawns a ``search`` worker."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    spawned: list[dict[str, object]] = []

    def fake_worker(*args: object, **kwargs: object) -> None:
        spawned.append({"args": args, "kwargs": kwargs})

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "run_worker", fake_worker)
        app._search_input.focus()
        await pilot.pause()
        app._search_input.value = "bliss"
        await pilot.pause(0.1)
        assert spawned == [], f"value change alone should not spawn; got {spawned}"
        await pilot.press("enter")
        await pilot.pause(0.1)
        groups = [t.cast("dict[str, object]", entry["kwargs"]).get("group") for entry in spawned]
        assert "search" in groups, f"expected a search-group worker, got {spawned}"


async def test_search_input_enter_replaces_control_to_cancel_prior_search(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each new search signals the prior control and installs a fresh one.

    The cooperative cancel contract is: the old worker thread keeps its
    (now-signaled) ``SearchControl`` reference and bails out; the new
    worker gets a fresh, un-signaled control.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)

    async with app.run_test() as pilot:
        await pilot.pause()
        # Stub run_worker so the app's worker bookkeeping doesn't fight us.
        monkeypatch.setattr(app, "run_worker", lambda *a, **kw: None)
        app._search_input.focus()
        await pilot.pause()
        app._search_input.value = "first"
        await pilot.press("enter")
        await pilot.pause(0.1)
        first_control = app.control
        assert first_control.answer_now_requested() is False
        app._search_input.value = "second"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert app.control is not first_control, "control should be replaced on new search"
        assert first_control.answer_now_requested() is True, (
            "prior control should be signaled to cancel"
        )
        assert app.control.answer_now_requested() is False, (
            "fresh control should not carry over the cancel flag"
        )


async def test_tab_moves_focus_from_filter_to_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tab on the filter input moves focus to the DataTable below it."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        # On empty initial query the search bar takes initial focus, so
        # manually move focus to the filter input for this test.
        app._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "filter"
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "results"


async def test_down_at_empty_filter_releases_focus_to_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``down`` arrow on an empty filter moves focus to the results table."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


async def test_up_at_results_top_row_releases_focus_to_filter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``up`` when the results-list cursor is at row 0 moves focus to the filter."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="seed row",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        # Seed one record so the list has a row 0 to be on.
        app.all_records.append(record)
        app.filtered_records.append(record)
        app._results.append_records([record])
        await pilot.pause()
        # Land focus on the filter and tab to the results.
        app._filter_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"
        # Ensure highlight is on row 0 before pressing up.
        assert app._results.highlighted in (None, 0)
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


async def test_l_from_results_focuses_detail_pane(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vim-style ``l`` (and right-arrow) from the results list focuses the detail pane."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="seed row",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.append(record)
        app.filtered_records.append(record)
        app._results.append_records([record])
        await pilot.pause()
        app._filter_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"
        await pilot.press("l")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"


async def test_k_at_detail_top_focuses_filter_input(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``k`` / ``up`` on the detail pane at scroll_y=0 releases focus to the filter input."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="seed row",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.append(record)
        app.filtered_records.append(record)
        app._results.append_records([record])
        await pilot.pause()
        app._detail_scroll.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        # Pre-condition: at the top of the (short) detail body.
        assert app._detail_scroll.scroll_y <= 0
        await pilot.press("k")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


async def test_h_from_detail_focuses_results_pane(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vim-style ``h`` (and left-arrow) from the detail pane focuses the results list."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="seed row",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.append(record)
        app.filtered_records.append(record)
        app._results.append_records([record])
        await pilot.pause()
        # Focus the detail-scroll widget directly, then bounce back via ``h``.
        app._detail_scroll.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        await pilot.press("h")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


def _seed_records(
    agentgrep: t.Any,
    tmp_path: pathlib.Path,
    count: int,
) -> list[t.Any]:
    """Build ``count`` ``SearchRecord`` instances under ``tmp_path``."""
    return [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(count)
    ]


async def test_g_on_results_jumps_to_top(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``g`` while the results list is focused snaps the cursor to row 0."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        app._filter_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        app._results.highlighted = 3
        await pilot.pause()
        assert app._results.highlighted == 3
        await pilot.press("g")
        await pilot.pause()
        assert app._results.highlighted == 0


async def test_G_on_results_jumps_to_bottom(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``G`` while the results list is focused snaps the cursor to the last row."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        app._filter_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("G")
        await pilot.pause()
        assert app._results.highlighted == 4


async def test_ctrl_d_on_results_advances_half_page(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-D`` on the results list advances the highlight by at least one row."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 20)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        app._filter_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        app._results.highlighted = 0
        await pilot.pause()
        await pilot.press("ctrl+d")
        await pilot.pause()
        # Robust against tiny viewports during ``run_test`` — half-page may be
        # as small as 1 if the simulated screen is shallow. Either way, the
        # cursor must have moved forward and stayed within bounds.
        assert app._results.highlighted is not None
        assert app._results.highlighted > 0
        assert app._results.highlighted <= len(records) - 1


async def test_g_on_detail_scrolls_to_top(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``g`` on the detail pane jumps scroll_y back to 0."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    long_body = "\n".join(f"line {idx}" for idx in range(200))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "long.jsonl",
        text=long_body,
    )
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.all_records.append(record)
        app.filtered_records.append(record)
        app._results.append_records([record])
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        app._detail_scroll.scroll_to(y=50, animate=False)
        await pilot.pause()
        assert app._detail_scroll.scroll_y > 0
        app._detail_scroll.focus()
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        assert app._detail_scroll.scroll_y == 0


async def test_G_on_detail_scrolls_to_bottom(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``G`` on the detail pane snaps scroll_y to (near) the maximum."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    long_body = "\n".join(f"line {idx}" for idx in range(200))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "long.jsonl",
        text=long_body,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.append(record)
        app.filtered_records.append(record)
        app._results.append_records([record])
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        app._detail_scroll.focus()
        await pilot.pause()
        await pilot.press("G")
        await pilot.pause()
        assert app._detail_scroll.scroll_y >= app._detail_scroll.max_scroll_y - 0.5


async def test_ctrl_f_on_detail_pages_down(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-F`` on the detail pane scrolls down by approximately one page."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    long_body = "\n".join(f"line {idx}" for idx in range(200))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "long.jsonl",
        text=long_body,
    )
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.all_records.append(record)
        app.filtered_records.append(record)
        app._results.append_records([record])
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        app._detail_scroll.focus()
        await pilot.pause()
        before = app._detail_scroll.scroll_y
        await pilot.press("ctrl+f")
        await pilot.pause()
        # Scrolled forward; exact delta depends on viewport size, just assert
        # something happened in the right direction.
        assert app._detail_scroll.scroll_y > before


async def test_ctrl_j_from_filter_focuses_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-J`` while the filter input has focus moves focus to the results list."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        app._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("ctrl+j")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


async def test_ctrl_l_from_results_focuses_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-L`` from the results list moves focus rightward to the detail pane."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        app._filter_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"


async def test_ctrl_h_from_detail_focuses_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-H`` from the detail pane moves focus leftward to the results list."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        app._detail_scroll.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        await pilot.press("ctrl+h")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


async def test_ctrl_k_from_results_focuses_filter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-K`` from the results list moves focus up to the filter input."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        app._filter_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


async def test_ctrl_k_from_detail_focuses_filter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-K`` from the detail pane jumps focus all the way back to the filter."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        app._detail_scroll.focus()
        await pilot.pause()
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


async def test_backspace_from_detail_focuses_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backspace aliases ``Ctrl-H`` in many terminals — should focus results from detail."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        app._detail_scroll.focus()
        await pilot.pause()
        await pilot.press("backspace")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


async def test_backspace_in_filter_still_deletes_a_character(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backspace alias must NOT steal backspace from the filter input."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("a")
        await pilot.press("b")
        await pilot.press("c")
        await pilot.pause()
        assert app._filter_input.value == "abc"
        await pilot.press("backspace")
        await pilot.pause()
        # Backspace deleted the last character; focus stayed on filter.
        assert app._filter_input.value == "ab"
        assert app.focused.id == "filter"


async def test_ctrl_h_from_filter_is_a_noop(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-H`` on the filter does nothing (no pane to the left)."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        app._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("ctrl+h")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


async def test_up_on_empty_filter_releases_focus_to_search(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain ``up`` on an empty filter input lifts focus to the top search bar."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "search"


async def test_up_on_filter_with_cursor_at_start_releases_focus_to_search(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``up`` on a non-empty filter whose cursor is at position 0 still escapes upward."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._filter_input.focus()
        await pilot.pause()
        # Type something, then move cursor back to start.
        app._filter_input.value = "abc"
        app._filter_input.cursor_position = 0
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "search"


class FocusDetailRevealCase(t.NamedTuple):
    """One width scenario for ``right``/``l`` focusing the detail pane."""

    test_id: str
    size: tuple[int, int]
    expect_opened: bool


FOCUS_DETAIL_REVEAL_CASES: tuple[FocusDetailRevealCase, ...] = (
    FocusDetailRevealCase(
        test_id="wide-records-explicit-focus", size=(120, 24), expect_opened=True
    ),
    FocusDetailRevealCase(test_id="narrow-opens-on-focus", size=(80, 24), expect_opened=True),
)


@pytest.mark.parametrize(
    "case",
    FOCUS_DETAIL_REVEAL_CASES,
    ids=[case.test_id for case in FOCUS_DETAIL_REVEAL_CASES],
)
async def test_right_on_empty_filter_focuses_and_opens_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: FocusDetailRevealCase,
) -> None:
    """``right`` on an empty filter focuses the detail — opening it when stacked.

    On a narrow terminal the detail starts collapsed (``display: none``);
    focusing it must reveal it first, not move focus into a hidden pane.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=case.size) as pilot:
        await pilot.pause()
        app._filter_input.focus()
        await pilot.pause()
        assert app._filter_input.value == ""
        await pilot.press("right")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        assert not app._detail_column.has_class("-collapsed")
        # Explicit detail focus records the user's reader intent even when
        # wide mode already has the pane visible.
        assert app._detail_opened is case.expect_opened


class DetailFocusResizeCase(t.NamedTuple):
    """One explicit detail-focus route before a wide-to-narrow resize."""

    test_id: str
    key: str


DETAIL_FOCUS_RESIZE_CASES: tuple[DetailFocusResizeCase, ...] = (
    DetailFocusResizeCase(test_id="l-from-results", key="l"),
    DetailFocusResizeCase(test_id="right-from-results", key="right"),
    DetailFocusResizeCase(test_id="ctrl-l-from-results", key="ctrl+l"),
)


@pytest.mark.parametrize(
    "case",
    DETAIL_FOCUS_RESIZE_CASES,
    ids=[case.test_id for case in DETAIL_FOCUS_RESIZE_CASES],
)
async def test_explicit_wide_detail_focus_survives_narrow_resize(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: DetailFocusResizeCase,
) -> None:
    """Explicit reader focus in wide mode remains visible after stacking."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records = list(records)
        app._results.set_records(records)
        app._apply_responsive_layout()
        app._results.focus()
        await pilot.pause()

        await pilot.press(case.key)
        await pilot.pause()
        assert app._stacked is False
        assert app.focused is not None and app.focused.id == "detail-scroll"
        assert app._detail_opened is True

        await pilot.resize_terminal(80, 24)
        await pilot.pause(0.1)
        assert app._stacked is True
        assert app._detail_opened is True
        assert not app._detail_column.has_class("-collapsed")
        assert app.focused is not None and app.focused.id == "detail-scroll"


async def test_l_from_results_opens_stacked_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing ``l`` in the results list opens + focuses the stacked detail."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records = list(records)
        app._results.set_records(records)
        app._apply_responsive_layout()
        await pilot.pause()
        assert app._detail_column.has_class("-collapsed")
        app._results.focus()
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        assert not app._detail_column.has_class("-collapsed")
        assert app._detail_opened is True


class FocusDetailRenderCase(t.NamedTuple):
    """One explicit-detail focus scenario and the record it should render."""

    test_id: str
    highlighted: int | None
    expected_index: int


FOCUS_DETAIL_RENDER_CASES: tuple[FocusDetailRenderCase, ...] = (
    FocusDetailRenderCase(
        test_id="no-highlight-falls-back-to-first-record",
        highlighted=None,
        expected_index=0,
    ),
    FocusDetailRenderCase(
        test_id="highlighted-record-wins",
        highlighted=2,
        expected_index=2,
    ),
)


@pytest.mark.parametrize(
    "case",
    FOCUS_DETAIL_RENDER_CASES,
    ids=[case.test_id for case in FOCUS_DETAIL_RENDER_CASES],
)
async def test_focus_detail_renders_record_when_opening_stacked_streaming_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: FocusDetailRenderCase,
) -> None:
    """Opening a stacked streaming result renders a readable detail body."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    app.query = agentgrep.SearchQuery(
        terms=("VISIBLEPROBE",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"prefix\nVISIBLEPROBE record {idx}\nsuffix",
        )
        for idx in range(3)
    ]
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records = list(records)
        app._results.append_records(records)
        if case.highlighted is not None:
            # Seed Textual's reactive storage directly so this case can
            # model a highlighted row without dispatching the same genuine
            # cursor-move event that normally opens the stacked detail.
            app._results._reactive_highlighted = case.highlighted
            app._current_detail_record = records[0]
            app._detail_opened = False
        app._apply_responsive_layout()
        await pilot.pause()
        assert app._detail_column.has_class("-collapsed")
        app._results.focus()
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        expected = records[case.expected_index]
        assert app.focused is not None and app.focused.id == "detail-scroll"
        assert app._current_detail_record is expected
        assert not app._detail_column.has_class("-collapsed")
        screenshot = app.export_screenshot(simplify=True)
        assert "VISIBLEPROBE" in screenshot
        assert f"record&#160;{case.expected_index}" in screenshot


class _FakeFilterCompleted(t.NamedTuple):
    """Minimal ``FilterCompleted`` stand-in carrying just the payload."""

    payload: t.Any


class AutohighlightQueueCase(t.NamedTuple):
    """One filter-result scenario for queued programmatic highlights."""

    test_id: str
    record_count: int
    matching_count: int
    initial_highlighted: int | None
    expect_pending: int


AUTOHIGHLIGHT_QUEUE_CASES: tuple[AutohighlightQueueCase, ...] = (
    AutohighlightQueueCase(
        test_id="streamed-results-without-highlight",
        record_count=3,
        matching_count=3,
        initial_highlighted=None,
        expect_pending=0,
    ),
    AutohighlightQueueCase(
        test_id="empty-leaves-it-disarmed",
        record_count=3,
        matching_count=0,
        initial_highlighted=None,
        expect_pending=0,
    ),
    AutohighlightQueueCase(
        test_id="single-clamp-highlight",
        record_count=3,
        matching_count=2,
        initial_highlighted=2,
        expect_pending=1,
    ),
    AutohighlightQueueCase(
        test_id="multi-clamp-highlights",
        record_count=10,
        matching_count=5,
        initial_highlighted=9,
        expect_pending=5,
    ),
)


@pytest.mark.parametrize(
    "case",
    AUTOHIGHLIGHT_QUEUE_CASES,
    ids=[case.test_id for case in AUTOHIGHLIGHT_QUEUE_CASES],
)
async def test_filter_completion_counts_only_queued_autohighlights(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: AutohighlightQueueCase,
) -> None:
    """The suppression counter tracks queued highlights, not non-empty results."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, case.record_count)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records = list(records)
        app._results.append_records(records)
        if case.initial_highlighted is not None:
            app._results._reactive_highlighted = case.initial_highlighted
        app._pending_autohighlights = 99
        payload = agentgrep.FilterCompletedPayload(
            text="",
            matching=tuple(records[: case.matching_count]),
        )
        app.on_filter_completed(_FakeFilterCompleted(payload=payload))
        assert app._pending_autohighlights == case.expect_pending


class FilterUserMoveCase(t.NamedTuple):
    """One filter path and the first genuine cursor move after it."""

    test_id: str
    record_count: int
    matching_count: int
    initial_highlighted: int | None
    first_user_key: str


FILTER_USER_MOVE_CASES: tuple[FilterUserMoveCase, ...] = (
    FilterUserMoveCase(
        test_id="streamed-results-without-highlight",
        record_count=3,
        matching_count=3,
        initial_highlighted=None,
        first_user_key="j",
    ),
    FilterUserMoveCase(
        test_id="narrowing-keeps-highlight-index",
        record_count=3,
        matching_count=2,
        initial_highlighted=0,
        first_user_key="j",
    ),
    FilterUserMoveCase(
        test_id="single-clamp-highlight-is-programmatic",
        record_count=3,
        matching_count=2,
        initial_highlighted=2,
        first_user_key="k",
    ),
    FilterUserMoveCase(
        test_id="multi-clamp-highlights-are-programmatic",
        record_count=10,
        matching_count=5,
        initial_highlighted=9,
        first_user_key="k",
    ),
)


@pytest.mark.parametrize(
    "case",
    FILTER_USER_MOVE_CASES,
    ids=[case.test_id for case in FILTER_USER_MOVE_CASES],
)
async def test_filter_completion_does_not_swallow_first_real_cursor_move(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: FilterUserMoveCase,
) -> None:
    """Only queued programmatic highlights may keep stacked detail collapsed."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, case.record_count)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records = list(records)
        app._results.append_records(records)
        if case.initial_highlighted is not None:
            app._results._reactive_highlighted = case.initial_highlighted
        app._detail_opened = False
        app._apply_responsive_layout()
        app._results.focus()
        await pilot.pause()

        payload = agentgrep.FilterCompletedPayload(
            text="",
            matching=tuple(records[: case.matching_count]),
        )
        app.on_filter_completed(_FakeFilterCompleted(payload=payload))
        await pilot.pause()
        await pilot.pause()
        assert app._detail_opened is False
        assert app._detail_column.has_class("-collapsed")

        await pilot.press(case.first_user_key)
        await pilot.pause()
        assert app._detail_opened is True
        assert not app._detail_column.has_class("-collapsed")


async def test_right_on_non_empty_filter_moves_cursor(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``right`` on a non-empty filter walks the cursor — does not release focus."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._filter_input.focus()
        await pilot.pause()
        app._filter_input.value = "abc"
        app._filter_input.cursor_position = 0
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        # Focus stays on the filter; cursor advances by one.
        assert app.focused is not None and app.focused.id == "filter"
        assert app._filter_input.cursor_position == 1


async def test_search_results_list_append_under_load(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Appending 1000 records to the results list completes within a generous bound.

    Smoke test against accidental O(N²) regressions. Bound is intentionally
    loose because ``OptionList.add_options`` is O(M) per call (vs the prior
    custom widget's O(1)) — we're trading per-record speed for proven
    correctness (visible cursor + Tab focus). If the bound trips on real
    hardware, the escalation path is ``textual-fastdatatable``.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(1000)
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        start = time.monotonic()
        app._results.append_records(records)
        elapsed = time.monotonic() - start
        await pilot.pause()
        assert len(app._results._records) == 1000
        assert elapsed < 2.0, f"append_records(1000) took {elapsed:.3f}s; expected < 2.0s"


async def test_set_records_narrowing_avoids_clear_options(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A narrowing filter (subset of current records) must not full-rebuild the list."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(10)
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._results.append_records(records)
        await pilot.pause()
        clear_count = 0
        original_clear = app._results.clear_options

        def counting_clear() -> object:
            nonlocal clear_count
            clear_count += 1
            return original_clear()

        monkeypatch.setattr(app._results, "clear_options", counting_clear)
        # Narrow to the first 7 records (drop 3). 3 / 10 <= 50% → delta path.
        app._results.set_records(records[:7])
        await pilot.pause()
        assert clear_count == 0
        assert len(app._results._records) == 7
        assert [id(r) for r in app._results._records] == [id(r) for r in records[:7]]


async def test_set_records_widening_triggers_full_rebuild(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Widening (introducing records not currently shown) rebuilds for order correctness."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(5)
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._results.append_records(records[:3])
        await pilot.pause()
        clear_count = 0
        original_clear = app._results.clear_options

        def counting_clear() -> object:
            nonlocal clear_count
            clear_count += 1
            return original_clear()

        monkeypatch.setattr(app._results, "clear_options", counting_clear)
        # Widen to all 5 records — two of them weren't shown before.
        app._results.set_records(records)
        await pilot.pause()
        assert clear_count == 1, "widening must rebuild to preserve record order"
        assert len(app._results._records) == 5


async def test_apply_records_batch_yields_between_chunks(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Applying a large batch yields to the event loop every chunk_size records."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    chunk = app._APPLY_CHUNK_SIZE
    # Three chunks worth — should yield twice (between chunk 0/1 and 1/2).
    record_count = chunk * 3
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(record_count)
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        sleep_calls = 0
        real_sleep = asyncio.sleep

        async def counting_sleep(delay: float) -> None:
            nonlocal sleep_calls
            if delay == 0:
                sleep_calls += 1
            await real_sleep(delay)

        monkeypatch.setattr(asyncio, "sleep", counting_sleep)
        await app._apply_records_batch(records, record_count)
        assert sleep_calls >= 2, (
            f"expected >= 2 yields for {record_count} records in chunks of {chunk}, "
            f"got {sleep_calls}"
        )
        assert len(app._results._records) == record_count


async def test_set_records_majority_removal_falls_back_to_rebuild(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing more than half of the current records uses the chunked rebuild path."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(10)
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._results.append_records(records)
        await pilot.pause()
        clear_count = 0
        original_clear = app._results.clear_options

        def counting_clear() -> object:
            nonlocal clear_count
            clear_count += 1
            return original_clear()

        monkeypatch.setattr(app._results, "clear_options", counting_clear)
        # Drop 8 of 10 — well over the 50% threshold.
        app._results.set_records(records[:2])
        await pilot.pause()
        assert clear_count == 1, "majority-removal must take the rebuild path"
        assert len(app._results._records) == 2


def test_scroll_percent_returns_full_when_nothing_scrolls() -> None:
    """A pane that fits its viewport reports ``100%`` (tig convention)."""
    from agentgrep.ui.app import scroll_percent

    assert scroll_percent(0.0, 0.0) == 100


def test_scroll_percent_clamps_to_bounds() -> None:
    """Scroll percent is clamped to ``[0, 100]`` even for nonsense inputs."""
    from agentgrep.ui.app import scroll_percent

    assert scroll_percent(0.0, 100.0) == 0
    assert scroll_percent(50.0, 100.0) == 50
    assert scroll_percent(100.0, 100.0) == 100
    # Overshoot past max — clamped to 100.
    assert scroll_percent(500.0, 100.0) == 100
    # Negative scroll — clamped to 0.
    assert scroll_percent(-10.0, 100.0) == 0


async def test_results_status_right_shows_position_or_count(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wide right slots show ``{cursor+1}/{visible}`` once a cursor exists.

    Before a cursor exists the bare match count renders; the denominator
    carries the count afterwards, so the two never appear together.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    # Wide terminal — the narrow breakpoint has its own slot behavior.
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        # No streaming results yet — empty right slot regardless of args.
        assert app._format_results_right(cursor=None, visible=None) == ""
        # Seed streaming totals so the match count segment renders.
        app.all_records.extend(_seed_records(agentgrep, tmp_path, 10))
        # No cursor yet — bare match count.
        assert app._format_results_right(cursor=None, visible=10) == "10 matches"
        # Cursor at row 0 of all 10 — position only, no restated count.
        assert app._format_results_right(cursor=0, visible=10) == "1/10"


async def test_detail_statusline_shows_path_and_scroll_percent(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``show_detail`` populates the detail status line with path + scroll %."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "session.jsonl",
        text="hello",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        updates: list[str] = []
        real_update = app._detail_statusline.update

        def spy(content: t.Any = "", *args: t.Any, **kwargs: t.Any) -> None:
            updates.append(str(content))
            real_update(content, *args, **kwargs)

        monkeypatch.setattr(app._detail_statusline, "update", spy)
        app.show_detail(record)
        await pilot.pause()
        # Latest update should carry both the path's basename and a trailing ``%``.
        rendered = updates[-1] if updates else ""
        assert "session.jsonl" in rendered
        assert rendered.rstrip().endswith("%")


async def test_results_scroll_changed_updates_status_right(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app handler updates ``#status-right`` when the OptionList scrolls."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    # Wide terminal — the narrow breakpoint drops the cursor/visible
    # segment this test asserts on.
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        updates: list[str] = []
        real_update = app._matches_widget.update

        def spy(content: t.Any = "", *args: t.Any, **kwargs: t.Any) -> None:
            updates.append(str(content))
            real_update(content, *args, **kwargs)

        monkeypatch.setattr(app._matches_widget, "update", spy)
        # Pre-seed streaming records so the match count is non-zero.
        app.all_records.extend(records)
        app._results.append_records(records)
        await pilot.pause()
        # Explicitly land focus and move cursor to row 0 — the reactive
        # ``highlighted`` watcher fires on change, so set it directly.
        app._results.focus()
        await pilot.pause()
        app._results.highlighted = 0
        await pilot.pause()
        # The ``highlighted`` watcher posts ``ResultsScrollChanged`` which
        # the app handler renders as the cursor position ``1/5``.
        assert any(u == "1/5" for u in updates), f"expected '1/5' in {updates!r}"


class RightSlotWidthCase(t.NamedTuple):
    """One terminal-width scenario for the results-status right slot."""

    test_id: str
    size: tuple[int, int]
    searching: bool
    cursor: int | None
    expected: str


RIGHT_SLOT_WIDTH_CASES: tuple[RightSlotWidthCase, ...] = (
    RightSlotWidthCase(
        test_id="wide-cursor-shows-position-only",
        size=(160, 24),
        searching=False,
        cursor=0,
        expected="1/5",
    ),
    RightSlotWidthCase(
        test_id="wide-no-cursor-shows-count",
        size=(160, 24),
        searching=False,
        cursor=None,
        expected="5 matches",
    ),
    RightSlotWidthCase(
        test_id="narrow-searching-shows-search-percent",
        size=(40, 24),
        searching=True,
        cursor=0,
        expected="5 matches  84%",
    ),
    RightSlotWidthCase(
        test_id="narrow-done-shows-count-only",
        size=(40, 24),
        searching=False,
        cursor=0,
        expected="5 matches",
    ),
)


@pytest.mark.parametrize(
    "case",
    RIGHT_SLOT_WIDTH_CASES,
    ids=[case.test_id for case in RIGHT_SLOT_WIDTH_CASES],
)
async def test_results_status_right_adapts_to_width(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: RightSlotWidthCase,
) -> None:
    """Narrow right slots show search progress while running, count when done."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=case.size) as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        if case.searching:
            app._search_done = False
            # 5662/6748 sources scanned rounds to 84%.
            app._apply_progress(_make_progress_snapshot(agentgrep))
            await pilot.pause()
        assert app._format_results_right(case.cursor, 5) == case.expected


def _make_progress_snapshot(agentgrep: t.Any, **overrides: t.Any) -> t.Any:
    """Build a scanning-phase ``ProgressSnapshot`` with overridable fields."""
    fields: dict[str, t.Any] = {
        "query_label": "tmux",
        "phase": "scanning",
        "current": 5662,
        "total": 6748,
        "detail": "2176 records, 354 source matches",
        "matches": 2176,
        "elapsed": 32.0,
    }
    fields.update(overrides)
    return agentgrep.ProgressSnapshot(**fields)


async def test_apply_progress_drives_meter_and_left_text(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scanning snapshot fills the ▰▱ meter and paints the elapsed left text."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    # Wide terminal: the results column must clear the narrow breakpoint
    # so the bar and the "(0s)" elapsed suffix both render.
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app._search_done = False
        app._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        assert app._meter_widget._fraction == pytest.approx(5662 / 6748)
        rendered = app._meter_widget._compose_text()
        assert "▰" in rendered
        assert rendered.endswith("%")
        # The query itself is not repeated — the search box shows it.
        assert app._last_left_text.startswith("Searching… (")


async def test_meter_indeterminate_before_total_shows_phase_word(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a source total the meter shows the phase word, not a bar."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app._search_done = False
        app._apply_progress(
            _make_progress_snapshot(
                agentgrep,
                phase="discovering",
                current=None,
                total=None,
                detail=None,
            ),
        )
        await pilot.pause()
        assert app._meter_widget._fraction is None
        rendered = app._meter_widget._compose_text()
        assert rendered == "discovering"
        assert "▰" not in rendered


async def test_ctrl_backslash_toggles_scanning_detail_row(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""``Ctrl-\`` shows the verbose scanning row, a second press hides it."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app._search_done = False
        app._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        detail_row = app.screen.query_one("#status-detail")
        assert not detail_row.has_class("visible")
        await pilot.press("ctrl+backslash")
        await pilot.pause()
        assert app._detail_visible is True
        assert detail_row.has_class("visible")
        assert (
            app._last_detail_text == "Scanning 5662/6748 sources | 2176 records, 354 source matches"
        )
        await pilot.press("ctrl+backslash")
        await pilot.pause()
        assert app._detail_visible is False
        assert not detail_row.has_class("visible")


async def test_detail_row_visibility_sticky_across_search_reset(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new search keeps the detail row visible but wipes its stale content."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app._search_done = False
        app._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        await pilot.press("ctrl+backslash")
        await pilot.pause()
        assert app._detail_visible is True
        updates: list[str] = []
        real_update = app._detail_row.update

        def spy(content: t.Any = "", *args: t.Any, **kwargs: t.Any) -> None:
            updates.append(str(content))
            real_update(content, *args, **kwargs)

        monkeypatch.setattr(app._detail_row, "update", spy)
        app._reset_search_chrome()
        await pilot.pause()
        assert app._detail_visible is True
        assert updates[-1] == ""


async def test_elapsed_ticker_starts_on_progress_and_stops_on_finish(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 1 Hz ticker arms on the first snapshot and disarms on finish."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app._search_done = False
        assert app._elapsed_timer is None
        app._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        assert app._elapsed_timer is not None
        updates: list[str] = []
        real_update = app._status_widget.update

        def spy(content: t.Any = "", *args: t.Any, **kwargs: t.Any) -> None:
            updates.append(str(content))
            real_update(content, *args, **kwargs)

        monkeypatch.setattr(app._status_widget, "update", spy)
        app._apply_finished("complete", 100, 12.3, None)
        await pilot.pause()
        assert app._elapsed_timer is None
        # The frozen bar IS the wide summary: full, green, no left text.
        assert app._meter_widget._compose_text().endswith("100%")
        assert app._meter_widget.has_class("-done")
        assert updates[-1] == ""
        # The data summary lands in the toggleable detail row instead.
        assert app._last_detail_text == "Search complete: 100 matches in 12.3s"


async def test_search_complete_minimizes_on_narrow_statusline(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A narrow completed search shows just the check glyph and match count."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(40, 24)) as pilot:
        await pilot.pause()
        updates: list[str] = []
        real_update = app._status_widget.update

        def spy(content: t.Any = "", *args: t.Any, **kwargs: t.Any) -> None:
            updates.append(str(content))
            real_update(content, *args, **kwargs)

        monkeypatch.setattr(app._status_widget, "update", spy)
        app._apply_finished("complete", 100, 12.3, None)
        await pilot.pause()
        # Narrow has no room for the bar — the green check says it.
        assert updates[-1] == "Done"
        assert app._status_widget.has_class("-done")


class FinishOutcomeCase(t.NamedTuple):
    """One post-search outcome-by-width scenario for the statusline."""

    test_id: str
    size: tuple[int, int]
    outcome: str
    expected_left: str
    expected_class: str
    meter_shows_bar: bool
    seed_scanning: bool


FINISH_OUTCOME_CASES: tuple[FinishOutcomeCase, ...] = (
    FinishOutcomeCase(
        test_id="complete-wide-green-full-bar",
        size=(160, 24),
        outcome="complete",
        expected_left="",
        expected_class="-done",
        meter_shows_bar=True,
        seed_scanning=True,
    ),
    FinishOutcomeCase(
        test_id="complete-narrow-says-done",
        size=(40, 24),
        outcome="complete",
        expected_left="Done",
        expected_class="-done",
        meter_shows_bar=False,
        seed_scanning=True,
    ),
    FinishOutcomeCase(
        test_id="interrupted-wide-gray-partial-bar",
        size=(160, 24),
        outcome="interrupted",
        expected_left="",
        expected_class="-stopped",
        meter_shows_bar=True,
        seed_scanning=True,
    ),
    FinishOutcomeCase(
        test_id="interrupted-narrow-says-stopped",
        size=(40, 24),
        outcome="interrupted",
        expected_left="Stopped",
        expected_class="-stopped",
        meter_shows_bar=False,
        seed_scanning=True,
    ),
    FinishOutcomeCase(
        # Interrupted before the first scanning snapshot: no fraction, so
        # no bar — the wide statusline must still say "Stopped" rather
        # than collapse to a bare gray glyph.
        test_id="interrupted-wide-no-bar-says-stopped",
        size=(160, 24),
        outcome="interrupted",
        expected_left="Stopped",
        expected_class="-stopped",
        meter_shows_bar=False,
        seed_scanning=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    FINISH_OUTCOME_CASES,
    ids=[case.test_id for case in FINISH_OUTCOME_CASES],
)
async def test_finish_outcome_freezes_colored_bar(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: FinishOutcomeCase,
) -> None:
    """The frozen bar (or its narrow word) carries the search outcome."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=case.size) as pilot:
        await pilot.pause()
        app._search_done = False
        # Seed matches so the right slot occupies its real-world cells —
        # narrow meters only lose the bar when the count is present.
        app.all_records.extend(_seed_records(agentgrep, tmp_path, 5))
        if case.seed_scanning:
            app._apply_progress(_make_progress_snapshot(agentgrep))
            await pilot.pause()
        updates: list[str] = []
        real_update = app._status_widget.update

        def spy(content: t.Any = "", *args: t.Any, **kwargs: t.Any) -> None:
            updates.append(str(content))
            real_update(content, *args, **kwargs)

        monkeypatch.setattr(app._status_widget, "update", spy)
        app._apply_finished(case.outcome, 100, 12.3, None)
        await pilot.pause()
        assert updates[-1] == case.expected_left
        for widget in (app._meter_widget, app._status_widget, app._spinner_widget):
            assert widget.has_class(case.expected_class)
        rendered = app._meter_widget._compose_text()
        assert ("▰" in rendered) is case.meter_shows_bar
        if case.meter_shows_bar and case.outcome == "complete":
            # Complete fills the bar.
            assert "▱" not in rendered
            assert rendered.endswith("100%")
        if case.meter_shows_bar and case.outcome == "interrupted":
            # Interrupted freezes at the last fill (5662/6748 → 84%).
            assert "▱" in rendered
            assert rendered.endswith("84%")


async def test_detail_row_shows_summary_after_finish(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Toggling the detail row after a finished search shows the data summary."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app._search_done = False
        app._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        app._apply_finished("interrupted", 2976, 2.1, None)
        await pilot.pause()
        await pilot.press("ctrl+backslash")
        await pilot.pause()
        assert app._detail_visible is True
        assert app._last_detail_text == "Stopped at 2976 matches across 5662/6748 sources in 2.1s"


async def test_meter_change_gates_identical_progress(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first fraction repaints exactly once; an identical repeat adds none."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        meter = app._meter_widget
        refreshes: list[None] = []
        real_refresh = meter.refresh

        def spy(*args: t.Any, **kwargs: t.Any) -> t.Any:
            refreshes.append(None)
            return real_refresh(*args, **kwargs)

        monkeypatch.setattr(meter, "refresh", spy)
        # Mount rendered the idle meter as "" — the first real fraction
        # must compose a non-empty bar and trigger exactly one repaint.
        meter.set_progress(0.5, "")
        assert len(refreshes) == 1
        meter.set_progress(0.5, "")
        assert len(refreshes) == 1


class StaleGenerationCase(t.NamedTuple):
    """One generation-gate scenario for ``_apply_streaming_event``."""

    test_id: str
    use_current_generation: bool
    expect_applied: bool


STALE_GENERATION_CASES: tuple[StaleGenerationCase, ...] = (
    StaleGenerationCase(
        test_id="current-generation-applies",
        use_current_generation=True,
        expect_applied=True,
    ),
    StaleGenerationCase(
        test_id="stale-generation-dropped",
        use_current_generation=False,
        expect_applied=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    STALE_GENERATION_CASES,
    ids=[case.test_id for case in STALE_GENERATION_CASES],
)
async def test_streaming_events_gated_by_generation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: StaleGenerationCase,
) -> None:
    """Events from a cancelled worker's generation never touch the chrome.

    A cancelled worker keeps draining its queued events after the user
    starts a new search; the un-gated form repainted the new search's
    chrome with stale "Stopped" states and old bar fills.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app._search_done = False
        stale_generation = app._chrome_generation
        # A new search bumps the generation; the old reporter's events
        # still carry the previous one.
        app._reset_search_chrome()
        await pilot.pause()
        generation = app._chrome_generation if case.use_current_generation else stale_generation
        await app._apply_streaming_event(generation, _make_progress_snapshot(agentgrep))
        await pilot.pause()
        assert (app._last_snapshot is not None) is case.expect_applied
        assert (app._elapsed_timer is not None) is case.expect_applied
        assert (app._meter_widget._fraction is not None) is case.expect_applied


async def test_streaming_records_batch_lands_in_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A records batch routed through the generation gate populates the list.

    Regression guard: the records handler is a coroutine — the gate must
    await it, not drop the un-awaited coroutine on the floor (which left
    the results list silently empty).
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app._search_done = False
        batch = agentgrep.StreamingRecordsBatch(records=tuple(records), total=3)
        await app._apply_streaming_event(app._chrome_generation, batch)
        await pilot.pause()
        assert len(app.all_records) == 3
        assert len(app._results._records) == 3


async def test_narrow_statusline_drops_bar_and_elapsed(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below the breakpoint the elapsed suffix and the ▰▱ bar are dropped."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(40, 24)) as pilot:
        await pilot.pause()
        app._search_done = False
        app._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        assert app._statusline_narrow() is True
        assert app._last_left_text == "Searching"
        assert "▰" not in app._meter_widget._compose_text()


class _FakeHighlight(t.NamedTuple):
    """Minimal ``OptionHighlighted`` stand-in for the detail handler."""

    option_index: int | None


class SplitOrientationCase(t.NamedTuple):
    """One terminal-width scenario for the responsive detail split."""

    test_id: str
    size: tuple[int, int]
    expect_stacked: bool


SPLIT_ORIENTATION_CASES: tuple[SplitOrientationCase, ...] = (
    SplitOrientationCase(test_id="wide-side-by-side", size=(120, 24), expect_stacked=False),
    SplitOrientationCase(test_id="narrow-stacked", size=(80, 24), expect_stacked=True),
)


@pytest.mark.parametrize(
    "case",
    SPLIT_ORIENTATION_CASES,
    ids=[case.test_id for case in SPLIT_ORIENTATION_CASES],
)
async def test_body_stacks_below_split_breakpoint(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: SplitOrientationCase,
) -> None:
    """The body flips to a stacked layout below 100 cols, side-by-side above."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=case.size) as pilot:
        await pilot.pause()
        assert app._stacked is case.expect_stacked
        assert app._body.has_class("-stacked") is case.expect_stacked


async def test_narrow_detail_opens_on_user_selection_not_autohighlight(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stacked detail stays collapsed until a genuine cursor move (tig-style)."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records = list(records)
        app._results.set_records(records)
        app._apply_responsive_layout()
        await pilot.pause()
        # Narrow + nothing opened → detail collapsed.
        assert app._stacked is True
        assert app._detail_column.has_class("-collapsed")
        # The programmatic row-0 highlight must NOT open it.
        app._pending_autohighlights = 1
        app.on_option_list_option_highlighted(_FakeHighlight(0))
        await pilot.pause()
        assert app._pending_autohighlights == 0
        assert app._detail_opened is False
        assert app._detail_column.has_class("-collapsed")
        # A real cursor move opens it and keeps it open.
        app.on_option_list_option_highlighted(_FakeHighlight(1))
        await pilot.pause()
        assert app._detail_opened is True
        assert not app._detail_column.has_class("-collapsed")


async def test_wide_detail_always_visible(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Side-by-side keeps the detail pane visible regardless of selection."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records = list(records)
        app._apply_responsive_layout()
        await pilot.pause()
        assert app._stacked is False
        # Visible before any selection.
        assert app._detail_opened is False
        assert not app._detail_column.has_class("-collapsed")
        # ...and still visible after a genuine selection (the "regardless
        # of selection" property the docstring promises).
        app.on_option_list_option_highlighted(_FakeHighlight(0))
        await pilot.pause()
        assert not app._detail_column.has_class("-collapsed")


async def test_new_search_recollapses_narrow_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_reset_search_chrome`` re-collapses the stacked detail pane."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records = list(records)
        app._results.set_records(records)
        app._detail_opened = True
        app._apply_responsive_layout()
        await pilot.pause()
        assert not app._detail_column.has_class("-collapsed")
        app._reset_search_chrome()
        await pilot.pause()
        assert app._detail_opened is False
        assert app._detail_column.has_class("-collapsed")


async def test_stacked_focus_routes_results_and_detail_vertically(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When stacked, ctrl+j reaches the detail below and ctrl+k returns up."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.all_records.extend(records)
        app.filtered_records = list(records)
        app._results.set_records(records)
        app._apply_responsive_layout()
        await pilot.pause()
        app._results.focus()
        await pilot.pause()
        # Down from results opens + focuses the detail below.
        app.action_focus_pane_down()
        await pilot.pause()
        assert app._detail_opened is True
        assert app.focused is not None and app.focused.id == "detail-scroll"
        # Up from the detail returns to the results.
        app.action_focus_pane_up()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


def test_format_compact_path_passes_short_paths_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Paths that already fit the width budget are returned unchanged."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(agentgrep.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    short = tmp_path / "a" / "b.txt"
    assert agentgrep.format_compact_path(short, max_width=80) == "~/a/b.txt"


def test_format_compact_path_middle_elides_long_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Long paths get a ``…/`` middle elide, preserving the hidden-dir root."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(agentgrep.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    long_path = tmp_path / ".codex" / "sessions" / "2024" / "02" / "14" / "uuid.jsonl"
    result = agentgrep.format_compact_path(long_path, max_width=30)
    assert result == "~/.codex/…/14/uuid.jsonl"
    assert len(result) <= 30


def test_format_compact_path_drops_root_when_tight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """When even the rooted elide doesn't fit, drop the root: ``…/parent/file``."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(agentgrep.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    long_path = tmp_path / ".codex" / "sessions" / "2024" / "02" / "14" / "verylongfilename.jsonl"
    result = agentgrep.format_compact_path(long_path, max_width=20)
    # Either tier-2 (root dropped) or tier-3 (filename only) — whichever fits.
    assert len(result) <= 20
    assert "verylongfilename" in result or "…" in result


def test_truncate_lines_passes_short_text_through() -> None:
    """Short text is returned unchanged."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "a\nb\nc"
    assert agentgrep.truncate_lines(text, max_lines=10) == text


def test_truncate_lines_appends_overflow_marker() -> None:
    """Long text is truncated and a ``+N more`` marker is appended."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "\n".join(f"line {i}" for i in range(50))
    result = agentgrep.truncate_lines(text, max_lines=5)
    assert result.startswith("line 0\nline 1\nline 2\nline 3\nline 4\n")
    assert "(+45 more lines)" in result


async def test_show_detail_caps_body_at_max_lines(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``show_detail`` caps the body so giant records render instantly.

    The body is now wrapped in a ``VerticalScroll`` so the cap is a generous
    sanity bound (default 1000 lines), not the visible-height. Test the cap.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    cap = agentgrep.DETAIL_BODY_MAX_LINES
    huge_body = "\n".join(f"body line {i}" for i in range(cap + 1000))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text=huge_body,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        # ``Static.content`` is the original Group we passed to update().
        # For this plain-text body, the body renderable is a ``Text``.
        group = app._detail.content
        body_text = next(
            item
            for item in group.renderables
            if hasattr(item, "plain") and "body line" in item.plain
        )
        assert "more lines" in body_text.plain
        assert body_text.plain.count("body line") == cap


def test_format_timestamp_tig_renders_iso_with_offset_in_local_tz() -> None:
    """ISO inputs with explicit offsets are localized to the system timezone."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    result = agentgrep.format_timestamp_tig("2026-05-17T11:59:12+00:00")
    # Shape: ``YYYY-MM-DD HH:MM ±HHMM`` (22 chars)
    assert len(result) == 22
    assert result[4] == "-" and result[7] == "-"
    assert result[10] == " "
    assert result[13] == ":"
    assert result[16] == " "
    assert result[17] in {"+", "-"}


def test_format_timestamp_tig_renders_zulu_input() -> None:
    """``Z`` suffix is treated as ``+00:00`` (Python's ``fromisoformat`` requires the swap)."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    result = agentgrep.format_timestamp_tig("2026-05-17T11:59:12Z")
    assert len(result) == 22


def test_format_timestamp_tig_returns_empty_string_for_missing_input() -> None:
    """``None`` / empty inputs render as the empty string so callers can pad."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.format_timestamp_tig(None) == ""
    assert agentgrep.format_timestamp_tig("") == ""


def test_format_timestamp_tig_falls_back_to_raw_on_parse_error() -> None:
    """Unparseable inputs return the original string clipped to 22 chars."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.format_timestamp_tig("not-an-iso-timestamp") == "not-an-iso-timestamp"
    # Long unparseable input is clipped.
    long_input = "this-is-not-a-timestamp-but-it-is-too-long-anyway"
    assert agentgrep.format_timestamp_tig(long_input) == long_input[:22]


def test_find_first_match_line_returns_index_of_first_match() -> None:
    """Returns the line index of the first matching line; case-insensitive by default."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "alpha\nbeta\nFOO bar\nbaz"
    assert agentgrep.find_first_match_line(text, ("foo",)) == 2
    assert agentgrep.find_first_match_line(text, ("foo",), case_sensitive=True) is None
    assert agentgrep.find_first_match_line(text, ("FOO",), case_sensitive=True) == 2
    assert agentgrep.find_first_match_line("", ("foo",)) is None
    assert agentgrep.find_first_match_line(text, ()) is None
    # Regex mode
    assert agentgrep.find_first_match_line(text, (r"b\w+",), regex=True) == 1


def test_find_first_match_line_skips_malformed_regex() -> None:
    """Malformed regex patterns are silently skipped; valid siblings still match."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "alpha\nbeta gamma\ndelta"
    # ``[`` is unbalanced; should be ignored. ``gamma`` should still match.
    assert agentgrep.find_first_match_line(text, ("[", "gamma"), regex=True) == 1


def test_highlight_matches_styles_each_occurrence() -> None:
    """``highlight_matches`` adds a styled span for every occurrence of every term."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_text = agentgrep.highlight_matches("foo foo bar", ("foo",))
    # Two spans for two occurrences.
    assert sum(1 for span in rich_text.spans if "bold yellow" in str(span.style)) == 2


def test_highlight_matches_combines_terms() -> None:
    """Multiple terms each get their own styled spans."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_text = agentgrep.highlight_matches("alpha beta alpha gamma", ("alpha", "gamma"))
    styled = [str(span.style) for span in rich_text.spans if "bold yellow" in str(span.style)]
    assert len(styled) == 3  # 2 alpha + 1 gamma


async def test_show_detail_memoizes_body_formatting(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-rendering the same record + query reuses the cached body renderable."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    json_body = '{"alpha": 1, "beta": 2, "gamma": 3}'
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "j.jsonl",
        text=json_body,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        # Replace json.loads so a real cache miss would explode loudly.
        load_calls = 0
        real_loads = json.loads

        def counting_loads(*args: t.Any, **kwargs: t.Any) -> t.Any:
            nonlocal load_calls
            load_calls += 1
            return real_loads(*args, **kwargs)

        monkeypatch.setattr(json, "loads", counting_loads)
        app.show_detail(record)
        await pilot.pause()
        assert load_calls == 0, "JSON should not be re-parsed for the same record + query"


async def test_show_detail_memoizes_first_match_line(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``find_first_match_line`` is not called twice for the same record + query."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *args, **kwargs: [],
    )
    query = agentgrep.SearchQuery(
        terms=("needle",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    app = agentgrep.build_streaming_ui_app(home, query, control=agentgrep.SearchControl())
    body = "\n".join(["padding"] * 5 + ["needle here"] + ["padding"] * 5)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "n.jsonl",
        text=body,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        match_calls = 0
        real_match = agentgrep.find_first_match_line

        def counting_match(*args: t.Any, **kwargs: t.Any) -> t.Any:
            nonlocal match_calls
            match_calls += 1
            return real_match(*args, **kwargs)

        monkeypatch.setattr(agentgrep, "find_first_match_line", counting_match)
        app.show_detail(record)
        await pilot.pause()
        assert match_calls == 0, "first_match_line should be cached for repeat views"


async def test_reset_search_chrome_invalidates_detail_caches(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starting a new search clears any stale detail-pane caches."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "x.jsonl",
        text='{"x": 1}',
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        assert len(app._detail_body_cache) >= 1
        app._reset_search_chrome()
        assert len(app._detail_body_cache) == 0
        assert len(app._first_match_cache) == 0


async def test_show_detail_scrolls_to_first_match(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the record body contains a match, the detail-scroll jumps so the match centers."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *args, **kwargs: [],
    )
    # Build the app with a query that has terms; default _build_empty_ui_app
    # uses ``terms=()``, which would make first_match always return None.
    query = agentgrep.SearchQuery(
        terms=("needle",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    control = agentgrep.SearchControl()
    app = agentgrep.build_streaming_ui_app(home, query, control=control)
    # Match lands at line 50 of the body; record_at_match.
    body = "\n".join(["padding"] * 50 + ["this needle is the match"] + ["padding"] * 50)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "match.jsonl",
        text=body,
    )
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        # Match at body line 50 + 8 header lines = ~line 58; centered into a
        # multi-row viewport, scroll_y should be > 0.
        assert app._detail_scroll.scroll_y > 0


def test_detect_content_format_recognizes_json() -> None:
    """``detect_content_format`` returns ``"json"`` for parseable JSON objects/arrays."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format('{"a": 1, "b": 2}') == "json"
    assert agentgrep.detect_content_format("[1, 2, 3]") == "json"
    # Whitespace + pretty-printed JSON.
    assert agentgrep.detect_content_format('  {\n  "x": 1\n}') == "json"


def test_detect_content_format_falls_back_to_text_for_malformed_json() -> None:
    """A leading ``{`` that doesn't parse falls through to ``"text"``, not ``"json"``."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format('{"missing": ') == "text"
    assert agentgrep.detect_content_format("{not even json}") == "text"


def test_detect_content_format_recognizes_markdown() -> None:
    """ATX headings and fenced code blocks at line-start trip markdown mode."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format("# Heading\n\nbody") == "markdown"
    assert agentgrep.detect_content_format("intro\n\n## Subhead\n\nrest") == "markdown"
    assert agentgrep.detect_content_format("intro\n\n```python\nprint(1)\n```") == "markdown"


def test_detect_content_format_leans_false_negative_for_weak_markdown() -> None:
    """Bullet-style or inline-bold chat content is intentionally NOT classified as markdown."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    # A chat message starting with "- " should keep its match highlight.
    assert agentgrep.detect_content_format("- not really markdown") == "text"
    # Inline **bold** alone isn't enough either.
    assert agentgrep.detect_content_format("plain message with **emphasis** inline") == "text"


def test_detect_content_format_handles_empty_and_plain_text() -> None:
    """Empty body and plain chat prose both return ``"text"``."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format("") == "text"
    assert agentgrep.detect_content_format("just a plain prompt") == "text"
    assert agentgrep.detect_content_format("multi\nline\nplain\nbody") == "text"


async def test_show_detail_renders_json_with_syntax(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON record body produces a ``Syntax`` renderable in the detail Group."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_syntax = importlib.import_module("rich.syntax")
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "json.jsonl",
        text='{"alpha": 1, "beta": "two"}',
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        rendered = app._detail.content
        renderables = list(rendered.renderables)
        assert any(isinstance(item, rich_syntax.Syntax) for item in renderables)


async def test_show_detail_renders_markdown_with_markdown(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A markdown body produces a ``Markdown`` renderable in the detail Group."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_markdown = importlib.import_module("rich.markdown")
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "md.jsonl",
        text="# Heading\n\nbody paragraph\n",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        rendered = app._detail.content
        renderables = list(rendered.renderables)
        assert any(isinstance(item, rich_markdown.Markdown) for item in renderables)


async def test_show_detail_keeps_text_highlighting_for_plain_body(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain-text bodies still get yellow ``highlight_regex`` spans for matches."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_text_module = importlib.import_module("rich.text")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agentgrep, "run_search_query", lambda *args, **kwargs: [])
    query = agentgrep.SearchQuery(
        terms=("libtmux",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    control = agentgrep.SearchControl()
    app = agentgrep.build_streaming_ui_app(home, query, control=control)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "plain.jsonl",
        text="plain prose mentioning libtmux exactly once",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.show_detail(record)
        await pilot.pause()
        rendered = app._detail.content
        renderables = list(rendered.renderables)
        # Two Text instances: the header and the body. The body is the one
        # carrying the highlight spans (header is bold labels only).
        text_bodies = [
            item
            for item in renderables
            if isinstance(item, rich_text_module.Text) and "libtmux" in item.plain
        ]
        assert text_bodies, "expected the body Text containing 'libtmux'"
        styled = [str(span.style) for span in text_bodies[0].spans]
        assert any("bold yellow" in style for style in styled)


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
    with pytest.raises(agentgrep.pydantic.ValidationError):
        agentgrep.SearchFinishedPayload(outcome="not-a-valid-outcome", total=0, elapsed=0.0)
    with pytest.raises(agentgrep.pydantic.ValidationError):
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

    monkeypatch.setattr(agentgrep, "iter_source_records", iter_records)

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
    monkeypatch.setattr(agentgrep, "iter_source_records", raise_interrupt)

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

    monkeypatch.setattr(agentgrep, "run_readonly_command", fake_run)
    planned = agentgrep.plan_search_sources(
        query,
        sources,
        agentgrep.BackendSelection(None, "/fake/rg", None),
    )

    assert len(calls) == 1
    assert [source.path for source in planned] == [first]


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

    monkeypatch.setattr(agentgrep, "direct_source_matches", direct_source_matches)

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

    monkeypatch.setattr(agentgrep, "grep_root_paths", grep_root_paths)

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
            key_tokens=agentgrep.CURSOR_STATE_TOKENS,
        ),
    )

    assert fetched == list(expected_rows)
    key_scans = [trace for trace in traces if trace.upper().startswith("SELECT KEY FROM")]
    assert key_scans
    assert " WHERE " in key_scans[-1].upper()
    assert " LIKE " in key_scans[-1].upper()
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
    original_open_readonly_sqlite = agentgrep.open_readonly_sqlite

    def traced_open_readonly_sqlite(path: pathlib.Path) -> sqlite3.Connection:
        traced_connection = t.cast("sqlite3.Connection", original_open_readonly_sqlite(path))
        traced_connection.set_trace_callback(traces.append)
        return traced_connection

    monkeypatch.setattr(agentgrep, "open_readonly_sqlite", traced_open_readonly_sqlite)

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
    assert not [trace for trace in traces if "VALUE" in trace.upper() and " LIKE " in trace.upper()]


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

    monkeypatch.setattr(agentgrep, "grep_file_matches", grep_misses)

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
) -> None:
    """Build a minimal Antigravity CLI conversation database."""
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


class AntigravityProtobufCase(t.NamedTuple):
    """Parametrized case for inspectable Antigravity protobuf transcript stores."""

    test_id: str
    agent: AgentName
    relative_path: pathlib.Path
    store: str
    adapter_id: str
    sqlite_steps: bool


ANTIGRAVITY_PROTOBUF_CASES: tuple[AntigravityProtobufCase, ...] = (
    AntigravityProtobufCase(
        test_id="cli-conversation-db",
        agent="antigravity-cli",
        relative_path=pathlib.Path(".gemini/antigravity-cli/conversations/conv-1.db"),
        store="antigravity-cli.conversations",
        adapter_id="antigravity_cli.conversations_sqlite_protobuf.v1",
        sqlite_steps=True,
    ),
    AntigravityProtobufCase(
        test_id="cli-implicit-pb",
        agent="antigravity-cli",
        relative_path=pathlib.Path(".gemini/antigravity-cli/implicit/implicit-1.pb"),
        store="antigravity-cli.implicit",
        adapter_id="antigravity_cli.implicit_protobuf.v1",
        sqlite_steps=False,
    ),
    AntigravityProtobufCase(
        test_id="ide-conversation-pb",
        agent="antigravity-ide",
        relative_path=pathlib.Path(".gemini/antigravity/conversations/ide-1.pb"),
        store="antigravity-ide.conversations",
        adapter_id="antigravity_ide.conversations_protobuf.v1",
        sqlite_steps=False,
    ),
    AntigravityProtobufCase(
        test_id="ide-implicit-pb",
        agent="antigravity-ide",
        relative_path=pathlib.Path(".gemini/antigravity/implicit/implicit-1.pb"),
        store="antigravity-ide.implicit",
        adapter_id="antigravity_ide.implicit_protobuf.v1",
        sqlite_steps=False,
    ),
)


@pytest.mark.parametrize(
    AntigravityProtobufCase._fields,
    ANTIGRAVITY_PROTOBUF_CASES,
    ids=[case.test_id for case in ANTIGRAVITY_PROTOBUF_CASES],
)
def test_antigravity_protobuf_sources_are_inspectable(
    test_id: str,
    agent: AgentName,
    relative_path: pathlib.Path,
    store: str,
    adapter_id: str,
    sqlite_steps: bool,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opaque Antigravity transcript stores stay opt-in but expose readable text."""
    agentgrep = load_agentgrep_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    source_path = home / relative_path
    text = f"inspectable antigravity transcript text from {test_id}"
    if sqlite_steps:
        _build_antigravity_steps_db(source_path, text=text)
    else:
        source_path.parent.mkdir(parents=True, exist_ok=True)
        _ = source_path.write_bytes(_protobuf_field(text))

    backends = t.cast("t.Any", agentgrep).BackendSelection(None, None, None)
    agents: tuple[AgentName, ...] = (agent,)
    default_sources = t.cast("t.Any", agentgrep).discover_sources(home, agents, backends)
    inventory_sources = t.cast("t.Any", agentgrep).discover_sources(
        home,
        agents,
        backends,
        include_non_default=True,
    )

    assert source_path not in {source.path for source in default_sources}
    source = next(source for source in inventory_sources if source.path == source_path)
    assert source.store == store
    assert source.adapter_id == adapter_id
    assert source.coverage.value == "inspectable"

    records = list(t.cast("t.Any", agentgrep).iter_source_records(source))

    assert len(records) == 1
    record = records[0]
    assert record.agent == agent
    assert record.store == store
    assert record.adapter_id == adapter_id
    assert record.kind == "history"
    assert record.text == text
    assert record.session_id == source_path.stem
    assert record.conversation_id == source_path.stem


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
    result = t.cast("t.Any", agentgrep)._unix_to_isoformat(value)
    if expected is None:
        assert result is None, f"{test_id}: expected None, got {result!r}"
    else:
        assert result is not None, f"{test_id}: expected timestamp, got None"
        assert result.startswith(expected), f"{test_id}: {result!r}"
