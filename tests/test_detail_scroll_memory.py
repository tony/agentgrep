"""Per-record scroll-memory contracts for ``DetailScroll``."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from agentgrep.ui.widgets import DetailScroll, DetailScrollChanged

pytestmark = pytest.mark.tui


class _DetailScrollApp(App[None]):
    """Minimal mounted owner used to exercise real scrolling and messages."""

    def __init__(self) -> None:
        super().__init__()
        self.scroll_events: list[DetailScrollChanged] = []

    def compose(self) -> ComposeResult:
        """Mount one tall detail body inside the scroll widget."""
        with DetailScroll(id="detail-scroll"):
            yield Static("\n".join(f"line {index}" for index in range(200)))

    def on_detail_scroll_changed(self, message: DetailScrollChanged) -> None:
        """Retain emitted snapshots for assertions."""
        self.scroll_events.append(message)


async def test_detail_scroll_remembers_each_record_and_emits_token() -> None:
    """Switching records restores offsets and identifies scroll snapshots."""
    app = _DetailScrollApp()
    async with app.run_test(size=(40, 12)) as pilot:
        scroll = app.query_one(DetailScroll)
        scroll.activate_record(11)
        scroll.scroll_to(y=6, animate=False)
        await pilot.pause()
        assert scroll.scroll_y == 6
        assert app.scroll_events[-1].record_token == 11

        scroll.activate_record(22)
        await pilot.pause()
        assert scroll.scroll_y == 0
        scroll.scroll_to(y=9, animate=False)
        await pilot.pause()

        scroll.activate_record(11)
        await pilot.pause()
        assert scroll.scroll_y == 6

        scroll.clear_record_memory()
        scroll.activate_record(22)
        await pilot.pause()
        assert scroll.scroll_y == 0
