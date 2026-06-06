"""Search physical-plan execution helpers."""

from __future__ import annotations

import collections.abc as cabc
import concurrent.futures
import dataclasses
import json
import time
import typing as t

import agentgrep
from agentgrep._engine.matching import compile_record_matcher
from agentgrep._engine.planning import PhysicalSearchPlan, SourceTask


@dataclasses.dataclass(frozen=True, slots=True)
class ExecutionSourceStarted:
    """Internal event emitted before scanning one planned source task."""

    index: int
    total: int
    source: agentgrep.SourceHandle
    task: SourceTask


@dataclasses.dataclass(frozen=True, slots=True)
class ExecutionRecordEmitted:
    """Internal event emitted after dedupe admits one matching record."""

    record: agentgrep.SearchRecord
    result_count: int


@dataclasses.dataclass(frozen=True, slots=True)
class ExecutionSourceFinished:
    """Internal event emitted after scanning one planned source task."""

    index: int
    total: int
    source: agentgrep.SourceHandle
    task: SourceTask
    records_seen: int
    matches_seen: int


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


@dataclasses.dataclass(frozen=True, slots=True)
class ExecutionDriverConfig:
    """Execution-driver tuning for bounded source scheduling."""

    max_workers: int = 1

    @property
    def worker_count(self) -> int:
        """Return a normalized positive worker count."""
        return max(1, self.max_workers)


type SearchExecutionEvent = (
    ExecutionSourceStarted | ExecutionRecordEmitted | ExecutionSourceFinished
)


class ExecutionDriver(t.Protocol):
    """Protocol for drivers that execute physical search plans."""

    def iter_search_plan(
        self,
        query: agentgrep.SearchQuery,
        plan: PhysicalSearchPlan,
        *,
        progress: agentgrep.SearchProgress | None = None,
        control: agentgrep.SearchControl | None = None,
    ) -> cabc.Iterator[SearchExecutionEvent]:
        """Yield internal search execution events."""
        ...


class InlineExecutionDriver:
    """Deterministic in-process physical-plan executor."""

    def iter_search_plan(
        self,
        query: agentgrep.SearchQuery,
        plan: PhysicalSearchPlan,
        *,
        progress: agentgrep.SearchProgress | None = None,
        control: agentgrep.SearchControl | None = None,
    ) -> cabc.Iterator[SearchExecutionEvent]:
        """Yield internal search execution events for ``plan``."""
        active_progress = agentgrep.noop_search_progress() if progress is None else progress
        active_control = agentgrep.SearchControl() if control is None else control
        tasks = plan.tasks
        total = len(tasks)
        deduped: dict[tuple[str, str, str, str, str], agentgrep.SearchRecord] = {}
        raw_count = 0
        prompt_history_agents = agentgrep.prompt_history_agents_for_sources(
            task.source for task in tasks
        )
        source_predicate = query.compiled.source_predicate if query.compiled is not None else None

        def current_count() -> int:
            return len(deduped) if query.dedupe else raw_count

        def accept_matching_record(
            record: agentgrep.SearchRecord,
        ) -> ExecutionRecordEmitted | None:
            nonlocal raw_count
            if query.dedupe:
                dedupe_key = agentgrep.record_dedupe_key(record)
                if dedupe_key in deduped:
                    return None
                deduped[dedupe_key] = record
                result_count = len(deduped)
            else:
                raw_count += 1
                result_count = raw_count
            active_progress.record_added(record)
            active_progress.result_added(result_count)
            return ExecutionRecordEmitted(record=record, result_count=result_count)

        for index, task in enumerate(tasks, start=1):
            source = task.source
            if active_control.answer_now_requested() or (
                query.limit is not None and current_count() >= query.limit
            ):
                break
            if not agentgrep.source_matches_scope(
                source,
                query.scope,
                prompt_history_agents=prompt_history_agents,
            ):
                continue
            if source_predicate is not None and not source_predicate(source):
                continue

            active_progress.source_started(index, total, source)
            yield ExecutionSourceStarted(index=index, total=total, source=source, task=task)

            result = scan_source_task(
                query,
                task,
                index=index,
                total=total,
                control=active_control,
                progress=active_progress,
            )
            active_progress.source_finished(
                index,
                total,
                source,
                result.records_seen,
                result.matches_seen,
            )
            record_source_profile_sample(result)

            for record in result.records:
                emitted = accept_matching_record(record)
                if emitted is not None:
                    yield emitted
                if active_control.answer_now_requested() or (
                    query.limit is not None and current_count() >= query.limit
                ):
                    break
            yield ExecutionSourceFinished(
                index=index,
                total=total,
                source=source,
                task=task,
                records_seen=result.records_seen,
                matches_seen=result.matches_seen,
            )


class FrontierExecutionDriver:
    """Concurrent source-task executor with deterministic top-K merging."""

    def __init__(self, config: ExecutionDriverConfig | None = None) -> None:
        self._config = ExecutionDriverConfig() if config is None else config

    def iter_search_plan(
        self,
        query: agentgrep.SearchQuery,
        plan: PhysicalSearchPlan,
        *,
        progress: agentgrep.SearchProgress | None = None,
        control: agentgrep.SearchControl | None = None,
    ) -> cabc.Iterator[SearchExecutionEvent]:
        """Yield internal search events using a bounded source frontier."""
        active_progress = agentgrep.noop_search_progress() if progress is None else progress
        active_control = agentgrep.SearchControl() if control is None else control
        tasks = tuple(_eligible_tasks(query, plan.tasks))
        total = len(tasks)
        if total == 0:
            return

        frontier = _FrontierState(query)
        submitted_count = 0
        completed_count = 0
        skipped_count = 0
        scheduler_started_at = time.perf_counter()
        max_workers = min(self._config.worker_count, total)
        next_task_index = 0
        futures: dict[concurrent.futures.Future[SourceScanResult], tuple[int, SourceTask]] = {}

        def submit_next(
            executor: concurrent.futures.ThreadPoolExecutor,
        ) -> cabc.Iterator[ExecutionSourceStarted]:
            nonlocal next_task_index, submitted_count, skipped_count
            while len(futures) < max_workers and next_task_index < total:
                index = next_task_index + 1
                task = tasks[next_task_index]
                if _frontier_can_skip_remaining(query, frontier, task):
                    skipped_count = total - next_task_index
                    next_task_index = total
                    break
                next_task_index += 1
                submitted_count += 1
                active_progress.source_started(index, total, task.source)
                yield ExecutionSourceStarted(
                    index=index,
                    total=total,
                    source=task.source,
                    task=task,
                )
                future = executor.submit(
                    scan_source_task,
                    query,
                    task,
                    index=index,
                    total=total,
                    control=active_control,
                    progress=None,
                )
                futures[future] = (index, task)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            yield from submit_next(executor)
            while futures:
                if active_control.answer_now_requested():
                    for future in futures:
                        future.cancel()
                    break
                done, _pending = concurrent.futures.wait(
                    futures,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in sorted(
                    done,
                    key=lambda completed: futures[completed][0],
                ):
                    _index, task = futures.pop(future)
                    result = future.result()
                    completed_count += 1
                    active_progress.source_finished(
                        result.index,
                        result.total,
                        result.source,
                        result.records_seen,
                        result.matches_seen,
                    )
                    record_source_profile_sample(result)
                    frontier.add_records(result.records)
                    yield ExecutionSourceFinished(
                        index=result.index,
                        total=result.total,
                        source=result.source,
                        task=task,
                        records_seen=result.records_seen,
                        matches_seen=result.matches_seen,
                    )
                yield from submit_next(executor)

        emitted_count = 0
        for record in frontier.records():
            emitted_count += 1
            active_progress.record_added(record)
            active_progress.result_added(emitted_count)
            yield ExecutionRecordEmitted(record=record, result_count=emitted_count)

        agentgrep._record_engine_profile_sample(
            "search.collect.scheduler",
            time.perf_counter() - scheduler_started_at,
            agentgrep_execution_driver="frontier",
            agentgrep_worker_count=max_workers,
            agentgrep_source_count=total,
            agentgrep_submitted_source_count=submitted_count,
            agentgrep_completed_source_count=completed_count,
            agentgrep_skipped_source_count=skipped_count,
            agentgrep_emitted_record_count=emitted_count,
        )


class _FrontierState:
    """Owner-thread state for deterministic top-K result selection."""

    def __init__(self, query: agentgrep.SearchQuery) -> None:
        self._query = query
        self._deduped: dict[tuple[str, str, str, str, str], agentgrep.SearchRecord] = {}
        self._records: list[agentgrep.SearchRecord] = []

    def add_records(self, records: cabc.Iterable[agentgrep.SearchRecord]) -> None:
        """Merge source-local candidates into the global frontier."""
        if self._query.dedupe:
            for record in records:
                key = agentgrep.record_dedupe_key(record)
                current = self._deduped.get(key)
                if current is None or agentgrep.search_record_sort_key(
                    record,
                ) > agentgrep.search_record_sort_key(current):
                    self._deduped[key] = record
            return
        self._records.extend(records)

    def records(self) -> tuple[agentgrep.SearchRecord, ...]:
        """Return accepted records in final newest-first order."""
        records = list(self._deduped.values()) if self._query.dedupe else list(self._records)
        records.sort(key=agentgrep.search_record_sort_key, reverse=True)
        if self._query.limit is not None:
            records = records[: self._query.limit]
        return tuple(records)

    @property
    def is_satisfied(self) -> bool:
        """Return whether the query limit has enough accepted candidates."""
        if self._query.limit is None:
            return False
        accepted_count = len(self._deduped) if self._query.dedupe else len(self._records)
        return accepted_count >= self._query.limit


def _eligible_tasks(
    query: agentgrep.SearchQuery,
    tasks: cabc.Iterable[SourceTask],
) -> cabc.Iterator[SourceTask]:
    """Yield plan tasks that still match late-bound query predicates."""
    task_list = tuple(tasks)
    prompt_history_agents = agentgrep.prompt_history_agents_for_sources(
        task.source for task in task_list
    )
    source_predicate = query.compiled.source_predicate if query.compiled is not None else None
    for task in task_list:
        if not agentgrep.source_matches_scope(
            task.source,
            query.scope,
            prompt_history_agents=prompt_history_agents,
        ):
            continue
        if source_predicate is not None and not source_predicate(task.source):
            continue
        yield task


def _frontier_can_skip_remaining(
    query: agentgrep.SearchQuery,
    frontier: _FrontierState,
    _task: SourceTask,
) -> bool:
    """Return whether the source-order frontier already satisfies the limit."""
    return query.limit is not None and frontier.is_satisfied


def select_execution_driver(
    query: agentgrep.SearchQuery,
    plan: PhysicalSearchPlan,
    *,
    config: ExecutionDriverConfig | None = None,
) -> ExecutionDriver:
    """Choose the cheapest safe execution driver for one physical plan."""
    if _should_use_frontier_driver(query, plan):
        return FrontierExecutionDriver(config)
    return InlineExecutionDriver()


def _should_use_frontier_driver(
    query: agentgrep.SearchQuery,
    plan: PhysicalSearchPlan,
) -> bool:
    """Return whether the plan benefits from source-level scheduling."""
    return (
        query.limit is not None
        and query.match_surface == "haystack"
        and len(plan.tasks) > 1
        and any(task.limit_behavior == "bounded_source" for task in plan.tasks)
    )


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
    active_progress = agentgrep.noop_search_progress() if progress is None else progress
    source_started_at = time.perf_counter()
    records_seen = 0
    matches_seen = 0
    matching_records: list[agentgrep.SearchRecord] = []
    source_deduped: set[tuple[str, str, str, str, str]] = set()
    matcher = compile_record_matcher(query)

    def source_limit_satisfied() -> bool:
        return (
            task.limit_behavior == "bounded_source"
            and query.limit is not None
            and len(source_deduped if query.dedupe else matching_records) >= query.limit
        )

    for record in iter_source_task_records(task, query):
        if control.answer_now_requested():
            break
        records_seen += 1
        if matcher.matches(record):
            matches_seen += 1
            matching_records.append(record)
            if query.dedupe:
                source_deduped.add(agentgrep.record_dedupe_key(record))
            if source_limit_satisfied():
                break
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

    if task.limit_behavior == "drain_source":
        matching_records.sort(key=agentgrep.search_record_sort_key, reverse=True)
    return SourceScanResult(
        index=index,
        total=total,
        source=task.source,
        task=task,
        records=tuple(matching_records),
        records_seen=records_seen,
        matches_seen=matches_seen,
        duration_seconds=time.perf_counter() - source_started_at,
    )


def record_source_profile_sample(result: SourceScanResult) -> None:
    """Record one privacy-safe source execution timing sample."""
    agentgrep._record_engine_profile_sample(
        "search.collect.source",
        result.duration_seconds,
        **agentgrep._source_profile_attributes(result.source),
        agentgrep_source_strategy=result.task.strategy,
        agentgrep_records_seen=result.records_seen,
        agentgrep_matches_seen=result.matches_seen,
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
    if task.strategy == "jsonl_bounded_reverse_scan":
        yield from agentgrep.iter_source_records(task.source, reverse=True)
        return
    yield from agentgrep.iter_source_records(task.source)


def raw_text_skip_line_for_query(
    query: agentgrep.SearchQuery,
) -> cabc.Callable[[str], bool]:
    """Return a raw JSONL line skip predicate for a text-surface query."""
    if not query.terms:
        return lambda _raw_line: False
    if query.regex:
        return lambda raw_line: (
            "\\" not in raw_line
            and not agentgrep.matches_text(
                raw_line,
                query,
            )
        )

    needles = (
        query.terms if query.case_sensitive else tuple(term.casefold() for term in query.terms)
    )
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
