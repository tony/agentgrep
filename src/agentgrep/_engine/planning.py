"""Typed query planning helpers.

The planner is the engine boundary described by ADR-004: frontends submit
immutable query intent, adapters declare capability, and execution consumes
concrete source tasks. agentgrep is still alpha, so this module may reshape
APIs when a plan-first interface makes discovery, profiling, or non-blocking
execution simpler.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import pathlib
import re
import typing as t

from agentgrep._engine.orchestration import (
    direct_source_matches,
    prefilter_sources_by_root,
    source_matches_scope,
    source_order_key,
)
from agentgrep._engine.source_filters import source_may_match_query
from agentgrep.progress import SearchControl, SearchProgress, noop_search_progress
from agentgrep.records import (
    CONVERSATION_STORE_ROLES,
    DEFAULT_TARGETED_CONVERSATION_LIMIT,
    PROMPT_HISTORY_STORE_ROLES,
    AgentName,
    BackendSelection,
    SearchEffort,
    SearchQuery,
    SearchScope,
    SearchScopeProvenance,
    SourceHandle,
)
from agentgrep.stores import StoreRole

type SourceStrategy = t.Literal[
    "metadata_only",
    "direct_full_scan",
    "root_full_scan",
    "jsonl_raw_text_prefilter",
    "jsonl_bounded_reverse_scan",
    "jsonl_bounded_reverse_raw_text_prefilter",
    "jsonl_bounded_reverse_haystack_raw_text_prefilter",
]
type SourceRecordOrder = t.Literal["unknown", "newest_first"]
type SourceLimitBehavior = t.Literal["drain_source", "bounded_source"]
type LimitPolicyMode = t.Literal["source_order_frontier"]


class LimitFrontier(t.Protocol):
    """Owner-thread frontier state consulted by scheduler limit policies."""

    @property
    def is_satisfied(self) -> bool:
        """Return whether the frontier has enough accepted candidates."""
        ...


RAW_TEXT_PREFILTER_ADAPTERS: frozenset[str] = frozenset(
    {
        "codex.sessions_jsonl.v1",
        "codex.history_jsonl.v1",
        "antigravity_cli.history_jsonl.v1",
        "claude.projects_jsonl.v1",
        "grok.prompt_history_jsonl.v1",
        "grok.sessions_jsonl.v1",
        "pi.sessions_jsonl.v1",
    },
)
"""Adapters whose text-bearing records can be prefiltered from raw JSONL lines."""

APPEND_ONLY_JSONL_ADAPTERS: frozenset[str] = frozenset(
    {
        "codex.history_jsonl.v1",
        "antigravity_cli.history_jsonl.v1",
        "claude.projects_jsonl.v1",
        "grok.prompt_history_jsonl.v1",
        "grok.sessions_jsonl.v1",
    },
)
"""Adapters safe for newest-first bounded scans.

Members must be append-only and order-independent per record: no leading
header line may carry state (model, session id, cwd) forward into later
records. ``codex.sessions_jsonl.v1`` and ``pi.sessions_jsonl.v1`` read a
``session_meta`` / ``session`` header that earlier records depend on, so a
reverse scan would emit records before that state is known.
"""

HAYSTACK_RAW_TEXT_PREFILTER_ADAPTERS: frozenset[str] = frozenset(
    {
        "claude.projects_jsonl.v1",
        "grok.sessions_jsonl.v1",
        "pi.sessions_jsonl.v1",
    },
)
"""Adapters whose haystack-bearing JSONL records can use raw candidate checks.

Membership requires every haystack-matched field — text, role, model,
title, and source path — to be self-contained on each record's raw line
(ADR-0004). Cross-record session-identity fields are exempt because
``build_search_haystack`` does not include them, which is why
``pi.sessions_jsonl.v1`` qualifies despite reading ``session_id`` and
``conversation_id`` from its leading session header.
"""

STATEFUL_HEADER_JSONL_ADAPTERS: frozenset[str] = frozenset(
    {
        "codex.sessions_jsonl.v1",
        "pi.sessions_jsonl.v1",
        "gemini.tmp_chats_jsonl.v1",
    },
)
"""Adapters whose parsers carry state from a leading header line.

Members must never join :data:`APPEND_ONLY_JSONL_ADAPTERS`, and may join
:data:`RAW_TEXT_PREFILTER_ADAPTERS` only with parser-level header
handling (the Codex and pi parsers exempt their headers from raw skip
predicates and pre-read them for reverse scans; the Gemini parser has
neither and therefore joins no optimization set).
"""


@dataclasses.dataclass(frozen=True, slots=True)
class QueryRequest:
    """Immutable frontend-neutral search intent owned by the planner.

    Attributes
    ----------
    terms : tuple[str, ...]
        Text needles a record must match. Empty admits every record the remaining filters
        allow, which is the metadata-only shape of the plan.
    scope : SearchScope
        Which record kinds the plan may return: prompts, conversations, or both.
    scope_provenance : SearchScopeProvenance
        Whether the scope was inferred or explicitly selected.
    effort : SearchEffort
        Which source families the plan may open: prompt history only or prompt history
        plus transcript backends.
    order : str
        Result order the engine must preserve. ``"relevance"`` and ``"newest"``
        compare every eligible match; ``"scan"`` alone permits count-bounded
        execution without comparing later sources.
    agents : tuple[AgentName, ...]
        Agents whose stores are discovered. An empty tuple discovers nothing.
    limit : int | None
        Result ceiling, which also lets bounded sources stop scanning early. ``None``
        runs the search to exhaustion.
    conversation_limit : int | None
        Distinct conversation-attempt bound for targeted effort. ``None`` for
        prompt and exhaustive requests.
    dedupe : bool
        Whether records that collapse to one identity are folded together.
    any_term : bool
        Whether one matching term suffices. ``False`` requires every term to match.
    regex : bool
        Whether each term is a regular expression rather than a literal substring.
    case_sensitive : bool
        Whether matching respects case. ``False`` folds case on both sides.
    has_compiled_source_predicate : bool
        Whether the query carried a source-level predicate from the query compiler, so
        candidates can be pruned before any file is opened. Held as a flag rather than
        the closure itself so the request stays comparable and free of callables.
    """

    terms: tuple[str, ...]
    scope: SearchScope
    scope_provenance: SearchScopeProvenance
    effort: SearchEffort
    order: str
    agents: tuple[AgentName, ...]
    limit: int | None
    conversation_limit: int | None
    dedupe: bool
    any_term: bool
    regex: bool
    case_sensitive: bool
    has_compiled_source_predicate: bool


@dataclasses.dataclass(frozen=True, slots=True)
class AdapterCapability:
    """Declared cheap operations for one adapter family.

    Attributes
    ----------
    adapter_id : str
        Versioned parser identity the capabilities describe, e.g.
        ``"codex.sessions_jsonl.v1"``.
    metadata_only_discovery : bool
        Whether sources can be enumerated from discovery metadata alone, with no read of
        the source itself.
    source_predicate_pushdown : bool
        Whether a compiled source-level predicate can prune this adapter's sources before
        they are opened.
    jsonl_raw_text_prefilter : bool
        Whether raw JSONL lines can be tested for literal terms before JSON decode, as
        :data:`RAW_TEXT_PREFILTER_ADAPTERS` records per adapter id.
    sqlite_predicate_pushdown : bool
        Whether predicates can be pushed into the SQL the adapter issues instead of
        filtering rows after they are read.
    streaming_records : bool
        Whether the parser yields records incrementally instead of materializing the
        whole source first.
    """

    adapter_id: str
    metadata_only_discovery: bool = True
    source_predicate_pushdown: bool = True
    jsonl_raw_text_prefilter: bool = False
    sqlite_predicate_pushdown: bool = False
    streaming_records: bool = True


@dataclasses.dataclass(frozen=True, slots=True)
class LogicalSearchPlan:
    """Normalized search work before concrete source handles exist.

    Attributes
    ----------
    request : QueryRequest
        Frontend-neutral intent this plan normalizes.
    initial_store_roles : frozenset[StoreRole] | None
        Store roles the first discovery pass opens. ``None`` at ``all`` scope, where every
        role is admitted.
    source_predicate_available : bool
        Whether the compiled query supplies a source-level predicate, so candidates can be
        dropped before any file is opened.
    text_prefilter_required : bool
        Whether the query carries terms, so a root grep or raw-line prefilter can decide
        source admission. ``False`` for metadata-only searches, which visit every scoped
        source.
    """

    request: QueryRequest
    initial_store_roles: frozenset[StoreRole] | None
    source_predicate_available: bool
    text_prefilter_required: bool


@dataclasses.dataclass(frozen=True, slots=True)
class PlannerDecision:
    """One privacy-safe planning decision summary.

    Attributes
    ----------
    name : str
        Decision label, e.g. ``"scope_prune"``, ``"root_prefilter"``, or
        ``"candidate_order"``.
    source_count : int
        How many sources the decision left in play, or how many it admitted when the
        decision only re-admits a group.
    detail : str
        Short reason or mechanism behind the decision — the scope name, ``"grep_tool"``,
        ``"sqlite_source"``. Never a path or prompt text, so profiles stay shareable.
    """

    name: str
    source_count: int
    detail: str


@dataclasses.dataclass(frozen=True, slots=True)
class LimitPolicy:
    """Scheduler policy for deciding whether remaining source tasks can be skipped.

    Attributes
    ----------
    mode : LimitPolicyMode
        Skip rule the scheduler applies. ``"source_order_frontier"`` skips queued
        lower-priority tasks once a limited query's frontier holds enough accepted
        candidates, which is safe because tasks are queued newest-source-first.
    """

    mode: LimitPolicyMode = "source_order_frontier"

    def can_skip_remaining(
        self,
        *,
        query: SearchQuery,
        frontier: LimitFrontier,
    ) -> bool:
        """Return whether queued lower-priority source tasks can be skipped."""
        return (
            self.mode == "source_order_frontier"
            and query.limit is not None
            and frontier.is_satisfied
        )


@dataclasses.dataclass(frozen=True, slots=True)
class SourceTask:
    """One executable source scan in a physical search plan.

    Attributes
    ----------
    source : SourceHandle
        Discovered source this task scans.
    strategy : SourceStrategy
        Cheapest read the planner proved safe for this source, from a metadata-only visit
        through raw-line-prefiltered bounded reverse scans to a full scan.
    record_order : SourceRecordOrder
        Order records arrive in. ``"newest_first"`` for the bounded reverse strategies;
        ``"unknown"`` when the source is read front to back.
    limit_behavior : SourceLimitBehavior
        ``"bounded_source"`` when the scan may stop once the query limit is satisfied,
        ``"drain_source"`` when it must read the whole source before results are ordered.
    can_stream_records : bool
        Whether records reach the scan incrementally rather than after the whole source
        is parsed.
    restore_order_key : tuple[int, str]
        Newest-first sort key for the source — negated ``mtime_ns`` paired with the path
        string — so plan order can be reconstructed after concurrent execution finishes
        tasks out of order.
    cost_hint : int
        Relative scan cost for scheduling: lowest for a metadata-only visit, highest for a
        full scan, with the prefiltered strategies in between.
    source_group : str
        ``agent:store:adapter_id`` label that aggregates scheduler and profiler counters
        without exposing a path.
    can_yield_batches : bool
        Whether the strategy can emit partial batches before the source is exhausted.
        Only bounded sources can, since a drained source orders its records at the end.
    supports_cancellation : bool
        Whether the scan polls the control handle between records, so an answer-now
        request stops it mid-source.
    limit_policy : LimitPolicy
        Rule deciding whether queued lower-priority tasks can be skipped once the limit is
        satisfied.
    """

    source: SourceHandle
    strategy: SourceStrategy
    record_order: SourceRecordOrder
    limit_behavior: SourceLimitBehavior
    can_stream_records: bool
    restore_order_key: tuple[int, str]
    cost_hint: int = 100
    source_group: str = "default"
    can_yield_batches: bool = True
    supports_cancellation: bool = True
    limit_policy: LimitPolicy = dataclasses.field(default_factory=LimitPolicy)


@dataclasses.dataclass(frozen=True, slots=True)
class SourceAuthorityPlan:
    """Selected source families that may need candidate-level resolution.

    Attributes
    ----------
    codex_rollout_selected : bool
        Whether the plan selected any ``codex.sessions`` rollout transcript, the canonical
        copy of a Codex prompt.
    codex_state_selected : bool
        Whether the plan selected the ``codex.state_db`` index, which can repeat a prompt
        that a rollout transcript also holds.
    """

    codex_rollout_selected: bool = False
    codex_state_selected: bool = False

    @property
    def resolves_codex_candidates(self) -> bool:
        """Return whether matched Codex rollout/state candidates may overlap.

        Returns
        -------
        bool
            ``True`` when both physical source families were selected.
        """
        return self.codex_rollout_selected and self.codex_state_selected


@dataclasses.dataclass(frozen=True, slots=True)
class PhysicalSearchPlan:
    """Executable source-task plan consumed by search drivers.

    Attributes
    ----------
    logical : LogicalSearchPlan
        Normalized intent the tasks were derived from.
    tasks : tuple[SourceTask, ...]
        Executable source scans in newest-source-first order, which is also the priority
        order the scheduler drains them in.
    decisions : tuple[PlannerDecision, ...]
        Planning decisions in the order they applied, for profiler spans and explain
        output. Empty for plans built directly from a source list rather than by the
        planner.
    source_authority : SourceAuthorityPlan
        Which Codex source families the plan selected, telling execution whether matched
        candidates need cross-store resolution before dedupe.
    """

    logical: LogicalSearchPlan
    tasks: tuple[SourceTask, ...]
    decisions: tuple[PlannerDecision, ...]
    source_authority: SourceAuthorityPlan = dataclasses.field(
        default_factory=SourceAuthorityPlan,
    )


def build_source_authority_plan(
    sources: cabc.Iterable[SourceHandle],
) -> SourceAuthorityPlan:
    """Describe candidate authority families in query-selected sources.

    Parameters
    ----------
    sources : collections.abc.Iterable of SourceHandle
        Sources that survived scope, source predicates, and prefiltering.

    Returns
    -------
    SourceAuthorityPlan
        Immutable source-family presence flags. No filesystem probes or
        transcript parsing are performed.
    """
    selected = tuple(sources)
    return SourceAuthorityPlan(
        codex_rollout_selected=any(
            source.agent == "codex" and source.store == "codex.sessions" for source in selected
        ),
        codex_state_selected=any(
            source.agent == "codex" and source.store == "codex.state_db" for source in selected
        ),
    )


def build_query_request(query: SearchQuery) -> QueryRequest:
    """Build immutable planner intent from a search query."""
    if query.order not in {"newest", "relevance", "scan"}:
        msg = "order must be 'newest', 'relevance', or 'scan'"
        raise ValueError(msg)
    source_predicate = query.compiled.source_predicate if query.compiled is not None else None
    effort = _normalized_search_effort(query)
    conversation_limit = _normalized_conversation_limit(query, effort=effort)
    return QueryRequest(
        terms=query.terms,
        scope=query.scope,
        scope_provenance=query.scope_provenance,
        effort=effort,
        order=query.order,
        agents=query.agents,
        limit=query.limit,
        conversation_limit=conversation_limit,
        dedupe=query.dedupe,
        any_term=query.any_term,
        regex=query.regex,
        case_sensitive=query.case_sensitive,
        has_compiled_source_predicate=source_predicate is not None,
    )


def _normalized_search_effort(query: SearchQuery) -> SearchEffort:
    """Derive and validate the source-read policy for ``query``."""
    if query.effort not in {None, "prompt", "targeted", "exhaustive"}:
        msg = "effort must be 'prompt', 'targeted', or 'exhaustive'"
        raise ValueError(msg)
    if query.effort == "prompt" and query.scope != "prompts":
        msg = "prompt effort requires prompt scope"
        raise ValueError(msg)
    if query.effort == "targeted" and query.scope == "prompts":
        msg = "targeted effort requires conversation or all scope"
        raise ValueError(msg)
    return query.effort or ("prompt" if query.scope == "prompts" else "exhaustive")


def _normalized_conversation_limit(
    query: SearchQuery,
    *,
    effort: SearchEffort,
) -> int | None:
    """Validate and normalize the targeted conversation-attempt bound."""
    if effort != "targeted":
        if query.conversation_limit is not None:
            msg = "conversation_limit requires targeted effort"
            raise ValueError(msg)
        return None
    value = (
        DEFAULT_TARGETED_CONVERSATION_LIMIT
        if query.conversation_limit is None
        else query.conversation_limit
    )
    if value < 1:
        msg = "conversation_limit must be greater than 0"
        raise ValueError(msg)
    return value


def _query_limit_requires_drain(query: SearchQuery) -> bool:
    """Return whether a limited query must compare all eligible records."""
    return query.limit is not None and query.order != "scan"


def build_logical_search_plan(query: SearchQuery) -> LogicalSearchPlan:
    """Build a logical search plan from frontend-neutral query intent."""
    request = build_query_request(query)
    if request.effort == "prompt":
        store_roles = PROMPT_HISTORY_STORE_ROLES
    elif query.scope == "all":
        store_roles = None
    elif query.scope == "conversations":
        store_roles = CONVERSATION_STORE_ROLES
    else:
        store_roles = PROMPT_HISTORY_STORE_ROLES | CONVERSATION_STORE_ROLES

    source_predicate = query.compiled.source_predicate if query.compiled is not None else None
    return LogicalSearchPlan(
        request=request,
        initial_store_roles=store_roles,
        source_predicate_available=source_predicate is not None,
        text_prefilter_required=bool(query.terms),
    )


def build_physical_search_plan(
    query: SearchQuery,
    sources: t.Iterable[SourceHandle],
    backends: BackendSelection,
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
) -> PhysicalSearchPlan:
    """Build the executable source-task plan for a search query.

    Parameters
    ----------
    query : SearchQuery
        Compiled query — terms, agents, dedup choice, limit.
    sources : Iterable[SourceHandle]
        Discovered candidate sources, before scope pruning and
        prefilter admission.
    backends : BackendSelection
        Detected external tools; the grep tool gates root
        prefiltering.
    progress : SearchProgress or None
        Progress sink for prefilter phases. ``None`` uses the no-op
        sink.
    control : SearchControl or None
        Optional control handle polled during prefiltering so
        planning can stop early.

    Returns
    -------
    PhysicalSearchPlan
        Ordered source tasks with per-source strategies plus the
        planner decisions that produced them.
    """
    logical = build_logical_search_plan(query)
    strategy_query = query
    if _query_limit_requires_drain(query):
        strategy_query = dataclasses.replace(query, limit=None)
    source_list = list(sources)
    active_progress = noop_search_progress() if progress is None else progress
    active_control = SearchControl() if control is None else control
    scoped_sources = [
        source
        for source in source_list
        if source_matches_scope(
            source,
            query.scope,
            effort=logical.request.effort,
        )
    ]
    decisions: list[PlannerDecision] = [
        PlannerDecision(
            name="scope_prune",
            source_count=len(scoped_sources),
            detail=query.scope,
        ),
    ]
    source_filtered = [source for source in scoped_sources if source_may_match_query(query, source)]
    if len(source_filtered) != len(scoped_sources):
        decisions.append(
            PlannerDecision(
                name="source_predicate_prune",
                source_count=len(source_filtered),
                detail="compiled_or_origin",
            ),
        )
    scoped_sources = source_filtered

    if not query.terms:
        return PhysicalSearchPlan(
            logical=logical,
            tasks=tuple(_source_task(source, "metadata_only") for source in scoped_sources),
            decisions=tuple(decisions),
            source_authority=build_source_authority_plan(scoped_sources),
        )

    planned_sources = scoped_sources
    if backends.grep_tool is not None:
        eager_sources: list[SourceHandle] = []
        lazy_sources: list[SourceHandle] = []
        path_match_sources: list[SourceHandle] = []
        sqlite_sources: list[SourceHandle] = []
        path_term_matcher = _compile_path_term_matcher(query)
        for source in scoped_sources:
            if source.source_kind == "sqlite":
                sqlite_sources.append(source)
            elif (
                path_term_matcher is not None
                and source.search_root is not None
                and path_term_matcher(str(source.path))
            ):
                path_match_sources.append(source)
            elif _can_use_lazy_source_admission(strategy_query, source):
                lazy_sources.append(source)
            else:
                eager_sources.append(source)
        planned_sources = eager_sources
        compiled_record_predicate = (
            query.compiled is not None and query.compiled.record_predicate is not None
        )
        if planned_sources and compiled_record_predicate:
            # A compiled boolean/field query matches via its record
            # predicate; a flat-term root grep prefilter ANDs the terms and
            # would drop OR/NOT matches. Field-level pruning already ran via
            # the compiled source_predicate, so keep these sources and let
            # the record matcher decide.
            decisions.append(
                PlannerDecision(
                    name="root_prefilter_skipped",
                    source_count=len(planned_sources),
                    detail="compiled_record_predicate",
                ),
            )
        elif planned_sources:
            planned_sources = prefilter_sources_by_root(
                strategy_query,
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
        if path_match_sources:
            planned_sources = [*planned_sources, *path_match_sources]
            decisions.append(
                PlannerDecision(
                    name="root_prefilter_skipped",
                    source_count=len(path_match_sources),
                    detail="haystack_path_match",
                ),
            )
        if lazy_sources:
            planned_sources = [*planned_sources, *lazy_sources]
            decisions.append(
                PlannerDecision(
                    name="root_prefilter_skipped",
                    source_count=len(lazy_sources),
                    detail="bounded_append_only_jsonl",
                ),
            )
        if sqlite_sources:
            planned_sources = [*planned_sources, *sqlite_sources]
            decisions.append(
                PlannerDecision(
                    name="root_prefilter_skipped",
                    source_count=len(sqlite_sources),
                    detail="sqlite_source",
                ),
            )

    ordered_sources: list[SourceHandle] = []
    for source in planned_sources:
        if active_control.answer_now_requested():
            break
        if source.search_root is not None:
            ordered_sources.append(source)
            continue
        if direct_source_matches(source, strategy_query, backends, active_control):
            ordered_sources.append(source)
    ordered_sources.sort(key=source_order_key)
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
                _source_strategy(
                    strategy_query,
                    source,
                    source_route="root" if source.search_root is not None else "direct",
                ),
            )
            for source in ordered_sources
        ),
        decisions=tuple(decisions),
        source_authority=build_source_authority_plan(ordered_sources),
    )


def _source_task(source: SourceHandle, strategy: SourceStrategy) -> SourceTask:
    """Build one physical source task."""
    limit_behavior = _source_limit_behavior(strategy)
    return SourceTask(
        source=source,
        strategy=strategy,
        record_order=_source_record_order(strategy),
        limit_behavior=limit_behavior,
        can_stream_records=True,
        restore_order_key=_source_order_key(source),
        cost_hint=_source_cost_hint(strategy),
        source_group=_source_group(source),
        can_yield_batches=limit_behavior == "bounded_source",
        supports_cancellation=True,
    )


def _source_strategy(
    query: SearchQuery,
    source: SourceHandle,
    *,
    source_route: t.Literal["direct", "root"],
) -> SourceStrategy:
    """Return the cheapest safe execution strategy for one source."""
    if _can_use_bounded_reverse_jsonl(query, source):
        if _can_use_jsonl_haystack_raw_text_prefilter(query, source):
            return "jsonl_bounded_reverse_haystack_raw_text_prefilter"
        if _can_use_jsonl_raw_text_prefilter(query, source):
            return "jsonl_bounded_reverse_raw_text_prefilter"
        return "jsonl_bounded_reverse_scan"
    if _can_use_jsonl_raw_text_prefilter(query, source):
        return "jsonl_raw_text_prefilter"
    if source_route == "root":
        return "root_full_scan"
    return "direct_full_scan"


def _can_use_jsonl_raw_text_prefilter(
    query: SearchQuery,
    source: SourceHandle,
) -> bool:
    """Return whether raw JSONL filtering preserves query semantics."""
    return (
        bool(query.terms)
        and query.match_surface == "text"
        and not query.regex
        and query.compiled is None
        and source.source_kind == "jsonl"
        and source.adapter_id in RAW_TEXT_PREFILTER_ADAPTERS
    )


def _can_use_jsonl_haystack_raw_text_prefilter(
    query: SearchQuery,
    source: SourceHandle,
) -> bool:
    """Return whether raw JSONL filtering can safely prefilter haystack queries."""
    return (
        bool(query.terms)
        and query.limit is not None
        and query.match_surface == "haystack"
        and not query.regex
        and query.compiled is None
        and source.source_kind == "jsonl"
        and source.adapter_id in HAYSTACK_RAW_TEXT_PREFILTER_ADAPTERS
    )


def _can_use_bounded_reverse_jsonl(
    query: SearchQuery,
    source: SourceHandle,
) -> bool:
    """Return whether a limited query can read a source newest-first."""
    return (
        bool(query.terms)
        and query.limit is not None
        and query.compiled is None
        and source.source_kind == "jsonl"
        and source.adapter_id in APPEND_ONLY_JSONL_ADAPTERS
    )


def _can_use_lazy_source_admission(
    query: SearchQuery,
    source: SourceHandle,
) -> bool:
    """Return whether a bounded root source can skip eager whole-root prefiltering."""
    if source.search_root is None or not _can_use_bounded_reverse_jsonl(query, source):
        return False
    return _can_use_jsonl_raw_text_prefilter(query, source)


def _compile_path_term_matcher(
    query: SearchQuery,
) -> cabc.Callable[[str], bool] | None:
    """Compile a per-query predicate for source-path term matches.

    The haystack surface includes the source path, and content-only root
    prefilters cannot prove path matches impossible, so path-matched
    sources must be admitted without grep evidence regardless of limit
    or adapter. The planner evaluates this predicate once per candidate
    source, so term state is precomputed here instead of rebuilding a
    query per term per source.
    """
    if query.match_surface != "haystack" or not query.terms:
        return None
    if query.regex:
        flags = 0 if query.case_sensitive else re.IGNORECASE
        patterns = tuple(re.compile(term, flags) for term in query.terms)

        def regex_matches(path_text: str) -> bool:
            return any(pattern.search(path_text) is not None for pattern in patterns)

        return regex_matches
    needles = (
        query.terms if query.case_sensitive else tuple(term.casefold() for term in query.terms)
    )

    def literal_matches(path_text: str) -> bool:
        haystack = path_text if query.case_sensitive else path_text.casefold()
        return any(needle in haystack for needle in needles)

    return literal_matches


def _source_record_order(strategy: SourceStrategy) -> SourceRecordOrder:
    """Return the record order promised by one source strategy."""
    if strategy in {
        "jsonl_bounded_reverse_scan",
        "jsonl_bounded_reverse_raw_text_prefilter",
        "jsonl_bounded_reverse_haystack_raw_text_prefilter",
    }:
        return "newest_first"
    return "unknown"


def _source_limit_behavior(strategy: SourceStrategy) -> SourceLimitBehavior:
    """Return whether a source strategy may stop after satisfying the query limit."""
    if strategy in {
        "jsonl_bounded_reverse_scan",
        "jsonl_bounded_reverse_raw_text_prefilter",
        "jsonl_bounded_reverse_haystack_raw_text_prefilter",
    }:
        return "bounded_source"
    return "drain_source"


def _source_cost_hint(strategy: SourceStrategy) -> int:
    """Return a rough relative cost hint for source scheduling."""
    if strategy == "metadata_only":
        return 1
    if strategy in {
        "jsonl_bounded_reverse_raw_text_prefilter",
        "jsonl_bounded_reverse_haystack_raw_text_prefilter",
    }:
        return 20
    if strategy == "jsonl_bounded_reverse_scan":
        return 40
    if strategy == "jsonl_raw_text_prefilter":
        return 60
    return 100


def _source_group(source: SourceHandle) -> str:
    """Return a stable source group label for scheduler/profiler aggregation."""
    return f"{source.agent}:{source.store}:{source.adapter_id}"


def _source_order_key(source: SourceHandle) -> tuple[int, str]:
    """Return the stable task ordering key without importing the whole engine at module load."""
    return (-source.mtime_ns, str(pathlib.Path(source.path)))
