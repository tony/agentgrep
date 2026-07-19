# ruff: noqa: D103
"""Focused regression contracts for asynchronous TUI detail search."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import pathlib
import typing as t

import pytest

from agentgrep.progress import SearchControl
from agentgrep.records import RecordPosition, SearchQuery, SearchRecord

pytestmark = [pytest.mark.tui, pytest.mark.slow]


class _NoopInvoker:
    """Search seam for a HUD whose detail path is tested directly."""

    def run(
        self,
        query: SearchQuery,
        *,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        del query, control, emit


def _detail_app(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> t.Any:
    """Build an isolated HUD with no backing source scan."""
    from agentgrep.ui import registry
    from agentgrep.ui._context import UiContext
    from agentgrep.ui._shell import ExplorerApp

    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    query = SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    layout_spec = registry.layout_spec("hud")
    workflow_spec = registry.workflow_spec("search")
    assert layout_spec is not None
    assert workflow_spec is not None
    return ExplorerApp(
        UiContext(
            home=home,
            invoker=t.cast("t.Any", _NoopInvoker()),
            query=query,
            control=SearchControl(),
            base_scope=query.scope,
        ),
        composition=registry._UiComposition(
            layout_type=layout_spec.loader(),
            workflow_type=workflow_spec.loader(),
        ),
    )


async def test_detail_find_refreshes_after_async_json_render(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _detail_app(tmp_path, monkeypatch)
    body = '{"front":"x","farneedle":"target"}'
    query = '"farneedle": "target"'
    record = SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "pending-find.jsonl",
        text=body,
        session_id="pending",
        identity_namespace="codex.session",
        position=RecordPosition(native_id="message-pending"),
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        workers: list[t.Callable[[], None]] = []
        monkeypatch.setattr(
            app.screen,
            "run_worker",
            lambda target, **_kwargs: workers.append(target),
        )
        app.screen.show_detail(record)
        app.screen.action_open_detail_find()
        app.screen._detail_find_input.load_query(query)
        app.screen._run_detail_find(query, reset_cursor=True)
        assert app.screen._detail_find_matches == []

        await asyncio.to_thread(workers[0])
        await pilot.pause()

        assert len(app.screen._detail_find_matches) == 1
        assert query in app.screen._detail_find_source
