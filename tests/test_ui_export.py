"""Mounted export-command tests for the pi-like Textual HUD."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import pathlib
import threading
import time
import typing as t

import pytest

import agentgrep.identity as identity
import agentgrep.record_export as record_export
from agentgrep.records import RecordPosition, SearchRecord
from agentgrep.ui import _runtime
from tests._agentgrep_tui_support import _build_empty_ui_app
from tests.test_agentgrep_tui import _search_requested

pytestmark = pytest.mark.tui


def _record(
    tmp_path: pathlib.Path,
    text: str,
    *,
    ordinal: int,
    session_id: str | None = "session-a",
    source_name: str | None = None,
) -> SearchRecord:
    """Build one normalized source record with deterministic identities."""
    return SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / (source_name or f"source-{ordinal}.jsonl"),
        text=text,
        role="user",
        timestamp=f"2026-07-12T12:00:{ordinal:02d}Z",
        model="gpt-test",
        session_id=session_id,
        conversation_id=session_id,
        identity_namespace="codex.session" if session_id is not None else None,
        position=RecordPosition(ordinal=ordinal, quality="source_order"),
    )


async def _load_records(
    screen: t.Any,
    records: tuple[SearchRecord, ...],
    *,
    selected: int = 0,
) -> None:
    """Mount records through the bounded result applier and select one row."""
    await screen._apply_records_batch(records, len(records))
    screen._results.highlighted = selected
    screen._current_detail_record = records[selected]


async def _wait_for(predicate: t.Callable[[], bool], *, timeout: float = 3.0) -> None:
    """Yield until a worker-observable condition is true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("timed out waiting for export worker")


def _capture_notifications(
    screen: t.Any,
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[tuple[object, ...], dict[str, object]]]:
    """Capture HUD notifications without rendering a toast."""
    notes: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(screen, "notify", lambda *a, **k: notes.append((a, k)))
    return notes


@pytest.mark.slow
async def test_export_commands_accept_paths_but_legacy_args_stay_searches(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only export commands consume an argument remainder."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        requests: list[tuple[str, str]] = []
        searches: list[object] = []
        monkeypatch.setattr(
            app.screen,
            "request_export",
            lambda path, *, selection: requests.append((selection, path)),
        )
        monkeypatch.setattr(app.screen, "_start_search_worker", searches.append)

        app.screen._search_input.focus()
        app.screen._search_input.value = "/export nested/result.md"
        await pilot.pause()
        assert app.screen._enum_dropdown.display is False
        await pilot.press("enter")
        app.screen.on_search_requested(_search_requested("/export-thread thread.md"))
        app.screen.on_search_requested(_search_requested("/help still a query"))
        await pilot.pause()

        assert requests == [
            ("records", "nested/result.md"),
            ("thread", "thread.md"),
        ]
        assert len(searches) == 1


@pytest.mark.slow
async def test_export_without_selection_is_a_path_free_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command on an empty result set does not launch disk work."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        notes = _capture_notifications(app.screen, monkeypatch)
        workers: list[tuple[tuple[object, ...], dict[str, object]]] = []
        monkeypatch.setattr(
            app.screen,
            "run_worker",
            lambda *a, **k: workers.append((a, k)),
        )

        app.screen.on_search_requested(_search_requested("/export"))
        await pilot.pause()

        assert workers == []
        assert len(notes) == 1
        assert notes[0][1]["severity"] == "error"
        assert "select" in str(notes[0][0][0]).lower()
        assert str(tmp_path) not in str(notes)


@pytest.mark.parametrize("explicit", [False, True], ids=("private", "explicit"))
@pytest.mark.slow
async def test_record_export_writes_markdown_and_preserves_results(
    explicit: bool,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default and explicit sinks export exactly the selected record."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = (
        _record(tmp_path, "first exact body", ordinal=1),
        _record(tmp_path, "second private body", ordinal=2),
    )
    destination = tmp_path / "chosen directory" / "selected record.md"
    if explicit:
        destination.parent.mkdir()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, records, selected=0)
        notes = _capture_notifications(app.screen, monkeypatch)
        before_all = list(app.screen.all_records)
        before_filtered = list(app.screen.filtered_records)

        command = f"/export {destination}" if explicit else "/export"
        app.screen.on_search_requested(_search_requested(command))
        if explicit:
            await _wait_for(destination.exists)
            exported = destination
        else:
            export_dir = tmp_path / "data" / "agentgrep" / "exports"
            await _wait_for(lambda: bool(list(export_dir.glob("*.md"))))
            exported = next(export_dir.glob("*.md"))
        await pilot.pause()

        text = exported.read_text(encoding="utf-8")
        assert text.startswith("# agentgrep record export")
        assert "first exact body" in text
        assert "second private body" not in text
        assert app.screen.all_records == before_all
        assert app.screen.filtered_records == before_filtered
        assert app.screen._current_detail_record is records[0]
        assert len(notes) == 1
        message = str(notes[0][0][0])
        assert exported.name in message
        assert str(exported.parent) not in message
        assert "markdown" in message
        assert "1 record" in message


@pytest.mark.slow
async def test_thread_export_uses_only_selected_observed_thread(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed and threadless active results do not contaminate the chosen thread."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = (
        _record(tmp_path, "thread a first", ordinal=1, session_id="session-a"),
        _record(tmp_path, "thread b", ordinal=2, session_id="session-b"),
        _record(tmp_path, "thread a second", ordinal=3, session_id="session-a"),
        _record(tmp_path, "threadless", ordinal=4, session_id=None),
    )
    destination = tmp_path / "thread.md"
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, records, selected=2)
        notes = _capture_notifications(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested(f"/export-thread {destination}"))
        await _wait_for(destination.exists)
        await pilot.pause()

        text = destination.read_text(encoding="utf-8")
        assert text.startswith("# agentgrep observed thread export")
        assert "thread a first" in text
        assert "thread a second" in text
        assert "thread b" not in text
        assert "threadless" not in text
        assert "- Record count: 2" in text
        assert "- Fidelity: unordered" in text
        assert "2 records" in str(notes[0][0][0])


@pytest.mark.slow
async def test_thread_export_without_path_uses_private_markdown_sink(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-path thread command writes a collision-safe canonical artifact."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = (
        _record(tmp_path, "first", ordinal=1),
        _record(tmp_path, "second", ordinal=2),
    )
    export_dir = tmp_path / "data" / "agentgrep" / "exports"
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, records)
        notes = _capture_notifications(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested("/export-thread"))
        await _wait_for(lambda: bool(list(export_dir.glob("*.md"))))
        exported = next(export_dir.glob("*.md"))
        await pilot.pause()

        assert exported.name.startswith("agentgrep-agt1-")
        assert exported.read_text(encoding="utf-8").startswith(
            "# agentgrep observed thread export",
        )
        assert exported.name in str(notes[0][0][0])
        assert str(export_dir) not in str(notes)


@pytest.mark.slow
async def test_thread_export_freezes_result_count_when_accepted(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A streamed turn arriving before deferred capture is not retroactively selected."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    first = _record(tmp_path, "accepted turn", ordinal=1)
    late = _record(tmp_path, "late turn", ordinal=2)
    destination = tmp_path / "accepted-thread.md"
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (first,))
        scheduled: list[tuple[t.Callable[..., t.Awaitable[None]], tuple[object, ...]]] = []

        def defer(
            callback: t.Callable[..., t.Awaitable[None]],
            *args: object,
        ) -> None:
            scheduled.append((callback, args))

        monkeypatch.setattr(app.screen, "call_later", defer)
        app.screen.on_search_requested(_search_requested(f"/export-thread {destination}"))
        assert len(scheduled) == 1

        app.screen.filtered_records.append(late)
        callback, args = scheduled.pop()
        await callback(*args)
        await _wait_for(destination.exists)

        text = destination.read_text(encoding="utf-8")
        assert "accepted turn" in text
        assert "late turn" not in text
        assert "- Record count: 1" in text


@pytest.mark.slow
async def test_thread_export_rejects_threadless_selection(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A null canonical thread identity is rejected without creating a file."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "threadless", ordinal=1, session_id=None)
    destination = tmp_path / "thread.md"
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (record,))
        notes = _capture_notifications(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested(f"/export-thread {destination}"))
        await _wait_for(lambda: bool(notes))

        assert not destination.exists()
        assert notes[0][1]["severity"] == "error"
        assert "thread" in str(notes[0][0][0]).lower()
        assert str(tmp_path) not in str(notes)


@pytest.mark.parametrize("unsafe", ["exists", "symlink", "source"])
@pytest.mark.slow
async def test_explicit_export_refuses_unsafe_destinations(
    unsafe: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-overwrite, no-symlink, and source-alias rules reach the TUI."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "protected body", ordinal=1)
    destination = tmp_path / "destination.md"
    if unsafe == "exists":
        destination.write_text("keep", encoding="utf-8")
    elif unsafe == "symlink":
        target = tmp_path / "target.md"
        target.write_text("keep", encoding="utf-8")
        destination.symlink_to(target)
    else:
        destination = record.path
        destination.write_text("source", encoding="utf-8")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (record,))
        notes = _capture_notifications(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested(f"/export {destination}"))
        await _wait_for(lambda: bool(notes))

        assert notes[0][1]["severity"] == "error"
        assert str(tmp_path) not in str(notes)
        if unsafe == "exists":
            assert destination.read_text(encoding="utf-8") == "keep"
        elif unsafe == "symlink":
            assert destination.is_symlink()
            assert destination.read_text(encoding="utf-8") == "keep"
        else:
            assert destination.read_text(encoding="utf-8") == "source"


@pytest.mark.slow
async def test_unexpected_writer_error_is_path_free(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An arbitrary filesystem exception cannot leak its destination text."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "body", ordinal=1)
    secret_path = tmp_path / "private-name.md"

    def fail_write(*args: object, **kwargs: object) -> pathlib.Path:
        message = f"failed at {secret_path}"
        raise OSError(message)

    monkeypatch.setattr(record_export, "write_export", fail_write)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (record,))
        notes = _capture_notifications(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested(f"/export {secret_path}"))
        await _wait_for(lambda: bool(notes))

        assert notes[0][1]["severity"] == "error"
        assert str(secret_path) not in str(notes)
        assert "could not" in str(notes[0][0][0]).lower()


@pytest.mark.slow
async def test_rapid_duplicate_export_is_blocked_not_superseded(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only one durable export may be accepted at a time."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "body", ordinal=1)
    destination = tmp_path / "result.md"
    started = threading.Event()
    release = threading.Event()
    real_render = record_export.render_export
    calls = 0

    def slow_render(
        records: cabc.Iterable[SearchRecord],
        *,
        format: record_export.ExportFormat,  # noqa: A002 - mirrors public API.
        include_bodies: bool,
        selection: record_export.ExportSelection = "records",
    ) -> record_export.ExportArtifact:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(3)
        return real_render(
            records,
            format=format,
            include_bodies=include_bodies,
            selection=selection,
        )

    monkeypatch.setattr(record_export, "render_export", slow_render)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (record,))
        notes = _capture_notifications(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested(f"/export {destination}"))
        assert await asyncio.to_thread(started.wait, 2)
        app.screen.on_search_requested(_search_requested(f"/export {destination}"))
        await pilot.pause()
        assert calls == 1
        assert any("progress" in str(note[0][0]).lower() for note in notes)

        release.set()
        await _wait_for(destination.exists)
        await pilot.pause()
        assert calls == 1


@pytest.mark.slow
async def test_record_switch_does_not_change_accepted_export(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker owns the exact selection captured when the command was accepted."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = (
        _record(tmp_path, "first selected", ordinal=1),
        _record(tmp_path, "second later", ordinal=2),
    )
    destination = tmp_path / "selected.md"
    started = threading.Event()
    release = threading.Event()
    real_render = record_export.render_export

    def slow_render(
        records: cabc.Iterable[SearchRecord],
        *,
        format: record_export.ExportFormat,  # noqa: A002 - mirrors public API.
        include_bodies: bool,
        selection: record_export.ExportSelection = "records",
    ) -> record_export.ExportArtifact:
        started.set()
        assert release.wait(3)
        return real_render(
            records,
            format=format,
            include_bodies=include_bodies,
            selection=selection,
        )

    monkeypatch.setattr(record_export, "render_export", slow_render)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, records, selected=0)

        app.screen.on_search_requested(_search_requested(f"/export {destination}"))
        assert await asyncio.to_thread(started.wait, 2)
        app.screen._results.highlighted = 1
        app.screen._current_detail_record = records[1]
        release.set()
        await _wait_for(destination.exists)

        text = destination.read_text(encoding="utf-8")
        assert "first selected" in text
        assert "second later" not in text


@pytest.mark.slow
async def test_thread_snapshot_aborts_if_results_reset_mid_copy(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunk-yielded result capture never launches with a mixed-time tuple."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = tuple(
        _record(tmp_path, f"body {index}", ordinal=index, session_id="session-a")
        for index in range(1, 402)
    )
    destination = tmp_path / "thread.md"
    first_chunk = asyncio.Event()
    continue_copy = asyncio.Event()
    real_stream_apply = _runtime.stream_apply

    async def paused_stream_apply(
        items: cabc.Sequence[SearchRecord],
        apply_chunk: cabc.Callable[[cabc.Sequence[SearchRecord]], None],
        *,
        chunk_size: int = 200,
        yield_between: cabc.Callable[[], cabc.Awaitable[None]] | None = None,
    ) -> None:
        del yield_between

        async def pause_once() -> None:
            first_chunk.set()
            await continue_copy.wait()

        await real_stream_apply(
            items,
            apply_chunk,
            chunk_size=chunk_size,
            yield_between=pause_once,
        )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, records)
        monkeypatch.setattr(_runtime, "stream_apply", paused_stream_apply)
        notes = _capture_notifications(app.screen, monkeypatch)
        worker_calls: list[dict[str, object]] = []
        real_run_worker = app.screen.run_worker

        def track_worker(*args: object, **kwargs: object) -> object:
            if kwargs.get("group") == "export":
                worker_calls.append(kwargs)
            return real_run_worker(*args, **kwargs)

        monkeypatch.setattr(app.screen, "run_worker", track_worker)

        app.screen.on_search_requested(_search_requested(f"/export-thread {destination}"))
        await asyncio.wait_for(first_chunk.wait(), 2)
        app.screen._reset_search_chrome()
        continue_copy.set()
        await _wait_for(lambda: bool(notes))

        assert worker_calls == []
        assert not destination.exists()
        assert "changed" in str(notes[0][0][0]).lower()
        assert app.screen._export_pending is False


@pytest.mark.slow
async def test_teardown_cancels_export_before_write_and_drops_callback(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A suspended worker observes teardown before starting durable output."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "large body " * 200_000, ordinal=1)
    destination = tmp_path / "canceled.md"
    started = threading.Event()
    release = threading.Event()
    write_calls = 0
    real_render = record_export.render_export
    real_write = record_export.write_export

    def slow_render(
        records: cabc.Iterable[SearchRecord],
        *,
        format: record_export.ExportFormat,  # noqa: A002 - mirrors public API.
        include_bodies: bool,
        selection: record_export.ExportSelection = "records",
    ) -> record_export.ExportArtifact:
        started.set()
        assert release.wait(3)
        return real_render(
            records,
            format=format,
            include_bodies=include_bodies,
            selection=selection,
        )

    def track_write(
        artifact: record_export.ExportArtifact,
        destination: str | pathlib.Path,
        *,
        force: bool = False,
        protected_paths: cabc.Iterable[str | pathlib.Path] = (),
    ) -> pathlib.Path:
        nonlocal write_calls
        write_calls += 1
        return real_write(
            artifact,
            destination,
            force=force,
            protected_paths=protected_paths,
        )

    monkeypatch.setattr(record_export, "render_export", slow_render)
    monkeypatch.setattr(record_export, "write_export", track_write)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (record,))
        notes = _capture_notifications(app.screen, monkeypatch)

        app.screen.on_search_requested(_search_requested(f"/export {destination}"))
        assert await asyncio.to_thread(started.wait, 2)
        app.screen.on_unmount()
        release.set()
        await asyncio.sleep(0.1)

        assert write_calls == 0
        assert not destination.exists()
        assert notes == []


@pytest.mark.slow
async def test_stale_export_callback_cannot_clear_live_pending_state(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generation gating drops an old completion without touching a newer request."""
    from agentgrep.ui.layouts.hud import _ExportCompleted

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        notes = _capture_notifications(app.screen, monkeypatch)
        app.screen._export_generation = 8
        app.screen._export_pending = True

        app.screen._apply_export_completed(
            7,
            _ExportCompleted(
                filename="old.md",
                format="markdown",
                selection="records",
                record_count=1,
                error=None,
            ),
        )

        assert notes == []
        assert app.screen._export_pending is True


@pytest.mark.slow
async def test_large_export_worker_keeps_pump_responsive(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large body work stays off-pump while keystrokes continue to dispatch."""
    monkeypatch.setenv("AGENTGREP_TUI_WATCHDOG", "1")
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _record(tmp_path, "large body\n" * 300_000, ordinal=1)
    destination = tmp_path / "large.md"
    started = threading.Event()
    release = threading.Event()
    real_render = record_export.render_export
    real_write = record_export.write_export

    def slow_render(
        records: cabc.Iterable[SearchRecord],
        *,
        format: record_export.ExportFormat,  # noqa: A002 - mirrors public API.
        include_bodies: bool,
        selection: record_export.ExportSelection = "records",
    ) -> record_export.ExportArtifact:
        _runtime.assert_off_pump("export render")
        started.set()
        assert release.wait(3)
        return real_render(
            records,
            format=format,
            include_bodies=include_bodies,
            selection=selection,
        )

    def checked_write(
        artifact: record_export.ExportArtifact,
        output: str | pathlib.Path,
        *,
        force: bool = False,
        protected_paths: cabc.Iterable[str | pathlib.Path] = (),
    ) -> pathlib.Path:
        _runtime.assert_off_pump("export write")
        return real_write(
            artifact,
            output,
            force=force,
            protected_paths=protected_paths,
        )

    monkeypatch.setattr(record_export, "render_export", slow_render)
    monkeypatch.setattr(record_export, "write_export", checked_write)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, (record,))
        app.screen._search_input.focus()

        app.screen.on_search_requested(_search_requested(f"/export {destination}"))
        assert await asyncio.to_thread(started.wait, 2)
        await pilot.press("x")
        await pilot.pause()
        assert app.screen._search_input.value.endswith("x")

        release.set()
        await _wait_for(destination.exists)


@pytest.mark.slow
async def test_large_observed_thread_identity_and_output_stay_off_pump(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A many-record thread still leaves keystrokes responsive during identity work."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = tuple(
        _record(
            tmp_path,
            f"turn {index} " + ("x" * 5_000),
            ordinal=index,
            session_id="large-thread",
        )
        for index in range(1, 402)
    )
    destination = tmp_path / "large-thread.md"
    started = threading.Event()
    release = threading.Event()
    real_identity = identity.record_identity

    def slow_identity(record: SearchRecord) -> identity.RecordIdentity:
        _runtime.assert_off_pump("thread identity")
        if not started.is_set():
            started.set()
            assert release.wait(3)
        return real_identity(record)

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _load_records(app.screen, records)
        await pilot.pause()
        monkeypatch.setattr(identity, "record_identity", slow_identity)
        app.screen._search_input.focus()

        app.screen.on_search_requested(_search_requested(f"/export-thread {destination}"))
        assert await asyncio.to_thread(started.wait, 2)
        await pilot.press("x")
        await pilot.pause()
        assert app.screen._search_input.value.endswith("x")

        release.set()
        await _wait_for(destination.exists, timeout=5)
        text = destination.read_text(encoding="utf-8")
        assert "- Record count: 401" in text
        assert text.startswith("# agentgrep observed thread export")
