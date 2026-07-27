"""Engine-owned search lifecycle summary contracts."""

from __future__ import annotations

import dataclasses
import json
import pathlib
import typing as t

import pytest

import agentgrep.cli.render as cli_render
from agentgrep import (
    GrepArgs,
    SearchArgs,
    SearchControl,
    SearchQuery,
    iter_search_events,
    parse_args,
    run_search_result,
    search_sources,
)
from agentgrep._engine.planning import PhysicalSearchPlan, SourceTask
from agentgrep._engine.scanning import SourceScanResult
from agentgrep._engine.scheduling import (
    ExecutionRunFinished,
    ExecutionSourceStarted,
)
from agentgrep.cli.serializers import serialize_run_summary
from agentgrep.events import SearchFinished, SearchStarted, SourceFinished
from agentgrep.progress import NoopSearchProgress
from agentgrep.records import (
    BackendSelection,
    SearchEffort,
    SearchRecord,
    SearchScope,
    SearchScopeProvenance,
    SourceHandle,
)
from agentgrep.results import (
    RunCoverage,
    RunSummary,
    SearchRequestPatch,
    apply_search_request_patch,
    build_search_summary,
)
from agentgrep.stores import PathKind


def _no_backends() -> BackendSelection:
    """Return deterministic pure-Python backend selection."""
    return BackendSelection(find_tool=None, grep_tool=None, json_tool=None)


def _query(
    *,
    terms: tuple[str, ...] = ("missing",),
    scope: SearchScope = "prompts",
    limit: int | None = None,
    effort: SearchEffort = "prompt",
    conversation_limit: int | None = None,
    scope_provenance: SearchScopeProvenance = "inferred",
) -> SearchQuery:
    """Build one single-agent request over the flags every case here shares."""
    return SearchQuery(
        terms=terms,
        scope=scope,
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=limit,
        effort=effort,
        conversation_limit=conversation_limit,
        scope_provenance=scope_provenance,
    )


def _source(
    home: pathlib.Path,
    *,
    store: str = "codex.history",
    adapter_id: str = "codex.history_jsonl.v1",
    filename: str = "history.jsonl",
    path_kind: PathKind = "history_file",
) -> SourceHandle:
    """Build one synthetic Codex source rooted in an isolated home."""
    return SourceHandle(
        agent="codex",
        store=store,
        adapter_id=adapter_id,
        path=home / filename,
        path_kind=path_kind,
        source_kind="jsonl",
        search_root=home,
        mtime_ns=0,
    )


def _empty_coverage() -> RunCoverage:
    """Return one internally consistent zero-coverage baseline."""
    return RunCoverage(
        sources_discovered=0,
        sources_eligible=0,
        sources_planned=0,
        sources_attempted=0,
        sources_completed=0,
        sources_bounded=0,
        sources_skipped=0,
        sources_unsupported=0,
        sources_failed=0,
        sources_cancelled=0,
        records_seen=0,
        matches_seen=0,
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sources_failed": -1}, "non-negative"),
        ({"records_seen": 0, "matches_seen": 1}, "matches_seen"),
        (
            {"sources_discovered": 1, "sources_eligible": 1, "sources_planned": 1},
            "attempted plus skipped",
        ),
        (
            {"conversations_eligible": 1, "conversations_selected": 2},
            "conversation coverage",
        ),
    ],
)
def test_coverage_rejects_impossible_counts(
    changes: dict[str, int],
    message: str,
) -> None:
    """Keep malformed lifecycle evidence from crossing a typed boundary."""
    with pytest.raises(ValueError, match=message):
        dataclasses.replace(_empty_coverage(), **changes)


def test_prompt_completion_carries_engine_owned_summary(
    tmp_path: pathlib.Path,
) -> None:
    """Expose one normalized terminal contract even when no source matches."""
    events = tuple(iter_search_events(tmp_path, _query(), backends=_no_backends()))

    finished = events[-1]
    assert isinstance(finished, SearchFinished)
    summary = finished.summary
    assert summary.request.scope == "prompts"
    assert summary.request.effort == "prompt"
    assert summary.request.order == "newest"
    assert summary.request.scope_provenance == "inferred"
    assert summary.requested_effort == "prompt"
    assert summary.completed_effort == "prompt"
    assert summary.status.state == "complete"
    assert summary.status.reason is None
    assert summary.outcome == "no_prompt_match"
    assert summary.coverage.sources_discovered == 0
    assert summary.coverage.sources_eligible == 0
    assert summary.coverage.sources_planned == 0
    assert summary.coverage.sources_attempted == 0
    assert summary.coverage.sources_completed == 0
    assert summary.coverage.sources_unsupported == 0
    assert summary.coverage.sources_failed == 0
    assert summary.coverage.records_seen == 0
    assert summary.coverage.matches_seen == 0
    assert summary.match_count == 0
    assert summary.applied_order == "newest"
    assert summary.limit is None
    assert summary.diagnostics == ()
    assert len(summary.next_actions) == 2
    action = summary.next_actions[0]
    assert action.action_id == "search.targeted"
    assert action.kind == "search.escalate_effort"
    assert action.requires_confirmation is False
    assert action.patch.effort == "targeted"
    assert action.patch.scope == "all"
    assert action.patch.conversation_limit == 25
    assert summary.next_actions[1].action_id == "search.exhaustive"


def test_event_stream_forwards_terminal_summary_to_progress_sink(
    tmp_path: pathlib.Path,
) -> None:
    """Let interactive progress consumers use the engine's terminal evidence."""

    class SummaryProgress(NoopSearchProgress):
        def __init__(self) -> None:
            self.summaries: list[RunSummary] = []

        def summary_finished(self, summary: RunSummary) -> None:
            self.summaries.append(summary)

    progress = SummaryProgress()

    events = tuple(
        iter_search_events(tmp_path, _query(), backends=_no_backends(), progress=progress),
    )

    finished = events[-1]
    assert isinstance(finished, SearchFinished)
    assert progress.summaries == [finished.summary]


def test_request_patch_clears_target_only_limit_when_escalating() -> None:
    """Keep an engine-authored exhaustive follow-up valid after targeted search."""
    query = _query(
        terms=("needle",),
        scope="all",
        limit=10,
        effort="targeted",
        conversation_limit=7,
    )

    patched = apply_search_request_patch(
        query,
        SearchRequestPatch(effort="exhaustive"),
    )

    assert patched.effort == "exhaustive"
    assert patched.scope == "all"
    assert patched.conversation_limit is None
    assert patched.limit == 10


def test_search_result_collects_records_and_the_unique_terminal_summary(
    tmp_path: pathlib.Path,
) -> None:
    """Expose the validated list-shaped contract used by structured sinks."""
    result = run_search_result(tmp_path, _query(), backends=_no_backends())

    assert result.records == ()
    assert result.summary.match_count == 0
    assert result.summary.outcome == "no_prompt_match"
    payload = serialize_run_summary(result.summary)
    assert set(payload) == {
        "request",
        "effort",
        "status",
        "outcome",
        "coverage",
        "stats",
        "diagnostics",
        "next_actions",
    }
    assert payload["status"] == {
        "state": "complete",
        "reason": None,
        "conditions": [],
    }
    assert payload["effort"] == {
        "requested": "prompt",
        "completed": "prompt",
    }
    coverage = t.cast("dict[str, object]", payload["coverage"])
    assert coverage["source_stop_reasons"] == []
    assert payload["diagnostics"] == []
    next_actions = t.cast("list[dict[str, object]]", payload["next_actions"])
    patch = t.cast("dict[str, object]", next_actions[0]["patch"])
    assert patch == {
        "effort": "targeted",
        "scope": "all",
        "conversation_limit": 25,
    }


def test_unsupported_source_is_not_reported_as_a_clean_miss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Synthetic discovery is required because fixtures expose readable adapters."""
    source = _source(tmp_path, adapter_id="codex.unknown.v1")
    monkeypatch.setattr(
        "agentgrep._engine.search.discover_sources_for_search",
        lambda *_args, **_kwargs: [source],
    )
    query = _query(scope="all", effort="exhaustive")

    events = tuple(iter_search_events(tmp_path, query, backends=_no_backends()))

    source_finished = next(event for event in events if isinstance(event, SourceFinished))
    assert source_finished.outcome == "unsupported"
    finished = events[-1]
    assert isinstance(finished, SearchFinished)
    summary = finished.summary
    assert summary.status.state == "failed"
    assert summary.status.reason == "unsupported_source"
    assert summary.status.conditions == ("unsupported_source",)
    assert summary.outcome == "undetermined"
    assert summary.completed_effort is None
    assert summary.coverage.sources_attempted == 1
    assert summary.coverage.sources_completed == 0
    assert summary.coverage.sources_unsupported == 1
    assert summary.coverage.sources_failed == 0
    assert tuple(diagnostic.code for diagnostic in summary.diagnostics) == ("unsupported_source",)
    assert summary.next_actions == ()


def test_source_failure_finishes_with_privacy_safe_engine_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Synthetic parser failure is required because sample adapters are valid."""
    source = _source(
        tmp_path,
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        filename="sessions.jsonl",
        path_kind="session_file",
    )
    monkeypatch.setattr(
        "agentgrep._engine.search.discover_sources_for_search",
        lambda *_args, **_kwargs: [source],
    )

    def fail_source_read(*_args: object, **_kwargs: object) -> None:
        message = f"sensitive failure at {source.path}"
        raise OSError(message)

    monkeypatch.setattr(
        "agentgrep._engine.scanning.iter_source_task_records",
        fail_source_read,
    )
    query = _query(scope="all", effort="exhaustive")

    events = tuple(iter_search_events(tmp_path, query, backends=_no_backends()))

    source_finished = next(event for event in events if isinstance(event, SourceFinished))
    assert source_finished.outcome == "failed"
    finished = events[-1]
    assert isinstance(finished, SearchFinished)
    summary = finished.summary
    assert summary.status.state == "failed"
    assert summary.status.reason == "source_failure"
    assert summary.status.conditions == ("source_failure",)
    assert summary.coverage.sources_attempted == 1
    assert summary.coverage.sources_completed == 0
    assert summary.coverage.sources_failed == 1
    assert summary.coverage.sources_unsupported == 0
    assert tuple(diagnostic.code for diagnostic in summary.diagnostics) == ("source_failure",)
    assert all(str(source.path) not in item.message for item in summary.diagnostics)
    with pytest.raises(OSError, match="sensitive failure"):
        search_sources(query, [source], _no_backends())


def test_discovery_failure_finishes_with_privacy_safe_engine_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Terminalize failures that occur before a physical plan exists."""
    private_detail = f"sensitive discovery failure at {tmp_path}"

    def fail_discovery(*_args: object, **_kwargs: object) -> list[SourceHandle]:
        raise OSError(private_detail)

    monkeypatch.setattr(
        "agentgrep._engine.search.discover_sources_for_search",
        fail_discovery,
    )

    events = tuple(iter_search_events(tmp_path, _query(), backends=_no_backends()))

    assert isinstance(events[0], SearchStarted)
    assert events[0].source_count == 0
    finished = events[-1]
    assert isinstance(finished, SearchFinished)
    assert finished.summary.status.state == "failed"
    assert finished.summary.status.reason == "engine_failure"
    assert finished.summary.coverage.sources_discovered == 0
    assert finished.summary.coverage.sources_attempted == 0
    assert tuple(item.code for item in finished.summary.diagnostics) == ("engine_failure",)
    assert all(private_detail not in item.message for item in finished.summary.diagnostics)


@pytest.mark.parametrize(
    "driver_fault",
    ["missing_terminal", "early_terminal", "duplicate_start"],
)
def test_malformed_driver_terminalizes_every_started_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    driver_fault: str,
) -> None:
    """Preserve source pairing and the final summary on driver protocol failure."""
    source = _source(tmp_path)
    monkeypatch.setattr(
        "agentgrep._engine.search.discover_sources_for_search",
        lambda *_args, **_kwargs: [source],
    )

    class MalformedDriver:
        """Yield one unpaired source start, with or without a run terminal."""

        def iter_search_plan(
            self,
            _query: SearchQuery,
            plan: PhysicalSearchPlan,
            **_kwargs: object,
        ) -> t.Iterator[ExecutionSourceStarted | ExecutionRunFinished]:
            task = plan.tasks[0]
            yield ExecutionSourceStarted(
                index=1,
                total=1,
                source=task.source,
                task=task,
            )
            if driver_fault == "early_terminal":
                yield ExecutionRunFinished(
                    accepted_count=0,
                    has_more=False,
                    stop_reason=None,
                )
            elif driver_fault == "duplicate_start":
                yield ExecutionSourceStarted(
                    index=1,
                    total=1,
                    source=task.source,
                    task=task,
                )

    monkeypatch.setattr(
        "agentgrep._engine.execution.select_execution_driver",
        lambda *_args, **_kwargs: MalformedDriver(),
    )

    events = tuple(iter_search_events(tmp_path, _query(), backends=_no_backends()))

    source_finished = [event for event in events if isinstance(event, SourceFinished)]
    assert len(source_finished) == 1
    assert source_finished[0].outcome == "failed"
    finished = events[-1]
    assert isinstance(finished, SearchFinished)
    assert finished.summary.status.state == "failed"
    assert finished.summary.status.reason == "source_failure"
    assert finished.summary.coverage.sources_attempted == 1
    assert finished.summary.coverage.sources_failed == 1


def test_answer_now_is_a_bounded_return_not_a_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Synthetic discovery lets the control stop one already-planned source."""
    source = _source(tmp_path)
    monkeypatch.setattr(
        "agentgrep._engine.search.discover_sources_for_search",
        lambda *_args, **_kwargs: [source],
    )
    control = SearchControl()
    query = _query()
    event_stream = iter_search_events(
        tmp_path,
        query,
        backends=_no_backends(),
        control=control,
    )

    started = next(event_stream)
    assert isinstance(started, SearchStarted)
    assert started.source_count == 1
    control.request_answer_now()
    remaining = tuple(event_stream)

    finished = remaining[-1]
    assert isinstance(finished, SearchFinished)
    summary = finished.summary
    assert summary.status.state == "bounded"
    assert summary.status.reason == "answer_now"
    assert summary.status.conditions == ("answer_now",)
    assert summary.outcome == "undetermined"
    assert summary.completed_effort is None
    assert summary.coverage.sources_planned == 1
    assert summary.coverage.sources_attempted == 0
    assert summary.coverage.sources_skipped == 1
    assert summary.coverage.sources_cancelled == 0

    cancellation = SearchControl()
    cancelled_stream = iter_search_events(
        tmp_path,
        query,
        backends=_no_backends(),
        control=cancellation,
    )
    assert isinstance(next(cancelled_stream), SearchStarted)
    cancellation.request_answer_now(reason="caller_cancelled")
    cancelled_events = tuple(cancelled_stream)
    cancelled_finished = cancelled_events[-1]
    assert isinstance(cancelled_finished, SearchFinished)
    assert cancelled_finished.summary.status.state == "cancelled"
    assert cancelled_finished.summary.status.reason == "cancelled"
    assert cancelled_finished.summary.status.conditions == ("cancelled",)


def test_result_limit_is_bounded_only_when_an_extra_result_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Synthetic scanner output proves exact post-dedup lookahead."""
    source = _source(tmp_path)
    monkeypatch.setattr(
        "agentgrep._engine.search.discover_sources_for_search",
        lambda *_args, **_kwargs: [source],
    )
    records = [
        SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.history",
            adapter_id="codex.history_jsonl.v1",
            path=source.path,
            text="newest",
            timestamp="2026-02-01T00:00:00Z",
        ),
    ]

    def scan_source(
        _query: SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        **_kwargs: object,
    ) -> SourceScanResult:
        return SourceScanResult(
            index=index,
            total=total,
            source=source,
            task=task,
            records=tuple(records),
            records_seen=len(records),
            matches_seen=len(records),
            duration_seconds=0.0,
        )

    monkeypatch.setattr("agentgrep._engine.scanning.scan_source_task", scan_source)
    query = _query(terms=("match",), limit=1)

    exact_events = tuple(iter_search_events(tmp_path, query, backends=_no_backends()))
    exact_finished = exact_events[-1]
    assert isinstance(exact_finished, SearchFinished)
    assert exact_finished.summary.status.state == "complete"
    assert exact_finished.summary.status.reason is None

    records.append(
        SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.history",
            adapter_id="codex.history_jsonl.v1",
            path=source.path,
            text="older",
            timestamp="2026-01-01T00:00:00Z",
        ),
    )
    extra_events = tuple(iter_search_events(tmp_path, query, backends=_no_backends()))
    extra_finished = extra_events[-1]
    assert isinstance(extra_finished, SearchFinished)
    assert extra_finished.summary.status.state == "bounded"
    assert extra_finished.summary.status.reason == "result_limit"

    scan_events = tuple(
        iter_search_events(
            tmp_path,
            dataclasses.replace(query, order="scan"),
            backends=_no_backends(),
        ),
    )
    scan_finished = scan_events[-1]
    assert isinstance(scan_finished, SearchFinished)
    assert scan_finished.summary.status.state == "bounded"
    assert scan_finished.summary.status.reason == "result_limit"


def test_status_precedence_retains_independent_secondary_conditions() -> None:
    """A source failure outranks cancellation and bounds without hiding them."""
    summary = build_search_summary(
        _query(scope="all", limit=1, effort="exhaustive"),
        effort="exhaustive",
        coverage=RunCoverage(
            sources_discovered=2,
            sources_eligible=2,
            sources_planned=2,
            sources_attempted=2,
            sources_completed=0,
            sources_bounded=0,
            sources_skipped=0,
            sources_unsupported=0,
            sources_failed=1,
            sources_cancelled=1,
            records_seen=3,
            matches_seen=2,
            source_stop_reasons=("source_failure", "failure_cleanup"),
        ),
        match_count=1,
        elapsed_seconds=0.1,
        answer_now=True,
        cancelled=True,
        truncated=True,
        result_limit_reached=True,
    )

    assert summary.status.state == "failed"
    assert summary.status.reason == "source_failure"
    assert summary.status.conditions == (
        "source_failure",
        "cancelled",
        "response_truncated",
        "answer_now",
        "result_limit",
    )
    assert summary.outcome == "undetermined"
    assert summary.completed_effort is None


def test_truncation_outranks_approximate_effort() -> None:
    """Represent the full run-state precedence before adapter limiting uses it."""
    query = _query(scope="all", effort="exhaustive")

    truncated = build_search_summary(
        query,
        effort="exhaustive",
        coverage=_empty_coverage(),
        match_count=0,
        elapsed_seconds=0.0,
        truncated=True,
        approximate=True,
    )
    approximate = build_search_summary(
        query,
        effort="exhaustive",
        coverage=_empty_coverage(),
        match_count=0,
        elapsed_seconds=0.0,
        approximate=True,
    )

    assert truncated.status.state == "truncated"
    assert truncated.status.reason == "response_truncated"
    assert truncated.status.conditions == (
        "response_truncated",
        "approximate_execution",
    )
    assert truncated.outcome == "undetermined"
    assert approximate.status.state == "approximate"
    assert approximate.status.reason == "approximate_execution"
    assert approximate.outcome == "undetermined"
    assert approximate.completed_effort is None


def test_explicit_scope_requires_confirmation_before_broadening() -> None:
    """Engine-authored actions preserve a frontend's explicit scope choice."""
    summary = build_search_summary(
        _query(scope_provenance="explicit"),
        effort="prompt",
        coverage=RunCoverage(
            sources_discovered=0,
            sources_eligible=0,
            sources_planned=0,
            sources_attempted=0,
            sources_completed=0,
            sources_bounded=0,
            sources_skipped=0,
            sources_unsupported=0,
            sources_failed=0,
            sources_cancelled=0,
            records_seen=0,
            matches_seen=0,
        ),
        match_count=0,
        elapsed_seconds=0.0,
    )

    action = summary.next_actions[0]
    assert action.kind == "search.broaden_scope"
    assert action.requires_confirmation is True


def test_cli_json_serializes_the_engine_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: pathlib.Path,
) -> None:
    """Structured CLI output adapts terminal evidence instead of inferring it."""
    result = run_search_result(tmp_path, _query(), backends=_no_backends())
    monkeypatch.setattr(cli_render, "run_search_result", lambda *_args, **_kwargs: result)
    args = parse_args(["search", "--json", "--agent", "codex", "missing"])
    assert isinstance(args, SearchArgs)

    assert cli_render.run_search_command(args) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["status"]["state"] == "complete"
    assert payload["summary"]["outcome"] == "no_prompt_match"
    assert payload["summary"]["coverage"]["sources_discovered"] == 0
    assert payload["summary"]["diagnostics"] == []
    assert payload["summary"]["next_actions"][0]["action_id"] == "search.targeted"


def test_cli_text_explains_prompt_only_completion(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: pathlib.Path,
) -> None:
    """Teach interactive users how to escalate without affecting structured output."""
    result = run_search_result(tmp_path, _query(), backends=_no_backends())
    monkeypatch.setattr(cli_render, "run_search_result", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(cli_render.sys.stderr, "isatty", lambda: True)
    args = parse_args(
        ["search", "--no-progress", "--agent", "codex", "missing"],
    )
    assert isinstance(args, SearchArgs)

    assert cli_render.run_search_command(args) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No prompt matches found." in captured.err
    assert (
        "Searched prompts only. Use --deep to search selected conversations, "
        "or --exhaustive to search all readable conversations."
    ) in captured.err


@pytest.mark.parametrize(
    "argv",
    [
        ["search", "--scope", "prompts", "missing"],
        ["search", "scope:prompts missing"],
        ["grep", "--scope", "prompts", "missing"],
        ["grep", "scope:prompts missing"],
    ],
)
def test_cli_text_requires_scope_change_before_depth(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Render confirmation-required actions without advertising an invalid command."""
    parsed = parse_args(argv)
    assert isinstance(parsed, SearchArgs | GrepArgs)
    assert parsed.scope_provenance == "explicit"
    summary = build_search_summary(
        _query(scope_provenance=parsed.scope_provenance),
        effort="prompt",
        coverage=_empty_coverage(),
        match_count=0,
        elapsed_seconds=0.0,
    )
    monkeypatch.setattr(cli_render.sys.stderr, "isatty", lambda: True)

    cli_render._print_search_depth_hint(summary)

    assert capsys.readouterr().err == (
        "Searched prompts only. Change the explicit scope to all before using "
        "--deep or --exhaustive.\n"
    )
