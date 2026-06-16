"""Tests for typed query planning helpers."""

from __future__ import annotations

import pathlib
import typing as t

import pytest

import agentgrep
from agentgrep._engine.planning import (
    APPEND_ONLY_JSONL_ADAPTERS,
    RAW_TEXT_PREFILTER_ADAPTERS,
    STATEFUL_HEADER_JSONL_ADAPTERS,
    LogicalSearchPlan,
    PhysicalSearchPlan,
    QueryRequest,
    SourceTask,
    build_logical_search_plan,
    build_physical_search_plan,
)
from agentgrep.query.compile import CompiledQuery


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
    limit: int | None = 10,
    compiled: CompiledQuery | None = None,
    agents: tuple[agentgrep.AgentName, ...] = ("codex", "claude"),
) -> agentgrep.SearchQuery:
    """Build a search query for planner tests."""
    return agentgrep.SearchQuery(
        terms=terms,
        scope=scope,
        any_term=False,
        regex=regex,
        case_sensitive=False,
        agents=agents,
        limit=limit,
        dedupe=True,
        compiled=compiled,
        match_surface=match_surface,
    )


def _compiled_query() -> CompiledQuery:
    """Build a record-filtered query marker for planner tests."""
    return CompiledQuery(
        source_predicate=None,
        record_predicate=lambda record: record.kind == "prompt",
        text_terms=("tmux",),
        is_pure_text=False,
    )


def _source(
    *,
    agent: agentgrep.AgentName,
    path: str,
    store: str = "codex.sessions",
    adapter_id: str = "codex.sessions_jsonl.v1",
    path_kind: agentgrep.PathKind = "session_file",
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
        path_kind=path_kind,
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


def test_stateful_header_adapters_stay_off_unguarded_optimizations() -> None:
    """Header-stateful adapters never join order-sensitive optimization sets.

    The raw-prefilter overlap is allowed only for adapters whose parsers
    carry header exemptions; Gemini has none and must stay out entirely.
    """
    assert APPEND_ONLY_JSONL_ADAPTERS.isdisjoint(STATEFUL_HEADER_JSONL_ADAPTERS)
    assert {
        "codex.sessions_jsonl.v1",
        "pi.sessions_jsonl.v1",
    } == RAW_TEXT_PREFILTER_ADAPTERS & STATEFUL_HEADER_JSONL_ADAPTERS


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


def test_compiled_record_predicate_skips_root_grep_prefilter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compiled boolean/field query bypasses the flat-term root prefilter.

    The flat-term root grep ANDs the query terms, so an OR/NOT query whose
    matching file lacks one term would be dropped before the record matcher
    runs. With a compiled record predicate the prefilter must be skipped and
    the source kept; the record matcher is the source of truth.
    """

    def grep_root_paths(*_args: object, **_kwargs: object) -> set[pathlib.Path]:
        message = "root prefilter must not run for compiled record queries"
        raise AssertionError(message)

    monkeypatch.setattr(agentgrep, "grep_root_paths", grep_root_paths)
    root = pathlib.Path("/tmp/project")
    source = _source(
        agent="codex",
        path="/tmp/project/a.jsonl",
        search_root=root,
        mtime_ns=2,
    )
    query = _query(compiled=_compiled_query())
    backends = agentgrep.BackendSelection(find_tool=None, grep_tool="rg", json_tool=None)

    plan = build_physical_search_plan(query, (source,), backends)

    assert [task.source for task in plan.tasks] == [source]
    assert any(
        decision.name == "root_prefilter_skipped" and decision.detail == "compiled_record_predicate"
        for decision in plan.decisions
    )


def test_bounded_text_append_only_jsonl_root_source_uses_lazy_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded text-surface JSONL searches avoid eager whole-root text scans."""
    root = pathlib.Path("/tmp/claude-projects")
    source = _source(
        agent="claude",
        path="/tmp/claude-projects/project.jsonl",
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
        search_root=root,
        mtime_ns=2,
    )

    def fail_prefilter_sources_by_root(
        _query: agentgrep.SearchQuery,
        _sources: list[agentgrep.SourceHandle],
        _grep_program: str,
        *,
        progress: agentgrep.SearchProgress | None = None,
        control: agentgrep.SearchControl | None = None,
    ) -> list[agentgrep.SourceHandle]:
        _ = progress, control
        pytest.fail("bounded append-only JSONL sources should be admitted lazily")

    monkeypatch.setattr(agentgrep, "prefilter_sources_by_root", fail_prefilter_sources_by_root)

    plan = build_physical_search_plan(
        _query(scope="conversations", match_surface="text", limit=1),
        (source,),
        agentgrep.BackendSelection(find_tool=None, grep_tool="rg", json_tool=None),
    )

    assert [task.source for task in plan.tasks] == [source]
    assert [task.strategy for task in plan.tasks] == [
        "jsonl_bounded_reverse_raw_text_prefilter",
    ]
    assert [decision.name for decision in plan.decisions] == [
        "scope_prune",
        "root_prefilter_skipped",
        "candidate_order",
    ]
    assert plan.decisions[1].source_count == 1
    assert plan.decisions[1].detail == "bounded_append_only_jsonl"


def test_sqlite_root_source_skips_binary_grep_prefilter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite sources are admitted so adapters can apply SQL-side predicates."""
    root = pathlib.Path("/tmp/cursor-workspaces")
    source = _source(
        agent="cursor-ide",
        path="/tmp/cursor-workspaces/project/state.vscdb",
        store="cursor-ide.workspace_state",
        adapter_id="cursor_ide.state_vscdb_modern.v1",
        path_kind="sqlite_db",
        search_root=root,
        source_kind="sqlite",
        mtime_ns=2,
    )

    def fail_prefilter_sources_by_root(
        _query: agentgrep.SearchQuery,
        _sources: list[agentgrep.SourceHandle],
        _grep_program: str,
        *,
        progress: agentgrep.SearchProgress | None = None,
        control: agentgrep.SearchControl | None = None,
    ) -> list[agentgrep.SourceHandle]:
        _ = progress, control
        pytest.fail("SQLite sources should not use binary root grep prefiltering")

    monkeypatch.setattr(agentgrep, "prefilter_sources_by_root", fail_prefilter_sources_by_root)

    plan = build_physical_search_plan(
        _query(scope="all", limit=5),
        (source,),
        agentgrep.BackendSelection(find_tool=None, grep_tool="rg", json_tool=None),
    )

    assert [task.source for task in plan.tasks] == [source]
    assert [task.strategy for task in plan.tasks] == ["root_full_scan"]
    assert [decision.name for decision in plan.decisions] == [
        "scope_prune",
        "root_prefilter_skipped",
        "candidate_order",
    ]
    assert plan.decisions[1].source_count == 1
    assert plan.decisions[1].detail == "sqlite_source"


def test_bounded_haystack_root_source_without_path_match_keeps_eager_prefilter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broad haystack JSONL searches keep root prefiltering to avoid over-admission."""
    root = pathlib.Path("/tmp/claude-projects")
    source = _source(
        agent="claude",
        path="/tmp/claude-projects/project.jsonl",
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
        search_root=root,
        mtime_ns=2,
    )
    prefetched_sources: list[agentgrep.SourceHandle] = []

    def prefilter_sources_by_root(
        _query: agentgrep.SearchQuery,
        sources: list[agentgrep.SourceHandle],
        _grep_program: str,
        *,
        progress: agentgrep.SearchProgress | None = None,
        control: agentgrep.SearchControl | None = None,
    ) -> list[agentgrep.SourceHandle]:
        _ = progress, control
        prefetched_sources.extend(sources)
        return list(sources)

    monkeypatch.setattr(agentgrep, "prefilter_sources_by_root", prefilter_sources_by_root)

    plan = build_physical_search_plan(
        _query(scope="conversations", match_surface="haystack", limit=1),
        (source,),
        agentgrep.BackendSelection(find_tool=None, grep_tool="rg", json_tool=None),
    )

    assert prefetched_sources == [source]
    assert [task.source for task in plan.tasks] == [source]
    assert [task.strategy for task in plan.tasks] == [
        "jsonl_bounded_reverse_haystack_raw_text_prefilter",
    ]
    assert [decision.name for decision in plan.decisions] == [
        "scope_prune",
        "root_prefilter",
        "candidate_order",
    ]


def test_bounded_haystack_root_source_with_path_match_uses_lazy_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Haystack searches avoid content-only root prefiltering for path matches."""
    root = pathlib.Path("/tmp/claude-projects")
    source = _source(
        agent="claude",
        path="/tmp/claude-projects/tmux-project.jsonl",
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
        search_root=root,
        mtime_ns=2,
    )

    def fail_prefilter_sources_by_root(
        _query: agentgrep.SearchQuery,
        _sources: list[agentgrep.SourceHandle],
        _grep_program: str,
        *,
        progress: agentgrep.SearchProgress | None = None,
        control: agentgrep.SearchControl | None = None,
    ) -> list[agentgrep.SourceHandle]:
        _ = progress, control
        pytest.fail("path-matched haystack sources should be admitted lazily")

    monkeypatch.setattr(agentgrep, "prefilter_sources_by_root", fail_prefilter_sources_by_root)

    plan = build_physical_search_plan(
        _query(scope="conversations", match_surface="haystack", limit=1),
        (source,),
        agentgrep.BackendSelection(find_tool=None, grep_tool="rg", json_tool=None),
    )

    assert [task.source for task in plan.tasks] == [source]
    assert [task.strategy for task in plan.tasks] == [
        "jsonl_bounded_reverse_haystack_raw_text_prefilter",
    ]
    assert [decision.name for decision in plan.decisions] == [
        "scope_prune",
        "root_prefilter_skipped",
        "candidate_order",
    ]


def test_unbounded_haystack_path_match_uses_lazy_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unbounded haystack searches admit path-matched sources without grep.

    Regression guard: content-only root prefiltering dropped sources whose
    haystack match lived in the file path (project directory names), so
    unlimited searches for a project name silently missed those records.
    """
    root = pathlib.Path("/tmp/claude-projects")
    source = _source(
        agent="claude",
        path="/tmp/claude-projects/tmux-project.jsonl",
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
        search_root=root,
        mtime_ns=2,
    )

    def fail_prefilter_sources_by_root(
        _query: agentgrep.SearchQuery,
        _sources: list[agentgrep.SourceHandle],
        _grep_program: str,
        *,
        progress: agentgrep.SearchProgress | None = None,
        control: agentgrep.SearchControl | None = None,
    ) -> list[agentgrep.SourceHandle]:
        _ = progress, control
        pytest.fail("path-matched haystack sources should skip content prefiltering")

    monkeypatch.setattr(agentgrep, "prefilter_sources_by_root", fail_prefilter_sources_by_root)

    plan = build_physical_search_plan(
        _query(scope="conversations", match_surface="haystack", limit=None),
        (source,),
        agentgrep.BackendSelection(find_tool=None, grep_tool="rg", json_tool=None),
    )

    assert [task.source for task in plan.tasks] == [source]
    assert [task.strategy for task in plan.tasks] == ["root_full_scan"]
    skipped = [d for d in plan.decisions if d.name == "root_prefilter_skipped"]
    assert [d.detail for d in skipped] == ["haystack_path_match"]


def test_regex_haystack_path_match_uses_lazy_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regex haystack terms admit path-matched sources without grep."""
    root = pathlib.Path("/tmp/claude-projects")
    source = _source(
        agent="claude",
        path="/tmp/claude-projects/tmux-project.jsonl",
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
        search_root=root,
        mtime_ns=2,
    )

    def fail_prefilter_sources_by_root(
        _query: agentgrep.SearchQuery,
        _sources: list[agentgrep.SourceHandle],
        _grep_program: str,
        *,
        progress: agentgrep.SearchProgress | None = None,
        control: agentgrep.SearchControl | None = None,
    ) -> list[agentgrep.SourceHandle]:
        _ = progress, control
        pytest.fail("path-matched haystack sources should skip content prefiltering")

    monkeypatch.setattr(agentgrep, "prefilter_sources_by_root", fail_prefilter_sources_by_root)

    plan = build_physical_search_plan(
        _query(
            scope="conversations",
            match_surface="haystack",
            regex=True,
            terms=(r"TMUX-\w+",),
            limit=None,
        ),
        (source,),
        agentgrep.BackendSelection(find_tool=None, grep_tool="rg", json_tool=None),
    )

    assert [task.source for task in plan.tasks] == [source]


def test_bounded_haystack_path_match_admits_stateful_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path-matched haystack admission covers non-append-only adapters too."""
    root = pathlib.Path("/tmp/codex-sessions")
    source = _source(
        agent="codex",
        path="/tmp/codex-sessions/tmux-rollout.jsonl",
        search_root=root,
        mtime_ns=2,
    )

    def fail_prefilter_sources_by_root(
        _query: agentgrep.SearchQuery,
        _sources: list[agentgrep.SourceHandle],
        _grep_program: str,
        *,
        progress: agentgrep.SearchProgress | None = None,
        control: agentgrep.SearchControl | None = None,
    ) -> list[agentgrep.SourceHandle]:
        _ = progress, control
        pytest.fail("path-matched haystack sources should skip content prefiltering")

    monkeypatch.setattr(agentgrep, "prefilter_sources_by_root", fail_prefilter_sources_by_root)

    plan = build_physical_search_plan(
        _query(match_surface="haystack", limit=5),
        (source,),
        agentgrep.BackendSelection(find_tool=None, grep_tool="rg", json_tool=None),
    )

    assert [task.source for task in plan.tasks] == [source]
    assert [task.strategy for task in plan.tasks] == ["root_full_scan"]


def test_text_surface_path_match_keeps_eager_prefilter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text-surface searches never use path-match admission."""
    root = pathlib.Path("/tmp/codex-sessions")
    source = _source(
        agent="codex",
        path="/tmp/codex-sessions/tmux-rollout.jsonl",
        search_root=root,
        mtime_ns=2,
    )
    prefiltered: list[agentgrep.SourceHandle] = []

    def prefilter_sources_by_root(
        _query: agentgrep.SearchQuery,
        sources: list[agentgrep.SourceHandle],
        _grep_program: str,
        *,
        progress: agentgrep.SearchProgress | None = None,
        control: agentgrep.SearchControl | None = None,
    ) -> list[agentgrep.SourceHandle]:
        _ = progress, control
        prefiltered.extend(sources)
        return list(sources)

    monkeypatch.setattr(agentgrep, "prefilter_sources_by_root", prefilter_sources_by_root)

    plan = build_physical_search_plan(
        _query(match_surface="text", limit=None),
        (source,),
        agentgrep.BackendSelection(find_tool=None, grep_tool="rg", json_tool=None),
    )

    assert prefiltered == [source]
    assert [task.source for task in plan.tasks] == [source]


def test_unbounded_root_source_still_uses_eager_prefilter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unbounded root searches keep the whole-root grep prefilter."""
    root = pathlib.Path("/tmp/claude-projects")
    matched = _source(
        agent="claude",
        path="/tmp/claude-projects/matched.jsonl",
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
        search_root=root,
        mtime_ns=2,
    )
    missed = _source(
        agent="claude",
        path="/tmp/claude-projects/missed.jsonl",
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
        search_root=root,
        mtime_ns=1,
    )
    prefetched_sources: list[agentgrep.SourceHandle] = []

    def prefilter_sources_by_root(
        _query: agentgrep.SearchQuery,
        sources: list[agentgrep.SourceHandle],
        _grep_program: str,
        *,
        progress: agentgrep.SearchProgress | None = None,
        control: agentgrep.SearchControl | None = None,
    ) -> list[agentgrep.SourceHandle]:
        _ = progress, control
        prefetched_sources.extend(sources)
        return [matched]

    monkeypatch.setattr(agentgrep, "prefilter_sources_by_root", prefilter_sources_by_root)

    plan = build_physical_search_plan(
        _query(scope="conversations", match_surface="haystack", limit=None),
        (matched, missed),
        agentgrep.BackendSelection(find_tool=None, grep_tool="rg", json_tool=None),
    )

    assert prefetched_sources == [matched, missed]
    assert [task.source for task in plan.tasks] == [matched]
    assert [task.strategy for task in plan.tasks] == ["root_full_scan"]
    assert [decision.name for decision in plan.decisions] == [
        "scope_prune",
        "root_prefilter",
        "candidate_order",
    ]


class SourceStrategyCase(t.NamedTuple):
    """One query/source combination and its expected execution strategy."""

    test_id: str
    query: agentgrep.SearchQuery
    source: agentgrep.SourceHandle
    expected_strategy: str
    expected_record_order: str
    expected_limit_behavior: str


STRATEGY_CASES: tuple[SourceStrategyCase, ...] = (
    SourceStrategyCase(
        test_id="grep-text-codex-sessions-limited-uses-forward-raw-prefilter",
        query=_query(match_surface="text"),
        source=_source(
            agent="codex",
            path="/tmp/codex-session.jsonl",
            adapter_id="codex.sessions_jsonl.v1",
        ),
        expected_strategy="jsonl_raw_text_prefilter",
        expected_record_order="unknown",
        expected_limit_behavior="drain_source",
    ),
    SourceStrategyCase(
        test_id="grep-text-jsonl-unlimited-uses-raw-prefilter",
        query=_query(match_surface="text", limit=None),
        source=_source(
            agent="codex",
            path="/tmp/codex-session.jsonl",
            adapter_id="codex.sessions_jsonl.v1",
        ),
        expected_strategy="jsonl_raw_text_prefilter",
        expected_record_order="unknown",
        expected_limit_behavior="drain_source",
    ),
    SourceStrategyCase(
        test_id="search-haystack-codex-sessions-limited-keeps-full-scan",
        query=_query(match_surface="haystack"),
        source=_source(
            agent="codex",
            path="/tmp/codex-session.jsonl",
            adapter_id="codex.sessions_jsonl.v1",
        ),
        expected_strategy="direct_full_scan",
        expected_record_order="unknown",
        expected_limit_behavior="drain_source",
    ),
    SourceStrategyCase(
        test_id="search-haystack-safe-jsonl-limited-uses-bounded-raw-prefilter",
        query=_query(scope="conversations", match_surface="haystack"),
        source=_source(
            agent="claude",
            path="/tmp/claude-project.jsonl",
            store="claude.projects",
            adapter_id="claude.projects_jsonl.v1",
        ),
        expected_strategy="jsonl_bounded_reverse_haystack_raw_text_prefilter",
        expected_record_order="newest_first",
        expected_limit_behavior="bounded_source",
    ),
    SourceStrategyCase(
        test_id="regex-text-codex-sessions-limited-keeps-full-scan",
        query=_query(regex=True, match_surface="text"),
        source=_source(
            agent="codex",
            path="/tmp/codex-session.jsonl",
            adapter_id="codex.sessions_jsonl.v1",
        ),
        expected_strategy="direct_full_scan",
        expected_record_order="unknown",
        expected_limit_behavior="drain_source",
    ),
    SourceStrategyCase(
        test_id="regex-text-claude-projects-limited-still-bounded-reverse",
        query=_query(scope="conversations", regex=True, match_surface="text"),
        source=_source(
            agent="claude",
            path="/tmp/claude-project.jsonl",
            store="claude.projects",
            adapter_id="claude.projects_jsonl.v1",
        ),
        expected_strategy="jsonl_bounded_reverse_scan",
        expected_record_order="newest_first",
        expected_limit_behavior="bounded_source",
    ),
    SourceStrategyCase(
        test_id="grep-text-codex-history-limited-still-bounded-raw-prefilter",
        query=_query(match_surface="text"),
        source=_source(
            agent="codex",
            path="/tmp/history.jsonl",
            store="codex.history",
            adapter_id="codex.history_jsonl.v1",
            path_kind="history_file",
        ),
        expected_strategy="jsonl_bounded_reverse_raw_text_prefilter",
        expected_record_order="newest_first",
        expected_limit_behavior="bounded_source",
    ),
    SourceStrategyCase(
        test_id="grep-text-grok-prompt-history-limited-still-bounded-raw-prefilter",
        query=_query(match_surface="text", agents=("grok",)),
        source=_source(
            agent="grok",
            path="/tmp/grok-prompt-history.jsonl",
            store="grok.prompt_history",
            adapter_id="grok.prompt_history_jsonl.v1",
            path_kind="history_file",
        ),
        expected_strategy="jsonl_bounded_reverse_raw_text_prefilter",
        expected_record_order="newest_first",
        expected_limit_behavior="bounded_source",
    ),
    SourceStrategyCase(
        test_id="grep-text-antigravity-cli-history-limited-still-bounded-raw-prefilter",
        query=_query(match_surface="text", agents=("antigravity-cli",)),
        source=_source(
            agent="antigravity-cli",
            path="/tmp/antigravity-history.jsonl",
            store="antigravity-cli.history",
            adapter_id="antigravity_cli.history_jsonl.v1",
            path_kind="history_file",
        ),
        expected_strategy="jsonl_bounded_reverse_raw_text_prefilter",
        expected_record_order="newest_first",
        expected_limit_behavior="bounded_source",
    ),
    SourceStrategyCase(
        test_id="search-haystack-pi-sessions-limited-keeps-full-scan",
        query=_query(
            scope="conversations",
            match_surface="haystack",
            agents=("pi",),
        ),
        source=_source(
            agent="pi",
            path="/tmp/pi-session.jsonl",
            store="pi.sessions",
            adapter_id="pi.sessions_jsonl.v1",
        ),
        expected_strategy="direct_full_scan",
        expected_record_order="unknown",
        expected_limit_behavior="drain_source",
    ),
    SourceStrategyCase(
        test_id="grep-text-pi-sessions-limited-uses-forward-raw-prefilter",
        query=_query(
            scope="conversations",
            match_surface="text",
            agents=("pi",),
        ),
        source=_source(
            agent="pi",
            path="/tmp/pi-session.jsonl",
            store="pi.sessions",
            adapter_id="pi.sessions_jsonl.v1",
        ),
        expected_strategy="jsonl_raw_text_prefilter",
        expected_record_order="unknown",
        expected_limit_behavior="drain_source",
    ),
    SourceStrategyCase(
        test_id="compiled-jsonl-keeps-full-scan",
        query=_query(compiled=_compiled_query()),
        source=_source(
            agent="codex",
            path="/tmp/codex-session.jsonl",
            adapter_id="codex.sessions_jsonl.v1",
        ),
        expected_strategy="direct_full_scan",
        expected_record_order="unknown",
        expected_limit_behavior="drain_source",
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
        expected_record_order="unknown",
        expected_limit_behavior="drain_source",
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
    assert [task.record_order for task in plan.tasks] == [case.expected_record_order]
    assert [task.limit_behavior for task in plan.tasks] == [case.expected_limit_behavior]


def test_physical_plan_records_scheduler_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Physical source tasks expose scheduler-facing cost and grouping metadata."""
    source = _source(
        agent="claude",
        path="/tmp/claude-project.jsonl",
        store="claude.projects",
        adapter_id="claude.projects_jsonl.v1",
    )
    monkeypatch.setattr(agentgrep, "direct_source_matches", lambda *args, **kwargs: True)

    plan = build_physical_search_plan(
        _query(scope="conversations", match_surface="haystack", limit=10),
        (source,),
        agentgrep.BackendSelection(find_tool=None, grep_tool=None, json_tool=None),
    )

    task = plan.tasks[0]
    assert task.source_group == "claude:claude.projects:claude.projects_jsonl.v1"
    assert task.cost_hint == 20
    assert task.can_yield_batches is True
    assert task.supports_cancellation is True
