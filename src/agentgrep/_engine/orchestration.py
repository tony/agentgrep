"""Search/find orchestration and record matching, repatriated from the facade.

The query-execution entry points (search_sources, run_search_query,
plan_search_sources, collect_*), the grep-shaped subprocess helpers, and the
matching / haystack / dedupe layer. This is the engine logic ADR 0004 says the
engine owns, so it lives in the _engine package rather than the facade. Depends
on records, readers, adapters, discovery, progress, and the rest of _engine.
See ADR 0010.
"""

from __future__ import annotations

import collections.abc as cabc
import pathlib
import re
import time
import typing as t

from agentgrep.adapters import store_role_for_record
from agentgrep.discovery import discover_sources
from agentgrep.progress import SearchControl, SearchProgress, noop_search_progress
from agentgrep.readers import (
    _record_engine_profile_sample,
    read_text_file,
    run_readonly_command,
    select_backends,
)
from agentgrep.records import (
    CONVERSATION_STORE_ROLES,
    JSON_FILE_SUFFIXES,
    AgentName,
    BackendSelection,
    DiscoveryVersionDetail,
    FindRecord,
    JSONScalar,
    SearchMatchSurface,
    SearchQuery,
    SearchRecord,
    SearchScope,
    SourceHandle,
)
from agentgrep.stores import StoreRole

if t.TYPE_CHECKING:
    from agentgrep._engine.planning import PhysicalSearchPlan
    from agentgrep._engine.runtime import SearchRuntime


def search_sources(
    query: SearchQuery,
    sources: list[SourceHandle],
    backends: BackendSelection,
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
    runtime: SearchRuntime | None = None,
) -> list[SearchRecord]:
    """Parse and filter search results across all selected sources."""
    active_progress = noop_search_progress() if progress is None else progress
    active_control = SearchControl() if control is None else control
    # Apply the compiled-query source predicate before planning so the
    # ripgrep prefilter (which is the heavy step in
    # ``plan_search_sources``) runs on the smaller set. Without this
    # the per-file prefilter runs against every discovered source even
    # when ``agent:codex`` could rule most out from metadata alone.
    if query.compiled is not None and query.compiled.source_predicate is not None:
        sources = [s for s in sources if query.compiled.source_predicate(s)]
    from agentgrep._engine.planning import build_physical_search_plan

    plan = build_physical_search_plan(
        query,
        sources,
        backends,
        progress=active_progress,
        control=active_control,
    )
    if active_control.answer_now_requested():
        active_progress.answer_now(0)
        return []
    active_progress.sources_planned(len(plan.tasks), len(sources))
    records = collect_search_records_from_plan(
        query,
        plan,
        progress=active_progress,
        control=active_control,
        runtime=runtime,
    )
    if active_control.answer_now_requested():
        active_progress.answer_now(len(records))
    else:
        active_progress.finish(len(records))
    return records


def run_search_query(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    backends: BackendSelection | None = None,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
    runtime: SearchRuntime | None = None,
) -> list[SearchRecord]:
    """Discover sources and run a normalized search query."""
    active_backends = select_backends() if backends is None else backends
    active_progress = noop_search_progress() if progress is None else progress
    active_control = SearchControl() if control is None else control
    active_progress.start(query)
    interrupted = False
    try:
        sources = discover_sources_for_search(
            home,
            query,
            active_backends,
            version_detail="none",
        )
        active_progress.sources_discovered(len(sources))
        return search_sources(
            query,
            sources,
            active_backends,
            progress=active_progress,
            control=active_control,
            runtime=runtime,
        )
    except KeyboardInterrupt:
        interrupted = True
        active_progress.interrupt()
        raise
    finally:
        if not interrupted:
            active_progress.close()


def plan_search_sources(
    query: SearchQuery,
    sources: list[SourceHandle],
    backends: BackendSelection,
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
) -> list[SourceHandle]:
    """Return the candidate sources to parse for a search query."""
    from agentgrep._engine.planning import build_physical_search_plan

    plan = build_physical_search_plan(
        query,
        sources,
        backends,
        progress=progress,
        control=control,
    )
    return [task.source for task in plan.tasks]


def source_order_key(source: SourceHandle) -> tuple[int, str]:
    """Return a newest-first search order key for sources."""
    return (-source.mtime_ns, str(source.path))


def _source_profile_attributes(source: SourceHandle) -> dict[str, JSONScalar]:
    """Return privacy-safe profiler attributes for a source handle."""
    return {
        "agentgrep_agent": source.agent,
        "agentgrep_store": source.store,
        "agentgrep_adapter_id": source.adapter_id,
        "agentgrep_path_kind": source.path_kind,
        "agentgrep_source_kind": source.source_kind,
    }


def prefilter_sources_by_root(
    query: SearchQuery,
    sources: list[SourceHandle],
    grep_program: str,
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
) -> list[SourceHandle]:
    """Prefilter file-backed sources by searching each root once."""
    active_progress = noop_search_progress() if progress is None else progress
    active_control = SearchControl() if control is None else control
    matched_paths_by_root: dict[pathlib.Path, set[pathlib.Path] | None] = {}
    filtered_sources: list[SourceHandle] = []
    for source in sources:
        if active_control.answer_now_requested():
            break
        if source.source_kind == "sqlite":
            filtered_sources.append(source)
            continue
        search_root = source.search_root
        if search_root is None:
            filtered_sources.append(source)
            continue

        if search_root not in matched_paths_by_root:
            active_progress.prefilter_started(search_root)
            started_at = time.perf_counter()
            matched_paths_by_root[search_root] = grep_root_paths(
                search_root,
                query,
                grep_program,
                control=active_control,
            )
            matched_paths = matched_paths_by_root[search_root]
            _record_engine_profile_sample(
                "search.plan.prefilter_root",
                time.perf_counter() - started_at,
                # SQLite candidates bypass root prefiltering above, so they
                # do not count toward the sources this grep pass covers.
                agentgrep_source_count=sum(
                    1
                    for candidate in sources
                    if candidate.search_root == search_root and candidate.source_kind != "sqlite"
                ),
                agentgrep_matched_source_count=len(matched_paths)
                if matched_paths is not None
                else None,
                agentgrep_unknown=matched_paths is None,
            )
            if active_control.answer_now_requested():
                break

        matched_paths = matched_paths_by_root[search_root]
        if matched_paths is None or source.path in matched_paths:
            filtered_sources.append(source)
    return filtered_sources


def grep_root_paths(
    search_root: pathlib.Path,
    query: SearchQuery,
    grep_program: str,
    *,
    control: SearchControl | None = None,
) -> set[pathlib.Path] | None:
    """Return file paths matched by a whole-root grep."""
    active_control = SearchControl() if control is None else control
    matched_sets: list[set[pathlib.Path]] = []
    for term in query.terms:
        if active_control.answer_now_requested():
            return set()
        command = build_grep_command(
            grep_program,
            term,
            search_root,
            regex=query.regex,
            case_sensitive=query.case_sensitive,
        )
        completed = run_readonly_command(command, control=active_control)
        if active_control.answer_now_requested():
            return set()
        if completed.returncode not in {0, 1}:
            return None
        matched_sets.append(
            {pathlib.Path(line) for line in completed.stdout.splitlines() if line.strip()},
        )

    if not matched_sets:
        return set()
    if query.any_term:
        merged: set[pathlib.Path] = set()
        for matched in matched_sets:
            merged.update(matched)
        return merged

    intersection = matched_sets[0].copy()
    for matched in matched_sets[1:]:
        intersection.intersection_update(matched)
    return intersection


def direct_source_matches(
    source: SourceHandle,
    query: SearchQuery,
    backends: BackendSelection,
    control: SearchControl | None = None,
) -> bool:
    """Return whether a direct source should be parsed."""
    active_control = SearchControl() if control is None else control
    started_at = time.perf_counter()
    matched = False
    aborted = False
    if active_control.answer_now_requested():
        return False
    try:
        if query.compiled is not None and query.compiled.record_predicate is not None:
            # A compiled boolean/field query carries its own record
            # predicate; the flat-term text prefilter ANDs the terms and
            # would wrongly drop OR/NOT matches. Field-level source pruning
            # already ran via the compiled source_predicate during planning,
            # so admit and let the record matcher decide.
            matched = True
            return matched
        if source.adapter_id == "claude.history_jsonl.v1":
            # Claude history expands sibling paste-cache files into record
            # text, so a query term can match content that no grep over
            # history.jsonl itself can see. Admission must stay
            # unconditional; the record matcher filters after expansion.
            matched = True
            return matched
        if source.source_kind == "sqlite":
            matched = True
            return matched
        if backends.grep_tool is not None:
            grep_match = grep_file_matches(
                source.path,
                query,
                backends.grep_tool,
                control=active_control,
            )
            if active_control.answer_now_requested():
                aborted = True
                return False
            if grep_match is not None:
                matched = grep_match
                return matched
        if source.path.suffix in JSON_FILE_SUFFIXES and backends.json_tool is not None:
            extracted = flatten_json_strings_with_tool(
                source.path,
                backends.json_tool,
                control=active_control,
            )
            if active_control.answer_now_requested():
                aborted = True
                return False
            if extracted is not None:
                matched = matches_text(extracted, query)
                return matched
        matched = matches_text(read_text_file(source.path), query)
        return matched
    finally:
        # An answer-now abort is not a non-match; record nothing, matching
        # the pre-try early return above.
        if not aborted:
            _record_engine_profile_sample(
                "search.plan.direct_source",
                time.perf_counter() - started_at,
                **_source_profile_attributes(source),
                agentgrep_matched=matched,
            )


def collect_search_records(
    query: SearchQuery,
    sources: list[SourceHandle],
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
    runtime: SearchRuntime | None = None,
) -> list[SearchRecord]:
    """Parse candidate sources and collect matching records."""
    from agentgrep._engine.planning import (
        PhysicalSearchPlan,
        SourceTask,
        build_logical_search_plan,
    )

    plan = PhysicalSearchPlan(
        logical=build_logical_search_plan(query),
        tasks=tuple(
            SourceTask(
                source=source,
                strategy="direct_full_scan",
                record_order="unknown",
                limit_behavior="drain_source",
                can_stream_records=True,
                restore_order_key=source_order_key(source),
            )
            for source in sources
        ),
        decisions=(),
    )
    return collect_search_records_from_plan(
        query,
        plan,
        progress=progress,
        control=control,
        runtime=runtime,
    )


def collect_search_records_from_plan(
    query: SearchQuery,
    plan: PhysicalSearchPlan,
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
    runtime: SearchRuntime | None = None,
) -> list[SearchRecord]:
    """Execute a physical search plan and collect matching records.

    Parameters
    ----------
    query : SearchQuery
        Compiled query — terms, agents, dedup choice, limit.
    plan : PhysicalSearchPlan
        Planned source tasks from :func:`build_physical_search_plan`.
    progress : SearchProgress or None
        Progress sink for source and record events. ``None`` uses the
        no-op sink.
    control : SearchControl or None
        Optional control handle polled between records so consumers
        can stop the scan early.
    runtime : SearchRuntime or None
        Optional reusable runtime state; supplies the source-scan
        cache when one is configured.

    Returns
    -------
    list of SearchRecord
        Matching records sorted newest-first by
        :func:`search_record_sort_key`, truncated to ``query.limit``
        when set.
    """
    from agentgrep._engine.execution import ExecutionRecordEmitted, select_execution_driver

    results = [
        event.record
        for event in select_execution_driver(query, plan).iter_search_plan(
            query,
            plan,
            progress=progress,
            control=control,
            runtime=runtime,
        )
        if isinstance(event, ExecutionRecordEmitted)
    ]
    results.sort(key=search_record_sort_key, reverse=True)
    return results


def find_sources(
    pattern: str | None,
    sources: list[SourceHandle],
    limit: int | None,
) -> list[FindRecord]:
    """Build filtered ``find`` results from discovered sources."""
    query = pattern.casefold() if pattern is not None else None
    results: list[FindRecord] = []
    for source in sources:
        record = FindRecord(
            kind="find",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            path_kind=source.path_kind,
            metadata={"source_kind": source.source_kind},
        )
        if query is not None:
            haystack = " ".join(
                (
                    record.agent,
                    record.store,
                    record.adapter_id,
                    str(record.path),
                    record.path_kind,
                ),
            ).casefold()
            if query not in haystack:
                continue
        results.append(record)
        if limit is not None and len(results) >= limit:
            break
    return results


def run_find_query(
    home: pathlib.Path,
    agents: tuple[AgentName, ...],
    *,
    pattern: str | None,
    limit: int | None,
    backends: BackendSelection | None = None,
) -> list[FindRecord]:
    """Discover sources and build normalized ``find`` results."""
    active_backends = select_backends() if backends is None else backends
    sources = discover_sources(home, agents, active_backends, version_detail="none")
    return find_sources(pattern, sources, limit)


def build_grep_command(
    grep_program: str,
    term: str,
    target: pathlib.Path,
    *,
    regex: bool,
    case_sensitive: bool,
) -> list[str]:
    """Build a read-only grep command for one term and target.

    Always passes flags that disable ignore-file semantics — agent stores live
    inside the user's ``$HOME`` and may sit beneath a ``.gitignore`` from a
    dotfile manager (yadm, chezmoi, stow, bare-git). The grep tools would
    otherwise silently skip everything.
    """
    if grep_program.endswith("rg"):
        ignore_flags = ["--no-ignore", "--hidden"]
        fixed_flag = "-F"
    else:
        ignore_flags = ["--unrestricted", "--hidden"]
        fixed_flag = "-Q"
    command = [grep_program, *ignore_flags, "-l", term, str(target)]
    if not regex:
        command.insert(command.index("-l"), fixed_flag)
    if not case_sensitive:
        command.insert(1, "-i")
    return command


def flatten_json_strings_with_tool(
    path: pathlib.Path,
    program: str,
    *,
    control: SearchControl | None = None,
) -> str | None:
    """Return flattened JSON strings using ``jq`` or ``jaq``."""
    command = [program, "-r", ".. | strings", str(path)]
    completed = run_readonly_command(command, control=control)
    if completed.returncode != 0:
        return None
    return completed.stdout


def grep_file_matches(
    path: pathlib.Path,
    query: SearchQuery,
    program: str,
    *,
    control: SearchControl | None = None,
) -> bool | None:
    """Use ``rg`` or ``ag`` as a read-only prefilter."""
    active_control = SearchControl() if control is None else control
    matchers = [
        run_readonly_command(
            build_grep_command(
                program,
                term,
                path,
                regex=query.regex,
                case_sensitive=query.case_sensitive,
            ),
            control=active_control,
        ).returncode
        == 0
        for term in query.terms
        if not active_control.answer_now_requested()
    ]
    if active_control.answer_now_requested():
        return False
    return any(matchers) if query.any_term else all(matchers)


def record_matches_scope(record: SearchRecord, scope: SearchScope) -> bool:
    """Return whether ``record`` belongs to the requested search scope."""
    if scope == "all":
        return True
    if scope == "prompts":
        return record.kind == "prompt"
    role = store_role_for_record(record.store, record.adapter_id)
    return role in CONVERSATION_STORE_ROLES


def prompt_history_agents_for_sources(sources: cabc.Iterable[SourceHandle]) -> frozenset[str]:
    """Return agents with a dedicated prompt-history source in ``sources``."""
    return frozenset(
        source.agent
        for source in sources
        if store_role_for_record(source.store, source.adapter_id) == StoreRole.PROMPT_HISTORY
    )


def discover_sources_for_search(
    home: pathlib.Path,
    query: SearchQuery,
    backends: BackendSelection,
    *,
    version_detail: DiscoveryVersionDetail = "none",
) -> list[SourceHandle]:
    """Discover only the source roles needed for a search query scope."""
    from agentgrep._engine.planning import build_logical_search_plan

    logical_plan = build_logical_search_plan(query)
    if query.scope == "all":
        return discover_sources(
            home,
            query.agents,
            backends,
            version_detail=version_detail,
        )
    if query.scope == "conversations":
        return discover_sources(
            home,
            query.agents,
            backends,
            version_detail=version_detail,
            store_roles=logical_plan.initial_store_roles,
        )

    prompt_sources = discover_sources(
        home,
        query.agents,
        backends,
        version_detail=version_detail,
        store_roles=logical_plan.initial_store_roles,
    )
    agents_with_prompt_history = frozenset(
        source.agent
        for source in prompt_sources
        if store_role_for_record(source.store, source.adapter_id) == StoreRole.PROMPT_HISTORY
    )
    fallback_agents = tuple(
        agent for agent in query.agents if agent not in agents_with_prompt_history
    )
    if not fallback_agents:
        return prompt_sources

    sources = [
        *prompt_sources,
        *discover_sources(
            home,
            fallback_agents,
            backends,
            version_detail=version_detail,
            store_roles=CONVERSATION_STORE_ROLES,
        ),
    ]
    deduped: list[SourceHandle] = []
    seen: set[tuple[AgentName, str, str, pathlib.Path]] = set()
    for source in sources:
        key = (source.agent, source.store, source.adapter_id, source.path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(source)
    return deduped


def source_matches_scope(
    source: SourceHandle,
    scope: SearchScope,
    *,
    prompt_history_agents: frozenset[str] = frozenset(),
) -> bool:
    """Return whether ``source`` can yield records for the requested scope."""
    if scope == "all":
        return True
    role = store_role_for_record(source.store, source.adapter_id)
    if scope == "conversations":
        return role in CONVERSATION_STORE_ROLES
    if role == StoreRole.PROMPT_HISTORY:
        return True
    if role in CONVERSATION_STORE_ROLES:
        return source.agent not in prompt_history_agents
    return True


def matches_record(record: SearchRecord, query: SearchQuery) -> bool:
    """Return whether a normalized record should be included.

    When ``query.compiled`` carries a record-level predicate, the
    record must satisfy it in addition to the existing text + scope
    checks. Pure-text queries skip the predicate evaluation since
    the compiler leaves ``compiled = None`` for them.
    """
    from agentgrep._engine.matching import matches_record as compiled_matches_record

    return compiled_matches_record(record, query)


def build_record_match_surface(record: SearchRecord, surface: SearchMatchSurface) -> str:
    """Build the text surface used for unfielded query terms."""
    if surface == "text":
        return record.text
    return build_search_haystack(record)


def build_search_haystack(record: SearchRecord) -> str:
    """Build a searchable text surface for a record."""
    parts = [
        record.title or "",
        record.text,
        record.model or "",
        record.role or "",
        str(record.path),
    ]
    return "\n".join(part for part in parts if part)


_HAYSTACK_CACHE: dict[int, str] = {}


def cached_haystack(record: SearchRecord) -> str:
    """Return the casefolded haystack for ``record``, memoized by ``id``.

    The filter worker scans every loaded record on every keystroke;
    recomputing ``build_search_haystack(...).casefold()`` per record per
    pass dominates filter latency once the result set grows past a few
    thousand records. Memoizing by ``id`` is safe because the app
    retains every record in ``AgentGrepApp.all_records`` for the
    lifetime of one search, so Python cannot recycle a collected
    record's id while its entry sits in :data:`_HAYSTACK_CACHE`.

    Callers that need to invalidate (because a new search will allocate
    new records) should call :func:`clear_haystack_cache`.
    """
    key = id(record)
    cached = _HAYSTACK_CACHE.get(key)
    if cached is None:
        cached = build_search_haystack(record).casefold()
        _HAYSTACK_CACHE[key] = cached
    return cached


def clear_haystack_cache() -> None:
    """Drop every memoized haystack — call before allocating a new record set."""
    _HAYSTACK_CACHE.clear()


def compute_filter_matches(
    records: cabc.Sequence[SearchRecord],
    text: str,
) -> tuple[SearchRecord, ...]:
    """Return the subset of ``records`` whose haystack contains ``text`` (case-fold).

    Used by the TUI's filter worker. Pure function so the filter logic is
    directly unit-testable without spinning up a Textual app.

    Parameters
    ----------
    records : Sequence[SearchRecord]
        Records to test.
    text : str
        Filter text. Whitespace-trimmed and case-folded before matching.
        An empty (or whitespace-only) ``text`` returns all records.

    Returns
    -------
    tuple[SearchRecord, ...]
        Matching records in input order.
    """
    normalized = text.strip().casefold()
    if not normalized:
        return tuple(records)
    return tuple(record for record in records if normalized in cached_haystack(record))


def matches_text(text: str, query: SearchQuery) -> bool:
    """Return whether ``text`` matches the query."""
    if not query.terms:
        return True
    if query.regex:
        flags = 0 if query.case_sensitive else re.IGNORECASE
        results = [re.search(term, text, flags) is not None for term in query.terms]
    else:
        haystack = text if query.case_sensitive else text.casefold()
        needles = (
            query.terms if query.case_sensitive else tuple(term.casefold() for term in query.terms)
        )
        results = [needle in haystack for needle in needles]
    return any(results) if query.any_term else all(results)


def search_record_sort_key(record: SearchRecord) -> tuple[str, str, str]:
    """Return a stable sort key."""
    return (record.timestamp or "", record.agent, str(record.path))


def record_dedupe_key(record: SearchRecord) -> tuple[str, str, str, str, str]:
    """Return the per-session dedupe key for a search record."""
    session_identity = record.session_id or record.conversation_id or str(record.path)
    return (
        record.kind,
        record.agent,
        record.store,
        session_identity,
        record.text,
    )
