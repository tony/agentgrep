"""Execution drivers and source-task scheduling for search plans."""

from __future__ import annotations

import collections.abc as cabc
import concurrent.futures
import dataclasses
import queue
import time
import typing as t

import agentgrep
from agentgrep._engine import scanning
from agentgrep._engine.planning import PhysicalSearchPlan, SourceTask

if t.TYPE_CHECKING:
    from agentgrep._engine.runtime import SearchRuntime


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
class ExecutionDriverConfig:
    """Execution-driver tuning for bounded source scheduling."""

    max_workers: int = 1
    use_source_batches: bool = False

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
        runtime: SearchRuntime | None = None,
    ) -> cabc.Iterator[SearchExecutionEvent]:
        """Yield internal search execution events.

        Parameters
        ----------
        query : agentgrep.SearchQuery
            Compiled query — terms, agents, dedup choice, limit.
        plan : PhysicalSearchPlan
            Planned source tasks from
            :func:`agentgrep._engine.planning.build_physical_search_plan`.
        progress : agentgrep.SearchProgress or None
            Progress sink for source and record events. ``None`` uses
            the no-op sink.
        control : agentgrep.SearchControl or None
            Optional control handle polled between records and source
            tasks so consumers can stop the scan early.
        runtime : SearchRuntime or None
            Optional reusable runtime state; supplies the source-scan
            cache when one is configured.

        Yields
        ------
        SearchExecutionEvent
            One started and one finished event per submitted source,
            plus deduplicated record events.
        """
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
        runtime: SearchRuntime | None = None,
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

            result = scanning.scan_source_task(
                query,
                task,
                index=index,
                total=total,
                control=active_control,
                progress=active_progress,
                runtime=runtime,
            )
            active_progress.source_finished(
                index,
                total,
                source,
                result.records_seen,
                result.matches_seen,
            )
            scanning.record_source_profile_sample(result)

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
        runtime: SearchRuntime | None = None,
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
        cancelled_count = 0
        cancellation_requested_count = 0
        batch_count = 0
        queued_batch_count = 0
        processed_batch_count = 0
        queue_wait_seconds = 0.0
        scheduler_started_at = time.perf_counter()
        max_workers = min(self._config.worker_count, total)
        if not self._config.use_source_batches:
            yield from _iter_search_plan_whole_sources(
                query,
                tasks,
                progress=active_progress,
                control=active_control,
                scheduler_started_at=scheduler_started_at,
                max_workers=max_workers,
                runtime=runtime,
            )
            return
        if max_workers == 1:
            yield from _iter_search_plan_single_worker_batches(
                query,
                tasks,
                progress=active_progress,
                control=active_control,
                scheduler_started_at=scheduler_started_at,
                runtime=runtime,
            )
            return
        cache = runtime.source_scan_cache if runtime is not None else None
        next_task_index = 0
        batch_queue: queue.Queue[_QueueItem] = queue.Queue()
        running: dict[int, _RunningSourceTask] = {}
        futures: dict[concurrent.futures.Future[None], int] = {}
        deferred_error: BaseException | None = None

        def submit_next(
            executor: concurrent.futures.ThreadPoolExecutor,
        ) -> cabc.Iterator[SearchExecutionEvent]:
            nonlocal next_task_index, skipped_count, submitted_count, completed_count
            while len(running) < max_workers and next_task_index < total:
                index = next_task_index + 1
                task = tasks[next_task_index]
                if _frontier_can_skip_remaining(query, frontier, task):
                    skipped_count += total - next_task_index
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
                # Cache lookups happen on the owner thread so workers never
                # touch cache state and completion ordering stays simple.
                lookup_started_at = time.perf_counter()
                cache_key, cached = scanning.cached_source_scan_lookup(
                    query,
                    task,
                    control=active_control,
                    cache=cache,
                )
                if cached is not None:
                    frontier.add_records(cached.records)
                    completed_count += 1
                    active_progress.source_finished(
                        index,
                        total,
                        task.source,
                        cached.records_seen,
                        cached.matches_seen,
                    )
                    scanning.record_source_profile_sample(
                        scanning.SourceScanResult(
                            index=index,
                            total=total,
                            source=task.source,
                            task=task,
                            records=(),
                            records_seen=cached.records_seen,
                            matches_seen=cached.matches_seen,
                            duration_seconds=time.perf_counter() - lookup_started_at,
                            batch_count=cached.batch_count,
                            cache_hit=True,
                        ),
                    )
                    yield ExecutionSourceFinished(
                        index=index,
                        total=total,
                        source=task.source,
                        task=task,
                        records_seen=cached.records_seen,
                        matches_seen=cached.matches_seen,
                    )
                    continue
                task_control = _TaskSearchControl(active_control)
                running[index] = _RunningSourceTask(
                    index=index,
                    task=task,
                    control=task_control,
                    cache_key=cache_key,
                )
                future = executor.submit(
                    _scan_source_task_to_queue,
                    query,
                    task,
                    index=index,
                    total=total,
                    control=task_control,
                    batch_queue=batch_queue,
                )
                futures[future] = index

        def request_lower_priority_cancellation(source_index: int) -> None:
            nonlocal cancellation_requested_count
            for running_task in running.values():
                if (
                    running_task.index > source_index
                    and not running_task.control.answer_now_requested()
                ):
                    running_task.control.request_answer_now()
                    cancellation_requested_count += 1

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            yield from submit_next(executor)
            while running:
                if active_control.answer_now_requested():
                    for running_task in running.values():
                        if not running_task.control.answer_now_requested():
                            running_task.control.request_answer_now()
                            cancellation_requested_count += 1
                    for future, index in tuple(futures.items()):
                        if future.cancelled():
                            continue
                        if future.cancel():
                            cancelled_count += 1
                            # A queued task whose future cancels never runs,
                            # so it never posts a completion item: release it
                            # here and emit its finished event to keep the
                            # started/finished pairing and let the drain loop
                            # exit.
                            cancelled_task = running.pop(index, None)
                            if cancelled_task is not None:
                                active_progress.source_finished(
                                    index,
                                    total,
                                    cancelled_task.task.source,
                                    0,
                                    0,
                                )
                                yield ExecutionSourceFinished(
                                    index=index,
                                    total=total,
                                    source=cancelled_task.task.source,
                                    task=cancelled_task.task,
                                    records_seen=0,
                                    matches_seen=0,
                                )

                queue_wait_started_at = time.perf_counter()
                try:
                    item = batch_queue.get(timeout=0.05)
                except queue.Empty:
                    queue_wait_seconds += time.perf_counter() - queue_wait_started_at
                    continue
                queue_wait_seconds += time.perf_counter() - queue_wait_started_at

                if isinstance(item, scanning.SourceScanBatch):
                    queued_batch_count += 1
                    batch_count += 1
                    processed_batch_count += 1
                    running_task = running.get(item.index)
                    if running_task is not None:
                        running_task.batch_count += 1
                        running_task.records_seen = item.records_seen
                        running_task.matches_seen = item.matches_seen
                        if running_task.cache_key is not None:
                            running_task.records.extend(item.records)
                    frontier.add_records(item.records)
                    if frontier.is_satisfied:
                        request_lower_priority_cancellation(item.index)
                    continue

                if isinstance(item, _SourceTaskFailed):
                    deferred_error = item.error
                    # The failed worker never sends a matching completion
                    # item, so drop it from the running set here or the
                    # drain loop waits on an empty queue forever.
                    running.pop(item.index, None)
                    for running_task in running.values():
                        running_task.control.request_answer_now()
                    continue

                running_task = running.pop(item.index, None)
                if running_task is None:
                    continue
                completed_count += 1
                active_progress.source_finished(
                    item.index,
                    total,
                    item.task.source,
                    item.records_seen,
                    item.matches_seen,
                )
                completed_result = scanning.SourceScanResult(
                    index=item.index,
                    total=total,
                    source=item.task.source,
                    task=item.task,
                    records=tuple(running_task.records),
                    records_seen=item.records_seen,
                    matches_seen=item.matches_seen,
                    duration_seconds=item.duration_seconds,
                    batch_count=running_task.batch_count,
                )
                scanning.record_source_profile_sample(completed_result)
                scanning.remember_source_scan(
                    cache,
                    running_task.cache_key,
                    control=running_task.control,
                    result=completed_result,
                )
                yield ExecutionSourceFinished(
                    index=item.index,
                    total=total,
                    source=item.task.source,
                    task=item.task,
                    records_seen=item.records_seen,
                    matches_seen=item.matches_seen,
                )
                if frontier.is_satisfied:
                    request_lower_priority_cancellation(item.index)
                yield from submit_next(executor)

            for future, _index in tuple(futures.items()):
                if future.cancelled():
                    continue
                future.result()

        if deferred_error is not None:
            raise deferred_error

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
            agentgrep_cancelled_source_count=cancelled_count,
            agentgrep_cancellation_requested_source_count=cancellation_requested_count,
            agentgrep_batch_count=batch_count,
            agentgrep_processed_batch_count=processed_batch_count,
            agentgrep_queued_batch_count=queued_batch_count,
            agentgrep_queue_wait_seconds=queue_wait_seconds,
            agentgrep_emitted_record_count=emitted_count,
        )


@dataclasses.dataclass(slots=True)
class _RunningSourceTask:
    """Owner-thread counters for a running source task."""

    index: int
    task: SourceTask
    control: _TaskSearchControl
    cache_key: scanning._SourceScanCacheKey | None = None
    batch_count: int = 0
    records_seen: int = 0
    matches_seen: int = 0
    records: list[agentgrep.SearchRecord] = dataclasses.field(default_factory=list)


def _iter_search_plan_whole_sources(
    query: agentgrep.SearchQuery,
    tasks: tuple[SourceTask, ...],
    *,
    progress: agentgrep.SearchProgress,
    control: agentgrep.SearchControl,
    scheduler_started_at: float,
    max_workers: int,
    runtime: SearchRuntime | None = None,
) -> cabc.Iterator[SearchExecutionEvent]:
    """Yield search events by scheduling whole-source scan results."""
    total = len(tasks)
    frontier = _FrontierState(query)
    submitted_count = 0
    completed_count = 0
    skipped_count = 0
    cancelled_count = 0
    batch_count = 0
    next_task_index = 0
    futures: dict[concurrent.futures.Future[scanning.SourceScanResult], tuple[int, SourceTask]] = {}

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
            progress.source_started(index, total, task.source)
            yield ExecutionSourceStarted(
                index=index,
                total=total,
                source=task.source,
                task=task,
            )
            future = executor.submit(
                scanning.scan_source_task,
                query,
                task,
                index=index,
                total=total,
                control=control,
                progress=None,
                runtime=runtime,
            )
            futures[future] = (index, task)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        yield from submit_next(executor)
        while futures:
            if control.answer_now_requested():
                for future, (index, task) in sorted(
                    futures.items(),
                    key=lambda item: item[1][0],
                ):
                    if future.cancel():
                        cancelled_count += 1
                    # Results from still-running workers are discarded at
                    # executor shutdown; emit each remaining source's
                    # finished event so the started/finished pairing holds
                    # on early exit.
                    progress.source_finished(index, total, task.source, 0, 0)
                    yield ExecutionSourceFinished(
                        index=index,
                        total=total,
                        source=task.source,
                        task=task,
                        records_seen=0,
                        matches_seen=0,
                    )
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
                batch_count += result.batch_count
                progress.source_finished(
                    result.index,
                    result.total,
                    result.source,
                    result.records_seen,
                    result.matches_seen,
                )
                scanning.record_source_profile_sample(result)
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
        progress.record_added(record)
        progress.result_added(emitted_count)
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
        agentgrep_cancelled_source_count=cancelled_count,
        agentgrep_cancellation_requested_source_count=0,
        agentgrep_batch_count=batch_count,
        agentgrep_processed_batch_count=batch_count,
        agentgrep_queued_batch_count=0,
        agentgrep_queue_wait_seconds=0.0,
        agentgrep_emitted_record_count=emitted_count,
    )


def _iter_search_plan_single_worker_batches(
    query: agentgrep.SearchQuery,
    tasks: tuple[SourceTask, ...],
    *,
    progress: agentgrep.SearchProgress,
    control: agentgrep.SearchControl,
    scheduler_started_at: float,
    runtime: SearchRuntime | None = None,
) -> cabc.Iterator[SearchExecutionEvent]:
    """Yield search events by consuming source batches on the owner thread."""
    total = len(tasks)
    frontier = _FrontierState(query)
    cache = runtime.source_scan_cache if runtime is not None else None
    submitted_count = 0
    completed_count = 0
    skipped_count = 0
    batch_count = 0
    processed_batch_count = 0

    for index, task in enumerate(tasks, start=1):
        if control.answer_now_requested():
            skipped_count += total - index + 1
            break
        if _frontier_can_skip_remaining(query, frontier, task):
            skipped_count += total - index + 1
            break

        submitted_count += 1
        progress.source_started(index, total, task.source)
        yield ExecutionSourceStarted(index=index, total=total, source=task.source, task=task)

        source_started_at = time.perf_counter()
        cache_key, cached = scanning.cached_source_scan_lookup(
            query,
            task,
            control=control,
            cache=cache,
        )
        if cached is not None:
            frontier.add_records(cached.records)
            completed_count += 1
            progress.source_finished(
                index,
                total,
                task.source,
                cached.records_seen,
                cached.matches_seen,
            )
            scanning.record_source_profile_sample(
                scanning.SourceScanResult(
                    index=index,
                    total=total,
                    source=task.source,
                    task=task,
                    records=(),
                    records_seen=cached.records_seen,
                    matches_seen=cached.matches_seen,
                    duration_seconds=time.perf_counter() - source_started_at,
                    batch_count=cached.batch_count,
                    cache_hit=True,
                ),
            )
            yield ExecutionSourceFinished(
                index=index,
                total=total,
                source=task.source,
                task=task,
                records_seen=cached.records_seen,
                matches_seen=cached.matches_seen,
            )
            continue

        source_batch_count = 0
        records_seen = 0
        matches_seen = 0
        collected_records: list[agentgrep.SearchRecord] = []
        for batch in scanning.iter_source_task_batches(
            query,
            task,
            index=index,
            total=total,
            control=control,
            progress=None,
        ):
            batch_count += 1
            processed_batch_count += 1
            source_batch_count += 1
            records_seen = batch.records_seen
            matches_seen = batch.matches_seen
            collected_records.extend(batch.records)
            frontier.add_records(batch.records)
            if control.answer_now_requested():
                break

        completed_count += 1
        progress.source_finished(index, total, task.source, records_seen, matches_seen)
        completed_result = scanning.SourceScanResult(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=tuple(collected_records),
            records_seen=records_seen,
            matches_seen=matches_seen,
            duration_seconds=time.perf_counter() - source_started_at,
            batch_count=source_batch_count,
        )
        scanning.record_source_profile_sample(completed_result)
        scanning.remember_source_scan(
            cache,
            cache_key,
            control=control,
            result=completed_result,
        )
        yield ExecutionSourceFinished(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records_seen=records_seen,
            matches_seen=matches_seen,
        )

    emitted_count = 0
    for record in frontier.records():
        emitted_count += 1
        progress.record_added(record)
        progress.result_added(emitted_count)
        yield ExecutionRecordEmitted(record=record, result_count=emitted_count)

    agentgrep._record_engine_profile_sample(
        "search.collect.scheduler",
        time.perf_counter() - scheduler_started_at,
        agentgrep_execution_driver="frontier",
        agentgrep_worker_count=1,
        agentgrep_source_count=total,
        agentgrep_submitted_source_count=submitted_count,
        agentgrep_completed_source_count=completed_count,
        agentgrep_skipped_source_count=skipped_count,
        agentgrep_cancelled_source_count=0,
        agentgrep_cancellation_requested_source_count=0,
        agentgrep_batch_count=batch_count,
        agentgrep_processed_batch_count=processed_batch_count,
        agentgrep_queued_batch_count=processed_batch_count,
        agentgrep_queue_wait_seconds=0.0,
        agentgrep_emitted_record_count=emitted_count,
    )


@dataclasses.dataclass(frozen=True, slots=True)
class _SourceTaskCompleted:
    """Worker completion message for one source task."""

    index: int
    task: SourceTask
    records_seen: int
    matches_seen: int
    duration_seconds: float


@dataclasses.dataclass(frozen=True, slots=True)
class _SourceTaskFailed:
    """Worker failure message for one source task."""

    index: int
    task: SourceTask
    error: BaseException


type _QueueItem = scanning.SourceScanBatch | _SourceTaskCompleted | _SourceTaskFailed


class _TaskSearchControl(agentgrep.SearchControl):
    """Search control that honors both user and scheduler cancellation."""

    def __init__(self, parent: agentgrep.SearchControl) -> None:
        super().__init__()
        self._parent = parent

    def answer_now_requested(self) -> bool:
        """Return whether the user or scheduler asked this task to stop."""
        return self._parent.answer_now_requested() or super().answer_now_requested()


def _scan_source_task_to_queue(
    query: agentgrep.SearchQuery,
    task: SourceTask,
    *,
    index: int,
    total: int,
    control: agentgrep.SearchControl,
    batch_queue: queue.Queue[_QueueItem],
) -> None:
    """Run one source scan and push batches/completion to the scheduler."""
    source_started_at = time.perf_counter()
    records_seen = 0
    matches_seen = 0
    try:
        for batch in scanning.iter_source_task_batches(
            query,
            task,
            index=index,
            total=total,
            control=control,
            progress=None,
        ):
            records_seen = batch.records_seen
            matches_seen = batch.matches_seen
            batch_queue.put(batch)
    except BaseException as error:
        batch_queue.put(_SourceTaskFailed(index=index, task=task, error=error))
    else:
        batch_queue.put(
            _SourceTaskCompleted(
                index=index,
                task=task,
                records_seen=records_seen,
                matches_seen=matches_seen,
                duration_seconds=time.perf_counter() - source_started_at,
            ),
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
    task: SourceTask,
) -> bool:
    """Return whether the source-order frontier already satisfies the limit."""
    return task.limit_policy.can_skip_remaining(query=query, frontier=frontier)


def select_execution_driver(
    query: agentgrep.SearchQuery,
    plan: PhysicalSearchPlan,
    *,
    config: ExecutionDriverConfig | None = None,
) -> ExecutionDriver:
    """Choose the cheapest safe execution driver for one physical plan.

    Parameters
    ----------
    query : agentgrep.SearchQuery
        Compiled query — terms, agents, dedup choice, limit.
    plan : PhysicalSearchPlan
        Planned source tasks whose strategies and limit behaviors
        gate frontier-driver eligibility.
    config : ExecutionDriverConfig or None
        Worker-count and batch-scheduling tuning. ``None`` uses the
        defaults.

    Returns
    -------
    ExecutionDriver
        The frontier driver for limited bounded haystack plans;
        otherwise the inline driver.
    """
    active_config = ExecutionDriverConfig() if config is None else config
    if _should_use_frontier_driver(query, plan, config=active_config):
        return FrontierExecutionDriver(active_config)
    return InlineExecutionDriver()


def _should_use_frontier_driver(
    query: agentgrep.SearchQuery,
    plan: PhysicalSearchPlan,
    *,
    config: ExecutionDriverConfig,
) -> bool:
    """Return whether the plan benefits from source-level scheduling."""
    if (
        query.limit is None
        or len(plan.tasks) <= 1
        or not any(task.limit_behavior == "bounded_source" for task in plan.tasks)
    ):
        return False
    if query.match_surface == "haystack":
        return True
    return query.match_surface == "text" and config.worker_count > 1
