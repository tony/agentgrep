"""Pilot tests for the chat layout (ADR 0014, the conversation transcript).

``ChatLayout`` shares the engine seam and normalized records with the HUD and
grep-log but presents them as a conversation: a ``you ▸ …`` query turn followed
by result bubbles and a count note. These tests mount it (pushed onto the shell)
and drive its streaming/present hooks directly, mirroring the grep-log tests.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

from agentgrep.progress import StreamingRecordsBatch
from agentgrep.records import SearchRecord
from agentgrep.ui.widgets.turns import MessageTurn, QueryTurn, ResultTurn, SystemTurn
from tests.test_agentgrep import _build_empty_ui_app


def _record(tmp_path: pathlib.Path, idx: int, text: str) -> SearchRecord:
    """Build a minimal record for the transcript."""
    return SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / f"r{idx}.jsonl",
        text=text,
    )


async def _mount_chat(app: t.Any, pilot: t.Any) -> t.Any:
    """Push a chat layout (search workflow) onto the running shell."""
    from agentgrep.ui.layouts.chat import ChatLayout
    from agentgrep.ui.workflows.search import SearchWorkflow

    layout = ChatLayout(app._ctx, SearchWorkflow())
    await app.push_screen(layout)
    await pilot.pause()
    return layout


def _turns(layout: t.Any, kind: type) -> list[MessageTurn]:
    """Return the mounted bubbles whose value object is of ``kind``."""
    return [
        child
        for child in layout.query_one("#transcript").children
        if isinstance(child, MessageTurn) and isinstance(child.turn, kind)
    ]


async def test_chat_streams_records_as_result_turns(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A streamed batch extends the buffer and mounts one result bubble per record."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [_record(tmp_path, i, f"row {i}") for i in range(3)]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_chat(app, pilot)
        await layout._apply_event(
            layout._generation,
            StreamingRecordsBatch(records=tuple(records), total=3),
        )
        await pilot.pause()
        assert layout._records == records
        assert len(_turns(layout, ResultTurn)) == 3


async def test_chat_finished_posts_count_turn(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished search freezes the status line and posts a count system turn."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_chat(app, pilot)
        layout._apply_finished("complete", 5, 1.2, None)
        await pilot.pause()
        system_turns = _turns(layout, SystemTurn)
        assert system_turns
        assert "5" in system_turns[-1].turn.text


async def test_chat_run_search_posts_query_turn(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_search`` clears the transcript and posts the query as a user turn."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_chat(app, pilot)
        layout._pending_turn_text = "rust error"
        layout.run_search(layout.build_query("rust error"))
        await pilot.pause()
        query_turns = _turns(layout, QueryTurn)
        assert query_turns
        assert query_turns[-1].turn.text == "rust error"
        assert query_turns[-1].turn.depth == 0


async def test_chat_filter_appends_narrowed_block(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browse/deductive filter appends a query turn + only matching results."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        _record(tmp_path, 0, "needle here"),
        _record(tmp_path, 1, "haystack only"),
        _record(tmp_path, 2, "needle again"),
    ]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_chat(app, pilot)
        await layout._apply_event(
            layout._generation,
            StreamingRecordsBatch(records=tuple(records), total=3),
        )
        await pilot.pause()
        assert len(_turns(layout, ResultTurn)) == 3
        matching = tuple(r for r in records if "needle" in r.text)
        await layout._apply_block_filter(layout._filter_generation, matching)
        await pilot.pause()
        # 3 from the haystack stream + 2 from the narrowed block.
        assert len(_turns(layout, ResultTurn)) == 5


async def test_chat_stale_generation_is_dropped(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch from a superseded generation never reaches the transcript (NB-10)."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [_record(tmp_path, 0, "row")]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_chat(app, pilot)
        await layout._apply_event(
            layout._generation - 1,  # a stale generation
            StreamingRecordsBatch(records=tuple(records), total=1),
        )
        await pilot.pause()
        assert layout._records == []
        assert _turns(layout, ResultTurn) == []


async def test_chat_result_cap_bounds_mounted_turns(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large block mounts at most the per-block cap and reports the full count."""
    from agentgrep.ui.layouts import chat as chat_mod

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [_record(tmp_path, i, f"row {i}") for i in range(chat_mod._RESULT_TURN_CAP + 25)]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_chat(app, pilot)
        await layout._apply_event(
            layout._generation,
            StreamingRecordsBatch(records=tuple(records), total=len(records)),
        )
        layout._apply_finished("complete", len(records), 0.1, None)
        await pilot.pause()
        assert len(_turns(layout, ResultTurn)) == chat_mod._RESULT_TURN_CAP
        assert "narrow to see all" in _turns(layout, SystemTurn)[-1].turn.text


async def test_chat_opens_detail_on_focused_result(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Enter`` on a focused result bubble pushes the detail modal."""
    from agentgrep.ui.layouts.chat import DetailScreen

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [_record(tmp_path, 0, "needle body")]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_chat(app, pilot)
        await layout._apply_event(
            layout._generation,
            StreamingRecordsBatch(records=tuple(records), total=1),
        )
        await pilot.pause()
        result_turn = _turns(layout, ResultTurn)[0]
        result_turn.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)


async def test_chat_search_input_does_not_crash_on_keys(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typing in the chat prompt must not raise (it reuses ``SearchInput``)."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        layout = await _mount_chat(app, pilot)
        layout._search_input.focus()
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert layout._search_input.value == "a"
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert layout._search_input.value == ""
