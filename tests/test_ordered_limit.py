"""Ordered-limit contracts shared by every search effort."""

from __future__ import annotations

import concurrent.futures
import json
import os
import pathlib
import typing as t

import pytest

import agentgrep.cli.render as cli_render
from agentgrep import GrepArgs, SearchArgs, parse_args, ranking
from agentgrep._engine import scanning, scheduling
from agentgrep._engine.orchestration import (
    collect_search_records,
    search_sources,
)
from agentgrep._engine.planning import (
    PhysicalSearchPlan,
    SourceTask,
    build_logical_search_plan,
    build_physical_search_plan,
)
from agentgrep._engine.scheduling import (
    ExecutionDriverConfig,
    ExecutionRecordEmitted,
    ExecutionRunFinished,
    FrontierExecutionDriver,
    InlineExecutionDriver,
    select_execution_driver,
)
from agentgrep.mcp.models import SearchRequestModel
from agentgrep.mcp.tools.search_tools import _search_async
from agentgrep.progress import SearchControl
from agentgrep.records import (
    BackendSelection,
    RecordOrigin,
    SearchQuery,
    SearchRecord,
    SourceHandle,
)


class OrderCase(t.NamedTuple):
    """One count-bound expectation for a requested order.

    Attributes
    ----------
    test_id : str
        Stable pytest id.
    order : str
        Requested engine order.
    expected_behavior : Literal["drain_source", "bounded_source"]
        Expected source scan bound.
    """

    test_id: str
    order: str
    expected_behavior: t.Literal["drain_source", "bounded_source"]


ORDER_CASES = (
    OrderCase("newest-drains", "newest", "drain_source"),
    OrderCase("scan-bounds", "scan", "bounded_source"),
)


def _write_codex_session(
    home: pathlib.Path,
    *,
    filename: str,
    session_id: str,
    timestamp: str,
    text: str,
    mtime_ns: int,
) -> None:
    """Write one isolated Codex session with a controlled source mtime."""
    path = home / ".codex" / "sessions" / "2026" / "01" / "01" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": session_id,
                            "timestamp": timestamp,
                            "cwd": "/work/example",
                        },
                    },
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": text}],
                            "timestamp": timestamp,
                        },
                    },
                ),
            ),
        )
        + "\n",
        encoding="utf-8",
    )
    os.utime(path, ns=(mtime_ns, mtime_ns))


def _source(
    name: str,
    *,
    search_root: pathlib.Path | None = None,
    mtime_ns: int = 0,
) -> SourceHandle:
    """Build one synthetic prompt-history source."""
    return SourceHandle(
        agent="codex",
        store="codex.history",
        adapter_id="codex.history_jsonl.v1",
        path=pathlib.Path(name),
        path_kind="history_file",
        source_kind="jsonl",
        search_root=search_root,
        mtime_ns=mtime_ns,
    )


def _record(source: SourceHandle, text: str, timestamp: str) -> SearchRecord:
    """Build one globally ordered matching prompt."""
    return SearchRecord(
        kind="prompt",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        timestamp=timestamp,
        session_id=source.path.stem,
    )


def _query(
    terms: tuple[str, ...],
    *,
    limit: int | None,
    any_term: bool = False,
    dedupe: bool = True,
    order: str = "newest",
) -> SearchQuery:
    """Build one prompt-effort Codex request over the flags these cases share."""
    return SearchQuery(
        terms=terms,
        scope="prompts",
        any_term=any_term,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=limit,
        dedupe=dedupe,
        effort="prompt",
        order=order,
    )


def _single_record_scanner(
    records: dict[pathlib.Path, SearchRecord],
    scanned: list[pathlib.Path] | None = None,
) -> t.Callable[..., scanning.SourceScanResult]:
    """Return a scan stub emitting one prepared record per source."""

    def scan_source_task(
        _query: SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        **_kwargs: object,
    ) -> scanning.SourceScanResult:
        if scanned is not None:
            scanned.append(task.source.path)
        return scanning.SourceScanResult(
            index=index,
            total=total,
            source=task.source,
            task=task,
            records=(records[task.source.path],),
            records_seen=1,
            matches_seen=1,
            duration_seconds=0.0,
        )

    return scan_source_task


def test_prompt_limit_drains_sources_before_newest_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not let prompt effort return an older first-scanned match."""
    newer_record_source = _source(
        "newer-record.jsonl",
        search_root=pathlib.Path(),
        mtime_ns=1,
    )
    older_record_source = _source(
        "older-record.jsonl",
        search_root=pathlib.Path(),
        mtime_ns=2,
    )
    records = {
        newer_record_source.path: _record(
            newer_record_source,
            "newer",
            "2026-02-01T00:00:00Z",
        ),
        older_record_source.path: _record(
            older_record_source,
            "older",
            "2026-01-01T00:00:00Z",
        ),
    }
    scanned: list[pathlib.Path] = []

    monkeypatch.setattr(
        scanning,
        "scan_source_task",
        _single_record_scanner(records, scanned),
    )
    query = _query(("match",), limit=1, dedupe=False)
    results = search_sources(
        query,
        [newer_record_source, older_record_source],
        BackendSelection(find_tool=None, grep_tool=None, json_tool=None),
    )

    assert set(scanned) == {newer_record_source.path, older_record_source.path}
    assert [record.text for record in results] == ["newer"]


class MergeCase(t.NamedTuple):
    """One global-merge expectation for a requested result limit.

    Attributes
    ----------
    test_id : str
        Stable pytest id.
    limit : int | None
        Requested result cap, or ``None`` for an unbounded stream.
    expected : tuple[str, ...]
        Record texts the driver must emit, newest first.
    """

    test_id: str
    limit: int | None
    expected: tuple[str, ...]


MERGE_CASES = (
    MergeCase("unlimited", None, ("newer", "older")),
    # A cap must not turn into a first-scanned slice: both sources are still
    # read, and the globally newest record wins the single slot.
    MergeCase("bounded", 1, ("newer",)),
)


@pytest.mark.parametrize("case", MERGE_CASES, ids=[case.test_id for case in MERGE_CASES])
def test_newest_order_merges_sources_before_emission(
    monkeypatch: pytest.MonkeyPatch,
    case: MergeCase,
) -> None:
    """Globally order the result stream instead of relabeling scan order."""
    older_source = _source("source-first.jsonl")
    newer_source = _source("source-last.jsonl")
    records = {
        older_source.path: _record(
            older_source,
            "older",
            "2026-01-01T00:00:00Z",
        ),
        newer_source.path: _record(
            newer_source,
            "newer",
            "2026-02-01T00:00:00Z",
        ),
    }

    scanned: list[pathlib.Path] = []

    monkeypatch.setattr(
        scanning,
        "scan_source_task",
        _single_record_scanner(records, scanned),
    )
    query = _query(("match",), limit=case.limit, dedupe=False, order="newest")
    tasks = tuple(
        SourceTask(
            source=source,
            strategy="direct_full_scan",
            record_order="unknown",
            limit_behavior="drain_source",
            can_stream_records=True,
            restore_order_key=(index, str(source.path)),
        )
        for index, source in enumerate((older_source, newer_source))
    )
    plan = PhysicalSearchPlan(
        logical=build_logical_search_plan(query),
        tasks=tasks,
        decisions=(),
    )

    driver = select_execution_driver(query, plan)
    events = tuple(driver.iter_search_plan(query, plan))

    assert isinstance(driver, FrontierExecutionDriver)
    assert scanned == [older_source.path, newer_source.path], (
        "every source must be read before the global slice"
    )
    assert [
        event.record.text for event in events if isinstance(event, ExecutionRecordEmitted)
    ] == list(case.expected)


def test_ranked_json_summary_describes_relevance_limit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep the structured summary aligned with the ranked result window."""
    root = tmp_path / ".codex"
    root.mkdir()
    (root / "history.jsonl").write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "session_id": "00000000-0000-0000-0000-000000000401",
                        "ts": 1_800_000_000,
                        "text": "prefix needle suffix",
                    },
                ),
                json.dumps(
                    {
                        "session_id": "00000000-0000-0000-0000-000000000402",
                        "ts": 1_700_000_000,
                        "text": "needle",
                    },
                ),
            ),
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pathlib.Path,
        "home",
        classmethod(lambda _cls: tmp_path),
    )
    parsed = parse_args(
        [
            "search",
            "--json",
            "--limit",
            "1",
            "--no-progress",
            "needle",
        ],
    )
    assert isinstance(parsed, SearchArgs)

    assert cli_render.run_search_command(parsed) == 0

    payload = json.loads(capsys.readouterr().out)
    summary = payload["summary"]
    assert [record["text"] for record in payload["results"]] == ["needle"]
    assert summary["request"]["limit"] == 1
    assert summary["request"]["order"] == "relevance"
    assert summary["stats"]["matched"] == 1
    assert summary["stats"]["limit"] == 1
    assert summary["stats"]["applied_order"] == "relevance"
    assert summary["status"] == {
        "state": "bounded",
        "reason": "result_limit",
        "conditions": ["result_limit"],
    }


def test_relevance_frontier_caches_rank_until_records_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rank one unchanged final frontier once, then invalidate on mutation."""
    source = _source("ranked.jsonl")
    query = _query(("needle",), limit=1, dedupe=False, order="relevance")
    frontier = scheduling._FrontierState(query)
    original_rank = ranking.rank_search_records
    rank_calls = 0

    def counted_rank(
        records: list[SearchRecord],
        query_text: str,
        *,
        threshold: int = 0,
        origin_boost: RecordOrigin | None = None,
    ) -> list[tuple[SearchRecord, float]]:
        nonlocal rank_calls
        rank_calls += 1
        return original_rank(
            records,
            query_text,
            threshold=threshold,
            origin_boost=origin_boost,
        )

    monkeypatch.setattr(ranking, "rank_search_records", counted_rank)
    frontier.add_records(
        (
            _record(source, "prefix needle suffix", "2026-02-01T00:00:00Z"),
            _record(source, "needle", "2026-01-01T00:00:00Z"),
        ),
    )

    assert [record.text for record in frontier.records()] == ["needle"]
    assert frontier.accepted_count == 2
    assert frontier.has_more is True
    assert rank_calls == 1

    frontier.add_records(
        (_record(source, "another needle result", "2025-01-01T00:00:00Z"),),
    )

    assert frontier.accepted_count == 3
    assert rank_calls == 2


def test_metadata_only_ranked_search_ignores_text_threshold(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Preserve metadata-only results when no residual text can be scored."""
    root = tmp_path / ".codex"
    root.mkdir()
    (root / "history.jsonl").write_text(
        json.dumps(
            {
                "session_id": "00000000-0000-0000-0000-000000000403",
                "ts": 1_800_000_000,
                "text": "metadata-only match",
            },
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pathlib.Path,
        "home",
        classmethod(lambda _cls: tmp_path),
    )
    parsed = parse_args(
        [
            "search",
            "--json",
            "--threshold",
            "70",
            "--no-progress",
            "agent:codex",
        ],
    )
    assert isinstance(parsed, SearchArgs)

    assert cli_render.run_search_command(parsed) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [record["text"] for record in payload["results"]] == [
        "metadata-only match",
    ]
    assert payload["summary"]["request"]["order"] == "relevance"


@pytest.mark.mcp
async def test_mcp_limit_uses_newest_order_and_uncapped_match_count(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return the newest match and count every match behind a capped page."""
    _write_codex_session(
        tmp_path,
        filename="rollout-2026-01-01T00-00-00-newer.jsonl",
        session_id="00000000-0000-0000-0000-000000000301",
        timestamp="2026-02-01T00:00:00Z",
        text="zebraneedle newer",
        mtime_ns=1,
    )
    _write_codex_session(
        tmp_path,
        filename="rollout-2026-01-01T00-00-00-older.jsonl",
        session_id="00000000-0000-0000-0000-000000000302",
        timestamp="2026-01-01T00:00:00Z",
        text="zebraneedle older",
        mtime_ns=2,
    )
    monkeypatch.setattr(
        pathlib.Path,
        "home",
        classmethod(lambda _cls: tmp_path),
    )

    response = await _search_async(
        SearchRequestModel(
            terms=["zebraneedle"],
            agent="codex",
            scope="prompts",
            case_sensitive=False,
            effort="exhaustive",
            limit=1,
        ),
    )

    assert [record.text for record in response.results] == ["zebraneedle newer"]
    assert response.stats.matched == 2
    assert response.stats.emitted == 1
    assert response.page.limit == 1
    assert response.page.count == 1
    assert response.status.state == "bounded"
    assert response.status.reason == "result_limit"
    assert response.status.conditions == ["result_limit"]


@pytest.mark.parametrize(
    "case",
    ORDER_CASES,
    ids=[case.test_id for case in ORDER_CASES],
)
def test_order_controls_source_count_bounds(
    case: OrderCase,
) -> None:
    """Use count stopping only when the caller explicitly accepts scan order."""
    source = _source("bounded.jsonl", search_root=pathlib.Path())
    query = _query(("match",), limit=1, order=case.order)

    plan = build_physical_search_plan(
        query,
        [source],
        BackendSelection(find_tool=None, grep_tool=None, json_tool=None),
    )

    assert plan.logical.request.order == case.order
    assert len(plan.tasks) == 1
    assert plan.tasks[0].limit_behavior == case.expected_behavior


def test_scan_order_preserves_execution_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not relabel a count-bounded scan as a globally newest result."""
    older_source = _source("first.jsonl")
    newer_source = _source("last.jsonl")
    records = {
        older_source.path: _record(older_source, "older", "2026-01-01T00:00:00Z"),
        newer_source.path: _record(newer_source, "newer", "2026-02-01T00:00:00Z"),
    }

    monkeypatch.setattr(scanning, "scan_source_task", _single_record_scanner(records))
    query = _query(("match",), limit=2, dedupe=False, order="scan")

    results = collect_search_records(query, [older_source, newer_source])

    assert [record.text for record in results] == ["older", "newer"]


@pytest.mark.parametrize("use_source_batches", [False, True])
def test_scan_order_serializes_source_tasks(
    monkeypatch: pytest.MonkeyPatch,
    use_source_batches: bool,
) -> None:
    """Do not let a faster lower-priority worker win a scan-ordered limit."""
    first_source = _source("first.jsonl")
    second_source = _source("second.jsonl")
    records = {
        first_source.path: _record(first_source, "first", "2026-01-01T00:00:00Z"),
        second_source.path: _record(
            second_source,
            "second",
            "2026-02-01T00:00:00Z",
        ),
    }

    def iter_source_task_records(
        task: SourceTask,
        _query: SearchQuery,
    ) -> t.Iterator[SearchRecord]:
        yield records[task.source.path]

    monkeypatch.setattr(
        scanning,
        "iter_source_task_records",
        iter_source_task_records,
    )
    # Scan order pins every pool to one worker. Recording the widths the driver
    # asks for proves the pin directly; racing two threads could only ever make
    # overtaking unlikely, and would decide the result by wall-clock timing.
    pool_widths: list[int | None] = []
    real_pool = concurrent.futures.ThreadPoolExecutor

    def recording_pool(
        max_workers: int | None = None,
        **kwargs: t.Any,
    ) -> concurrent.futures.ThreadPoolExecutor:
        pool_widths.append(max_workers)
        return real_pool(max_workers=max_workers, **kwargs)

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", recording_pool)
    query = _query(
        ("first", "second"),
        limit=1,
        any_term=True,
        dedupe=False,
        order="scan",
    )
    tasks = tuple(
        SourceTask(
            source=source,
            strategy="direct_full_scan",
            record_order="unknown",
            limit_behavior="bounded_source",
            can_stream_records=True,
            restore_order_key=(index, str(source.path)),
        )
        for index, source in enumerate((first_source, second_source))
    )
    plan = PhysicalSearchPlan(
        logical=build_logical_search_plan(query),
        tasks=tasks,
        decisions=(),
    )

    events = tuple(
        FrontierExecutionDriver(
            ExecutionDriverConfig(
                max_workers=2,
                use_source_batches=use_source_batches,
            ),
        ).iter_search_plan(query, plan),
    )

    assert all(width == 1 for width in pool_widths), (
        f"scan order must serialize source tasks, got pool widths {pool_widths}"
    )
    assert [event.record.text for event in events if isinstance(event, ExecutionRecordEmitted)] == [
        "first"
    ]


def test_limited_grep_uses_scan_order_and_stops_after_first_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep grep's documented stop-after-N behavior across source boundaries."""
    first_source = _source("first.jsonl")
    second_source = _source("second.jsonl")
    records = {
        first_source.path: _record(first_source, "first", "2026-01-01T00:00:00Z"),
        second_source.path: _record(
            second_source,
            "second",
            "2026-02-01T00:00:00Z",
        ),
    }
    scanned: list[pathlib.Path] = []

    monkeypatch.setattr(
        scanning,
        "scan_source_task",
        _single_record_scanner(records, scanned),
    )
    parsed = parse_args(["grep", "--limit", "1", "match"])
    assert isinstance(parsed, GrepArgs)
    query = cli_render.build_grep_query(parsed)

    results = collect_search_records(query, [first_source, second_source])

    assert query.order == "scan"
    assert scanned == [first_source.path]
    assert [record.text for record in results] == ["first"]


def test_scan_order_preserves_source_record_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep one source's parser order when scan order is requested."""
    source = _source("one-source.jsonl")
    older = _record(source, "older", "2026-01-01T00:00:00Z")
    newer = _record(source, "newer", "2026-02-01T00:00:00Z")
    task = SourceTask(
        source=source,
        strategy="direct_full_scan",
        record_order="unknown",
        limit_behavior="drain_source",
        can_stream_records=True,
        restore_order_key=(0, str(source.path)),
    )
    query = _query((), limit=2, dedupe=False, order="scan")

    def iter_source_task_records(
        _task: SourceTask,
        _query: SearchQuery,
    ) -> t.Iterator[SearchRecord]:
        return iter((older, newer))

    monkeypatch.setattr(
        scanning,
        "iter_source_task_records",
        iter_source_task_records,
    )

    result = scanning.scan_source_task(
        query,
        task,
        index=1,
        total=1,
        control=SearchControl(),
    )

    assert [record.text for record in result.records] == ["older", "newer"]


@pytest.mark.parametrize("use_source_batches", [False, True])
def test_frontier_exact_limit_with_unscanned_source_is_undetermined(
    monkeypatch: pytest.MonkeyPatch,
    use_source_batches: bool,
) -> None:
    """Do not claim no further match when the frontier skips eligible work."""
    first_source = _source("first.jsonl")
    second_source = _source("second.jsonl")
    first_record = _record(first_source, "match", "2026-02-01T00:00:00Z")

    def iter_source_task_records(
        task: SourceTask,
        _query: SearchQuery,
    ) -> t.Iterator[SearchRecord]:
        if task.source == first_source:
            yield first_record

    monkeypatch.setattr(
        scanning,
        "iter_source_task_records",
        iter_source_task_records,
    )
    query = _query(("match",), limit=1, dedupe=False, order="scan")
    tasks = tuple(
        SourceTask(
            source=source,
            strategy="direct_full_scan",
            record_order="unknown",
            limit_behavior="drain_source",
            can_stream_records=True,
            restore_order_key=(index, str(source.path)),
        )
        for index, source in enumerate((first_source, second_source))
    )
    plan = PhysicalSearchPlan(
        logical=build_logical_search_plan(query),
        tasks=tasks,
        decisions=(),
    )

    events = tuple(
        FrontierExecutionDriver(
            ExecutionDriverConfig(
                max_workers=1,
                use_source_batches=use_source_batches,
            ),
        ).iter_search_plan(query, plan),
    )

    terminal = events[-1]
    assert isinstance(terminal, ExecutionRunFinished)
    assert terminal.accepted_count == 1
    assert terminal.has_more is None
    assert terminal.stop_reason == "result_limit"


def test_inline_bounded_source_exact_limit_is_undetermined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Propagate a source-local count bound into the run terminal."""
    source = _source("bounded.jsonl")
    record = _record(source, "match", "2026-02-01T00:00:00Z")

    def iter_source_task_records(
        _task: SourceTask,
        _query: SearchQuery,
    ) -> t.Iterator[SearchRecord]:
        yield record

    monkeypatch.setattr(
        scanning,
        "iter_source_task_records",
        iter_source_task_records,
    )
    query = _query(("match",), limit=1, dedupe=False, order="scan")
    task = SourceTask(
        source=source,
        strategy="direct_full_scan",
        record_order="unknown",
        limit_behavior="bounded_source",
        can_stream_records=True,
        restore_order_key=(0, str(source.path)),
    )
    plan = PhysicalSearchPlan(
        logical=build_logical_search_plan(query),
        tasks=(task,),
        decisions=(),
    )

    events = tuple(InlineExecutionDriver().iter_search_plan(query, plan))

    terminal = events[-1]
    assert isinstance(terminal, ExecutionRunFinished)
    assert terminal.accepted_count == 1
    assert terminal.has_more is None
    assert terminal.stop_reason == "result_limit"


def test_inline_answer_now_keeps_records_collected_before_the_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emit valid partial records returned by a cooperatively stopped source."""
    source = _source("partial.jsonl")
    records = (
        _record(source, "first", "2026-02-01T00:00:00Z"),
        _record(source, "second", "2026-01-01T00:00:00Z"),
    )
    control = SearchControl()

    def scan_source_task(
        _query: SearchQuery,
        task: SourceTask,
        *,
        index: int,
        total: int,
        **_kwargs: object,
    ) -> scanning.SourceScanResult:
        control.request_answer_now()
        return scanning.SourceScanResult(
            index=index,
            total=total,
            source=source,
            task=task,
            records=records,
            records_seen=2,
            matches_seen=2,
            duration_seconds=0.0,
            outcome="bounded",
            stop_reason="answer_now",
        )

    monkeypatch.setattr(scanning, "scan_source_task", scan_source_task)
    query = _query(("match",), limit=None, dedupe=False)
    task = SourceTask(
        source=source,
        strategy="direct_full_scan",
        record_order="unknown",
        limit_behavior="drain_source",
        can_stream_records=True,
        restore_order_key=(0, str(source.path)),
    )
    plan = PhysicalSearchPlan(
        logical=build_logical_search_plan(query),
        tasks=(task,),
        decisions=(),
    )

    events = tuple(
        InlineExecutionDriver().iter_search_plan(
            query,
            plan,
            control=control,
        ),
    )

    assert [event.record.text for event in events if isinstance(event, ExecutionRecordEmitted)] == [
        "first",
        "second",
    ]


def test_unknown_order_is_rejected() -> None:
    """Reject misspellings instead of silently applying a different order."""
    query = _query(("match",), limit=1, order="newset")

    with pytest.raises(
        ValueError,
        match="order must be 'newest', 'relevance', or 'scan'",
    ):
        build_logical_search_plan(query)
