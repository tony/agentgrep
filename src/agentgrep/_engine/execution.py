"""Search physical-plan execution helpers."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import time
import typing as t

import agentgrep
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

            source_started_at = time.perf_counter()
            records_seen = 0
            matches_seen = 0
            matching_records: list[agentgrep.SearchRecord] = []
            for record in iter_source_task_records(task, query):
                if active_control.answer_now_requested():
                    break
                records_seen += 1
                if agentgrep.matches_record(record, query):
                    matches_seen += 1
                    matching_records.append(record)
                if records_seen % agentgrep._SOURCE_PROGRESS_RECORD_INTERVAL == 0:
                    agentgrep._report_source_progress(
                        active_progress,
                        index,
                        total,
                        source,
                        records_seen,
                        matches_seen,
                    )
                    time.sleep(0)

            active_progress.source_finished(
                index,
                total,
                source,
                records_seen,
                matches_seen,
            )
            agentgrep._record_engine_profile_sample(
                "search.collect.source",
                time.perf_counter() - source_started_at,
                **agentgrep._source_profile_attributes(source),
                agentgrep_source_strategy=task.strategy,
                agentgrep_records_seen=records_seen,
                agentgrep_matches_seen=matches_seen,
            )

            matching_records.sort(key=agentgrep.search_record_sort_key, reverse=True)
            for record in matching_records:
                if query.dedupe:
                    dedupe_key = agentgrep.record_dedupe_key(record)
                    if dedupe_key in deduped:
                        continue
                    deduped[dedupe_key] = record
                    result_count = len(deduped)
                else:
                    raw_count += 1
                    result_count = raw_count
                active_progress.record_added(record)
                active_progress.result_added(result_count)
                yield ExecutionRecordEmitted(record=record, result_count=result_count)
                if active_control.answer_now_requested() or (
                    query.limit is not None and current_count() >= query.limit
                ):
                    break

            yield ExecutionSourceFinished(
                index=index,
                total=total,
                source=source,
                task=task,
                records_seen=records_seen,
                matches_seen=matches_seen,
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
    yield from agentgrep.iter_source_records(task.source)


def raw_text_skip_line_for_query(
    query: agentgrep.SearchQuery,
) -> cabc.Callable[[str], bool]:
    """Return a raw JSONL line skip predicate for a text-surface query."""

    def skip_line(raw_line: str) -> bool:
        if "\\" in raw_line:
            return False
        return not agentgrep.matches_text(raw_line, query)

    return skip_line
