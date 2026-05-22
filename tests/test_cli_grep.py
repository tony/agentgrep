"""Tests for the ``agentgrep grep`` subcommand.

Covers argument parsing into :class:`agentgrep.GrepArgs`, the
:func:`agentgrep.build_grep_query` translation from grep flags to a
:class:`agentgrep.SearchQuery`, the text/JSON/NDJSON output renderers,
and the rg-style exit codes.

Engine-level integration (running grep against real fixture stores) is
covered indirectly by the existing search-engine tests; the grep
dispatcher reuses :func:`agentgrep.run_search_query` via the same
monkeypatch surface those tests rely on.
"""

from __future__ import annotations

import io
import typing as t

import pytest

import agentgrep


class ParseDefaultsCase(t.NamedTuple):
    """Parametrized case for :func:`agentgrep.parse_args` on ``grep``."""

    test_id: str
    argv: tuple[str, ...]
    expected_patterns: tuple[str, ...]
    expected_case_mode: agentgrep.CaseMode
    expected_pattern_mode: agentgrep.PatternMode
    expected_no_dedupe: bool


PARSE_DEFAULTS_CASES: tuple[ParseDefaultsCase, ...] = (
    ParseDefaultsCase(
        "single-pattern-defaults", ("grep", "foo"), ("foo",), "smart", "regex", False
    ),
    ParseDefaultsCase(
        "multi-pattern-and",
        ("grep", "foo", "bar"),
        ("foo", "bar"),
        "smart",
        "regex",
        False,
    ),
    ParseDefaultsCase(
        "ignore-case-short",
        ("grep", "-i", "FOO"),
        ("FOO",),
        "ignore",
        "regex",
        False,
    ),
    ParseDefaultsCase(
        "case-sensitive-short",
        ("grep", "-s", "foo"),
        ("foo",),
        "respect",
        "regex",
        False,
    ),
    ParseDefaultsCase(
        "smart-case-explicit",
        ("grep", "-S", "foo"),
        ("foo",),
        "smart",
        "regex",
        False,
    ),
    ParseDefaultsCase(
        "fixed-strings",
        ("grep", "-F", "1.2.3"),
        ("1.2.3",),
        "smart",
        "fixed",
        False,
    ),
    ParseDefaultsCase(
        "word-regexp",
        ("grep", "-w", "foo"),
        ("foo",),
        "smart",
        "word",
        False,
    ),
    ParseDefaultsCase(
        "no-dedupe-flag",
        ("grep", "--no-dedupe", "foo"),
        ("foo",),
        "smart",
        "regex",
        True,
    ),
)


@pytest.mark.parametrize(
    "case",
    PARSE_DEFAULTS_CASES,
    ids=[c.test_id for c in PARSE_DEFAULTS_CASES],
)
def test_parse_grep_args_resolves_modes(case: ParseDefaultsCase) -> None:
    """Parse argv into GrepArgs with correct case/pattern-mode resolution."""
    parsed = agentgrep.parse_args(list(case.argv))
    assert isinstance(parsed, agentgrep.GrepArgs)
    assert parsed.patterns == case.expected_patterns
    assert parsed.case_mode == case.expected_case_mode
    assert parsed.pattern_mode == case.expected_pattern_mode
    assert parsed.no_dedupe is case.expected_no_dedupe


def test_grep_default_output_mode_is_text() -> None:
    """Bare ``grep PATTERN`` defaults to text output."""
    parsed = agentgrep.parse_args(["grep", "foo"])
    assert isinstance(parsed, agentgrep.GrepArgs)
    assert parsed.output_mode == "text"


def test_grep_json_sets_output_mode() -> None:
    """``grep --json PATTERN`` switches output to json."""
    parsed = agentgrep.parse_args(["grep", "--json", "foo"])
    assert isinstance(parsed, agentgrep.GrepArgs)
    assert parsed.output_mode == "json"


def test_grep_ndjson_sets_output_mode() -> None:
    """``grep --ndjson PATTERN`` switches output to ndjson."""
    parsed = agentgrep.parse_args(["grep", "--ndjson", "foo"])
    assert isinstance(parsed, agentgrep.GrepArgs)
    assert parsed.output_mode == "ndjson"


def test_grep_vimgrep_flag_propagates() -> None:
    """``--vimgrep`` reaches GrepArgs.vimgrep."""
    parsed = agentgrep.parse_args(["grep", "--vimgrep", "foo"])
    assert isinstance(parsed, agentgrep.GrepArgs)
    assert parsed.vimgrep is True


def test_grep_max_count_propagates() -> None:
    """``-m N`` propagates as max_count."""
    parsed = agentgrep.parse_args(["grep", "-m", "5", "foo"])
    assert isinstance(parsed, agentgrep.GrepArgs)
    assert parsed.max_count == 5


class QueryTranslationCase(t.NamedTuple):
    """Parametrized case for :func:`agentgrep.build_grep_query`."""

    test_id: str
    case_mode: agentgrep.CaseMode
    pattern_mode: agentgrep.PatternMode
    patterns: tuple[str, ...]
    no_dedupe: bool
    expected_case_sensitive: bool
    expected_regex: bool
    expected_dedupe: bool
    expected_terms: tuple[str, ...]


QUERY_TRANSLATION_CASES: tuple[QueryTranslationCase, ...] = (
    QueryTranslationCase(
        "smart-lowercase-insensitive",
        "smart",
        "regex",
        ("foo",),
        False,
        False,
        True,
        True,
        ("foo",),
    ),
    QueryTranslationCase(
        "smart-uppercase-sensitive",
        "smart",
        "regex",
        ("FOO",),
        False,
        True,
        True,
        True,
        ("FOO",),
    ),
    QueryTranslationCase(
        "ignore-forces-insensitive",
        "ignore",
        "regex",
        ("FOO",),
        False,
        False,
        True,
        True,
        ("FOO",),
    ),
    QueryTranslationCase(
        "respect-forces-sensitive",
        "respect",
        "regex",
        ("foo",),
        False,
        True,
        True,
        True,
        ("foo",),
    ),
    QueryTranslationCase(
        "fixed-disables-regex",
        "smart",
        "fixed",
        ("foo.bar",),
        False,
        False,
        False,
        True,
        ("foo.bar",),
    ),
    QueryTranslationCase(
        "word-wraps-patterns",
        "smart",
        "word",
        ("foo",),
        False,
        False,
        True,
        True,
        (r"\bfoo\b",),
    ),
    QueryTranslationCase(
        "no-dedupe-disables-dedup",
        "smart",
        "regex",
        ("foo",),
        True,
        False,
        True,
        False,
        ("foo",),
    ),
)


@pytest.mark.parametrize(
    "case",
    QUERY_TRANSLATION_CASES,
    ids=[c.test_id for c in QUERY_TRANSLATION_CASES],
)
def test_build_grep_query_translates_modes(case: QueryTranslationCase) -> None:
    """Grep flags map onto SearchQuery semantics rg-faithfully."""
    args = agentgrep.GrepArgs(
        patterns=case.patterns,
        agents=agentgrep.AGENT_CHOICES,
        search_type="prompts",
        case_mode=case.case_mode,
        pattern_mode=case.pattern_mode,
        invert_match=False,
        count_only=False,
        files_with_matches=False,
        files_without_match=False,
        only_matching=False,
        no_dedupe=case.no_dedupe,
        line_number=None,
        heading=None,
        max_count=None,
        vimgrep=False,
        output_mode="text",
        color_mode="never",
        progress_mode="never",
    )
    query = agentgrep.build_grep_query(args)
    assert query.case_sensitive is case.expected_case_sensitive
    assert query.regex is case.expected_regex
    assert query.dedupe is case.expected_dedupe
    assert query.terms == case.expected_terms


def _make_grep_args(**overrides: object) -> agentgrep.GrepArgs:
    """Build a :class:`agentgrep.GrepArgs` with sensible test defaults."""
    base: dict[str, object] = {
        "patterns": ("foo",),
        "agents": agentgrep.AGENT_CHOICES,
        "search_type": "prompts",
        "case_mode": "smart",
        "pattern_mode": "regex",
        "invert_match": False,
        "count_only": False,
        "files_with_matches": False,
        "files_without_match": False,
        "only_matching": False,
        "no_dedupe": False,
        "line_number": None,
        "heading": None,
        "max_count": None,
        "vimgrep": False,
        "output_mode": "text",
        "color_mode": "never",
        "progress_mode": "never",
    }
    base.update(overrides)
    return agentgrep.GrepArgs(**t.cast("t.Any", base))


def _fake_search_records(
    records: list[agentgrep.SearchRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub ``run_search_query`` so dispatcher tests don't touch the FS."""

    def _stub(
        home: object,
        query: object,
        *,
        progress: object = None,
        control: object = None,
    ) -> list[agentgrep.SearchRecord]:
        return list(records)

    monkeypatch.setattr(agentgrep, "run_search_query", _stub)


class ExitCodeCase(t.NamedTuple):
    """Parametrized case for grep exit code semantics."""

    test_id: str
    has_matches: bool
    expected_exit_code: int


EXIT_CODE_CASES: tuple[ExitCodeCase, ...] = (
    ExitCodeCase("matches-exit-zero", True, 0),
    ExitCodeCase("no-matches-exit-one", False, 1),
)


@pytest.mark.parametrize(
    "case",
    EXIT_CODE_CASES,
    ids=[c.test_id for c in EXIT_CODE_CASES],
)
def test_run_grep_command_returns_grep_exit_codes(
    case: ExitCodeCase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """run_grep_command returns 0 when records exist, 1 when empty (rg parity)."""
    records: list[agentgrep.SearchRecord] = []
    if case.has_matches:
        records.append(
            agentgrep.SearchRecord(
                kind="prompt",
                agent="codex",
                store="sessions",
                adapter_id="codex.sessions.jsonl",
                path=t.cast("t.Any", "/tmp/fake.jsonl"),
                text="foo bar baz",
                title=None,
                role="user",
                timestamp=None,
                model=None,
                session_id=None,
                conversation_id=None,
                metadata={},
            ),
        )
    _fake_search_records(records, monkeypatch)
    args = _make_grep_args()
    exit_code = agentgrep.run_grep_command(args)
    _ = capsys.readouterr()
    assert exit_code == case.expected_exit_code


def test_run_grep_command_json_output_emits_event_stream(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``grep --json`` emits match events plus a summary event."""
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="sessions",
            adapter_id="codex.sessions.jsonl",
            path=t.cast("t.Any", "/tmp/fake.jsonl"),
            text="foo match",
            title=None,
            role="user",
            timestamp=None,
            model=None,
            session_id="sess-1",
            conversation_id=None,
            metadata={},
        ),
    ]
    _fake_search_records(records, monkeypatch)
    args = _make_grep_args(output_mode="json")
    exit_code = agentgrep.run_grep_command(args)
    captured = capsys.readouterr()
    assert exit_code == 0
    import json as _json

    payload = _json.loads(captured.out)
    events = t.cast("list[dict[str, object]]", payload["events"])
    assert any(event["type"] == "match" for event in events)
    assert any(event["type"] == "summary" for event in events)


def test_run_grep_command_ndjson_outputs_one_record_per_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``grep --ndjson`` emits one match event per line, no summary."""
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="sessions",
            adapter_id="codex.sessions.jsonl",
            path=t.cast("t.Any", f"/tmp/fake-{i}.jsonl"),
            text=f"match {i}",
            title=None,
            role="user",
            timestamp=None,
            model=None,
            session_id=f"sess-{i}",
            conversation_id=None,
            metadata={},
        )
        for i in range(3)
    ]
    _fake_search_records(records, monkeypatch)
    args = _make_grep_args(output_mode="ndjson")
    exit_code = agentgrep.run_grep_command(args)
    captured = capsys.readouterr()
    assert exit_code == 0
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 3


def test_run_grep_command_count_only_prints_match_count(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``-c`` prints just the count and uses rg exit code."""
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="sessions",
            adapter_id="codex.sessions.jsonl",
            path=t.cast("t.Any", f"/tmp/fake-{i}.jsonl"),
            text=f"match {i}",
            title=None,
            role="user",
            timestamp=None,
            model=None,
            session_id=f"sess-{i}",
            conversation_id=None,
            metadata={},
        )
        for i in range(5)
    ]
    _fake_search_records(records, monkeypatch)
    args = _make_grep_args(count_only=True)
    exit_code = agentgrep.run_grep_command(args)
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip() == "5"


def test_run_grep_command_files_with_matches_dedupes_by_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``-l`` lists each path once even if many records share it."""
    repeated_path = t.cast("t.Any", "/tmp/repeated.jsonl")
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="sessions",
            adapter_id="codex.sessions.jsonl",
            path=repeated_path,
            text=f"match {i}",
            title=None,
            role="user",
            timestamp=None,
            model=None,
            session_id=f"sess-{i}",
            conversation_id=None,
            metadata={},
        )
        for i in range(3)
    ]
    _fake_search_records(records, monkeypatch)
    args = _make_grep_args(files_with_matches=True)
    exit_code = agentgrep.run_grep_command(args)
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.count(".jsonl") == 1


def test_run_grep_command_with_no_patterns_exits_with_systemexit() -> None:
    """Empty patterns is a programmer error — surface SystemExit."""
    args = _make_grep_args(patterns=())
    with pytest.raises(SystemExit):
        _ = agentgrep.run_grep_command(args)


def test_grep_help_renders_without_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``agentgrep grep --help`` exits cleanly via argparse SystemExit(0)."""
    monkeypatch.setattr("sys.stdout", io.StringIO())
    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(["grep", "--help"])
    assert exc_info.value.code == 0
