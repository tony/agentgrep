"""Tests for compiled record matching helpers."""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep._engine.matching import compile_record_matcher
from agentgrep.query.compile import CompiledQuery


def _query(
    *,
    terms: tuple[str, ...] = ("bliss",),
    scope: agentgrep.SearchScope = "prompts",
    any_term: bool = False,
    regex: bool = False,
    case_sensitive: bool = False,
    compiled: CompiledQuery | None = None,
    match_surface: agentgrep.SearchMatchSurface = "haystack",
) -> agentgrep.SearchQuery:
    """Build a search query for matcher tests."""
    return agentgrep.SearchQuery(
        terms=terms,
        scope=scope,
        any_term=any_term,
        regex=regex,
        case_sensitive=case_sensitive,
        agents=("codex",),
        limit=10,
        dedupe=True,
        compiled=compiled,
        match_surface=match_surface,
    )


def _record(
    *,
    kind: t.Literal["prompt", "history"] = "prompt",
    text: str = "bliss in text",
    title: str | None = "session title",
    role: str | None = "user",
    model: str | None = "gpt",
) -> agentgrep.SearchRecord:
    """Build a normalized record for matcher tests."""
    return agentgrep.SearchRecord(
        kind=kind,
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("/tmp/session.jsonl"),
        text=text,
        title=title,
        role=role,
        model=model,
    )


class MatcherCase(t.NamedTuple):
    """One compiled matcher behavior case."""

    test_id: str
    query: agentgrep.SearchQuery
    record: agentgrep.SearchRecord
    expected: bool


def _role_user_query() -> agentgrep.SearchQuery:
    """Build a query with a record-level predicate."""
    return _query(
        compiled=CompiledQuery(
            source_predicate=None,
            record_predicate=lambda record: record.role == "user",
            text_terms=("bliss",),
            is_pure_text=False,
        ),
    )


MATCHER_CASES: tuple[MatcherCase, ...] = (
    MatcherCase(
        test_id="termless-prompt-in-scope",
        query=_query(terms=()),
        record=_record(text="anything"),
        expected=True,
    ),
    MatcherCase(
        test_id="scope-rejects-history-for-prompts",
        query=_query(),
        record=_record(kind="history"),
        expected=False,
    ),
    MatcherCase(
        test_id="haystack-matches-title",
        query=_query(terms=("session",), match_surface="haystack"),
        record=_record(text="body misses", title="session title"),
        expected=True,
    ),
    MatcherCase(
        test_id="text-surface-ignores-title",
        query=_query(terms=("session",), match_surface="text"),
        record=_record(text="body misses", title="session title"),
        expected=False,
    ),
    MatcherCase(
        test_id="all-terms-can-span-haystack-fields",
        query=_query(terms=("gpt", "bliss"), match_surface="haystack"),
        record=_record(text="bliss body", model="gpt-5"),
        expected=True,
    ),
    MatcherCase(
        test_id="any-term-accepts-one-match",
        query=_query(terms=("missing", "bliss"), any_term=True),
        record=_record(text="bliss body"),
        expected=True,
    ),
    MatcherCase(
        test_id="case-sensitive-miss",
        query=_query(terms=("BLISS",), case_sensitive=True),
        record=_record(text="bliss body"),
        expected=False,
    ),
    MatcherCase(
        test_id="regex-match",
        query=_query(terms=(r"bli.+",), regex=True, match_surface="text"),
        record=_record(text="bliss body"),
        expected=True,
    ),
    MatcherCase(
        test_id="compiled-record-predicate-accepts",
        query=_role_user_query(),
        record=_record(text="bliss body", role="user"),
        expected=True,
    ),
    MatcherCase(
        test_id="compiled-record-predicate-rejects",
        query=_role_user_query(),
        record=_record(text="bliss body", role="assistant"),
        expected=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    MATCHER_CASES,
    ids=[case.test_id for case in MATCHER_CASES],
)
def test_compiled_record_matcher_preserves_record_match_semantics(
    case: MatcherCase,
) -> None:
    """Compiled matchers preserve existing record matching semantics."""
    matcher = compile_record_matcher(case.query)

    assert matcher.matches(case.record) is case.expected


def test_compiled_record_matcher_avoids_legacy_text_match_for_literal_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Literal compiled matching avoids rebuilding generic text-match state."""
    matcher = compile_record_matcher(_query(terms=("bliss",), match_surface="text"))

    def fail_matches_text(_text: str, _query: agentgrep.SearchQuery) -> bool:
        pytest.fail("literal compiled matcher should not call matches_text")

    monkeypatch.setattr(agentgrep, "matches_text", fail_matches_text)

    assert matcher.matches(_record(text="bliss body")) is True
    assert matcher.matches(_record(text="missing body")) is False
