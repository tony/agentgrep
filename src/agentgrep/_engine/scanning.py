"""Source-local scan helpers for physical search tasks."""

from __future__ import annotations

import collections
import collections.abc as cabc
import dataclasses
import json
import threading
import time
import typing as t

import agentgrep
from agentgrep._engine.matching import compile_record_matcher
from agentgrep._engine.planning import SourceTask

_SOURCE_SCAN_CACHE_MAX_ENTRIES = 512

if t.TYPE_CHECKING:
    from agentgrep._engine.runtime import SearchRuntime


@dataclasses.dataclass(frozen=True, slots=True)
class _SourceScanCacheKey:
    """Hashable identity for one reusable source scan."""

    source_identity: tuple[str, str, str, str, str]
    source_fingerprint: tuple[int, int]
    query_shape: tuple[object, ...]
    task_shape: tuple[object, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class _SourceScanCacheEntry:
    """Reusable scan payload without run-specific coordinates."""

    records: tuple[agentgrep.SearchRecord, ...]
    records_seen: int
    matches_seen: int
    batch_count: int


@dataclasses.dataclass(frozen=True, slots=True)
class SourceScanCacheStats:
    """Privacy-safe counters for one source-scan cache."""

    entries: int
    hits: int
    misses: int
    stores: int
    evictions: int

    def to_payload(self) -> dict[str, int]:
        """Return a JSON-ready cache summary."""
        return {
            "entries": self.entries,
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "evictions": self.evictions,
        }


class SourceScanCache:
    """Bounded, in-process cache for reusable source-scan results."""

    def __init__(self, *, max_entries: int = _SOURCE_SCAN_CACHE_MAX_ENTRIES) -> None:
        self._max_entries = max(0, max_entries)
        self._lock = threading.Lock()
        self._entries: collections.OrderedDict[
            _SourceScanCacheKey,
            _SourceScanCacheEntry,
        ] = collections.OrderedDict()
        self._hits = 0
        self._misses = 0
        self._stores = 0
        self._evictions = 0

    def clear(self) -> None:
        """Clear cached scans and reset counters."""
        with self._lock:
            self._entries.clear()
            self._hits = 0
            self._misses = 0
            self._stores = 0
            self._evictions = 0

    def stats(self) -> SourceScanCacheStats:
        """Return privacy-safe cache counters."""
        with self._lock:
            return SourceScanCacheStats(
                entries=len(self._entries),
                hits=self._hits,
                misses=self._misses,
                stores=self._stores,
                evictions=self._evictions,
            )

    def _lookup(self, key: _SourceScanCacheKey) -> _SourceScanCacheEntry | None:
        """Return a cached source scan entry, recording hit/miss counters."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            self._hits += 1
            self._entries.move_to_end(key)
            return entry

    def _remember(
        self,
        key: _SourceScanCacheKey,
        result: SourceScanResult,
    ) -> None:
        """Store a completed source scan in the bounded cache."""
        if self._max_entries <= 0:
            return
        entry = _SourceScanCacheEntry(
            records=result.records,
            records_seen=result.records_seen,
            matches_seen=result.matches_seen,
            batch_count=result.batch_count,
        )
        with self._lock:
            if key in self._entries:
                self._entries[key] = entry
                self._entries.move_to_end(key)
                self._stores += 1
                return
            while len(self._entries) >= self._max_entries:
                self._entries.popitem(last=False)
                self._evictions += 1
            self._entries[key] = entry
            self._stores += 1


@dataclasses.dataclass(frozen=True, slots=True)
class SourceScanResult:
    """Candidate records and counters from one planned source task."""

    index: int
    total: int
    source: agentgrep.SourceHandle
    task: SourceTask
    records: tuple[agentgrep.SearchRecord, ...]
    records_seen: int
    matches_seen: int
    duration_seconds: float
    batch_count: int = 1
    cache_hit: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class SourceScanBatch:
    """One source-local batch of matching candidate records."""

    index: int
    total: int
    source: agentgrep.SourceHandle
    task: SourceTask
    records: tuple[agentgrep.SearchRecord, ...]
    records_seen: int
    matches_seen: int
    duration_seconds: float
    is_final: bool


def scan_source_task(
    query: agentgrep.SearchQuery,
    task: SourceTask,
    *,
    index: int,
    total: int,
    control: agentgrep.SearchControl,
    progress: agentgrep.SearchProgress | None = None,
    runtime: SearchRuntime | None = None,
) -> SourceScanResult:
    """Scan one source task and return source-local matching candidates.

    Parameters
    ----------
    query : agentgrep.SearchQuery
        Compiled query — terms, agents, dedup choice, limit.
    task : SourceTask
        Planned source task naming the execution strategy.
    index : int
        One-based position of this source in the plan.
    total : int
        Number of planned sources, used for progress reporting.
    control : agentgrep.SearchControl
        Control handle polled between records so the scan can stop
        early.
    progress : agentgrep.SearchProgress or None
        Progress sink for match counts. ``None`` skips per-record
        progress.
    runtime : SearchRuntime or None
        Optional reusable runtime state; supplies the source-scan
        cache when one is configured.

    Returns
    -------
    SourceScanResult
        Source-local matching records plus scan counters; served from
        the runtime cache when a fresh entry exists.
    """
    cache = runtime.source_scan_cache if runtime is not None else None
    cache_started_at = time.perf_counter()
    cache_key, cached = cached_source_scan_lookup(
        query,
        task,
        control=control,
        cache=cache,
    )
    if cached is not None:
        return SourceScanResult(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=cached.records,
            records_seen=cached.records_seen,
            matches_seen=cached.matches_seen,
            duration_seconds=time.perf_counter() - cache_started_at,
            batch_count=cached.batch_count,
            cache_hit=True,
        )

    source_started_at = time.perf_counter()
    matching_records: list[agentgrep.SearchRecord] = []
    records_seen = 0
    matches_seen = 0
    batch_count = 0
    for batch in iter_source_task_batches(
        query,
        task,
        index=index,
        total=total,
        control=control,
        progress=progress,
    ):
        batch_count += 1
        matching_records.extend(batch.records)
        records_seen = batch.records_seen
        matches_seen = batch.matches_seen

    if task.limit_behavior == "drain_source":
        matching_records.sort(key=agentgrep.search_record_sort_key, reverse=True)
    result = SourceScanResult(
        index=index,
        total=total,
        source=task.source,
        task=task,
        records=tuple(matching_records),
        records_seen=records_seen,
        matches_seen=matches_seen,
        duration_seconds=time.perf_counter() - source_started_at,
        batch_count=batch_count,
    )
    remember_source_scan(cache, cache_key, control=control, result=result)
    return result


def cached_source_scan_lookup(
    query: agentgrep.SearchQuery,
    task: SourceTask,
    *,
    control: agentgrep.SearchControl,
    cache: SourceScanCache | None,
) -> tuple[_SourceScanCacheKey | None, _SourceScanCacheEntry | None]:
    """Return the scan cache key and any cached entry for one source task.

    Records a hit/miss profile sample only when a real lookup happens; a
    cancelled task skips the lookup but keeps its key so a caller can still
    decide whether to remember a clean completion.
    """
    cache_key = _source_scan_cache_key(query, task, cache)
    if cache_key is None or control.answer_now_requested():
        return cache_key, None
    assert cache is not None
    lookup_started_at = time.perf_counter()
    cached = cache._lookup(cache_key)
    _record_source_scan_cache_sample(
        task,
        hit=cached is not None,
        duration_seconds=time.perf_counter() - lookup_started_at,
    )
    return cache_key, cached


def remember_source_scan(
    cache: SourceScanCache | None,
    cache_key: _SourceScanCacheKey | None,
    *,
    control: agentgrep.SearchControl,
    result: SourceScanResult,
) -> None:
    """Store a cleanly completed source scan when caching is enabled.

    Mirrors the :func:`scan_source_task` guard: cancelled scans are partial
    and must not populate the cache.
    """
    if cache is None or cache_key is None or control.answer_now_requested():
        return
    cache._remember(cache_key, result)


def _source_scan_cache_key(
    query: agentgrep.SearchQuery,
    task: SourceTask,
    cache: SourceScanCache | None,
) -> _SourceScanCacheKey | None:
    """Return a cache key for runtime-owned reusable source scans."""
    if cache is None:
        return None
    if query.compiled is not None:
        return None
    try:
        stat_result = task.source.path.stat()
    except OSError:
        return None
    return _SourceScanCacheKey(
        source_identity=(
            task.source.agent,
            task.source.store,
            task.source.adapter_id,
            task.source.source_kind,
            str(task.source.path),
        ),
        source_fingerprint=(stat_result.st_size, stat_result.st_mtime_ns),
        query_shape=(
            query.terms,
            query.scope,
            query.any_term,
            query.regex,
            query.case_sensitive,
            query.agents,
            query.limit,
            query.dedupe,
            query.match_surface,
        ),
        task_shape=(
            task.strategy,
            task.record_order,
            task.limit_behavior,
            task.limit_policy.mode,
        ),
    )


def _record_source_scan_cache_sample(
    task: SourceTask,
    *,
    hit: bool,
    duration_seconds: float,
) -> None:
    """Record a privacy-safe cache lookup timing sample."""
    agentgrep._record_engine_profile_sample(
        "search.collect.source_scan_cache",
        duration_seconds,
        **agentgrep._source_profile_attributes(task.source),
        agentgrep_cache_hit=hit,
        agentgrep_source_strategy=task.strategy,
        agentgrep_source_group=task.source_group,
        agentgrep_source_cost_hint=task.cost_hint,
    )


def iter_source_task_batches(
    query: agentgrep.SearchQuery,
    task: SourceTask,
    *,
    index: int,
    total: int,
    control: agentgrep.SearchControl,
    progress: agentgrep.SearchProgress | None = None,
    batch_size: int = 32,
) -> cabc.Iterator[SourceScanBatch]:
    """Yield source-local candidate batches for one planned source task.

    Parameters
    ----------
    query : agentgrep.SearchQuery
        Compiled query — terms, agents, dedup choice, limit.
    task : SourceTask
        Planned source task naming the execution strategy.
    index : int
        One-based position of this source in the plan.
    total : int
        Number of planned sources, used for progress reporting.
    control : agentgrep.SearchControl
        Control handle polled between records so the scan can stop
        early.
    progress : agentgrep.SearchProgress or None
        Progress sink for match counts. ``None`` skips per-record
        progress.
    batch_size : int
        Maximum records per emitted batch.

    Yields
    ------
    SourceScanBatch
        Incremental matching candidates; the final batch is marked
        ``is_final`` and carries the closing counters.
    """
    active_progress = agentgrep.noop_search_progress() if progress is None else progress
    source_started_at = time.perf_counter()
    records_seen = 0
    matches_seen = 0
    source_match_count = 0
    yielded_final = False
    yielded_batch = False
    matching_records: list[agentgrep.SearchRecord] = []
    source_deduped: set[tuple[str, str, str, str, str]] = set()
    matcher = compile_record_matcher(query)

    def source_limit_satisfied() -> bool:
        # Counts source-local dedupe keys only: the frontier's global
        # cross-source dedup may drop some of these later, so bounded scans
        # can return fewer than the limit when stores share dedupe keys.
        # Accepted approximation per ADR-0004.
        accepted_count = len(source_deduped) if query.dedupe else source_match_count
        return (
            task.limit_behavior == "bounded_source"
            and query.limit is not None
            and accepted_count >= query.limit
        )

    def emit_batch(*, is_final: bool) -> SourceScanBatch:
        nonlocal yielded_batch, yielded_final
        batch = SourceScanBatch(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=tuple(matching_records),
            records_seen=records_seen,
            matches_seen=matches_seen,
            duration_seconds=time.perf_counter() - source_started_at,
            is_final=is_final,
        )
        matching_records.clear()
        yielded_batch = True
        yielded_final = is_final
        return batch

    normalized_batch_size = max(1, batch_size)
    for record in iter_source_task_records(task, query):
        if control.answer_now_requested():
            break
        records_seen += 1
        if matcher.matches(record):
            matches_seen += 1
            source_match_count += 1
            matching_records.append(record)
            if query.dedupe:
                source_deduped.add(agentgrep.record_dedupe_key(record))
            if source_limit_satisfied():
                if matching_records:
                    yield emit_batch(is_final=True)
                break
            if (
                task.limit_behavior == "bounded_source"
                and len(matching_records) >= normalized_batch_size
            ):
                yield emit_batch(is_final=False)
        if records_seen % agentgrep._SOURCE_PROGRESS_RECORD_INTERVAL == 0:
            agentgrep._report_source_progress(
                active_progress,
                index,
                total,
                task.source,
                records_seen,
                matches_seen,
            )
            time.sleep(0)

    if matching_records or (not yielded_final and (yielded_batch or records_seen > 0)):
        yield emit_batch(is_final=True)


def record_source_profile_sample(result: SourceScanResult) -> None:
    """Record one privacy-safe source execution timing sample."""
    agentgrep._record_engine_profile_sample(
        "search.collect.source",
        result.duration_seconds,
        **agentgrep._source_profile_attributes(result.source),
        agentgrep_source_strategy=result.task.strategy,
        agentgrep_source_group=result.task.source_group,
        agentgrep_source_cost_hint=result.task.cost_hint,
        agentgrep_records_seen=result.records_seen,
        agentgrep_matches_seen=result.matches_seen,
        agentgrep_batch_count=result.batch_count,
        agentgrep_source_scan_cache_hit=result.cache_hit,
    )


def iter_source_task_records(
    task: SourceTask,
    query: agentgrep.SearchQuery,
) -> cabc.Iterator[agentgrep.SearchRecord]:
    """Yield records for one source task."""
    if task.strategy == "jsonl_raw_text_prefilter":
        yield from agentgrep.iter_source_records(
            task.source,
            raw_skip_line=raw_text_skip_line_for_query(query),
        )
        return
    if task.strategy == "jsonl_bounded_reverse_raw_text_prefilter":
        yield from agentgrep.iter_source_records(
            task.source,
            raw_skip_line=raw_text_skip_line_for_query(query),
            reverse=True,
        )
        return
    if task.strategy == "jsonl_bounded_reverse_haystack_raw_text_prefilter":
        yield from agentgrep.iter_source_records(
            task.source,
            raw_skip_line=raw_text_skip_line_for_haystack_query(query, task.source),
            reverse=True,
        )
        return
    if task.strategy == "jsonl_bounded_reverse_scan":
        yield from agentgrep.iter_source_records(task.source, reverse=True)
        return
    yield from agentgrep.iter_source_records(task.source)


def raw_text_skip_line_for_query(
    query: agentgrep.SearchQuery,
) -> cabc.Callable[[str], bool]:
    """Return a raw JSONL line skip predicate for a text-surface query.

    Parameters
    ----------
    query : agentgrep.SearchQuery
        Compiled query whose literal terms gate the raw-line check.

    Returns
    -------
    Callable[[str], bool]
        Predicate returning ``True`` for raw lines that provably
        cannot satisfy the query and are safe to skip before JSON
        decode.
    """
    return _raw_text_skip_line_for_terms(query, query.terms)


def raw_text_skip_line_for_haystack_query(
    query: agentgrep.SearchQuery,
    source: agentgrep.SourceHandle,
) -> cabc.Callable[[str], bool]:
    """Return a source-aware raw skip predicate for a haystack-surface query.

    Parameters
    ----------
    query : agentgrep.SearchQuery
        Compiled query whose literal terms gate the raw-line check.
    source : agentgrep.SourceHandle
        Source whose path metadata may already satisfy part of the
        haystack query; path-matched terms are not required on the
        raw line.

    Returns
    -------
    Callable[[str], bool]
        Predicate returning ``True`` for raw lines that provably
        cannot satisfy the query and are safe to skip before JSON
        decode.
    """
    if not query.terms:
        return lambda _raw_line: False
    if query.regex:
        return lambda _raw_line: False

    source_text = str(source.path)
    source_haystack = source_text if query.case_sensitive else source_text.casefold()
    terms = query.terms if query.case_sensitive else tuple(term.casefold() for term in query.terms)
    source_matches = tuple(term in source_haystack for term in terms)
    if query.any_term:
        if any(source_matches):
            return lambda _raw_line: False
        return _raw_text_skip_line_for_terms(query, query.terms)

    remaining_terms = tuple(
        original_term
        for original_term, matched in zip(query.terms, source_matches, strict=True)
        if not matched
    )
    if not remaining_terms:
        return lambda _raw_line: False
    return _raw_text_skip_line_for_terms(query, remaining_terms)


def _raw_text_skip_line_for_terms(
    query: agentgrep.SearchQuery,
    terms: tuple[str, ...],
) -> cabc.Callable[[str], bool]:
    """Return a raw JSONL line skip predicate for literal query terms."""
    if not terms:
        return lambda _raw_line: False
    if query.regex:
        return lambda raw_line: (
            "\\" not in raw_line
            and not agentgrep.matches_text(
                raw_line,
                query,
            )
        )

    needles = terms if query.case_sensitive else tuple(term.casefold() for term in terms)
    escaped_needles = tuple(json.dumps(needle, ensure_ascii=True)[1:-1] for needle in needles)
    # JSON encoders may legally escape "/" as "\/" even though json.dumps
    # never emits it, so terms containing a solidus need a third variant.
    solidus_needles = tuple(escaped.replace("/", "\\/") for escaped in escaped_needles)
    any_solidus_term = any("/" in needle for needle in needles)
    any_term = query.any_term
    case_sensitive = query.case_sensitive

    def skip_line(raw_line: str) -> bool:
        haystack = raw_line if case_sensitive else raw_line.casefold()
        if "\\u" in haystack:
            return False
        needle_results = [
            needle in haystack or escaped_needle in haystack or solidus_needle in haystack
            for needle, escaped_needle, solidus_needle in zip(
                needles,
                escaped_needles,
                solidus_needles,
                strict=True,
            )
        ]
        matched = any(needle_results) if any_term else all(needle_results)
        if not matched and any_solidus_term and "\\/" in haystack:
            # Mixed or partial solidus escaping is valid JSON the variants
            # cannot enumerate; keep the line rather than risk a false skip.
            return False
        return not matched

    return skip_line
