"""Tests for the ``agentgrep insights`` concept commands."""

from __future__ import annotations

import json
import pathlib
import typing as t

import pytest

import agentgrep


class InsightsParseCase(t.NamedTuple):
    """Parametrized parse case for ``agentgrep insights report``."""

    test_id: str
    argv: tuple[str, ...]
    expected_scope: agentgrep.SearchScope
    expected_level: str
    expected_limit: int | None
    expected_all_records: bool


INSIGHTS_PARSE_CASES: tuple[InsightsParseCase, ...] = (
    InsightsParseCase(
        test_id="report-defaults-bounded-builtin",
        argv=("insights", "report"),
        expected_scope="prompts",
        expected_level="builtin",
        expected_limit=500,
        expected_all_records=False,
    ),
    InsightsParseCase(
        test_id="report-all-removes-bound",
        argv=("insights", "report", "--all"),
        expected_scope="prompts",
        expected_level="builtin",
        expected_limit=None,
        expected_all_records=True,
    ),
    InsightsParseCase(
        test_id="report-best-installed-level",
        argv=("insights", "report", "--scope", "all", "--level", "best-installed"),
        expected_scope="all",
        expected_level="best-installed",
        expected_limit=500,
        expected_all_records=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    INSIGHTS_PARSE_CASES,
    ids=[case.test_id for case in INSIGHTS_PARSE_CASES],
)
def test_insights_report_parse_args(case: InsightsParseCase) -> None:
    """The report parser captures bounded pure-Python defaults."""
    parsed = agentgrep.parse_args(case.argv)
    assert isinstance(parsed, agentgrep.InsightsReportArgs)
    assert parsed.scope == case.expected_scope
    assert parsed.level == case.expected_level
    assert parsed.limit == case.expected_limit
    assert parsed.all_records == case.expected_all_records


def test_insights_report_rejects_limit_with_all(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--all`` and ``--limit`` are mutually exclusive report bounds."""
    with pytest.raises(SystemExit) as exc_info:
        _ = agentgrep.parse_args(("insights", "report", "--all", "--limit", "20"))
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "--all cannot be combined with --limit" in captured.err
    assert "Traceback" not in captured.err


def _search_record(
    text: str,
    *,
    agent: agentgrep.AgentName = "codex",
    store: str = "codex.history",
    timestamp: str | None = None,
) -> agentgrep.SearchRecord:
    """Build one synthetic search record for report tests."""
    return agentgrep.SearchRecord(
        kind="prompt",
        agent=agent,
        store=store,
        adapter_id="codex.history_jsonl.v1",
        path=pathlib.Path("/tmp/history.jsonl"),
        text=text,
        timestamp=timestamp,
    )


def test_insights_report_json_uses_bounded_builtin_query(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON reports summarize bounded records without optional dependencies."""
    seen_queries: list[agentgrep.SearchQuery] = []

    def fake_run_search_query(
        home: pathlib.Path,
        query: agentgrep.SearchQuery,
        *,
        progress: object | None = None,
        control: object | None = None,
    ) -> list[agentgrep.SearchRecord]:
        _ = (home, progress, control)
        seen_queries.append(query)
        return [
            _search_record(
                "Deploy docs and docs release notes",
                timestamp="2026-06-01T12:00:00Z",
            ),
            _search_record(
                "Deploy docs again",
                store="claude.projects",
                agent="claude",
                timestamp="2026-06-02T12:00:00Z",
            ),
        ]

    monkeypatch.setattr(agentgrep, "run_search_query", fake_run_search_query)

    exit_code = agentgrep.main(("insights", "report", "--json", "--limit", "2"))

    assert exit_code == 0
    assert len(seen_queries) == 1
    query = seen_queries[0]
    assert query.terms == ()
    assert query.scope == "prompts"
    assert query.limit == 2
    assert query.dedupe is False

    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "insights report"
    result = payload["results"][0]
    assert result["level"] == "builtin"
    assert result["records_analyzed"] == 2
    assert result["sampled"] is True
    assert result["record_limit"] == 2
    assert result["agents"] == {"claude": 1, "codex": 1}
    assert result["stores"] == {"claude.projects": 1, "codex.history": 1}
    assert result["top_terms"][0]["term"] == "docs"


def test_insights_report_text_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default text output is human-readable and mentions builtin mode."""

    def fake_run_search_query(
        home: pathlib.Path,
        query: agentgrep.SearchQuery,
        *,
        progress: object | None = None,
        control: object | None = None,
    ) -> list[agentgrep.SearchRecord]:
        _ = (home, query, progress, control)
        return [_search_record("Local report without models")]

    monkeypatch.setattr(agentgrep, "run_search_query", fake_run_search_query)

    exit_code = agentgrep.main(("insights", "report", "--no-progress"))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Insights report" in output
    assert "level: builtin" in output
    assert "records analyzed: 1" in output
    assert "optional enrichers skipped" in output
