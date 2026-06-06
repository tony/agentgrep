"""Tests for typed query planning helpers."""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep._engine.planning import (
    LogicalSearchPlan,
    PhysicalSearchPlan,
    QueryRequest,
    SourceTask,
    build_logical_search_plan,
    build_physical_search_plan,
)


class LogicalPlanCase(t.NamedTuple):
    """One search scope and the logical discovery shape it implies."""

    test_id: str
    scope: agentgrep.SearchScope
    expected_roles: frozenset[agentgrep.StoreRole] | None
    expects_prompt_fallback: bool


LOGICAL_PLAN_CASES: tuple[LogicalPlanCase, ...] = (
    LogicalPlanCase(
        test_id="all-keeps-default-discovery",
        scope="all",
        expected_roles=None,
        expects_prompt_fallback=False,
    ),
    LogicalPlanCase(
        test_id="conversations-discovers-conversation-roles",
        scope="conversations",
        expected_roles=agentgrep.CONVERSATION_STORE_ROLES,
        expects_prompt_fallback=False,
    ),
    LogicalPlanCase(
        test_id="prompts-discovers-prompt-history-with-fallback",
        scope="prompts",
        expected_roles=agentgrep.PROMPT_HISTORY_STORE_ROLES,
        expects_prompt_fallback=True,
    ),
)


def _query(
    *,
    scope: agentgrep.SearchScope = "prompts",
    terms: tuple[str, ...] = ("tmux",),
    regex: bool = False,
    match_surface: agentgrep.SearchMatchSurface = "haystack",
) -> agentgrep.SearchQuery:
    """Build a search query for planner tests."""
    return agentgrep.SearchQuery(
        terms=terms,
        scope=scope,
        any_term=False,
        regex=regex,
        case_sensitive=False,
        agents=("codex", "claude"),
        limit=10,
        dedupe=True,
        match_surface=match_surface,
    )


def _source(
    *,
    agent: agentgrep.AgentName,
    path: str,
    store: str = "codex.sessions",
    adapter_id: str = "codex.sessions_jsonl.v1",
    search_root: pathlib.Path | None = None,
    source_kind: agentgrep.SourceKind = "jsonl",
    mtime_ns: int = 0,
) -> agentgrep.SourceHandle:
    """Build a synthetic source handle for planning tests."""
    return agentgrep.SourceHandle(
        agent=agent,
        store=store,
        adapter_id=adapter_id,
        path=pathlib.Path(path),
        path_kind="session_file",
        source_kind=source_kind,
        search_root=search_root,
        mtime_ns=mtime_ns,
    )


@pytest.mark.parametrize(
    "case",
    LOGICAL_PLAN_CASES,
    ids=[c.test_id for c in LOGICAL_PLAN_CASES],
)
def test_build_logical_search_plan_normalizes_scope_to_store_roles(
    case: LogicalPlanCase,
) -> None:
    """Logical plans make discovery role requirements explicit."""
    plan = build_logical_search_plan(_query(scope=case.scope))

    assert isinstance(plan, LogicalSearchPlan)
    assert isinstance(plan.request, QueryRequest)
    assert plan.initial_store_roles == case.expected_roles
    assert plan.expects_prompt_fallback is case.expects_prompt_fallback
    assert plan.request.scope == case.scope


def test_build_physical_search_plan_preserves_existing_source_order_for_termless_query() -> None:
    """Termless planning keeps the existing scoped-source order."""
    sources = (
        _source(agent="codex", path="/tmp/older.jsonl", mtime_ns=1),
        _source(agent="codex", path="/tmp/newer.jsonl", mtime_ns=2),
    )

    plan = build_physical_search_plan(
        _query(terms=()),
        sources,
        agentgrep.BackendSelection(find_tool=None, grep_tool=None, json_tool=None),
    )

    assert isinstance(plan, PhysicalSearchPlan)
    assert [task.source.path.name for task in plan.tasks] == ["older.jsonl", "newer.jsonl"]
    assert {task.strategy for task in plan.tasks} == {"metadata_only"}
    assert all(isinstance(task, SourceTask) for task in plan.tasks)


def test_plan_search_sources_delegates_to_physical_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exported search path consumes the typed physical plan."""
    root = pathlib.Path("/tmp/project")
    matched = _source(
        agent="codex",
        path="/tmp/project/matched.jsonl",
        search_root=root,
        mtime_ns=2,
    )
    missed = _source(
        agent="codex",
        path="/tmp/project/missed.jsonl",
        search_root=root,
        mtime_ns=1,
    )

    def grep_root_paths(
        _root: pathlib.Path,
        _query: agentgrep.SearchQuery,
        _grep_program: str,
        *,
        control: agentgrep.SearchControl | None = None,
    ) -> set[pathlib.Path]:
        assert control is not None
        return {matched.path}

    monkeypatch.setattr(agentgrep, "grep_root_paths", grep_root_paths)
    query = _query()
    backends = agentgrep.BackendSelection(find_tool=None, grep_tool="rg", json_tool=None)

    plan = build_physical_search_plan(query, (matched, missed), backends)
    legacy_sources = agentgrep.plan_search_sources(query, [matched, missed], backends)

    assert [task.source for task in plan.tasks] == legacy_sources == [matched]
    assert [task.strategy for task in plan.tasks] == ["root_full_scan"]


class SourceStrategyCase(t.NamedTuple):
    """One query/source combination and its expected execution strategy."""

    test_id: str
    query: agentgrep.SearchQuery
    source: agentgrep.SourceHandle
    expected_strategy: str


STRATEGY_CASES: tuple[SourceStrategyCase, ...] = (
    SourceStrategyCase(
        test_id="grep-text-jsonl-uses-raw-prefilter",
        query=_query(match_surface="text"),
        source=_source(
            agent="codex",
            path="/tmp/codex-session.jsonl",
            adapter_id="codex.sessions_jsonl.v1",
        ),
        expected_strategy="jsonl_raw_text_prefilter",
    ),
    SourceStrategyCase(
        test_id="search-haystack-jsonl-keeps-full-scan",
        query=_query(match_surface="haystack"),
        source=_source(
            agent="codex",
            path="/tmp/codex-session.jsonl",
            adapter_id="codex.sessions_jsonl.v1",
        ),
        expected_strategy="direct_full_scan",
    ),
    SourceStrategyCase(
        test_id="regex-text-jsonl-keeps-full-scan",
        query=_query(regex=True, match_surface="text"),
        source=_source(
            agent="codex",
            path="/tmp/codex-session.jsonl",
            adapter_id="codex.sessions_jsonl.v1",
        ),
        expected_strategy="direct_full_scan",
    ),
    SourceStrategyCase(
        test_id="json-source-keeps-full-scan",
        query=_query(match_surface="text"),
        source=_source(
            agent="codex",
            path="/tmp/history.json",
            adapter_id="codex.history_json.v1",
            store="codex.history",
            source_kind="json",
        ),
        expected_strategy="direct_full_scan",
    ),
)


@pytest.mark.parametrize(
    "case",
    STRATEGY_CASES,
    ids=[c.test_id for c in STRATEGY_CASES],
)
def test_physical_plan_selects_source_execution_strategy(
    case: SourceStrategyCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Physical planning chooses the cheapest safe source execution strategy."""
    monkeypatch.setattr(agentgrep, "direct_source_matches", lambda *args, **kwargs: True)

    plan = build_physical_search_plan(
        case.query,
        (case.source,),
        agentgrep.BackendSelection(find_tool=None, grep_tool=None, json_tool=None),
    )

    assert [task.strategy for task in plan.tasks] == [case.expected_strategy]
