"""Tests for the fd-shaped extensions to ``agentgrep find``.

Covers argument parsing into the expanded :class:`agentgrep.FindArgs`,
the fd-style ``--pattern-mode`` resolution (regex / glob / fixed /
exact), the ``-t/--type`` source-kind filter, the ``-e/--extension``
suffix filter, and the ``-l/--list-details`` / ``-0/--print0`` text
renderers.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep import events as ag_events


class FindParseCase(t.NamedTuple):
    """Parametrized case for :func:`agentgrep.parse_args` on ``find``."""

    test_id: str
    argv: tuple[str, ...]
    expected_pattern_mode: agentgrep.FindPatternMode
    expected_case_mode: agentgrep.CaseMode
    expected_type_filter: agentgrep.FindTypeFilter
    expected_extensions: tuple[str, ...]
    expected_list_details: bool
    expected_print0: bool


FIND_PARSE_CASES: tuple[FindParseCase, ...] = (
    FindParseCase(
        "default-is-regex-smart",
        ("find", "codex"),
        "regex",
        "smart",
        "all",
        (),
        False,
        False,
    ),
    FindParseCase(
        "glob-flag",
        ("find", "-g", "*.jsonl"),
        "glob",
        "smart",
        "all",
        (),
        False,
        False,
    ),
    FindParseCase(
        "fixed-flag",
        ("find", "-F", "sessions"),
        "fixed",
        "smart",
        "all",
        (),
        False,
        False,
    ),
    FindParseCase(
        "exact-flag",
        ("find", "--exact", "codex.sessions_jsonl.v1"),
        "exact",
        "smart",
        "all",
        (),
        False,
        False,
    ),
    FindParseCase(
        "case-sensitive",
        ("find", "-s", "Foo"),
        "regex",
        "respect",
        "all",
        (),
        False,
        False,
    ),
    FindParseCase(
        "case-ignore",
        ("find", "-i", "FOO"),
        "regex",
        "ignore",
        "all",
        (),
        False,
        False,
    ),
    FindParseCase(
        "type-prompts",
        ("find", "-t", "prompts", "anything"),
        "regex",
        "smart",
        "prompts",
        (),
        False,
        False,
    ),
    FindParseCase(
        "extension-repeatable",
        ("find", "-e", "jsonl", "-e", "db", "anything"),
        "regex",
        "smart",
        "all",
        ("jsonl", "db"),
        False,
        False,
    ),
    FindParseCase(
        "list-details",
        ("find", "-l", "codex"),
        "regex",
        "smart",
        "all",
        (),
        True,
        False,
    ),
    FindParseCase(
        "print-null",
        ("find", "-0", "codex"),
        "regex",
        "smart",
        "all",
        (),
        False,
        True,
    ),
)


@pytest.mark.parametrize(
    "case",
    FIND_PARSE_CASES,
    ids=[c.test_id for c in FIND_PARSE_CASES],
)
def test_find_args_parse_resolves_fd_flags(case: FindParseCase) -> None:
    """Argparse resolves fd flag combinations into typed FindArgs fields."""
    parsed = agentgrep.parse_args(list(case.argv))
    assert isinstance(parsed, agentgrep.FindArgs)
    assert parsed.pattern_mode == case.expected_pattern_mode
    assert parsed.case_mode == case.expected_case_mode
    assert parsed.type_filter == case.expected_type_filter
    assert parsed.extensions == case.expected_extensions
    assert parsed.list_details is case.expected_list_details
    assert parsed.print0 is case.expected_print0


def _make_find_record(
    *,
    agent: agentgrep.AgentName = "codex",
    store: str = "sessions",
    adapter_id: str = "codex.sessions_jsonl.v1",
    path: str = "/tmp/codex/sessions/2025/01/abc.jsonl",
    path_kind: str = "session_file",
    source_kind: str = "jsonl",
) -> agentgrep.FindRecord:
    """Build a synthetic FindRecord for filter tests."""
    return agentgrep.FindRecord(
        kind="find",
        agent=agent,
        store=store,
        adapter_id=adapter_id,
        path=pathlib.Path(path),
        path_kind=t.cast("t.Any", path_kind),
        metadata={"source_kind": source_kind},
    )


def _make_find_args(**overrides: object) -> agentgrep.FindArgs:
    """Build a FindArgs with sensible test defaults."""
    base: dict[str, object] = {
        "pattern": None,
        "agents": agentgrep.AGENT_CHOICES,
        "limit": None,
        "output_mode": "text",
        "color_mode": "never",
        "pattern_mode": "regex",
        "type_filter": "all",
        "extensions": (),
        "case_mode": "smart",
        "list_details": False,
        "print0": False,
        "absolute_path": False,
        "full_path": False,
    }
    base.update(overrides)
    return agentgrep.FindArgs(**t.cast("t.Any", base))


class FilterCase(t.NamedTuple):
    """Parametrized case for :func:`agentgrep.filter_find_records`."""

    test_id: str
    args_overrides: dict[str, t.Any]
    record_kwargs: dict[str, t.Any]
    expected_match: bool


FILTER_CASES: tuple[FilterCase, ...] = (
    FilterCase(
        "no-pattern-passes-everything",
        {},
        {},
        True,
    ),
    FilterCase(
        "regex-matches-on-path",
        {"pattern": r"\.jsonl", "pattern_mode": "regex"},
        {},
        True,
    ),
    FilterCase(
        "regex-no-match-fails",
        {"pattern": r"\.parquet", "pattern_mode": "regex"},
        {},
        False,
    ),
    FilterCase(
        "fixed-matches-substring",
        {"pattern": "sessions", "pattern_mode": "fixed"},
        {},
        True,
    ),
    FilterCase(
        "exact-matches-adapter-id",
        {"pattern": "codex.sessions_jsonl.v1", "pattern_mode": "exact"},
        {},
        True,
    ),
    FilterCase(
        "exact-rejects-partial",
        {"pattern": "codex", "pattern_mode": "exact"},
        {},
        False,
    ),
    FilterCase(
        "glob-matches-jsonl",
        {"pattern": "*jsonl*", "pattern_mode": "glob"},
        {},
        True,
    ),
    FilterCase(
        "type-sessions-matches-session-file",
        {"type_filter": "sessions"},
        {"path_kind": "session_file"},
        True,
    ),
    FilterCase(
        "type-sessions-rejects-history-file",
        {"type_filter": "sessions"},
        {"path_kind": "history_file"},
        False,
    ),
    FilterCase(
        "type-history-matches-history-file",
        {"type_filter": "history"},
        {"path_kind": "history_file"},
        True,
    ),
    FilterCase(
        "type-history-rejects-session-file",
        {"type_filter": "history"},
        {"path_kind": "session_file"},
        False,
    ),
    FilterCase(
        "type-prompts-matches-history-file",
        {"type_filter": "prompts"},
        {"path_kind": "history_file"},
        True,
    ),
    FilterCase(
        "type-prompts-rejects-session-file",
        {"type_filter": "prompts"},
        {"path_kind": "session_file"},
        False,
    ),
    FilterCase(
        "type-all-accepts-store-file",
        {"type_filter": "all"},
        {"path_kind": "store_file"},
        True,
    ),
    FilterCase(
        "extension-jsonl-matches",
        {"extensions": ("jsonl",)},
        {"path": "/tmp/foo.jsonl"},
        True,
    ),
    FilterCase(
        "extension-db-rejects-jsonl",
        {"extensions": ("db",)},
        {"path": "/tmp/foo.jsonl"},
        False,
    ),
    FilterCase(
        "extension-with-dot-prefix",
        {"extensions": (".jsonl",)},
        {"path": "/tmp/foo.jsonl"},
        True,
    ),
)


@pytest.mark.parametrize(
    "case",
    FILTER_CASES,
    ids=[c.test_id for c in FILTER_CASES],
)
def test_filter_find_records_honors_fd_filters(case: FilterCase) -> None:
    """filter_find_records applies pattern/type/extension filters."""
    args = _make_find_args(**case.args_overrides)
    record = _make_find_record(**case.record_kwargs)
    actual = agentgrep.filter_find_records([record], args)
    assert (len(actual) == 1) is case.expected_match


def test_filter_find_records_applies_limit() -> None:
    """``--limit`` truncates the filtered result list."""
    records = [
        _make_find_record(path=f"/tmp/foo-{i}.jsonl", store=f"sessions-{i}") for i in range(5)
    ]
    args = _make_find_args(limit=2)
    filtered = agentgrep.filter_find_records(records, args)
    assert len(filtered) == 2


class GlobBasenameCase(t.NamedTuple):
    """Parametrized case for ``find -g`` matching against the path basename."""

    test_id: str
    pattern: str
    path: str
    expected_match: bool


GLOB_BASENAME_CASES: tuple[GlobBasenameCase, ...] = (
    GlobBasenameCase("star-jsonl-matches-basename", "*.jsonl", "/tmp/codex/a.jsonl", True),
    GlobBasenameCase("exact-basename-matches", "a.jsonl", "/tmp/codex/a.jsonl", True),
    GlobBasenameCase("wrong-extension-rejects", "*.txt", "/tmp/codex/a.jsonl", False),
    GlobBasenameCase("prefix-glob-matches", "b.j*", "/tmp/codex/b.jsonl", True),
    GlobBasenameCase("dir-fragment-not-matched-by-default", "*codex*", "/tmp/codex/a.jsonl", False),
)


@pytest.mark.parametrize(
    "case",
    GLOB_BASENAME_CASES,
    ids=[c.test_id for c in GLOB_BASENAME_CASES],
)
def test_find_glob_default_matches_basename(case: GlobBasenameCase) -> None:
    """``find -g`` matches the glob against the path basename (fd parity)."""
    record = _make_find_record(path=case.path)
    args = _make_find_args(pattern=case.pattern, pattern_mode="glob")
    actual = agentgrep.filter_find_records([record], args)
    assert (len(actual) == 1) is case.expected_match


class GlobFullPathCase(t.NamedTuple):
    """Parametrized case for ``find -g --full-path`` matching the absolute path."""

    test_id: str
    pattern: str
    path: str
    expected_match: bool


GLOB_FULL_PATH_CASES: tuple[GlobFullPathCase, ...] = (
    GlobFullPathCase(
        "full-path-glob-matches-sessions-dir",
        "*/sessions/*.jsonl",
        "/tmp/codex/sessions/a.jsonl",
        True,
    ),
    GlobFullPathCase(
        "full-path-glob-matches-codex-prefix",
        "*/codex/*",
        "/tmp/codex/sessions/a.jsonl",
        True,
    ),
    GlobFullPathCase(
        "wrong-directory-rejects",
        "*/claude/*",
        "/tmp/codex/sessions/a.jsonl",
        False,
    ),
)


@pytest.mark.parametrize(
    "case",
    GLOB_FULL_PATH_CASES,
    ids=[c.test_id for c in GLOB_FULL_PATH_CASES],
)
def test_find_glob_full_path_matches_absolute(case: GlobFullPathCase) -> None:
    """``--full-path`` switches glob matching to the absolute path (fd -p)."""
    record = _make_find_record(path=case.path)
    args = _make_find_args(
        pattern=case.pattern,
        pattern_mode="glob",
        full_path=True,
    )
    actual = agentgrep.filter_find_records([record], args)
    assert (len(actual) == 1) is case.expected_match


def test_find_full_path_flag_parses() -> None:
    """``--full-path`` is captured on FindArgs."""
    parsed = agentgrep.parse_args(["find", "-g", "*.jsonl", "--full-path"])
    assert isinstance(parsed, agentgrep.FindArgs)
    assert parsed.full_path is True


def test_find_full_path_default_is_false() -> None:
    """Without ``--full-path`` the flag defaults to False."""
    parsed = agentgrep.parse_args(["find", "-g", "*.jsonl"])
    assert isinstance(parsed, agentgrep.FindArgs)
    assert parsed.full_path is False


def test_print_find_results_default_emits_one_path_per_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """fd-faithful default: one path per record, no metadata header."""
    records = [
        _make_find_record(path="/tmp/a.jsonl"),
        _make_find_record(path="/tmp/b.jsonl", store="other"),
    ]
    args = _make_find_args()
    agentgrep.print_find_results(records, args)
    captured = capsys.readouterr().out
    rows = captured.splitlines()
    assert rows == ["/tmp/a.jsonl", "/tmp/b.jsonl"]
    # No agent/kind/store header line.
    assert "codex" not in captured
    assert "sessions" not in captured


def test_print_find_results_default_collapses_home_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default text output preserves privacy by collapsing home paths."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    record = _make_find_record(path=str(home / ".codex" / "history.jsonl"))
    args = _make_find_args()

    agentgrep.print_find_results([record], args)

    captured = capsys.readouterr().out
    assert captured == "~/.codex/history.jsonl\n"


def test_print_find_results_list_details_uses_tabs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--list-details`` prints one tab-separated line per record."""
    record = _make_find_record()
    args = _make_find_args(list_details=True)
    agentgrep.print_find_results([record], args)
    captured = capsys.readouterr().out
    assert "\t" in captured
    assert captured.count("\n") == 1


def test_print_find_results_print0_uses_nul_separator(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``-0`` separates records with NUL instead of newline."""
    records = [
        _make_find_record(path="/tmp/a.jsonl"),
        _make_find_record(path="/tmp/b.jsonl", store="other"),
    ]
    args = _make_find_args(print0=True)
    agentgrep.print_find_results(records, args)
    captured = capsys.readouterr().out
    assert "\0" in captured
    # No standalone newlines when -0 is on.
    assert captured.count("\n") == 0


def test_print_find_results_print0_emits_raw_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``-0`` emits real paths so shell consumers can open them."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    raw_path = home / ".codex" / "history.jsonl"
    record = _make_find_record(path=str(raw_path))
    args = _make_find_args(print0=True)

    agentgrep.print_find_results([record], args)

    captured = capsys.readouterr().out
    assert captured == f"{raw_path}\0"


def test_print_find_results_absolute_path_emits_raw_text_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--absolute-path`` opts text output into real filesystem paths."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    raw_path = home / ".codex" / "history.jsonl"
    record = _make_find_record(path=str(raw_path))
    args = _make_find_args(absolute_path=True)

    agentgrep.print_find_results([record], args)

    captured = capsys.readouterr().out
    assert captured == f"{raw_path}\n"


def test_stream_find_results_print0_emits_raw_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Streaming ``find -0`` keeps the same shell-safe raw path contract."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    raw_path = home / ".codex" / "history.jsonl"
    record = _make_find_record(path=str(raw_path))

    def _stub_iter_find_events(
        *args: object,
        **kwargs: object,
    ) -> t.Iterator[ag_events.FindEvent]:
        del args, kwargs
        yield ag_events.FindStarted(source_count=1)
        yield ag_events.FindRecordEmitted(record=record)
        yield ag_events.FindFinished(match_count=1, elapsed_seconds=0.0)

    monkeypatch.setattr(agentgrep, "iter_find_events", _stub_iter_find_events)
    args = _make_find_args(print0=True)

    exit_code = agentgrep.stream_find_results(args)

    captured = capsys.readouterr().out
    assert exit_code == 0
    assert captured == f"{raw_path}\0"


def test_find_invalid_regex_errors_at_parse_time() -> None:
    """An invalid regex pattern exits with a clean argparse error, not silent empty results."""
    with pytest.raises(SystemExit) as exc_info:
        agentgrep.parse_args(["find", "[invalid"])
    assert exc_info.value.code == 2
