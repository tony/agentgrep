"""Pilot tests for the grep-log layout (ADR 0013, the layout axis).

``GrepLogLayout`` shares the engine seam and normalized records with the HUD but
composes a single append-only log and presents records as lines. These tests
mount it (pushed onto the shell) and drive its streaming/present hooks directly,
mirroring the HUD's ``_apply_records_batch`` tests.
"""

from __future__ import annotations

import asyncio
import pathlib
import typing as t

import pytest

from agentgrep.progress import ProgressSnapshot, StreamingRecordsBatch, StreamingSearchFinished
from agentgrep.records import SearchRecord, SourceHandle
from agentgrep.ui._seams import _UiStreamingSearchProgress
from tests._agentgrep_tui_support import _build_empty_ui_app

pytestmark = pytest.mark.tui


def _record(tmp_path: pathlib.Path, idx: int, text: str) -> SearchRecord:
    """Build a minimal record for the log."""
    return SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / f"r{idx}.jsonl",
        text=text,
    )


async def _mount_greplog(app: t.Any, pilot: t.Any) -> t.Any:
    """Push a grep-log layout (search workflow) onto the running shell."""
    from agentgrep.ui.layouts.greplog import GrepLogLayout
    from agentgrep.ui.workflows.search import SearchWorkflow

    layout = GrepLogLayout(app._ctx, SearchWorkflow())
    await app.push_screen(layout)
    await pilot.pause()
    return layout


async def _submit_command(pilot: t.Any, layout: t.Any, text: str) -> None:
    """Submit one slash command through grep-log's mounted search input."""
    layout._search_input.value = text
    layout._search_input.cursor_position = len(text)
    layout._search_input.focus()
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.slow
async def test_greplog_zoom_recovers_status_and_keeps_command_shell(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Log zoom grows only the log; typed minimize restores its status chrome."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        search = layout.query_one("#search")
        log = layout.query_one("#greplog")
        status = layout.query_one("#greplog-status")
        footer = layout.query_one("Footer")
        by_name = {command.name: command for command in layout.slash_commands}
        original = (log.region, status.region, search.region, footer.region)
        assert by_name["maximize"].argument_hint == "[log]"
        assert layout.maximized is None

        await _submit_command(pilot, layout, "/maximize log")

        assert layout.maximized is None
        assert layout.has_class("-zoom-log")
        assert status.region.height == 0
        assert log.region.height > original[0].height
        assert search.region == original[2]
        assert footer.region == original[3]
        assert app.focused is search

        await pilot.press(*"/minimize", "enter")
        await pilot.pause()

        assert layout.maximized is None
        assert not layout.has_class("-zoom-log")
        assert (log.region, status.region, search.region, footer.region) == original
        assert app.focused is search

        await _submit_command(pilot, layout, "/maximize")
        assert layout.has_class("-zoom-log")
        assert layout.maximized is None
        await _submit_command(pilot, layout, "/minimize")
        assert not layout.has_class("-zoom-log")


@pytest.mark.slow
async def test_greplog_streams_records_into_the_log(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A streamed batch extends the buffer and appends one log line per record."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [_record(tmp_path, i, f"row {i}") for i in range(3)]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        assert layout.query_one("#greplog") is not None
        await layout._apply_event(
            layout._generation,
            StreamingRecordsBatch(records=tuple(records), total=3),
        )
        await pilot.pause()
        assert layout._records == records
        assert len(layout.query_one("#greplog").lines) == 3


@pytest.mark.slow
async def test_greplog_write_chunk_does_not_warm_haystack_on_pump(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Writing log rows must not build full record haystacks."""
    from agentgrep.ui.layouts import greplog as greplog_mod

    def fail_cached_haystack(record: SearchRecord) -> t.NoReturn:
        del record
        raise AssertionError

    monkeypatch.setattr(greplog_mod, "cached_haystack", fail_cached_haystack, raising=False)
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, 0, "needle")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        layout._write_chunk((record,))
        assert len(layout.query_one("#greplog").lines) == 1


@pytest.mark.slow
async def test_greplog_writes_each_chunk_in_one_batch(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bounded record chunk causes one public ``RichLog.write`` call."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = tuple(_record(tmp_path, index, f"row {index}") for index in range(3))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        log = layout.query_one("#greplog")
        writes: list[object] = []
        original = log.write

        def spy(content: object, *args: object, **kwargs: object) -> object:
            writes.append(content)
            return original(content, *args, **kwargs)

        monkeypatch.setattr(log, "write", spy)
        layout._write_chunk(records)

        assert len(writes) == 1
        assert str(writes[0]).count("\n") == len(records) - 1
        assert len(log.lines) == len(records)


@pytest.mark.slow
async def test_greplog_finished_sets_status_line(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished grep freezes the status line with the match count."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        layout._apply_finished("complete", 5, 1.2, None)
        await pilot.pause()
        assert "5" in str(layout.query_one("#greplog-status").render())


@pytest.mark.slow
async def test_greplog_error_status_treats_markup_as_text(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bracketed backend error is displayed literally."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        layout._apply_finished("error", 0, 1.2, "[red]backend[/red]")
        await pilot.pause()
        assert str(layout.query_one("#greplog-status").render()) == (
            "grep failed: [red]backend[/red]"
        )


@pytest.mark.slow
async def test_greplog_renders_lifecycle_and_heartbeat_progress(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real UI reporter shows source lifecycle and heartbeat progress."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    events: list[object] = []
    reporter = _UiStreamingSearchProgress(emit=events.append)
    source = SourceHandle(
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "session.jsonl",
        path_kind="session_file",
        source_kind="jsonl",
        search_root=None,
        mtime_ns=1,
    )
    reporter.source_started(3, 82, source)
    reporter.source_progress(3, 82, source, records=128, matches=1)

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        layout._search_done = False
        await layout._apply_event(layout._generation, events[0])
        await pilot.pause()
        assert str(layout.query_one("#greplog-status").render()) == "scanning 3/82…"
        await layout._apply_event(layout._generation, events[1])
        await pilot.pause()
        assert str(layout.query_one("#greplog-status").render()) == ("scanning 3/82 · 128 records…")


@pytest.mark.parametrize(
    ("record_count", "expected"),
    [
        (0, "scanning 3/82…"),
        (1, "scanning 3/82 · 1 record…"),
        (128, "scanning 3/82 · 128 records…"),
    ],
    ids=("zero", "singular", "plural"),
)
def test_greplog_scanning_text_uses_record_grammar(
    record_count: int,
    expected: str,
) -> None:
    """Heartbeat text omits zero and distinguishes one from many records."""
    from agentgrep.ui.layouts.greplog import GrepLogLayout

    snapshot = ProgressSnapshot(
        query_label="bliss",
        phase="scanning",
        current=3,
        total=82,
        detail=None,
        matches=0,
        elapsed=0.0,
        source_records_seen=record_count,
    )

    assert GrepLogLayout._scanning_text(snapshot) == expected


@pytest.mark.slow
async def test_greplog_filter_renders_only_matches(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browse-style filter re-renders the log to the matching subset (NB-4)."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        _record(tmp_path, 0, "needle here"),
        _record(tmp_path, 1, "haystack only"),
        _record(tmp_path, 2, "needle again"),
    ]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        await layout._apply_event(
            layout._generation,
            StreamingRecordsBatch(records=tuple(records), total=3),
        )
        await pilot.pause()
        assert len(layout.query_one("#greplog").lines) == 3
        matching = tuple(r for r in records if "needle" in r.text)
        await layout._apply_log_filter(layout._filter_generation, matching)
        await pilot.pause()
        assert len(layout.query_one("#greplog").lines) == 2


@pytest.mark.slow
async def test_greplog_stale_generation_is_dropped(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch from a superseded generation never reaches the log (NB-10)."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [_record(tmp_path, 0, "row")]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        await layout._apply_event(
            layout._generation - 1,  # a stale generation
            StreamingRecordsBatch(records=tuple(records), total=1),
        )
        await pilot.pause()
        assert layout._records == []
        assert len(layout.query_one("#greplog").lines) == 0


class ResetStaleEventCase(t.NamedTuple):
    """A stale worker event that arrives after reset."""

    test_id: str
    event_kind: t.Literal["records", "finished"]


RESET_STALE_EVENT_CASES = (
    ResetStaleEventCase("records-batch", "records"),
    ResetStaleEventCase("finished-event", "finished"),
)


@pytest.mark.slow
@pytest.mark.parametrize("case", RESET_STALE_EVENT_CASES, ids=lambda case: case.test_id)
async def test_greplog_reset_drops_stale_search_events(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: ResetStaleEventCase,
) -> None:
    """Search events from before reset must not repaint the cleared log."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [_record(tmp_path, 0, "old row")]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        old_generation = layout._generation
        layout.reset_view()
        if case.event_kind == "records":
            await layout._apply_event(
                old_generation,
                StreamingRecordsBatch(records=tuple(records), total=1),
            )
        else:
            await layout._apply_event(
                old_generation,
                StreamingSearchFinished(outcome="complete", total=9, elapsed=0.1),
            )
        await pilot.pause()
        assert layout._records == []
        assert len(layout.query_one("#greplog").lines) == 0
        assert str(layout.query_one("#greplog-status").render()) == ""


class StaleFilterCase(t.NamedTuple):
    """A stale filter apply after another layout state change."""

    test_id: str
    invalidation: t.Literal["new-filter", "reset", "new-search"]


STALE_FILTER_CASES = (
    StaleFilterCase("newer-filter", "new-filter"),
    StaleFilterCase("reset-view", "reset"),
    StaleFilterCase("new-search", "new-search"),
)


@pytest.mark.slow
@pytest.mark.parametrize("case", STALE_FILTER_CASES, ids=lambda case: case.test_id)
async def test_greplog_stale_filter_results_are_dropped(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: StaleFilterCase,
) -> None:
    """Filter worker results from before a newer state must not repaint the log."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        _record(tmp_path, 0, "needle here"),
        _record(tmp_path, 1, "haystack only"),
        _record(tmp_path, 2, "needle again"),
    ]

    def no_worker(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        monkeypatch.setattr(layout, "run_worker", no_worker)
        await layout._apply_event(
            layout._generation,
            StreamingRecordsBatch(records=tuple(records), total=3),
        )
        await pilot.pause()
        old_generation = layout._filter_generation
        if case.invalidation == "new-filter":
            layout.filter_loaded("newer")
            expected_records = records
            expected_lines = 3
        elif case.invalidation == "reset":
            layout.reset_view()
            expected_records = []
            expected_lines = 0
        else:
            layout.run_search(layout.search_query)
            expected_records = []
            expected_lines = 0

        await layout._apply_log_filter(old_generation, tuple(records[:1]))
        await pilot.pause()
        assert layout._records == expected_records
        assert len(layout.query_one("#greplog").lines) == expected_lines


@pytest.mark.slow
async def test_greplog_filter_chunks_stop_after_generation_change(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A yielded filter repaint cannot append after a newer filter wins."""
    from agentgrep.ui import _runtime
    from agentgrep.ui.layouts import greplog as greplog_mod

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    entered_yield = asyncio.Event()
    release_yield = asyncio.Event()

    async def pause_between_chunks() -> None:
        entered_yield.set()
        await release_yield.wait()

    monkeypatch.setattr(_runtime, "_sleep_zero", pause_between_chunks)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        records = tuple(
            _record(tmp_path, index, f"old row {index}")
            for index in range(greplog_mod._APPLY_CHUNK_SIZE * 2 + 1)
        )
        generation = layout._filter_generation
        apply_task = asyncio.create_task(layout._apply_log_filter(generation, records))
        await asyncio.wait_for(entered_yield.wait(), timeout=2)

        layout._filter_generation += 1
        log = layout.query_one("#greplog")
        log.clear()
        log.write("new view")
        release_yield.set()
        await apply_task

        assert len(log.lines) == 1
        assert "new view" in log.lines[0].text


class ActiveFilterBatchCase(t.NamedTuple):
    """A streamed batch that arrives while a browse filter is active."""

    test_id: str
    filter_text: str
    expected_lines: int
    expected_records: int


ACTIVE_FILTER_BATCH_CASES = (ActiveFilterBatchCase("later-batch-stays-filtered", "needle", 2, 4),)


@pytest.mark.slow
@pytest.mark.parametrize("case", ACTIVE_FILTER_BATCH_CASES, ids=lambda case: case.test_id)
async def test_greplog_streaming_batches_respect_active_filter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: ActiveFilterBatchCase,
) -> None:
    """Later streamed records stay under the active browse filter."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    first = [
        _record(tmp_path, 0, "needle first"),
        _record(tmp_path, 1, "plain first"),
    ]
    later = [
        _record(tmp_path, 2, "plain later"),
        _record(tmp_path, 3, "needle later"),
    ]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        await layout._apply_event(
            layout._generation,
            StreamingRecordsBatch(records=tuple(first), total=len(first)),
        )
        layout.filter_loaded(case.filter_text)
        await pilot.pause(0.2)
        await layout._apply_event(
            layout._generation,
            StreamingRecordsBatch(records=tuple(later), total=len(first) + len(later)),
        )
        await pilot.pause(0.2)
        assert len(layout._records) == case.expected_records
        assert len(layout.query_one("#greplog").lines) == case.expected_lines


@pytest.mark.slow
async def test_greplog_active_filter_scans_each_streamed_record_once(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active filter projects new batches without rescanning its prefix."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    initial = [_record(tmp_path, index, f"needle {index}") for index in range(100)]
    later = [_record(tmp_path, 100 + index, f"needle {100 + index}") for index in range(10)]

    class CountingMatcher:
        calls = 0

        def matches(self, record: SearchRecord) -> bool:
            self.calls += 1
            return "needle" in record.text

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        matcher = CountingMatcher()
        layout._records.extend(initial)
        layout._filter_matcher = matcher
        layout._refresh_filter_log(matcher)
        for _ in range(100):
            if len(layout.query_one("#greplog").lines) == len(initial):
                break
            await asyncio.sleep(0.01)

        await layout._apply_event(
            layout._generation,
            StreamingRecordsBatch(records=tuple(later), total=len(initial) + len(later)),
        )
        for _ in range(100):
            if len(layout.query_one("#greplog").lines) == len(initial) + len(later):
                break
            await asyncio.sleep(0.01)

        expected = len(initial) + len(later)
        assert len(layout.query_one("#greplog").lines) == expected
        assert matcher.calls == expected


@pytest.mark.slow
async def test_greplog_clear_filter_does_not_duplicate_streamed_tail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch arriving during an unfiltered repaint is projected only once."""
    from agentgrep.ui import _runtime
    from agentgrep.ui.layouts import greplog as greplog_mod

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    entered_yield = asyncio.Event()
    release_yield = asyncio.Event()

    async def pause_between_chunks() -> None:
        entered_yield.set()
        await release_yield.wait()

    monkeypatch.setattr(_runtime, "_sleep_zero", pause_between_chunks)
    initial = [
        _record(tmp_path, index, f"initial {index}")
        for index in range(greplog_mod._APPLY_CHUNK_SIZE * 2 + 1)
    ]
    tail = [_record(tmp_path, len(initial) + index, f"tail {index}") for index in range(2)]

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        layout._records.extend(initial)
        layout.filter_loaded("")
        await asyncio.wait_for(entered_yield.wait(), timeout=2)

        await layout._apply_event(
            layout._generation,
            StreamingRecordsBatch(records=tuple(tail), total=len(initial) + len(tail)),
        )
        release_yield.set()
        for _ in range(200):
            if layout._filter_scan_generation is None and layout._filter_scanned_count == len(
                initial
            ) + len(tail):
                break
            await asyncio.sleep(0.01)

        lines = tuple(line.text for line in layout.query_one("#greplog").lines)
        assert len(lines) == len(initial) + len(tail)
        assert [index for index, line in enumerate(lines) if "tail " in line] == [
            len(initial),
            len(initial) + 1,
        ]


class InterleavedFilterCase(t.NamedTuple):
    """A filter that lands while an unfiltered batch apply is yielding."""

    test_id: str
    filter_text: str
    matching_records: int
    plain_records: int


INTERLEAVED_FILTER_CASES = (
    InterleavedFilterCase("filter-stops-old-raw-apply", "needle", 200, 200),
)


@pytest.mark.slow
@pytest.mark.parametrize("case", INTERLEAVED_FILTER_CASES, ids=lambda case: case.test_id)
async def test_greplog_filter_interrupts_unfiltered_batch_apply(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: InterleavedFilterCase,
) -> None:
    """A filter repaint must not be followed by stale unfiltered batch rows."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    matching = [
        _record(tmp_path, i, f"{case.filter_text} row {i}") for i in range(case.matching_records)
    ]
    plain = [
        _record(tmp_path, case.matching_records + i, f"plain row {i}")
        for i in range(case.plain_records)
    ]
    records = [*matching, *plain]

    def no_worker(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        monkeypatch.setattr(layout, "run_worker", no_worker)
        task = asyncio.create_task(
            layout._apply_event(
                layout._generation,
                StreamingRecordsBatch(records=tuple(records), total=len(records)),
            ),
        )
        await asyncio.sleep(0)
        layout.filter_loaded(case.filter_text)
        await layout._apply_log_filter(layout._filter_generation, tuple(matching))
        await task
        await pilot.pause()
        lines = tuple(str(line) for line in layout.query_one("#greplog").lines)
        assert len(layout._records) == len(records)
        assert len(lines) == case.matching_records
        assert all("plain row" not in line for line in lines)


@pytest.mark.slow
async def test_greplog_search_input_does_not_crash_on_keys(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typing in the grep-log search box must not raise.

    ``SearchInput`` routes the non-ctrl-c "disarm" and ctrl-c through
    ``self.screen``; a layout reusing it (greplog) needs the LayoutScreen
    defaults, else every keystroke would raise AttributeError.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        layout._search_input.focus()
        await pilot.pause()
        await pilot.press("a")  # a normal key -> screen._disarm_confirm_exit
        await pilot.pause()
        assert layout._search_input.value == "a"
        await pilot.press("ctrl+c")  # with text -> screen._handle_input_ctrl_c clears it
        await pilot.pause()
        assert layout._search_input.value == ""


@pytest.mark.slow
async def test_greplog_search_highlighting_follows_active_theme(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A layout mounted in light mode keeps query colors in sync thereafter."""
    from agentgrep.ui import theme

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.theme = theme.LIGHT_THEME_NAME
        await pilot.pause()
        layout = await _mount_greplog(app, pilot)
        layout._search_input.value = "agent:claude"
        await pilot.pause()

        assert any("#007f7f" in str(span.style) for span in layout._search_input._value.spans)

        app.theme = theme.DARK_THEME_NAME
        await pilot.pause()
        assert any("#5fd7af" in str(span.style) for span in layout._search_input._value.spans)


def test_greplog_summary_slices_before_line_splitting(tmp_path: pathlib.Path) -> None:
    """Compact log projection never scans an entire oversized record body."""
    from agentgrep.ui.layouts.greplog import _format_log_line

    class GuardedText(str):
        def splitlines(self, keepends: bool = False) -> list[str]:
            del keepends
            raise AssertionError

    record = _record(tmp_path, 0, GuardedText("summary\n" + "x" * 1_000_000))
    assert "summary" in _format_log_line(record)
