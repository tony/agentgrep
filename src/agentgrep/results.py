"""Dependency-light search lifecycle result types."""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, replace

from agentgrep.records import (
    DEFAULT_TARGETED_CONVERSATION_LIMIT,
    AgentName,
    SearchEffort,
    SearchMatchSurface,
    SearchQuery,
    SearchRecord,
    SearchScope,
    SearchScopeProvenance,
)

type RunState = t.Literal[
    "complete",
    "bounded",
    "truncated",
    "cancelled",
    "approximate",
    "failed",
]
type SearchOutcome = t.Literal[
    "matches",
    "no_prompt_match",
    "no_candidate_conversation",
    "no_selected_conversation_match",
    "no_exhaustive_match",
    "undetermined",
]
type DiagnosticSeverity = t.Literal["info", "warning", "error"]


@dataclass(frozen=True, slots=True)
class NormalizedSearchRequest:
    """Frontend-neutral request values applied by the engine.

    Attributes
    ----------
    terms : tuple[str, ...]
        Text terms after query compilation.
    scope : SearchScope
        Record kinds admitted by the normalized request.
    scope_provenance : SearchScopeProvenance
        Whether the scope was inferred or explicitly selected.
    effort : SearchEffort
        Source-read effort applied by the planner.
    agents : tuple[AgentName, ...]
        Agents admitted to discovery.
    limit : int | None
        Requested result cap.
    conversation_limit : int | None
        Applied targeted conversation-attempt cap, or ``None`` for other efforts.
    dedupe : bool
        Whether duplicate records are folded.
    case_sensitive : bool
        Whether text matching respects case.
    order : str
        Result order applied by the collector.
    match_surface : SearchMatchSurface
        Record surface used for text matching.
    """

    terms: tuple[str, ...]
    scope: SearchScope
    scope_provenance: SearchScopeProvenance
    effort: SearchEffort
    agents: tuple[AgentName, ...]
    limit: int | None
    conversation_limit: int | None
    dedupe: bool
    case_sensitive: bool
    order: str
    match_surface: SearchMatchSurface


@dataclass(frozen=True, slots=True)
class RunStatus:
    """Primary terminal state plus retained secondary conditions.

    Attributes
    ----------
    state : RunState
        Highest-precedence terminal state.
    reason : str | None
        Stable code naming the primary condition.
    conditions : tuple[str, ...]
        Stable codes for every condition that affected the run.
    """

    state: RunState
    reason: str | None = None
    conditions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunDiagnostic:
    """One privacy-safe lifecycle warning or error.

    Attributes
    ----------
    code : str
        Stable machine-readable diagnostic code.
    message : str
        Human-readable text without prompt content or local paths.
    severity : DiagnosticSeverity
        Diagnostic severity.
    """

    code: str
    message: str
    severity: DiagnosticSeverity = "warning"


@dataclass(frozen=True, slots=True)
class RunCoverage:
    """Aggregate source and record coverage for one search.

    Attributes
    ----------
    sources_discovered : int
        Sources returned by discovery before query predicates.
    sources_eligible : int
        Discovered sources that survived source predicates.
    sources_planned : int
        Eligible sources admitted to physical execution.
    sources_attempted : int
        Planned sources whose execution started.
    sources_completed : int
        Attempted sources that completed normally.
    sources_bounded : int
        Attempted sources that stopped under a declared execution bound.
    sources_skipped : int
        Planned sources not attempted because execution stopped.
    sources_unsupported : int
        Planned sources without a registered readable adapter.
    sources_failed : int
        Attempted sources that failed.
    sources_cancelled : int
        Attempted sources stopped by cancellation.
    records_seen : int
        Parsed records across completed source attempts.
    matches_seen : int
        Pre-dedup matching records across completed source attempts.
    conversations_eligible : int
        Conversation candidates eligible for targeted routing.
    conversations_selected : int
        Conversation candidates selected by targeted routing.
    conversations_completed : int
        Selected conversations scanned to completion.
    source_stop_reasons : tuple[str, ...]
        Stable unique reason codes reported by non-complete source attempts.
    """

    sources_discovered: int
    sources_eligible: int
    sources_planned: int
    sources_attempted: int
    sources_completed: int
    sources_bounded: int
    sources_skipped: int
    sources_unsupported: int
    sources_failed: int
    sources_cancelled: int
    records_seen: int
    matches_seen: int
    conversations_eligible: int = 0
    conversations_selected: int = 0
    conversations_completed: int = 0
    source_stop_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject coverage that loses or double-terminalizes a started source."""
        counts = (
            self.sources_discovered,
            self.sources_eligible,
            self.sources_planned,
            self.sources_attempted,
            self.sources_completed,
            self.sources_bounded,
            self.sources_skipped,
            self.sources_unsupported,
            self.sources_failed,
            self.sources_cancelled,
            self.records_seen,
            self.matches_seen,
            self.conversations_eligible,
            self.conversations_selected,
            self.conversations_completed,
        )
        if any(count < 0 for count in counts):
            msg = "coverage counts must be non-negative"
            raise ValueError(msg)
        if not (
            self.sources_discovered
            >= self.sources_eligible
            >= self.sources_planned
            >= self.sources_attempted
        ):
            msg = "source coverage must narrow from discovered to attempted"
            raise ValueError(msg)
        if self.sources_attempted + self.sources_skipped != self.sources_planned:
            msg = "attempted plus skipped sources must equal planned sources"
            raise ValueError(msg)
        terminal_sources = (
            self.sources_completed
            + self.sources_bounded
            + self.sources_unsupported
            + self.sources_failed
            + self.sources_cancelled
        )
        if terminal_sources != self.sources_attempted:
            msg = "terminal source counts must equal attempted sources"
            raise ValueError(msg)
        if self.matches_seen > self.records_seen:
            msg = "matches_seen must not exceed records_seen"
            raise ValueError(msg)
        if not (
            self.conversations_eligible
            >= self.conversations_selected
            >= self.conversations_completed
        ):
            msg = "conversation coverage must narrow from eligible to completed"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class SearchRequestPatch:
    """Bounded changes for starting a related search request.

    Attributes
    ----------
    effort : SearchEffort | None
        Replacement effort, or ``None`` to preserve it.
    scope : SearchScope | None
        Replacement scope, or ``None`` to preserve it.
    conversation_limit : int | None
        Replacement targeted-conversation cap.
    """

    effort: SearchEffort | None = None
    scope: SearchScope | None = None
    conversation_limit: int | None = None


@dataclass(frozen=True, slots=True)
class NextAction:
    """One engine-authored follow-up action.

    Attributes
    ----------
    action_id : str
        Stable identity used by frontends to reject stale actions.
    kind : str
        Extensible action kind.
    label : str
        Short human label.
    reason : str
        Privacy-safe explanation for offering the action.
    patch : SearchRequestPatch
        Validated changes applied to the current normalized request.
    requires_confirmation : bool
        Whether the patch broadens an explicitly selected scope.
    """

    action_id: str
    kind: str
    label: str
    reason: str
    patch: SearchRequestPatch
    requires_confirmation: bool = False


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Terminal engine evidence consumed by every result sink.

    Attributes
    ----------
    request : NormalizedSearchRequest
        Request values the engine applied.
    requested_effort : SearchEffort
        Effort requested after normalization.
    completed_effort : SearchEffort | None
        Highest effort completed without a coverage failure.
    status : RunStatus
        Primary state and retained secondary conditions.
    outcome : SearchOutcome
        Match or distinguishable empty outcome.
    coverage : RunCoverage
        Aggregate source, record, and conversation evidence.
    diagnostics : tuple[RunDiagnostic, ...]
        Privacy-safe warnings and errors.
    next_actions : tuple[NextAction, ...]
        Follow-up request patches authored by the engine.
    match_count : int
        Deduplicated records emitted by the engine.
    elapsed_seconds : float
        Monotonic execution duration.
    applied_order : str
        Result order applied by the collector.
    limit : int | None
        Requested result cap.
    """

    request: NormalizedSearchRequest
    requested_effort: SearchEffort
    completed_effort: SearchEffort | None
    status: RunStatus
    outcome: SearchOutcome
    coverage: RunCoverage
    diagnostics: tuple[RunDiagnostic, ...]
    next_actions: tuple[NextAction, ...]
    match_count: int
    elapsed_seconds: float
    applied_order: str
    limit: int | None


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Validated records plus their engine-owned terminal summary.

    Attributes
    ----------
    records : tuple[SearchRecord, ...]
        Unique records emitted by the search event stream.
    summary : RunSummary
        Terminal request, status, outcome, coverage, diagnostics, and actions.
    """

    records: tuple[SearchRecord, ...]
    summary: RunSummary


def apply_search_request_patch(
    query: SearchQuery,
    patch: SearchRequestPatch,
) -> SearchQuery:
    """Apply one engine-authored follow-up patch to an existing request.

    Targeted conversation caps are request-local: they are retained while
    targeted effort remains active and cleared when a follow-up escalates to
    exhaustive effort.
    """
    effort_value = query.effort if patch.effort is None else patch.effort
    if effort_value is None:
        effort_value = "prompt" if query.scope == "prompts" else "exhaustive"
    if effort_value not in {"prompt", "targeted", "exhaustive"}:
        msg = f"unsupported search effort {effort_value!r}"
        raise ValueError(msg)
    effort = effort_value
    scope = query.scope if patch.scope is None else patch.scope
    conversation_limit = None
    if effort == "targeted":
        conversation_limit = (
            query.conversation_limit
            if patch.conversation_limit is None
            else patch.conversation_limit
        )
    return replace(
        query,
        effort=effort,
        scope=scope,
        conversation_limit=conversation_limit,
    )


def _depth_actions(
    from_effort: SearchEffort,
    *,
    scope_provenance: SearchScopeProvenance,
) -> tuple[NextAction, ...]:
    """Return the depth escalations reachable from one effort rung.

    Parameters
    ----------
    from_effort : SearchEffort
        Effort the escalation starts from.
    scope_provenance : SearchScopeProvenance
        Whether the request's scope was inferred or explicitly selected. An
        explicit scope makes a broadening escalation require confirmation.

    Returns
    -------
    tuple[NextAction, ...]
        Ordered escalations, cheapest first, or ``()`` at the top rung.
    """
    if from_effort == "prompt":
        requires_confirmation = scope_provenance == "explicit"
        kind = "search.broaden_scope" if requires_confirmation else "search.escalate_effort"
        reason = "Prompt search does not read conversation bodies."
        return (
            NextAction(
                action_id="search.targeted",
                kind=kind,
                label="Deep search",
                reason=reason,
                patch=SearchRequestPatch(
                    effort="targeted",
                    scope="all",
                    conversation_limit=DEFAULT_TARGETED_CONVERSATION_LIMIT,
                ),
                requires_confirmation=requires_confirmation,
            ),
            NextAction(
                action_id="search.exhaustive",
                kind=kind,
                label="Search all conversations",
                reason=reason,
                patch=SearchRequestPatch(effort="exhaustive", scope="all"),
                requires_confirmation=requires_confirmation,
            ),
        )
    if from_effort == "targeted":
        return (
            NextAction(
                action_id="search.exhaustive",
                kind="search.escalate_effort",
                label="Search all conversations",
                reason="Targeted search can omit conversations.",
                patch=SearchRequestPatch(effort="exhaustive"),
            ),
        )
    return ()


def offered_depth_actions(query: SearchQuery) -> tuple[NextAction, ...]:
    """Return the depth escalations the engine offers for an unrun request.

    :func:`build_search_summary` publishes the same vocabulary *after* a run.
    This is the pre-run view of it, so a frontend can present engine-authored
    depth choices for a request that has not started yet. An offer describes
    only what a later run would read; it asserts nothing about coverage.

    Parameters
    ----------
    query : SearchQuery
        Request whose effort rung the offer starts from. An unset effort is
        derived from the scope exactly as the planner derives it.

    Returns
    -------
    tuple[NextAction, ...]
        Ordered escalations, cheapest first, or ``()`` at the top rung.

    Examples
    --------
    >>> from agentgrep.records import SearchQuery
    >>> query = SearchQuery(
    ...     terms=("deploy",),
    ...     scope="prompts",
    ...     any_term=False,
    ...     regex=False,
    ...     case_sensitive=False,
    ...     agents=(),
    ...     limit=None,
    ... )
    >>> [action.action_id for action in offered_depth_actions(query)]
    ['search.targeted', 'search.exhaustive']
    >>> offered_depth_actions(replace(query, scope="all", effort="exhaustive"))
    ()
    """
    effort = query.effort or ("prompt" if query.scope == "prompts" else "exhaustive")
    return _depth_actions(effort, scope_provenance=query.scope_provenance)


def normalize_search_request(
    query: SearchQuery,
    *,
    effort: SearchEffort,
) -> NormalizedSearchRequest:
    """Return the serializable request values applied by the engine."""
    conversation_limit = None
    if effort == "targeted":
        conversation_limit = (
            DEFAULT_TARGETED_CONVERSATION_LIMIT
            if query.conversation_limit is None
            else query.conversation_limit
        )
    return NormalizedSearchRequest(
        terms=query.terms,
        scope=query.scope,
        scope_provenance=query.scope_provenance,
        effort=effort,
        agents=query.agents,
        limit=query.limit,
        conversation_limit=conversation_limit,
        dedupe=query.dedupe,
        case_sensitive=query.case_sensitive,
        order=query.order,
        match_surface=query.match_surface,
    )


def build_search_summary(
    query: SearchQuery,
    *,
    effort: SearchEffort,
    coverage: RunCoverage,
    match_count: int,
    elapsed_seconds: float,
    answer_now: bool = False,
    cancelled: bool = False,
    failed: bool = False,
    truncated: bool = False,
    approximate: bool = False,
    result_limit_reached: bool = False,
    diagnostics: tuple[RunDiagnostic, ...] = (),
) -> RunSummary:
    """Build one terminal summary from execution evidence."""
    conditions: list[str] = []
    run_diagnostics = list(diagnostics)
    if coverage.sources_unsupported:
        conditions.append("unsupported_source")
        run_diagnostics.append(
            RunDiagnostic(
                code="unsupported_source",
                message="One or more planned sources have no registered adapter.",
                severity="error",
            ),
        )
    if coverage.sources_failed:
        conditions.append("source_failure")
        run_diagnostics.append(
            RunDiagnostic(
                code="source_failure",
                message="One or more planned sources could not be read.",
                severity="error",
            ),
        )
    if failed and not coverage.sources_failed and not coverage.sources_unsupported:
        conditions.append("engine_failure")
        run_diagnostics.append(
            RunDiagnostic(
                code="engine_failure",
                message="The execution engine did not finish cleanly.",
                severity="error",
            ),
        )
    if cancelled:
        conditions.append("cancelled")
    if truncated:
        conditions.append("response_truncated")
    if answer_now:
        conditions.append("answer_now")
    if result_limit_reached:
        conditions.append("result_limit")
    for stop_reason in coverage.source_stop_reasons:
        if (
            stop_reason not in {"failure_cleanup", "source_failure", "unsupported_adapter"}
            and stop_reason not in conditions
        ):
            conditions.append(stop_reason)
    approximate_reason = (
        "heuristic_candidate_selection" if effort == "targeted" else "approximate_execution"
    )
    if approximate or effort == "targeted":
        conditions.append(approximate_reason)

    if failed or coverage.sources_failed or coverage.sources_unsupported:
        status = RunStatus("failed", reason=conditions[0], conditions=tuple(conditions))
    elif cancelled:
        status = RunStatus("cancelled", reason="cancelled", conditions=tuple(conditions))
    elif truncated:
        status = RunStatus(
            "truncated",
            reason="response_truncated",
            conditions=tuple(conditions),
        )
    elif approximate or effort == "targeted":
        status = RunStatus(
            "approximate",
            reason=approximate_reason,
            conditions=tuple(conditions),
        )
    elif (
        answer_now
        or "result_limit" in conditions
        or coverage.sources_bounded
        or (query.limit is not None and coverage.sources_skipped)
    ):
        status = RunStatus(
            "bounded",
            reason=(
                "answer_now"
                if answer_now
                else "result_limit"
                if "result_limit" in conditions
                else next(
                    (
                        reason
                        for reason in coverage.source_stop_reasons
                        if reason != "failure_cleanup"
                    ),
                    "bounded_execution",
                )
            ),
            conditions=tuple(conditions),
        )
    else:
        status = RunStatus("complete")

    incomplete_coverage = bool(
        answer_now
        or approximate
        or coverage.sources_bounded
        or coverage.sources_cancelled
        or (query.limit is not None and coverage.sources_skipped)
    )
    if status.state in {"failed", "cancelled", "truncated"} or incomplete_coverage:
        outcome: SearchOutcome = (
            "matches"
            if approximate and match_count and status.state == "approximate"
            else "undetermined"
        )
        completed_effort = None
    elif effort == "targeted":
        completed_effort = effort
        if match_count:
            outcome = "matches"
        elif coverage.conversations_selected == 0:
            outcome = "no_candidate_conversation"
        else:
            outcome = "no_selected_conversation_match"
    elif match_count:
        outcome = "matches"
        completed_effort = effort
    elif effort == "prompt":
        outcome = "no_prompt_match"
        completed_effort = effort
    elif effort == "exhaustive":
        outcome = "no_exhaustive_match"
        completed_effort = effort
    else:
        outcome = "no_selected_conversation_match"
        completed_effort = effort

    next_actions: tuple[NextAction, ...] = ()
    if (
        effort == "prompt"
        and completed_effort == "prompt"
        and status.state in {"complete", "bounded"}
    ):
        next_actions = _depth_actions(
            "prompt",
            scope_provenance=query.scope_provenance,
        )
    elif effort == "targeted" and completed_effort == "targeted":
        next_actions = _depth_actions(
            "targeted",
            scope_provenance=query.scope_provenance,
        )

    return RunSummary(
        request=normalize_search_request(query, effort=effort),
        requested_effort=effort,
        completed_effort=completed_effort,
        status=status,
        outcome=outcome,
        coverage=coverage,
        diagnostics=tuple(run_diagnostics),
        next_actions=next_actions,
        match_count=match_count,
        elapsed_seconds=elapsed_seconds,
        applied_order=query.order,
        limit=query.limit,
    )


__all__ = [
    "DiagnosticSeverity",
    "NextAction",
    "NormalizedSearchRequest",
    "RunCoverage",
    "RunDiagnostic",
    "RunState",
    "RunStatus",
    "RunSummary",
    "SearchOutcome",
    "SearchRequestPatch",
    "SearchResult",
    "apply_search_request_patch",
    "build_search_summary",
    "normalize_search_request",
    "offered_depth_actions",
]
