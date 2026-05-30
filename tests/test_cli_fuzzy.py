"""Tests for the ``agentgrep fuzzy`` subcommand.

Covers argument parsing into :class:`agentgrep.FuzzyArgs`, the
:func:`agentgrep.fuzzy_filter_lines` filter pipeline, field selection
via ``-d/--delimiter`` / ``-n/--nth`` / ``--with-nth``, the strict
no-arg behavior (usage + exit 2), and the end-to-end stdin → stdout
filter flow.
"""

from __future__ import annotations

import io
import json
import typing as t

import pytest

import agentgrep


def _make_fuzzy_args(**overrides: t.Any) -> agentgrep.FuzzyArgs:
    """Build a FuzzyArgs with sensible test defaults."""
    base: dict[str, t.Any] = {
        "query": "foo",
        "agents": agentgrep.AGENT_CHOICES,
        "case_mode": "smart",
        "algo": "v2",
        "tiebreak": "length",
        "exact": False,
        "extended": True,
        "sort": True,
        "delimiter": None,
        "nth": None,
        "with_nth": None,
        "print_query": False,
        "read0": False,
        "print0": False,
        "output_mode": "text",
        "color_mode": "never",
    }
    base.update(overrides)
    return agentgrep.FuzzyArgs(**base)


class FuzzyParseCase(t.NamedTuple):
    """Parametrized case for :func:`agentgrep.parse_args` on ``fuzzy``."""

    test_id: str
    argv: tuple[str, ...]
    expected_query: str
    expected_case_mode: agentgrep.CaseMode
    expected_algo: agentgrep.FuzzyAlgo
    expected_exact: bool
    expected_extended: bool
    expected_sort: bool


FUZZY_PARSE_CASES: tuple[FuzzyParseCase, ...] = (
    FuzzyParseCase("positional-query", ("fuzzy", "foo"), "foo", "smart", "v2", False, True, True),
    FuzzyParseCase(
        "explicit-filter",
        ("fuzzy", "-f", "bar"),
        "bar",
        "smart",
        "v2",
        False,
        True,
        True,
    ),
    FuzzyParseCase(
        "explicit-filter-overrides-positional",
        ("fuzzy", "-f", "bar", "baz"),
        "bar",
        "smart",
        "v2",
        False,
        True,
        True,
    ),
    FuzzyParseCase(
        "ignore-case-short",
        ("fuzzy", "-i", "FOO"),
        "FOO",
        "ignore",
        "v2",
        False,
        True,
        True,
    ),
    FuzzyParseCase(
        "respect-case-via-long",
        ("fuzzy", "--no-ignore-case", "foo"),
        "foo",
        "respect",
        "v2",
        False,
        True,
        True,
    ),
    FuzzyParseCase(
        "exact-short",
        ("fuzzy", "-e", "foo"),
        "foo",
        "smart",
        "v2",
        True,
        True,
        True,
    ),
    FuzzyParseCase(
        "no-extended",
        ("fuzzy", "--no-extended", "foo"),
        "foo",
        "smart",
        "v2",
        False,
        False,
        True,
    ),
    FuzzyParseCase(
        "algo-v1",
        ("fuzzy", "--algo=v1", "foo"),
        "foo",
        "smart",
        "v1",
        False,
        True,
        True,
    ),
    FuzzyParseCase(
        "no-sort-long-only",
        ("fuzzy", "--no-sort", "foo"),
        "foo",
        "smart",
        "v2",
        False,
        True,
        False,
    ),
)


@pytest.mark.parametrize(
    "case",
    FUZZY_PARSE_CASES,
    ids=[c.test_id for c in FUZZY_PARSE_CASES],
)
def test_parse_fuzzy_args_resolves_flags(case: FuzzyParseCase) -> None:
    """Argparse populates FuzzyArgs with fzf-shaped semantics."""
    parsed = agentgrep.parse_args(list(case.argv))
    assert isinstance(parsed, agentgrep.FuzzyArgs)
    assert parsed.query == case.expected_query
    assert parsed.case_mode == case.expected_case_mode
    assert parsed.algo == case.expected_algo
    assert parsed.exact is case.expected_exact
    assert parsed.extended is case.expected_extended
    assert parsed.sort is case.expected_sort


class _FakeStdin(io.StringIO):
    """StringIO subclass with an overrideable isatty for fuzzy stdin tests."""

    def __init__(self, data: str = "", *, tty: bool) -> None:
        super().__init__(data)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_fuzzy_no_args_with_tty_stdin_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """No query and no piped stdin → usage + exit 2 (strict, no TUI fallback)."""
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=True))
    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(["fuzzy"])
    assert exc_info.value.code == 2


def test_fuzzy_no_args_with_piped_stdin_uses_empty_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No query but piped stdin → parse with empty query, defer matching to stdin."""
    monkeypatch.setattr("sys.stdin", _FakeStdin("foo\nbar\n", tty=False))
    parsed = agentgrep.parse_args(["fuzzy"])
    assert isinstance(parsed, agentgrep.FuzzyArgs)
    assert parsed.query == ""


class FilterCase(t.NamedTuple):
    """Parametrized case for :func:`agentgrep.fuzzy_filter_lines`."""

    test_id: str
    query: str
    lines: tuple[str, ...]
    overrides: dict[str, t.Any]
    expected_lines: tuple[str, ...]


FILTER_CASES: tuple[FilterCase, ...] = (
    FilterCase(
        "exact-substring-match",
        "two",
        ("one", "two", "three", "twos"),
        {"exact": True},
        ("two", "twos"),
    ),
    FilterCase(
        "exact-rejects-non-substring",
        "qq",
        ("one", "two", "three"),
        {"exact": True},
        (),
    ),
    FilterCase(
        "fuzzy-default-sorts-by-score",
        "tw",
        ("xyz", "two", "twos", "noop"),
        {},
        ("two", "twos"),
    ),
    FilterCase(
        "extended-negation-filters",
        "foo !bar",
        ("foobaz", "foobar", "foobaz_extra"),
        {"extended": True, "exact": True},
        ("foobaz", "foobaz_extra"),
    ),
    FilterCase(
        "no-sort-preserves-order-exact",
        "two",
        ("twos", "two", "twosx"),
        {"exact": True, "sort": False},
        ("twos", "two", "twosx"),
    ),
)


@pytest.mark.parametrize(
    "case",
    FILTER_CASES,
    ids=[c.test_id for c in FILTER_CASES],
)
def test_fuzzy_filter_lines_honors_flags(case: FilterCase) -> None:
    """fuzzy_filter_lines applies exact / extended / sort semantics."""
    args = _make_fuzzy_args(query=case.query, **case.overrides)
    ranked = agentgrep.fuzzy_filter_lines(list(case.lines), args)
    actual_lines = tuple(line for line, _ in ranked)
    assert actual_lines == case.expected_lines


def test_fuzzy_filter_lines_with_delimiter_and_nth() -> None:
    """``--delimiter`` + ``--nth`` scopes scoring to the chosen field."""
    args = _make_fuzzy_args(query="foo", delimiter=":", nth=2, exact=True)
    lines = ["1:bar", "2:foo", "3:foobar"]
    ranked = agentgrep.fuzzy_filter_lines(lines, args)
    actual = [line for line, _ in ranked]
    # The score target is the 2nd field — "foo" matches itself and "foobar".
    assert "2:foo" in actual
    assert "3:foobar" in actual
    assert "1:bar" not in actual


def test_run_fuzzy_command_reads_stdin_and_writes_matches(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``agentgrep fuzzy QUERY`` consumes stdin and emits matching lines."""
    monkeypatch.setattr("sys.stdin", io.StringIO("one\ntwo\nthree\n"))
    args = _make_fuzzy_args(query="two", exact=True)
    exit_code = agentgrep.run_fuzzy_command(args)
    captured = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert captured == ["two"]


def test_run_fuzzy_command_no_matches_returns_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No matches → exit 1 (fzf parity, also rg's convention)."""
    monkeypatch.setattr("sys.stdin", io.StringIO("alpha\nbeta\ngamma\n"))
    args = _make_fuzzy_args(query="zzz", exact=True)
    exit_code = agentgrep.run_fuzzy_command(args)
    _ = capsys.readouterr()
    assert exit_code == 1


def test_run_fuzzy_command_print_query_prepends_query_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--print-query`` adds the query as the first emitted line."""
    monkeypatch.setattr("sys.stdin", io.StringIO("one\ntwo\n"))
    args = _make_fuzzy_args(query="two", exact=True, print_query=True)
    exit_code = agentgrep.run_fuzzy_command(args)
    captured = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert captured[0] == "two"  # the query echoed first
    assert "two" in captured


def test_run_fuzzy_command_print0_uses_nul_separator(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--print0`` separates output with NUL instead of newline."""
    monkeypatch.setattr("sys.stdin", io.StringIO("one\ntwo\nthree\n"))
    args = _make_fuzzy_args(query="o", exact=True, print0=True)
    _ = agentgrep.run_fuzzy_command(args)
    captured = capsys.readouterr().out
    assert "\0" in captured
    assert captured.count("\n") == 0


def test_run_fuzzy_command_read0_splits_stdin_by_nul(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--read0`` treats input as NUL-delimited."""
    monkeypatch.setattr("sys.stdin", io.StringIO("one\0two\0three\0"))
    args = _make_fuzzy_args(query="two", exact=True, read0=True)
    exit_code = agentgrep.run_fuzzy_command(args)
    captured = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert captured == ["two"]


class FuzzyOutputModeCase(t.NamedTuple):
    """Parametrized case for fuzzy structured (``--json``/``--ndjson``) output."""

    test_id: str
    output_mode: str


FUZZY_OUTPUT_MODE_CASES: tuple[FuzzyOutputModeCase, ...] = (
    FuzzyOutputModeCase("json", "json"),
    FuzzyOutputModeCase("ndjson", "ndjson"),
)


@pytest.mark.parametrize(
    FuzzyOutputModeCase._fields,
    FUZZY_OUTPUT_MODE_CASES,
    ids=[case.test_id for case in FUZZY_OUTPUT_MODE_CASES],
)
def test_run_fuzzy_command_emits_structured_output(
    test_id: str,
    output_mode: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json``/``--ndjson`` emit scored matches, not plain text lines."""
    _ = test_id
    monkeypatch.setattr("sys.stdin", io.StringIO("design notes\nconfig design\nother\n"))
    args = _make_fuzzy_args(query="design", output_mode=output_mode)
    exit_code = agentgrep.run_fuzzy_command(args)
    out = capsys.readouterr().out
    assert exit_code == 0
    if output_mode == "json":
        payload = json.loads(out)
        assert payload["command"] == "fuzzy"
        assert payload["query"]["query"] == "design"
        matches = payload["results"]
    else:
        matches = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert {match["text"] for match in matches} == {"design notes", "config design"}
    assert all("score" in match for match in matches)


def test_run_fuzzy_command_ndjson_no_match_returns_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Structured fuzzy output exits 1 and emits nothing when nothing matches."""
    monkeypatch.setattr("sys.stdin", io.StringIO("alpha\nbeta\n"))
    args = _make_fuzzy_args(query="zzqq", output_mode="ndjson")
    exit_code = agentgrep.run_fuzzy_command(args)
    assert exit_code == 1
    assert capsys.readouterr().out == ""
