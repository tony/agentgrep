"""Request-local prompt-guided routing for targeted search.

The router consumes compact prompt-history evidence and resolves only a
bounded set of proof-bound conversation locators. Its private rank selects
work; it never establishes, scores, orders, or emits a final search result.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import json
import os
import pathlib
import typing as t
import uuid

from agentgrep._engine.matching import compile_record_matcher
from agentgrep.adapters import iter_source_records, store_role_for_record
from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery, SearchRecord, SourceHandle
from agentgrep.results import RunDiagnostic
from agentgrep.stores import StoreCoverage, StoreRole

if t.TYPE_CHECKING:
    from agentgrep.records import AgentName

type RoutingAttemptOutcome = t.Literal[
    "resolved",
    "unresolved",
    "ambiguous",
    "budget_exhausted",
]

_TARGETED_LOCATOR_ENTRY_LIMIT = 16_384
"""Maximum directory entries examined by one targeted locator decision."""


@dataclasses.dataclass(frozen=True, order=True, slots=True)
class ConversationKey:
    """Private adapter-proven conversation equality key.

    Attributes
    ----------
    agent : AgentName
        Agent namespace that owns the native identifier.
    provider : str
        Prompt adapter whose storage contract proves the identifier.
    native_id : str
        Canonical provider-native conversation identifier.
    """

    agent: AgentName
    provider: str
    native_id: str


@dataclasses.dataclass(frozen=True, slots=True)
class ConversationCandidate:
    """One grouped prompt-evidence candidate.

    Attributes
    ----------
    key : ConversationKey
        Proven grouping identity.
    prompt_source : SourceHandle
        Prompt-history observation that supplied the locator.
    evidence_timestamp : str
        Newest matching prompt timestamp, used only for deterministic work order.
    """

    key: ConversationKey
    prompt_source: SourceHandle
    evidence_timestamp: str


@dataclasses.dataclass(frozen=True, slots=True)
class RoutingAttempt:
    """Resolution outcome for one selected conversation.

    Attributes
    ----------
    key : ConversationKey
        Selected private conversation identity.
    outcome : RoutingAttemptOutcome
        Whether the locator resolved uniquely.
    source : SourceHandle | None
        Exact transcript source for a resolved attempt.
    """

    key: ConversationKey
    outcome: RoutingAttemptOutcome
    source: SourceHandle | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class _CandidateResolutionBatch:
    """Resolved sources plus candidates whose locator work exhausted its budget."""

    sources: dict[ConversationKey, tuple[SourceHandle, ...]]
    budget_exhausted: frozenset[ConversationKey] = frozenset()


@dataclasses.dataclass(slots=True)
class _LocatorEntryBudget:
    """Request-local directory-entry budget shared by locator providers."""

    remaining: int

    def consume(self) -> bool:
        """Consume one directory entry, returning whether work may continue."""
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


@dataclasses.dataclass(frozen=True, slots=True)
class TargetedRoutingPlan:
    """Fixed bounded decision for one targeted request.

    Attributes
    ----------
    policy : str
        Versioned deterministic routing policy name.
    candidates_eligible : int
        Distinct proof-bearing candidates before the request bound.
    candidates_selected : int
        Candidates that consumed a conversation slot.
    attempts : tuple[RoutingAttempt, ...]
        Resolution outcome for every selected candidate.
    sources : tuple[SourceHandle, ...]
        Uniquely resolved transcript sources.
    evidence_sources_failed : int
        Prompt-evidence sources that could not be read for routing.
    diagnostics : tuple[RunDiagnostic, ...]
        Privacy-safe evidence and locator gap summaries.
    """

    policy: str
    candidates_eligible: int
    candidates_selected: int
    attempts: tuple[RoutingAttempt, ...]
    sources: tuple[SourceHandle, ...]
    evidence_sources_failed: int = 0
    diagnostics: tuple[RunDiagnostic, ...] = ()

    @property
    def source_paths(self) -> frozenset[pathlib.Path]:
        """Return exact resolved paths used to count completed conversations."""
        return frozenset(source.path for source in self.sources)


def build_targeted_routing_plan(
    query: SearchQuery,
    prompt_sources: cabc.Iterable[SourceHandle],
    *,
    conversation_limit: int,
    control: SearchControl | None = None,
) -> TargetedRoutingPlan:
    """Build a deterministic bounded conversation decision from prompt evidence.

    Parameters
    ----------
    query : SearchQuery
        Original query. Its matcher remains untouched for final execution.
    prompt_sources : Iterable[SourceHandle]
        Dedicated prompt-history sources discovered for the request.
    conversation_limit : int
        Positive distinct-conversation attempt bound.
    control : SearchControl | None
        Cooperative cancellation checked between prompt records and resolutions.

    Returns
    -------
    TargetedRoutingPlan
        Private fixed routing decision and privacy-safe gap diagnostics.
    """
    if conversation_limit < 1:
        msg = "conversation_limit must be greater than 0"
        raise ValueError(msg)
    active_control = SearchControl() if control is None else control
    compiled = query.compiled
    routing_terms = query.terms if compiled is None else compiled.routing_terms
    if not routing_terms and not (compiled is not None and compiled.has_positive_routing_metadata):
        return TargetedRoutingPlan(
            policy="lexical.prompt.v1",
            candidates_eligible=0,
            candidates_selected=0,
            attempts=(),
            sources=(),
            diagnostics=(
                RunDiagnostic(
                    code="targeted_no_positive_clue",
                    message="The query contains no positive prompt-routing clue.",
                    severity="info",
                ),
            ),
        )

    evidence_query = dataclasses.replace(
        query,
        terms=routing_terms,
        scope="prompts",
        any_term=True,
        limit=None,
        dedupe=False,
        compiled=None,
        match_surface="text",
        effort="prompt",
        conversation_limit=None,
    )
    evidence_matcher = compile_record_matcher(evidence_query)
    candidates: dict[ConversationKey, ConversationCandidate] = {}
    unroutable_matches = 0
    failed_sources = 0
    for source in prompt_sources:
        if active_control.answer_now_requested():
            break
        if store_role_for_record(source.store, source.adapter_id) != StoreRole.PROMPT_HISTORY:
            continue
        try:
            records = iter_source_records(source)
            for record in records:
                if active_control.answer_now_requested():
                    break
                if not evidence_matcher.matches(record):
                    continue
                if (
                    compiled is not None
                    and compiled.routing_predicate is not None
                    and not compiled.routing_predicate(record)
                ):
                    continue
                candidate = _candidate_from_record(source, record)
                if candidate is None:
                    unroutable_matches += 1
                    continue
                previous = candidates.get(candidate.key)
                if previous is None or _candidate_order(candidate) > _candidate_order(previous):
                    candidates[candidate.key] = candidate
        except OSError, UnicodeError, json.JSONDecodeError:
            failed_sources += 1

    ordered = sorted(candidates.values(), key=_candidate_order, reverse=True)
    selected = ordered[:conversation_limit]
    attempts = list(
        _resolve_candidates(
            selected,
            control=active_control,
        ),
    )
    sources = [attempt.source for attempt in attempts if attempt.source is not None]

    diagnostics: list[RunDiagnostic] = []
    unresolved = sum(attempt.outcome == "unresolved" for attempt in attempts)
    ambiguous = sum(attempt.outcome == "ambiguous" for attempt in attempts)
    budget_exhausted = sum(attempt.outcome == "budget_exhausted" for attempt in attempts)
    if unresolved:
        diagnostics.append(
            RunDiagnostic(
                code="targeted_locator_unresolved",
                message="One or more selected conversation locators were unavailable.",
            ),
        )
    if ambiguous:
        diagnostics.append(
            RunDiagnostic(
                code="targeted_locator_ambiguous",
                message="One or more selected conversation locators were ambiguous.",
            ),
        )
    if budget_exhausted:
        diagnostics.append(
            RunDiagnostic(
                code="targeted_locator_budget_exhausted",
                message=(
                    "One or more selected conversation locators exceeded the "
                    "request-local entry budget."
                ),
            ),
        )
    if unroutable_matches:
        diagnostics.append(
            RunDiagnostic(
                code="targeted_evidence_unroutable",
                message="Some matching prompt evidence had no proof-bound conversation locator.",
                severity="info",
            ),
        )
    if failed_sources:
        diagnostics.append(
            RunDiagnostic(
                code="targeted_evidence_failure",
                message="One or more prompt-evidence sources could not be read for routing.",
                severity="error",
            ),
        )
    return TargetedRoutingPlan(
        policy="lexical.prompt.v1",
        candidates_eligible=len(ordered),
        candidates_selected=len(selected),
        attempts=tuple(attempts),
        sources=tuple(sources),
        evidence_sources_failed=failed_sources,
        diagnostics=tuple(diagnostics),
    )


def _candidate_order(candidate: ConversationCandidate) -> tuple[str, ConversationKey]:
    """Return deterministic newest-evidence-first routing order material."""
    return (candidate.evidence_timestamp, candidate.key)


def _candidate_from_record(
    source: SourceHandle,
    record: SearchRecord,
) -> ConversationCandidate | None:
    """Build a proof-bearing candidate for supported prompt adapters."""
    supported = {
        "codex.history_jsonl.v1",
        "claude.history_jsonl.v1",
        "grok.prompt_history_jsonl.v1",
        "antigravity_cli.history_jsonl.v1",
    }
    if source.adapter_id not in supported:
        return None
    native_id = _canonical_uuid(record.conversation_id or record.session_id)
    if native_id is None:
        return None
    return ConversationCandidate(
        key=ConversationKey(
            agent=source.agent,
            provider=source.adapter_id,
            native_id=native_id,
        ),
        prompt_source=source,
        evidence_timestamp=record.timestamp or "",
    )


def _canonical_uuid(value: str | None) -> str | None:
    """Return a canonical UUID string or ``None`` for an unsafe locator value."""
    if value is None:
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError, AttributeError:
        return None
    return str(parsed)


def _resolve_candidate(candidate: ConversationCandidate) -> RoutingAttempt:
    """Resolve one selected locator through its owning adapter contract."""
    resolver = _RESOLVERS.get(candidate.key.provider)
    if resolver is None:
        return RoutingAttempt(candidate.key, "unresolved")
    sources = resolver(candidate)
    if not sources:
        return RoutingAttempt(candidate.key, "unresolved")
    if len(sources) != 1:
        return RoutingAttempt(candidate.key, "ambiguous")
    return RoutingAttempt(candidate.key, "resolved", sources[0])


def _resolve_candidates(
    candidates: cabc.Sequence[ConversationCandidate],
    *,
    control: SearchControl,
) -> tuple[RoutingAttempt, ...]:
    """Resolve selected locators with at most one corpus walk per provider root."""
    batch_cache: dict[str, _CandidateResolutionBatch] = {}
    entry_budget = _LocatorEntryBudget(_TARGETED_LOCATOR_ENTRY_LIMIT)
    attempts: list[RoutingAttempt] = []
    for candidate in candidates:
        if control.answer_now_requested():
            break
        provider = candidate.key.provider
        if provider == "codex.history_jsonl.v1":
            resolved = batch_cache.get(provider)
            if resolved is None:
                resolved = _resolve_codex_candidates(
                    candidates,
                    control=control,
                    entry_budget=entry_budget,
                )
                batch_cache[provider] = resolved
            if control.answer_now_requested():
                break
            attempts.append(_attempt_from_batch(candidate, resolved))
            continue
        if provider == "claude.history_jsonl.v1":
            resolved = batch_cache.get(provider)
            if resolved is None:
                resolved = _resolve_claude_candidates(
                    candidates,
                    control=control,
                    entry_budget=entry_budget,
                )
                batch_cache[provider] = resolved
            if control.answer_now_requested():
                break
            attempts.append(_attempt_from_batch(candidate, resolved))
            continue
        attempts.append(_resolve_candidate(candidate))
    return tuple(attempts)


def _attempt_from_batch(
    candidate: ConversationCandidate,
    batch: _CandidateResolutionBatch,
) -> RoutingAttempt:
    """Convert one bounded provider batch into a typed attempt."""
    if candidate.key in batch.budget_exhausted:
        return RoutingAttempt(candidate.key, "budget_exhausted")
    return _attempt_from_sources(candidate, batch.sources.get(candidate.key, ()))


def _attempt_from_sources(
    candidate: ConversationCandidate,
    sources: tuple[SourceHandle, ...],
) -> RoutingAttempt:
    """Convert one resolver result set into a typed attempt outcome."""
    if not sources:
        return RoutingAttempt(candidate.key, "unresolved")
    if len(sources) != 1:
        return RoutingAttempt(candidate.key, "ambiguous")
    return RoutingAttempt(candidate.key, "resolved", sources[0])


def _bounded_files(
    root: pathlib.Path,
    *,
    control: SearchControl,
    entry_budget: _LocatorEntryBudget,
    max_depth: int | None = None,
) -> tuple[pathlib.Path, ...] | None:
    """Return every JSONL file under ``root`` or ``None`` on budget exhaustion."""
    pending: list[tuple[pathlib.Path, int]] = [(root, 0)]
    matched: list[pathlib.Path] = []
    while pending:
        if control.answer_now_requested():
            return ()
        directory, depth = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if control.answer_now_requested():
                    return ()
                if not entry_budget.consume():
                    return None
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError:
                    continue
                path = pathlib.Path(entry.path)
                if is_directory:
                    if max_depth is None or depth + 1 < max_depth:
                        pending.append((path, depth + 1))
                    continue
                if is_file and path.suffix == ".jsonl":
                    matched.append(path)
    return tuple(matched)


def _source_handle(
    path: pathlib.Path,
    *,
    agent: AgentName,
    store: str,
    adapter_id: str,
    source_kind: t.Literal["json", "jsonl", "sqlite"],
    coverage: StoreCoverage = StoreCoverage.DEFAULT_SEARCH,
) -> SourceHandle | None:
    """Build a normalized handle only for one existing regular file."""
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return SourceHandle(
        agent=agent,
        store=store,
        adapter_id=adapter_id,
        path=path,
        path_kind="sqlite_db" if source_kind == "sqlite" else "session_file",
        source_kind=source_kind,
        search_root=path.parent,
        mtime_ns=stat.st_mtime_ns,
        coverage=coverage,
    )


def _first_jsonl_mappings(
    path: pathlib.Path,
    *,
    limit: int = 8,
) -> cabc.Iterator[dict[str, object]]:
    """Yield a bounded prefix of JSON object lines for locator verification."""
    seen = 0
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if seen >= limit:
                return
            stripped = line.strip()
            if not stripped:
                continue
            seen += 1
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield t.cast("dict[str, object]", value)


def _resolve_codex_candidates(
    candidates: cabc.Sequence[ConversationCandidate],
    *,
    control: SearchControl,
    entry_budget: _LocatorEntryBudget,
) -> _CandidateResolutionBatch:
    """Resolve selected Codex ids in one cancellable walk per sessions root."""
    grouped: dict[pathlib.Path, dict[str, ConversationKey]] = {}
    for candidate in candidates:
        if candidate.key.provider != "codex.history_jsonl.v1":
            continue
        root = candidate.prompt_source.path.parent / "sessions"
        grouped.setdefault(root, {})[candidate.key.native_id] = candidate.key

    found: dict[ConversationKey, list[SourceHandle]] = {}
    grouped_roots = tuple(grouped.items())
    for root_index, (root, targets) in enumerate(grouped_roots):
        if control.answer_now_requested():
            break
        if not root.is_dir():
            continue
        paths = _bounded_files(
            root,
            control=control,
            entry_budget=entry_budget,
        )
        if paths is None:
            return _CandidateResolutionBatch(
                sources={key: tuple(sources) for key, sources in found.items()},
                budget_exhausted=frozenset(
                    key
                    for _, pending_targets in grouped_roots[root_index:]
                    for key in pending_targets.values()
                ),
            )
        for path in paths:
            if control.answer_now_requested():
                break
            native_id = _canonical_uuid(path.stem[-36:])
            if native_id is None or not path.stem.endswith(f"-{native_id}"):
                continue
            key = targets.get(native_id)
            if key is None:
                continue
            try:
                first = next(_first_jsonl_mappings(path, limit=1), None)
            except OSError, UnicodeError:
                first = None
            if first is None or first.get("type") != "session_meta":
                continue
            payload = first.get("payload")
            if not isinstance(payload, dict) or payload.get("id") != native_id:
                continue
            source = _source_handle(
                path,
                agent="codex",
                store="codex.sessions",
                adapter_id="codex.sessions_jsonl.v1",
                source_kind="jsonl",
            )
            if source is not None:
                found.setdefault(key, []).append(source)
    return _CandidateResolutionBatch(
        sources={key: tuple(sources) for key, sources in found.items()},
    )


def _resolve_claude_candidates(
    candidates: cabc.Sequence[ConversationCandidate],
    *,
    control: SearchControl,
    entry_budget: _LocatorEntryBudget,
) -> _CandidateResolutionBatch:
    """Resolve selected Claude ids in one cancellable walk per projects root."""
    grouped: dict[pathlib.Path, dict[str, ConversationKey]] = {}
    for candidate in candidates:
        if candidate.key.provider != "claude.history_jsonl.v1":
            continue
        root = candidate.prompt_source.path.parent / "projects"
        grouped.setdefault(root, {})[candidate.key.native_id] = candidate.key

    found: dict[ConversationKey, list[SourceHandle]] = {}
    grouped_roots = tuple(grouped.items())
    for root_index, (root, targets) in enumerate(grouped_roots):
        if control.answer_now_requested():
            break
        if not root.is_dir():
            continue
        paths = _bounded_files(
            root,
            control=control,
            entry_budget=entry_budget,
            max_depth=2,
        )
        if paths is None:
            return _CandidateResolutionBatch(
                sources={key: tuple(sources) for key, sources in found.items()},
                budget_exhausted=frozenset(
                    key
                    for _, pending_targets in grouped_roots[root_index:]
                    for key in pending_targets.values()
                ),
            )
        for path in paths:
            if control.answer_now_requested():
                break
            key = targets.get(path.stem)
            if key is None:
                continue
            try:
                mappings = tuple(_first_jsonl_mappings(path))
            except OSError, UnicodeError:
                continue
            if not any(mapping.get("sessionId") == key.native_id for mapping in mappings):
                continue
            source = _source_handle(
                path,
                agent="claude",
                store="claude.projects",
                adapter_id="claude.projects_jsonl.v1",
                source_kind="jsonl",
            )
            if source is not None:
                found.setdefault(key, []).append(source)
    return _CandidateResolutionBatch(
        sources={key: tuple(sources) for key, sources in found.items()},
    )


def _resolve_grok(candidate: ConversationCandidate) -> tuple[SourceHandle, ...]:
    """Resolve a Grok transcript relative to its prompt-history project."""
    path = candidate.prompt_source.path.parent / candidate.key.native_id / "chat_history.jsonl"
    source = _source_handle(
        path,
        agent="grok",
        store="grok.sessions",
        adapter_id="grok.sessions_jsonl.v1",
        source_kind="jsonl",
    )
    return () if source is None else (source,)


def _resolve_antigravity(
    candidate: ConversationCandidate,
) -> tuple[SourceHandle, ...]:
    """Resolve the preferred readable Antigravity conversation representation."""
    root = candidate.prompt_source.path.parent
    native_id = candidate.key.native_id
    transcript = root / "brain" / native_id / ".system_generated" / "logs" / "transcript_full.jsonl"
    transcript_source = _source_handle(
        transcript,
        agent="antigravity-cli",
        store="antigravity-cli.transcript",
        adapter_id="antigravity_cli.transcript_jsonl.v1",
        source_kind="jsonl",
        coverage=StoreCoverage.INSPECTABLE,
    )
    if transcript_source is not None:
        return (transcript_source,)
    database_source = _source_handle(
        root / "conversations" / f"{native_id}.db",
        agent="antigravity-cli",
        store="antigravity-cli.conversations",
        adapter_id="antigravity_cli.conversations_sqlite_protobuf.v1",
        source_kind="sqlite",
        coverage=StoreCoverage.INSPECTABLE,
    )
    return () if database_source is None else (database_source,)


type _Resolver = cabc.Callable[[ConversationCandidate], tuple[SourceHandle, ...]]
_RESOLVERS: dict[str, _Resolver] = {
    "grok.prompt_history_jsonl.v1": _resolve_grok,
    "antigravity_cli.history_jsonl.v1": _resolve_antigravity,
}


__all__ = ["TargetedRoutingPlan", "build_targeted_routing_plan"]
