"""Typed query planning helpers.

The planner is the engine boundary described by ADR-004: frontends submit
immutable query intent, adapters declare capability, and execution consumes
concrete source tasks. agentgrep is still alpha, so this module may reshape
APIs when a plan-first interface makes discovery, profiling, or non-blocking
execution simpler.
"""

from __future__ import annotations

import dataclasses
import pathlib
import typing as t

if t.TYPE_CHECKING:
    import agentgrep

type SourceStrategy = t.Literal["metadata", "root_prefilter", "direct_source"]


@dataclasses.dataclass(frozen=True, slots=True)
class QueryRequest:
    """Immutable frontend-neutral search intent owned by the planner."""

    terms: tuple[str, ...]
    scope: agentgrep.SearchScope
    agents: tuple[agentgrep.AgentName, ...]
    limit: int | None
    dedupe: bool
    any_term: bool
    regex: bool
    case_sensitive: bool
    has_compiled_source_predicate: bool


@dataclasses.dataclass(frozen=True, slots=True)
class AdapterCapability:
    """Declared cheap operations for one adapter family."""

    adapter_id: str
    metadata_only_discovery: bool = True
    source_predicate_pushdown: bool = True
    raw_text_prefilter: bool = False
    sqlite_predicate_pushdown: bool = False
    streaming_records: bool = True


@dataclasses.dataclass(frozen=True, slots=True)
class LogicalSearchPlan:
    """Normalized search work before concrete source handles exist."""

    request: QueryRequest
    initial_store_roles: frozenset[agentgrep.StoreRole] | None
    expects_prompt_fallback: bool
    source_predicate_available: bool
    text_prefilter_required: bool


@dataclasses.dataclass(frozen=True, slots=True)
class PlannerDecision:
    """One privacy-safe planning decision summary."""

    name: str
    source_count: int
    detail: str


@dataclasses.dataclass(frozen=True, slots=True)
class SourceTask:
    """One executable source scan in a physical search plan."""

    source: agentgrep.SourceHandle
    strategy: SourceStrategy
    can_stream_records: bool
    restore_order_key: tuple[int, str]


@dataclasses.dataclass(frozen=True, slots=True)
class PhysicalSearchPlan:
    """Executable source-task plan consumed by search drivers."""

    logical: LogicalSearchPlan
    tasks: tuple[SourceTask, ...]
    decisions: tuple[PlannerDecision, ...]


def build_query_request(query: agentgrep.SearchQuery) -> QueryRequest:
    """Build immutable planner intent from a search query."""
    source_predicate = query.compiled.source_predicate if query.compiled is not None else None
    return QueryRequest(
        terms=query.terms,
        scope=query.scope,
        agents=query.agents,
        limit=query.limit,
        dedupe=query.dedupe,
        any_term=query.any_term,
        regex=query.regex,
        case_sensitive=query.case_sensitive,
        has_compiled_source_predicate=source_predicate is not None,
    )


def build_logical_search_plan(query: agentgrep.SearchQuery) -> LogicalSearchPlan:
    """Build a logical search plan from frontend-neutral query intent."""
    import agentgrep

    if query.scope == "all":
        store_roles = None
        expects_prompt_fallback = False
    elif query.scope == "conversations":
        store_roles = agentgrep.CONVERSATION_STORE_ROLES
        expects_prompt_fallback = False
    else:
        store_roles = agentgrep.PROMPT_HISTORY_STORE_ROLES
        expects_prompt_fallback = True

    source_predicate = query.compiled.source_predicate if query.compiled is not None else None
    return LogicalSearchPlan(
        request=build_query_request(query),
        initial_store_roles=store_roles,
        expects_prompt_fallback=expects_prompt_fallback,
        source_predicate_available=source_predicate is not None,
        text_prefilter_required=bool(query.terms),
    )


def build_physical_search_plan(
    query: agentgrep.SearchQuery,
    sources: t.Iterable[agentgrep.SourceHandle],
    backends: agentgrep.BackendSelection,
    *,
    progress: agentgrep.SearchProgress | None = None,
    control: agentgrep.SearchControl | None = None,
) -> PhysicalSearchPlan:
    """Build the executable source-task plan for a search query."""
    import agentgrep

    logical = build_logical_search_plan(query)
    source_list = list(sources)
    active_progress = agentgrep.noop_search_progress() if progress is None else progress
    active_control = agentgrep.SearchControl() if control is None else control
    prompt_history_agents = agentgrep.prompt_history_agents_for_sources(source_list)
    scoped_sources = [
        source
        for source in source_list
        if agentgrep.source_matches_scope(
            source,
            query.scope,
            prompt_history_agents=prompt_history_agents,
        )
    ]
    decisions: list[PlannerDecision] = [
        PlannerDecision(
            name="scope_prune",
            source_count=len(scoped_sources),
            detail=query.scope,
        ),
    ]

    if not query.terms:
        return PhysicalSearchPlan(
            logical=logical,
            tasks=tuple(_source_task(source, "metadata") for source in scoped_sources),
            decisions=tuple(decisions),
        )

    planned_sources = scoped_sources
    if backends.grep_tool is not None:
        planned_sources = agentgrep.prefilter_sources_by_root(
            query,
            planned_sources,
            backends.grep_tool,
            progress=active_progress,
            control=active_control,
        )
        decisions.append(
            PlannerDecision(
                name="root_prefilter",
                source_count=len(planned_sources),
                detail="grep_tool",
            ),
        )

    ordered_sources: list[agentgrep.SourceHandle] = []
    for source in planned_sources:
        if active_control.answer_now_requested():
            break
        if source.search_root is not None:
            ordered_sources.append(source)
            continue
        if agentgrep.direct_source_matches(source, query, backends, active_control):
            ordered_sources.append(source)
    ordered_sources.sort(key=agentgrep.source_order_key)
    decisions.append(
        PlannerDecision(
            name="candidate_order",
            source_count=len(ordered_sources),
            detail="newest_first",
        ),
    )
    return PhysicalSearchPlan(
        logical=logical,
        tasks=tuple(
            _source_task(
                source,
                "root_prefilter" if source.search_root is not None else "direct_source",
            )
            for source in ordered_sources
        ),
        decisions=tuple(decisions),
    )


def _source_task(source: agentgrep.SourceHandle, strategy: SourceStrategy) -> SourceTask:
    """Build one physical source task."""
    return SourceTask(
        source=source,
        strategy=strategy,
        can_stream_records=True,
        restore_order_key=_source_order_key(source),
    )


def _source_order_key(source: agentgrep.SourceHandle) -> tuple[int, str]:
    """Return the stable task ordering key without importing the whole engine at module load."""
    return (-source.mtime_ns, str(pathlib.Path(source.path)))
