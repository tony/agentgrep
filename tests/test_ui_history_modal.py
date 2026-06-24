"""Tests for the Ctrl-R search-history recall modal (``HistoryRecall``).

The pure preview/row composition is tested on a bare instance; the interactive
behaviour (filter narrows, Enter accepts, Esc cancels, highlight drives the
preview) is driven through a tiny host ``App`` and ``Pilot`` — the modal is a
``ModalScreen`` so it needs a running app to push onto.
"""

from __future__ import annotations

from textual.app import App
from textual.widgets import OptionList, Static

from agentgrep.ui._history import HistoryEntry
from agentgrep.ui.widgets.history import HistoryRecall


def _preview_text(app: App[None]) -> str:
    """Read the plain text currently shown in the modal's preview pane."""
    preview = app.screen.query_one("#history-preview", Static)
    content = getattr(preview, "_Static__content", "")  # the VisualType last passed to update()
    return getattr(content, "plain", str(content))


def _entries() -> list[HistoryEntry]:
    """Two newest-first history entries for the modal tests."""
    return [
        HistoryEntry(text="agent:codex refactor planner", ts=200, scope="prompts"),
        HistoryEntry(text="tmux pane capture", ts=100, scope="prompts"),
    ]


class _HistoryHostApp(App[None]):
    """Minimal host that pushes the modal and captures its dismiss value."""

    def __init__(self, entries: list[HistoryEntry], *, seed: str = "") -> None:
        super().__init__()
        self._entries = entries
        self._seed = seed
        self.result: str | None | object = "UNSET"

    def on_mount(self) -> None:
        self.push_screen(HistoryRecall(self._entries, seed=self._seed), self._capture)

    def _capture(self, value: str | None) -> None:
        self.result = value


def test_preview_truncates_with_plus_n_lines() -> None:
    """A long entry's preview shows the first rows then a '+N lines' indicator."""
    modal = HistoryRecall([], seed="")
    entry = HistoryEntry(text="\n".join(f"line {i}" for i in range(20)), ts=0)
    content = modal._preview_content(entry)
    assert "+9 lines" in content.plain  # 20 lines, budget 12 -> show 11 + "+9 lines"


def test_row_includes_relative_time_and_text() -> None:
    """Each list row carries a relative-time prefix and the query text."""
    from textual.fuzzy import Matcher

    modal = HistoryRecall([], seed="")
    entry = HistoryEntry(text="study the mcp server", ts=0)
    row = modal._row(entry, Matcher("mcp"))
    assert "study the mcp server" in row.plain
    assert "ago" in row.plain  # the relative-time prefix


async def test_modal_enter_accepts_highlighted_query() -> None:
    """Enter dismisses with the highlighted (newest) query's text."""
    app = _HistoryHostApp(_entries())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "agent:codex refactor planner"


async def test_modal_filter_narrows_then_accepts() -> None:
    """Typing filters the list; Enter then accepts the surviving match."""
    app = _HistoryHostApp(_entries())
    async with app.run_test() as pilot:
        await pilot.pause()
        for char in "tmux":
            await pilot.press(char)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "tmux pane capture"


async def test_modal_escape_dismisses_none() -> None:
    """Escape cancels and dismisses with ``None`` (restore the prior box text)."""
    app = _HistoryHostApp(_entries())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.result is None


async def test_modal_down_updates_preview() -> None:
    """Moving the highlight down repaints the preview with the older entry."""
    app = _HistoryHostApp(_entries())
    async with app.run_test() as pilot:
        await pilot.pause()
        # On open the newest entry previews.
        assert "agent:codex refactor planner" in _preview_text(app)
        await pilot.press("down")
        await pilot.pause()
        assert app.screen.query_one("#history-list", OptionList).highlighted == 1
        assert "tmux pane capture" in _preview_text(app)


async def test_modal_empty_history_shows_hint() -> None:
    """With no history the modal shows a muted hint, not a crash."""
    app = _HistoryHostApp([])
    async with app.run_test() as pilot:
        await pilot.pause()
        option_list = app.screen.query_one("#history-list", OptionList)
        assert option_list.option_count == 1  # the disabled hint row
        # Enter on the empty hint cancels rather than accepting a bogus value.
        await pilot.press("enter")
        await pilot.pause()
        assert app.result is None


async def test_modal_seed_filters_on_open() -> None:
    """Opening with a seed pre-fills the filter and narrows immediately."""
    app = _HistoryHostApp(_entries(), seed="tmux")
    async with app.run_test() as pilot:
        await pilot.pause()
        option_list = app.screen.query_one("#history-list", OptionList)
        assert option_list.option_count == 1
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "tmux pane capture"
