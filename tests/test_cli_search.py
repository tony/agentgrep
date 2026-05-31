"""Tests for the ``agentgrep search`` subcommand.

Covers argument parsing into :class:`agentgrep.SearchArgs`, the
ranking-specific flags (``--threshold``, ``--no-group``, ``--no-rank``),
and the integration between the ranking engine and the CLI dispatch.
"""

from __future__ import annotations

import json
import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep.cli.render import run_search_command

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class SearchParseCase(t.NamedTuple):
    """Parametrized case for :func:`agentgrep.parse_args` on ``search``."""

    test_id: str
    argv: tuple[str, ...]
    expected_terms: tuple[str, ...]
    expected_threshold: int
    expected_no_group: bool
    expected_no_rank: bool
    expected_search_type: agentgrep.SearchType
    expected_case_sensitive: bool


SEARCH_PARSE_CASES: tuple[SearchParseCase, ...] = (
    SearchParseCase(
        "defaults-single-term",
        ("search", "bliss"),
        ("bliss",),
        0,
        False,
        False,
        "prompts",
        False,
    ),
    SearchParseCase(
        "multi-term",
        ("search", "streaming", "parser"),
        ("streaming", "parser"),
        0,
        False,
        False,
        "prompts",
        False,
    ),
    SearchParseCase(
        "threshold-flag",
        ("search", "--threshold", "70", "migration"),
        ("migration",),
        70,
        False,
        False,
        "prompts",
        False,
    ),
    SearchParseCase(
        "no-group-flag",
        ("search", "--no-group", "caching"),
        ("caching",),
        0,
        True,
        False,
        "prompts",
        False,
    ),
    SearchParseCase(
        "no-rank-flag",
        ("search", "--no-rank", "bliss"),
        ("bliss",),
        0,
        False,
        True,
        "prompts",
        False,
    ),
    SearchParseCase(
        "no-group-and-no-rank",
        ("search", "--no-group", "--no-rank", "query"),
        ("query",),
        0,
        True,
        True,
        "prompts",
        False,
    ),
    SearchParseCase(
        "threshold-with-ranking",
        ("search", "--threshold", "50", "--no-group", "query"),
        ("query",),
        50,
        True,
        False,
        "prompts",
        False,
    ),
    SearchParseCase(
        "scope-conversations",
        ("search", "--scope", "conversations", "todo"),
        ("todo",),
        0,
        False,
        False,
        "conversations",
        False,
    ),
    SearchParseCase(
        "case-sensitive-flag",
        ("search", "--case-sensitive", "Bliss"),
        ("Bliss",),
        0,
        False,
        False,
        "prompts",
        True,
    ),
    SearchParseCase(
        "no-terms",
        ("search",),
        (),
        0,
        False,
        False,
        "prompts",
        False,
    ),
)


@pytest.mark.parametrize(
    SearchParseCase._fields,
    SEARCH_PARSE_CASES,
    ids=[case.test_id for case in SEARCH_PARSE_CASES],
)
def test_search_parse_args(
    test_id: str,
    argv: tuple[str, ...],
    expected_terms: tuple[str, ...],
    expected_threshold: int,
    expected_no_group: bool,
    expected_no_rank: bool,
    expected_search_type: agentgrep.SearchType,
    expected_case_sensitive: bool,
) -> None:
    """Search subparser captures ranking-specific flags correctly."""
    _ = test_id
    parsed = agentgrep.parse_args(argv)
    assert isinstance(parsed, agentgrep.SearchArgs)
    assert parsed.terms == expected_terms
    assert parsed.threshold == expected_threshold
    assert parsed.no_group == expected_no_group
    assert parsed.no_rank == expected_no_rank
    assert parsed.search_type == expected_search_type
    assert parsed.case_sensitive == expected_case_sensitive


def test_search_parse_limit() -> None:
    """--limit is captured in SearchArgs."""
    parsed = agentgrep.parse_args(("search", "--limit", "5", "bliss"))
    assert isinstance(parsed, agentgrep.SearchArgs)
    assert parsed.limit == 5


def test_search_parse_output_json() -> None:
    """--json sets output_mode correctly."""
    parsed = agentgrep.parse_args(("search", "--json", "bliss"))
    assert isinstance(parsed, agentgrep.SearchArgs)
    assert parsed.output_mode == "json"


def test_search_parse_output_ndjson() -> None:
    """--ndjson sets output_mode correctly."""
    parsed = agentgrep.parse_args(("search", "--ndjson", "bliss"))
    assert isinstance(parsed, agentgrep.SearchArgs)
    assert parsed.output_mode == "ndjson"


def test_search_parse_progress_never() -> None:
    """--no-progress sets progress_mode to never."""
    parsed = agentgrep.parse_args(("search", "--no-progress", "bliss"))
    assert isinstance(parsed, agentgrep.SearchArgs)
    assert parsed.progress_mode == "never"


def test_search_parse_agent_filter() -> None:
    """--agent filters are captured."""
    parsed = agentgrep.parse_args(("search", "--agent", "codex", "bliss"))
    assert isinstance(parsed, agentgrep.SearchArgs)
    assert parsed.agents == ("codex",)


class RemovedSearchFlagCase(t.NamedTuple):
    """Parametrized case for search flags removed pending bounded-memory support."""

    test_id: str
    flag: str
    argv: tuple[str, ...]


REMOVED_SEARCH_FLAG_CASES: tuple[RemovedSearchFlagCase, ...] = (
    RemovedSearchFlagCase("any-mode", "--any", ("search", "--any", "foo", "bar")),
    RemovedSearchFlagCase("regex-mode", "--regex", ("search", "--regex", "foo.*bar")),
    RemovedSearchFlagCase("type-mode", "--type", ("search", "--type", "history", "todo")),
)


@pytest.mark.parametrize(
    "case",
    REMOVED_SEARCH_FLAG_CASES,
    ids=[case.test_id for case in REMOVED_SEARCH_FLAG_CASES],
)
def test_search_removed_flags_are_rejected(
    case: RemovedSearchFlagCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``search --any`` / ``--regex`` no longer parse; removed pending bounded-memory support."""
    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(case.argv)
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "unrecognized arguments" in captured.err
    assert case.flag in captured.err
    assert "Traceback" not in captured.err


def test_search_scope_field_broadens_coarse_search_type() -> None:
    """A query-language ``scope:`` predicate controls search-scope filtering."""
    parsed = agentgrep.parse_args(("search", "scope:conversations", "bliss"))
    assert isinstance(parsed, agentgrep.SearchArgs)
    assert parsed.search_type == "all"
    assert parsed.terms == ("bliss",)
    assert parsed.compiled is not None


def test_search_scope_field_conversation_record_reaches_compiled_predicate() -> None:
    """``scope:conversations`` must not be pre-filtered by default prompt scope."""
    parsed = agentgrep.parse_args(("search", "scope:conversations", "bliss"))
    assert isinstance(parsed, agentgrep.SearchArgs)
    record = agentgrep.SearchRecord(
        kind="history",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text="bliss command",
    )
    query = agentgrep.SearchQuery(
        terms=parsed.terms,
        search_type=parsed.search_type,
        any_term=False,
        regex=False,
        case_sensitive=parsed.case_sensitive,
        agents=parsed.agents,
        limit=parsed.limit,
        compiled=parsed.compiled,
    )

    assert query.search_type == "all"
    assert agentgrep.matches_record(record, query)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def _make_search_args(**overrides: t.Any) -> agentgrep.SearchArgs:
    """Build a SearchArgs with sensible test defaults."""
    base: dict[str, t.Any] = {
        "terms": ("bliss",),
        "agents": agentgrep.AGENT_CHOICES,
        "search_type": "prompts",
        "case_sensitive": False,
        "limit": None,
        "output_mode": "text",
        "color_mode": "never",
        "progress_mode": "never",
        "threshold": 0,
        "no_group": False,
        "no_rank": False,
        "compiled": None,
        "raw_query": "",
    }
    base.update(overrides)
    return agentgrep.SearchArgs(**base)


def _canned_records() -> list[agentgrep.SearchRecord]:
    """Return a small set of canned records for search integration tests."""
    return [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="test",
            adapter_id="test.v1",
            path=pathlib.Path("/tmp/test-a"),
            text="the bliss of streaming parsers",
            session_id="sess-1",
        ),
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="test",
            adapter_id="test.v1",
            path=pathlib.Path("/tmp/test-b"),
            text="unrelated noise about caching",
            session_id="sess-2",
        ),
        agentgrep.SearchRecord(
            kind="prompt",
            agent="claude",
            store="test",
            adapter_id="test.v1",
            path=pathlib.Path("/tmp/test-c"),
            text="bliss in every line of code",
            session_id="sess-1",
        ),
    ]


def test_search_command_no_terms_raises() -> None:
    """Search without terms and without --ui raises SystemExit."""
    args = _make_search_args(terms=())
    with pytest.raises(SystemExit, match="search requires at least one term"):
        run_search_command(args)


def test_search_field_only_query_allowed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Field-only queries like agent:codex work without text terms."""
    parsed = agentgrep.parse_args(("search", "agent:codex"))
    assert parsed is not None
    assert isinstance(parsed, agentgrep.SearchArgs)
    assert parsed.compiled is not None
    assert parsed.terms == ()
    canned = _canned_records()
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *_args, **_kwargs: canned,
    )
    code = run_search_command(parsed)
    assert code == 0


def test_search_routes_through_ranking(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Search dispatches through the ranking pipeline and produces output."""
    canned = _canned_records()
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *_args, **_kwargs: canned,
    )
    args = _make_search_args(terms=("bliss",))
    code = run_search_command(args)
    assert code == 0
    captured = capsys.readouterr()
    assert "bliss" in captured.out


def test_search_no_rank_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--no-rank skips scoring and preserves discovery order."""
    canned = _canned_records()
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *_args, **_kwargs: canned,
    )
    args = _make_search_args(terms=("bliss",), no_rank=True)
    code = run_search_command(args)
    assert code == 0
    captured = capsys.readouterr()
    assert "bliss" in captured.out.lower()


def test_search_threshold_filters_low_scores(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--threshold filters records below the minimum score."""
    canned = _canned_records()
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *_args, **_kwargs: canned,
    )
    args = _make_search_args(terms=("bliss",), threshold=99)
    code = run_search_command(args)
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out.strip() == ""


def test_search_json_includes_scores(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--json output includes score fields."""
    canned = _canned_records()
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *_args, **_kwargs: canned,
    )
    args = _make_search_args(terms=("bliss",), output_mode="json", no_group=True)
    code = run_search_command(args)
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "results" in payload
    for result in payload["results"]:
        assert "score" in result
        assert isinstance(result["score"], (int, float))


def test_search_ndjson_includes_scores(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--ndjson output includes score in each line."""
    canned = _canned_records()
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *_args, **_kwargs: canned,
    )
    args = _make_search_args(terms=("bliss",), output_mode="ndjson", no_group=True)
    code = run_search_command(args)
    assert code == 0
    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().splitlines() if line]
    assert len(lines) >= 1
    for line in lines:
        obj = json.loads(line)
        assert "score" in obj


def test_search_empty_results_returns_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search with no matches returns exit code 1."""
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *_args, **_kwargs: [],
    )
    args = _make_search_args(terms=("nonexistent",))
    code = run_search_command(args)
    assert code == 1


def test_search_limit_caps_results(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--limit caps the number of results after ranking."""
    canned = _canned_records()
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *_args, **_kwargs: canned,
    )
    args = _make_search_args(terms=("bliss",), limit=1, no_group=True, output_mode="json")
    code = run_search_command(args)
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert len(payload["results"]) == 1
