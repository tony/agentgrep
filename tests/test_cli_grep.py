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
import json
import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep import events as ag_events


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
    """Stub the search engine surface so dispatcher tests don't touch the FS.

    Patches both ``run_search_query`` (the eager list-return wrapper used
    by --json / -c / -l / -L / --invert-match paths) and
    ``iter_search_events`` (the streaming surface used by text and
    NDJSON paths) so every dispatcher route is FS-isolated.
    """

    def _stub_list(
        home: object,
        query: object,
        *,
        progress: object = None,
        control: object = None,
    ) -> list[agentgrep.SearchRecord]:
        return list(records)

    def _stub_iter(
        home: object,
        query: object,
        *,
        backends: object = None,
        control: object = None,
    ) -> t.Iterator[ag_events.SearchEvent]:
        yield ag_events.SearchStarted(source_count=1)
        yield ag_events.SourceStarted(
            adapter_id="codex.test",
            index=1,
            total=1,
        )
        for record in records:
            yield ag_events.RecordEmitted(record=record)
        yield ag_events.SourceFinished(
            adapter_id="codex.test",
            records_seen=len(records),
            matches_seen=len(records),
        )
        yield ag_events.SearchFinished(
            match_count=len(records),
            elapsed_seconds=0.0,
        )

    monkeypatch.setattr(agentgrep, "run_search_query", _stub_list)
    monkeypatch.setattr(agentgrep, "iter_search_events", _stub_iter)


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


class CountCase(t.NamedTuple):
    """Parametrized case for ``grep -c`` rg-faithful per-record output."""

    test_id: str
    records: list[agentgrep.SearchRecord]
    expected_stdout_lines: list[str]
    expected_exit_code: int


def _make_count_record(*, text: str, name: str = "fake.jsonl") -> agentgrep.SearchRecord:
    """Build a SearchRecord with one-line ``text`` for count tests."""
    return agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="sessions",
        adapter_id="codex.sessions.jsonl",
        path=pathlib.Path("/tmp") / name,
        text=text,
        timestamp=None,
        session_id=name.removesuffix(".jsonl"),
    )


COUNT_CASES: tuple[CountCase, ...] = (
    CountCase(
        test_id="single-record-emits-bare-count",
        records=[_make_count_record(text="foo line one\nfoo line two\nno match here")],
        expected_stdout_lines=["2"],
        expected_exit_code=0,
    ),
    CountCase(
        test_id="multi-record-emits-path-colon-count",
        records=[
            _make_count_record(text="foo once\nno match", name="a.jsonl"),
            _make_count_record(text="foo here\nfoo there\nfoo everywhere", name="b.jsonl"),
        ],
        expected_stdout_lines=["/tmp/a.jsonl:1", "/tmp/b.jsonl:3"],
        expected_exit_code=0,
    ),
    CountCase(
        test_id="zero-records-exits-one",
        records=[],
        expected_stdout_lines=[],
        expected_exit_code=1,
    ),
    CountCase(
        test_id="single-record-no-matching-lines-emits-zero",
        records=[_make_count_record(text="completely disjoint text")],
        expected_stdout_lines=["0"],
        expected_exit_code=0,
    ),
)


@pytest.mark.parametrize(
    "case",
    COUNT_CASES,
    ids=[c.test_id for c in COUNT_CASES],
)
def test_run_grep_command_count_only_rg_shape(
    case: CountCase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``-c`` emits rg-faithful path:N per record (or N alone for single record)."""
    _fake_search_records(case.records, monkeypatch)
    args = _make_grep_args(patterns=("foo",), count_only=True, color_mode="never")
    exit_code = agentgrep.run_grep_command(args)
    captured = capsys.readouterr()
    assert exit_code == case.expected_exit_code
    actual_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert actual_lines == case.expected_stdout_lines


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


# ----- line-aware match helpers --------------------------------------------


class LineExtractionCase(t.NamedTuple):
    """Parametrized case for :func:`agentgrep.iter_match_lines`."""

    test_id: str
    record_text: str
    args_overrides: dict[str, t.Any]
    expected: tuple[tuple[int, str, tuple[tuple[int, int], ...]], ...]


LINE_EXTRACTION_CASES: tuple[LineExtractionCase, ...] = (
    LineExtractionCase(
        "single-match-on-second-line",
        "no match here\nfoo lives here\nstill no match",
        {"patterns": ("foo",)},
        ((2, "foo lives here", ((0, 3),)),),
    ),
    LineExtractionCase(
        "two-matches-same-line-merged",
        "foo and foo again on one line",
        {"patterns": ("foo",)},
        ((1, "foo and foo again on one line", ((0, 3), (8, 11))),),
    ),
    LineExtractionCase(
        "multi-line-many-matches",
        "alpha foo\nbar foo baz\nno hits",
        {"patterns": ("foo",)},
        (
            (1, "alpha foo", ((6, 9),)),
            (2, "bar foo baz", ((4, 7),)),
        ),
    ),
    LineExtractionCase(
        "smart-case-uppercase-pattern-is-strict",
        "FOO once\nfoo twice",
        {"patterns": ("FOO",)},
        ((1, "FOO once", ((0, 3),)),),
    ),
    LineExtractionCase(
        "ignore-case-matches-mixed",
        "Foo once\nFOO twice",
        {"patterns": ("foo",), "case_mode": "ignore"},
        (
            (1, "Foo once", ((0, 3),)),
            (2, "FOO twice", ((0, 3),)),
        ),
    ),
    LineExtractionCase(
        "fixed-mode-treats-dot-as-literal",
        "foo.bar matches\nfoo_bar does not",
        {"patterns": ("foo.bar",), "pattern_mode": "fixed"},
        ((1, "foo.bar matches", ((0, 7),)),),
    ),
    LineExtractionCase(
        "word-mode-anchors-boundaries",
        "foo bar\nfoolish bar",
        {"patterns": ("foo",), "pattern_mode": "word"},
        ((1, "foo bar", ((0, 3),)),),
    ),
    LineExtractionCase(
        "multiple-patterns-or-at-line-level",
        "alpha foo\nbeta bar\nneither",
        {"patterns": ("foo", "bar")},
        (
            (1, "alpha foo", ((6, 9),)),
            (2, "beta bar", ((5, 8),)),
        ),
    ),
    LineExtractionCase(
        "no-match-yields-nothing",
        "completely disjoint text",
        {"patterns": ("zzz",)},
        (),
    ),
)


def _make_grep_args_for_helpers(**overrides: t.Any) -> agentgrep.GrepArgs:
    """Build a GrepArgs with helper-friendly defaults."""
    base: dict[str, t.Any] = {
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
    return agentgrep.GrepArgs(**base)


@pytest.mark.parametrize(
    "case",
    LINE_EXTRACTION_CASES,
    ids=[c.test_id for c in LINE_EXTRACTION_CASES],
)
def test_iter_match_lines_yields_matching_lines(case: LineExtractionCase) -> None:
    """iter_match_lines splits text into lines and yields only matchers."""
    from agentgrep.cli.render import iter_match_lines

    args = _make_grep_args_for_helpers(**case.args_overrides)
    actual = tuple(
        (line_no, line, tuple(spans))
        for line_no, line, spans in iter_match_lines(case.record_text, args)
    )
    assert actual == case.expected


def test_format_grep_line_wraps_matches_in_ansi() -> None:
    """format_grep_line emits line:col:text with ANSI on matched spans."""
    from agentgrep.cli.render import format_grep_line

    colors = agentgrep.AnsiColors(enabled=True)
    rendered = format_grep_line(
        12,
        "the foo and the bar",
        [(4, 7)],
        colors=colors,
    )
    # Line number wrapped in green LINE_NUMBER color.
    assert agentgrep.AnsiColors.LINE_NUMBER in rendered
    assert ":12:" not in rendered  # the bare line-number isn't unstyled
    assert "12" in rendered
    # Column is 1-indexed (start=4 → col=5).
    assert ":5:" in rendered
    # Match itself wrapped in red+bold.
    assert agentgrep.AnsiColors.MATCH in rendered
    assert "foo" in rendered


def test_format_grep_line_plain_when_colors_disabled() -> None:
    """When colors are disabled, format_grep_line emits no ANSI escapes."""
    from agentgrep.cli.render import format_grep_line

    colors = agentgrep.AnsiColors(enabled=False)
    rendered = format_grep_line(
        12,
        "the foo and the bar",
        [(4, 7)],
        colors=colors,
    )
    assert "\x1b[" not in rendered
    assert rendered == "12:5:the foo and the bar"


def test_format_grep_heading_includes_agent_path_timestamp() -> None:
    """format_grep_heading surfaces agent, path, and timestamp."""
    from agentgrep.cli.render import format_grep_heading

    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/abc.jsonl"),
        text="ignored",
        timestamp="2026-05-22T12:00:00Z",
    )
    colors = agentgrep.AnsiColors(enabled=False)
    rendered = format_grep_heading(record, colors=colors)
    assert "codex" in rendered
    assert "2026-05-22T12:00:00Z" in rendered
    assert "/tmp/abc.jsonl" in rendered


def test_format_grep_heading_skips_missing_timestamp() -> None:
    """No timestamp → no stray separator in the heading."""
    from agentgrep.cli.render import format_grep_heading

    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/abc.jsonl"),
        text="ignored",
        timestamp=None,
    )
    colors = agentgrep.AnsiColors(enabled=False)
    rendered = format_grep_heading(record, colors=colors)
    assert rendered == "codex  /tmp/abc.jsonl"


def test_ansi_colors_match_method_wraps_red_bold() -> None:
    """AnsiColors.match wraps text in the red+bold MATCH escape."""
    colors = agentgrep.AnsiColors(enabled=True)
    wrapped = colors.match("hit")
    assert wrapped.startswith(agentgrep.AnsiColors.MATCH)
    assert wrapped.endswith(agentgrep.AnsiColors.RESET)
    assert "hit" in wrapped


def test_ansi_colors_match_method_passthrough_when_disabled() -> None:
    """AnsiColors.match returns plain text when disabled."""
    colors = agentgrep.AnsiColors(enabled=False)
    assert colors.match("hit") == "hit"


# ----- streaming dispatch (slice 1 of the event-stream engine) ------------


def test_run_grep_command_text_mode_consumes_event_stream(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The text path routes through iter_search_events, not run_search_query."""
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="sessions",
            adapter_id="codex.test",
            path=pathlib.Path("/tmp/demo.jsonl"),
            text=f"bliss line {i}",
            timestamp=None,
        )
        for i in range(3)
    ]
    _fake_search_records(records, monkeypatch)
    args = _make_grep_args(patterns=("bliss",), color_mode="never")
    exit_code = agentgrep.run_grep_command(args)
    captured = capsys.readouterr().out
    assert exit_code == 0
    # All three records should appear in stdout.
    assert captured.count("bliss line") == 3


def test_run_grep_command_no_matches_streams_to_no_matches_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty stream produces the rg-style exit 1 + 'No matches found.' notice."""
    _fake_search_records([], monkeypatch)
    args = _make_grep_args(patterns=("bliss",), color_mode="never")
    exit_code = agentgrep.run_grep_command(args)
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No matches found." in captured.err


def test_run_grep_command_ndjson_streams_each_record(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--ndjson now streams events through iter_search_events too."""
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="sessions",
            adapter_id="codex.test",
            path=pathlib.Path(f"/tmp/demo-{i}.jsonl"),
            text=f"bliss {i}",
            timestamp=None,
            session_id=f"sess-{i}",
        )
        for i in range(2)
    ]
    _fake_search_records(records, monkeypatch)
    args = _make_grep_args(patterns=("bliss",), output_mode="ndjson", color_mode="never")
    exit_code = agentgrep.run_grep_command(args)
    captured = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert len([line for line in captured if line.strip()]) == 2


def test_run_grep_command_json_still_uses_eager_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--json keeps using run_search_query because the summary needs a total."""
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="sessions",
            adapter_id="codex.test",
            path=pathlib.Path("/tmp/demo.jsonl"),
            text="bliss once",
            timestamp=None,
            session_id="sess-1",
        ),
    ]

    def _eager_stub(
        home: object,
        query: object,
        *,
        progress: object = None,
        control: object = None,
    ) -> list[agentgrep.SearchRecord]:
        return records

    monkeypatch.setattr(agentgrep, "run_search_query", _eager_stub)
    args = _make_grep_args(patterns=("bliss",), output_mode="json")
    exit_code = agentgrep.run_grep_command(args)
    captured = capsys.readouterr().out
    assert exit_code == 0
    payload = json.loads(captured)
    assert any(event["type"] == "match" for event in payload["events"])
    assert any(event["type"] == "summary" for event in payload["events"])


def test_format_grep_record_default_emits_line_aware_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default text mode emits one line per match, not the full record body."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/abc.jsonl"),
        text="prelude line\nbliss appears here\nstill nothing\nbliss again",
        timestamp=None,
    )
    args = _make_grep_args(patterns=("bliss",), color_mode="never", heading=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    rendered = agentgrep.format_grep_record(record, args)
    # Two matching lines, both with path prefix in flat mode.
    rows = rendered.splitlines()
    assert len(rows) == 2
    assert all(row.startswith("/tmp/abc.jsonl:") for row in rows)
    assert ":2:1:bliss appears here" in rows[0]
    assert ":4:1:bliss again" in rows[1]
    # No verbatim body dump (the unrelated "prelude line" must not appear).
    assert "prelude line" not in rendered


def test_format_grep_record_heading_mode_groups_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With heading on, record opens with a header then `line:col:text` rows."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/abc.jsonl"),
        text="bliss line one\nno match\nbliss line three",
        timestamp="2026-05-22T12:00:00Z",
    )
    args = _make_grep_args(patterns=("bliss",), color_mode="never", heading=True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    rendered = agentgrep.format_grep_record(record, args)
    rows = rendered.splitlines()
    # Heading on its own line, then two body rows.
    assert "codex" in rows[0]
    assert "/tmp/abc.jsonl" in rows[0]
    assert rows[1] == "1:1:bliss line one"
    assert rows[2] == "3:1:bliss line three"


def test_format_grep_record_vimgrep_emits_one_row_per_match() -> None:
    """``--vimgrep`` produces one path:line:col:text row per match span."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/abc.jsonl"),
        text="bliss and bliss on one line\nlater bliss",
        timestamp=None,
    )
    args = _make_grep_args(patterns=("bliss",), color_mode="never", vimgrep=True)
    rendered = agentgrep.format_grep_record(record, args)
    rows = rendered.splitlines()
    # Three matches total: two on line 1, one on line 2.
    assert len(rows) == 3
    assert rows[0] == "/tmp/abc.jsonl:1:1:bliss and bliss on one line"
    assert rows[1] == "/tmp/abc.jsonl:1:11:bliss and bliss on one line"
    assert rows[2] == "/tmp/abc.jsonl:2:7:later bliss"


def test_grep_no_progress_aliases_progress_never() -> None:
    """``agentgrep grep --no-progress foo`` resolves progress_mode to "never"."""
    parsed = agentgrep.parse_args(["grep", "--no-progress", "foo"])
    assert isinstance(parsed, agentgrep.GrepArgs)
    assert parsed.progress_mode == "never"


def test_grep_progress_never_long_form_still_works() -> None:
    """The explicit ``--progress never`` form continues to work."""
    parsed = agentgrep.parse_args(["grep", "--progress", "never", "foo"])
    assert isinstance(parsed, agentgrep.GrepArgs)
    assert parsed.progress_mode == "never"


def test_find_no_progress_aliases_progress_never() -> None:
    """``agentgrep find --no-progress codex`` resolves progress_mode to "never"."""
    parsed = agentgrep.parse_args(["find", "--no-progress", "codex"])
    assert isinstance(parsed, agentgrep.FindArgs)
    assert parsed.progress_mode == "never"


def test_find_progress_default_is_auto() -> None:
    """Find now carries progress_mode with auto default."""
    parsed = agentgrep.parse_args(["find", "codex"])
    assert isinstance(parsed, agentgrep.FindArgs)
    assert parsed.progress_mode == "auto"


def test_search_no_progress_aliases_progress_never() -> None:
    """``agentgrep search --no-progress bliss`` resolves progress_mode to "never"."""
    parsed = agentgrep.parse_args(["search", "--no-progress", "bliss"])
    assert isinstance(parsed, agentgrep.SearchArgs)
    assert parsed.progress_mode == "never"


def test_format_grep_record_only_matching_emits_just_spans() -> None:
    """``-o`` / ``--only-matching`` emits only the matched substrings."""
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/abc.jsonl"),
        text="alpha bliss beta\nblissful",
        timestamp=None,
    )
    args = _make_grep_args(patterns=("bliss",), color_mode="never", only_matching=True)
    rendered = agentgrep.format_grep_record(record, args)
    assert rendered.splitlines() == ["bliss", "bliss"]
