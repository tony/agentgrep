"""Source-local scan helpers for physical search tasks."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import json
import os
import threading
import time

import agentgrep
from agentgrep._engine.matching import compile_record_matcher
from agentgrep._engine.planning import SourceTask

_SOURCE_SCAN_CACHE_ENV = "AGENTGREP_EXPERIMENTAL_SOURCE_SCAN_CACHE"
_SOURCE_SCAN_CACHE_MAX_ENTRIES = 512


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


_SOURCE_SCAN_CACHE_LOCK = threading.Lock()
_SOURCE_SCAN_CACHE: dict[_SourceScanCacheKey, _SourceScanCacheEntry] = {}


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
) -> SourceScanResult:
    """Scan one source task and return source-local matching candidates."""
    cache_started_at = time.perf_counter()
    cache_key = _source_scan_cache_key(query, task)
    if cache_key is not None and not control.answer_now_requested():
        with _SOURCE_SCAN_CACHE_LOCK:
            cached = _SOURCE_SCAN_CACHE.get(cache_key)
        _record_source_scan_cache_sample(
            task,
            hit=cached is not None,
            duration_seconds=time.perf_counter() - cache_started_at,
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
    if cache_key is not None and not control.answer_now_requested():
        _remember_source_scan_result(cache_key, result)
    return result


def clear_source_scan_cache() -> None:
    """Clear the experimental in-process source scan cache."""
    with _SOURCE_SCAN_CACHE_LOCK:
        _SOURCE_SCAN_CACHE.clear()


def _source_scan_cache_key(
    query: agentgrep.SearchQuery,
    task: SourceTask,
) -> _SourceScanCacheKey | None:
    """Return a cache key for opt-in reusable source scans."""
    if not _source_scan_cache_enabled():
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


def _source_scan_cache_enabled() -> bool:
    """Return whether the experimental source scan cache is enabled."""
    return os.environ.get(_SOURCE_SCAN_CACHE_ENV) == "1"


def _remember_source_scan_result(
    key: _SourceScanCacheKey,
    result: SourceScanResult,
) -> None:
    """Store a completed source scan in the bounded in-process cache."""
    entry = _SourceScanCacheEntry(
        records=result.records,
        records_seen=result.records_seen,
        matches_seen=result.matches_seen,
        batch_count=result.batch_count,
    )
    with _SOURCE_SCAN_CACHE_LOCK:
        if len(_SOURCE_SCAN_CACHE) >= _SOURCE_SCAN_CACHE_MAX_ENTRIES:
            oldest_key = next(iter(_SOURCE_SCAN_CACHE), None)
            if oldest_key is not None:
                del _SOURCE_SCAN_CACHE[oldest_key]
        _SOURCE_SCAN_CACHE[key] = entry


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
    """Yield source-local candidate batches for one planned source task."""
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
    """Return a raw JSONL line skip predicate for a text-surface query."""
    return _raw_text_skip_line_for_terms(query, query.terms)


def raw_text_skip_line_for_haystack_query(
    query: agentgrep.SearchQuery,
    source: agentgrep.SourceHandle,
) -> cabc.Callable[[str], bool]:
    """Return a source-aware raw skip predicate for a haystack-surface query."""
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
    any_term = query.any_term
    case_sensitive = query.case_sensitive

    def skip_line(raw_line: str) -> bool:
        haystack = raw_line if case_sensitive else raw_line.casefold()
        if "\\u" in haystack:
            return False
        needle_results = [
            needle in haystack or escaped_needle in haystack
            for needle, escaped_needle in zip(needles, escaped_needles, strict=True)
        ]
        matched = any(needle_results) if any_term else all(needle_results)
        return not matched

    return skip_line
