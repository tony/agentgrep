"""What the explorer tells the user after a copy.

``App.copy_to_clipboard`` writes one bare OSC-52 escape and returns nothing, so
no code path can observe whether the bytes arrived. The toast must therefore
report what was sent rather than assert delivery, and inside tmux it must name
the ``set-clipboard`` option that decides whether the sequence survives at all.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui.app import build_streaming_ui_app

pytestmark = pytest.mark.tui


def _make_record(text: str) -> SearchRecord:
    """Build a small plain-text prompt record (built inline, no worker)."""
    return SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions",
        path=pathlib.Path("/home/user/.codex/sessions/2026/07/22/demo.jsonl"),
        text=text,
    )


def _empty_query() -> SearchQuery:
    """Build an idle search query (no terms)."""
    return SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )


def _notification_bodies(app: t.Any) -> list[str]:
    """Return every notification message the app has raised, oldest first."""
    return [str(notification.message) for notification in app._notifications]


async def test_copy_reports_what_was_sent_not_that_it_arrived(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``y`` toast names the payload size and the mechanism, and claims nothing."""
    monkeypatch.delenv("TMUX", raising=False)
    record = _make_record("line one\nline two\n")
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        await pilot.pause()
        layout.all_records = [record]
        layout.filtered_records = [record]
        layout.show_detail(record)
        await app.workers.wait_for_complete()
        await pilot.pause()

        layout.action_copy_detail_source()
        await pilot.pause()

        bodies = _notification_bodies(app)
        assert any("sent source to the clipboard" in body for body in bodies)
        assert any("OSC 52" in body for body in bodies)
        # The word the old toast used, and the claim the code cannot support.
        assert not any(body.startswith("copied ") for body in bodies)


async def test_tmux_caveat_is_raised_once_per_session(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside tmux the first copy carries the caveat and later copies do not."""
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,108550,24")
    record = _make_record("line one\nline two\n")
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(tmp_path, _empty_query(), control=SearchControl()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        layout = app.screen
        await pilot.pause()
        layout.all_records = [record]
        layout.filtered_records = [record]
        layout.show_detail(record)
        await app.workers.wait_for_complete()
        await pilot.pause()

        layout.action_copy_detail_source()
        await pilot.pause()
        first = [b for b in _notification_bodies(app) if "set-clipboard" in b]
        assert len(first) == 1

        layout.action_copy_detail_rendered()
        await pilot.pause()
        again = [b for b in _notification_bodies(app) if "set-clipboard" in b]
        assert len(again) == 1
