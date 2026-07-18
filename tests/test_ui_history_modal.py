"""Tests for the Ctrl-R search-history recall modal (``HistoryRecall``).

The pure preview/row composition is tested on a bare instance; the interactive
behaviour (filter narrows, Enter accepts, Esc cancels, highlight drives the
preview) is driven through a tiny host ``App`` and ``Pilot`` — the modal is a
``ModalScreen`` so it needs a running app to push onto.
"""

from __future__ import annotations

import threading
import typing as t

import pytest
from textual.app import App
from textual.widgets import Input, OptionList, Static

from agentgrep.ui._history import HistoryEntry
from agentgrep.ui.widgets import history as history_module
from agentgrep.ui.widgets.history import _ROW_TEXT_MAX_CHARS, HistoryRecall
from agentgrep.ui.widgets.inputs import INPUT_MAX_LENGTH


def _preview_text(app: App[None]) -> str:
    """Read the plain text currently shown in the modal's preview pane."""
    preview = app.screen.query_one("#history-preview", Static)
    content = preview.render()
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
    modal = HistoryRecall([], seed="")
    entry = HistoryEntry(text="study the mcp server", ts=0)
    row = modal._row(entry, "mcp")
    assert "study the mcp server" in row.plain
    assert "ago" in row.plain  # the relative-time prefix


def test_modal_bounds_foreign_entries_and_row_projection() -> None:
    """Injected entries are bounded, while each list row stays compact."""
    modal = HistoryRecall([HistoryEntry(text="x" * 10_000, ts=0)])
    [entry] = modal._entries
    assert len(entry.text) == INPUT_MAX_LENGTH
    row = modal._row(entry, "")
    assert len(row.plain) <= _ROW_TEXT_MAX_CHARS + 10
    assert row.plain.endswith("…")


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
        await pilot.pause(0.2)
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "tmux pane capture"


async def test_modal_submit_flushes_pending_filter() -> None:
    """Enter waits for the current query instead of accepting a stale row."""
    app = _HistoryHostApp(_entries())
    async with app.run_test() as pilot:
        await pilot.pause()
        history_filter = app.screen.query_one("#history-filter", Input)
        history_filter.value = "tmux"
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "tmux pane capture"


async def test_modal_debounces_rapid_filter_scoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rapid typing scores only the final immutable query snapshot."""
    calls: list[str] = []
    original = history_module._score_snapshot

    def spy(snapshot: history_module._FilterSnapshot) -> tuple[history_module._FilterRow, ...]:
        calls.append(snapshot.query)
        return original(snapshot)

    monkeypatch.setattr(history_module, "_score_snapshot", spy)
    app = _HistoryHostApp(_entries())
    async with app.run_test() as pilot:
        await pilot.pause()
        calls.clear()
        history_filter = app.screen.query_one("#history-filter", Input)
        history_filter.value = "t"
        history_filter.value = "tm"
        history_filter.value = "tmux"
        await pilot.pause(0.05)
        assert calls == []
        await pilot.pause(0.2)
        assert calls == ["tmux"]


async def test_modal_filter_offloads_and_drops_stale_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A draining old scorer cannot repaint the newest off-pump result."""
    pump_thread = threading.get_ident()
    score_threads: list[int] = []
    slow_started = threading.Event()
    release_slow = threading.Event()
    original = history_module._score_snapshot

    def controlled_score(
        snapshot: history_module._FilterSnapshot,
    ) -> tuple[history_module._FilterRow, ...]:
        score_threads.append(threading.get_ident())
        if snapshot.query == "codex" and not slow_started.is_set():
            slow_started.set()
            release_slow.wait(timeout=2)
        return original(snapshot)

    monkeypatch.setattr(history_module, "_score_snapshot", controlled_score)
    app = _HistoryHostApp(_entries())
    async with app.run_test() as pilot:
        await pilot.pause()
        history_filter = app.screen.query_one("#history-filter", Input)
        history_filter.value = "codex"
        await pilot.pause(0.2)
        assert slow_started.is_set()

        history_filter.value = "tmux"
        await pilot.pause(0.2)
        modal = t.cast("HistoryRecall", app.screen)
        assert modal._matches[0].text == "tmux pane capture"

        release_slow.set()
        await pilot.pause()
        assert modal._matches[0].text == "tmux pane capture"
        assert score_threads and all(thread != pump_thread for thread in score_threads)


async def test_modal_close_invalidates_draining_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the modal makes a draining scorer's callback harmless."""
    slow_started = threading.Event()
    release_slow = threading.Event()
    original = history_module._score_snapshot

    def controlled_score(
        snapshot: history_module._FilterSnapshot,
    ) -> tuple[history_module._FilterRow, ...]:
        if snapshot.query == "codex":
            slow_started.set()
            release_slow.wait(timeout=2)
        return original(snapshot)

    monkeypatch.setattr(history_module, "_score_snapshot", controlled_score)
    app = _HistoryHostApp(_entries())
    async with app.run_test() as pilot:
        await pilot.pause()
        history_filter = app.screen.query_one("#history-filter", Input)
        history_filter.value = "codex"
        await pilot.pause(0.2)
        assert slow_started.is_set()

        await pilot.press("escape")
        await pilot.pause()
        assert app.result is None
        release_slow.set()
        await pilot.pause()


async def test_modal_filter_avoids_recursive_textual_matcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adversarial fuzzy text never reaches Textual's recursive matcher."""
    from textual.fuzzy import Matcher

    def fail_recursive_match(self: Matcher, candidate: str) -> float:
        del self, candidate
        raise AssertionError

    monkeypatch.setattr(Matcher, "match", fail_recursive_match)
    entries = [HistoryEntry(text="a_" * 30 + "b", ts=1)]
    app = _HistoryHostApp(entries)
    async with app.run_test() as pilot:
        await pilot.pause()
        history_filter = app.screen.query_one("#history-filter", Input)
        history_filter.value = "aaaaab"
        await pilot.pause()
        assert app.screen.query_one("#history-list", OptionList).option_count == 1


async def test_modal_filter_matches_beyond_row_projection() -> None:
    """Compact list rows do not narrow the searchable recalled query."""
    full_query = "x" * (_ROW_TEXT_MAX_CHARS + 20) + " needle"
    app = _HistoryHostApp([HistoryEntry(text=full_query, ts=1)])
    async with app.run_test() as pilot:
        await pilot.pause()
        history_filter = app.screen.query_one("#history-filter", Input)
        history_filter.value = "needle"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == full_query


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


async def test_modal_ctrl_c_clears_filter_then_closes() -> None:
    """Ctrl-C clears a non-empty filter; a second Ctrl-C on empty closes the modal."""
    app = _HistoryHostApp(_entries())
    async with app.run_test() as pilot:
        await pilot.pause()
        for char in "tmux":
            await pilot.press(char)
        await pilot.pause()
        filter_input = app.screen.query_one("#history-filter", Input)
        assert filter_input.value == "tmux"
        # First Ctrl-C clears the filter and repaints the full list; still open.
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert filter_input.value == ""
        assert app.result == "UNSET"
        assert app.screen.query_one("#history-list", OptionList).option_count == 2
        # Second Ctrl-C on the empty filter closes the modal.
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app.result is None


async def test_modal_seed_filters_on_open() -> None:
    """Opening with a seed pre-fills the filter and narrows immediately."""
    app = _HistoryHostApp(_entries(), seed="tmux")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        option_list = app.screen.query_one("#history-list", OptionList)
        assert option_list.option_count == 1
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "tmux pane capture"


class SeededOpenCase(t.NamedTuple):
    """A modal open and the single ``_refilter`` query it should trigger."""

    test_id: str
    seed: str
    expected_calls: list[str]


SEEDED_OPEN_CASES = (
    SeededOpenCase(test_id="seeded", seed="tmux", expected_calls=["tmux"]),
    SeededOpenCase(test_id="unseeded", seed="", expected_calls=[""]),
)


@pytest.mark.parametrize("case", SEEDED_OPEN_CASES, ids=[c.test_id for c in SEEDED_OPEN_CASES])
async def test_modal_filters_once_on_open(
    case: SeededOpenCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening scores exactly one immutable seed snapshot."""
    calls: list[str] = []
    original = history_module._score_snapshot

    def spy(snapshot: history_module._FilterSnapshot) -> tuple[history_module._FilterRow, ...]:
        calls.append(snapshot.query)
        return original(snapshot)

    monkeypatch.setattr(history_module, "_score_snapshot", spy)
    app = _HistoryHostApp(_entries(), seed=case.seed)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert calls == case.expected_calls


async def test_modal_adds_matching_rows_in_one_bulk_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full history rebuild uses one mounted ``OptionList`` update."""
    calls: list[int] = []
    original = OptionList.add_options

    def spy(self: OptionList, new_options: t.Iterable[t.Any]) -> OptionList:
        options = tuple(new_options)
        if self.id == "history-list" and options:
            calls.append(len(options))
        return original(self, options)

    monkeypatch.setattr(OptionList, "add_options", spy)
    entries = [HistoryEntry(text=f"query {index}", ts=index) for index in range(200)]
    app = _HistoryHostApp(entries)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert calls == [len(entries)]
