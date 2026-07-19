"""Functional tests for the legacy ``agentgrep`` Textual surface."""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
import threading
import time
import typing as t

import pytest

import agentgrep as _agentgrep_module
from agentgrep._engine import orchestration
from agentgrep.records import RecordOrigin
from tests._agentgrep_tui_support import (
    _build_empty_ui_app,
    _seed_records,
    _static_content,
    _ui_record,
    load_agentgrep_module,
)

pytestmark = pytest.mark.tui


async def test_streaming_ui_app_mounts_cleanly(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot the Textual app via ``Pilot`` to surface CSS / mount errors in CI.

    Also asserts the results widget is in the screen's focus chain — the
    Textual API requires ``can_focus=True`` as a class keyword (not a class
    attribute), and that detail is easy to get wrong on a dynamic-base
    subclass.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    # Wide enough for the side-by-side layout — below the split breakpoint
    # the detail pane collapses (display: none) and leaves the focus chain.
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        # Leave the pre-search bare canvas so the body panes are mounted/visible
        # (they are hidden until a search runs).
        app.screen._set_empty_state(empty=False)
        await pilot.pause()
        focus_chain_ids = {getattr(w, "id", None) for w in app.screen.focus_chain}
        assert "results" in focus_chain_ids, f"#results not in focus chain; chain={focus_chain_ids}"
        # Both inputs and the detail pane should be focusable too.
        assert {"search", "filter", "detail-scroll"}.issubset(focus_chain_ids)


@pytest.mark.slow
async def test_streaming_ui_app_wires_inline_completion(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The search and filter inputs carry working inline-completion suggesters."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        filter_input = app.screen.query_one("#filter")
        assert search.suggester is not None
        assert filter_input.suggester is not None
        # The query suggester completes a bare field-name prefix.
        suggestion = await search.suggester.get_suggestion("age")
        assert suggestion == "agent:"


@pytest.mark.slow
async def test_streaming_ui_filter_and_results_rules_match_their_contents(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rules above the filter and result list name the content below them."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        filter_input = app.screen.query_one("#filter")
        filter_headers = list(app.screen.query("#filter-header"))
        results_headers = list(app.screen.query("#results-header"))

        assert len(filter_headers) == 1
        assert len(results_headers) == 1
        assert filter_headers[0].render().plain.startswith("─filter")
        assert results_headers[0].render().plain.startswith("─results")
        # The dedicated rules own both labels; the input itself stays bare.
        assert not filter_input.border_title
        assert not filter_input.border_subtitle
        # The prompt itself has no border label.
        assert not search.border_title


@pytest.mark.slow
async def test_streaming_ui_search_rule_state_classes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The search rule reflects search state as a single ``-`` class on ``#search``.

    Mirrors pi's dynamic editor border: idle (no class), searching, and each
    finished outcome map to mutually-exclusive classes recolored in TCSS.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")

        app.screen._set_search_rule_state("searching")
        assert search.has_class("-searching")

        # Outcomes are mutually exclusive — the prior class is cleared.
        app.screen._set_search_rule_state("complete")
        assert search.has_class("-done")
        assert not search.has_class("-searching")

        app.screen._set_search_rule_state("interrupted")
        assert search.has_class("-stopped")
        assert not search.has_class("-done")

        # Empty state returns the rule to idle (no state class).
        app.screen._set_search_rule_state("")
        assert not any(search.has_class(c) for c in ("-searching", "-done", "-stopped", "-error"))


@pytest.mark.slow
async def test_streaming_ui_centered_panel_until_first_result(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The centered searching panel owns the canvas until the first result.

    The hybrid lifecycle: while a search runs with no results yet the body
    carries ``-searching`` and the centered ``#searching-panel`` is shown; the
    first record batch swaps to the results list and clears ``-searching``.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        body = app.screen.query_one("#body")
        panel = app.screen.query_one("#searching-panel")

        # Enter the searching view (no results yet): the centered panel shows.
        app.screen._set_results_view("searching")
        await pilot.pause()
        assert body.has_class("-searching")
        assert panel.display

        # The first batch of results collapses to the list view.
        record = _agentgrep_module.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.history",
            adapter_id="codex.history_jsonl.v1",
            path=tmp_path / "history.jsonl",
            text="tmux pane",
            title="tmux pane",
        )
        await app.screen._apply_records_batch((record,), 1)
        await pilot.pause()
        assert not body.has_class("-searching")


@pytest.mark.slow
async def test_streaming_ui_zero_result_search_freezes_centered_panel(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A search that finds nothing keeps the centered panel and freezes it.

    With no results to collapse into, the finished search stays on the
    centered panel and freezes it into its terminal ``No matches`` state
    rather than revealing an empty results list.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        body = app.screen.query_one("#body")
        app.screen._set_results_view("searching")
        await pilot.pause()

        app.screen._apply_finished("complete", 0, 1.2, None)
        await pilot.pause()
        assert body.has_class("-searching")
        panel = app.screen.query_one("#searching-panel")
        assert "No matches" in panel.render().plain


class HistoryWriteOffloadCase(t.NamedTuple):
    """Search-history append that must run outside the pump thread."""

    test_id: str
    text: str


HISTORY_WRITE_OFFLOAD_CASES = (HistoryWriteOffloadCase(test_id="plain-query", text="tmux"),)


async def _wait_for_history_text(home: pathlib.Path, text: str) -> None:
    """Wait until ``text`` is visible in the persisted search history."""
    from agentgrep.ui import _history

    path = _history.history_path(home)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        entries = await asyncio.to_thread(_history.load_history, path)
        if any(entry.text == text for entry in entries):
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"history entry was not persisted: {text!r}")


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    HISTORY_WRITE_OFFLOAD_CASES,
    ids=[case.test_id for case in HISTORY_WRITE_OFFLOAD_CASES],
)
async def test_search_history_append_runs_off_pump(
    case: HistoryWriteOffloadCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """History persistence uses a worker instead of blocking the pump."""
    from agentgrep.ui import _history, _runtime

    appended = threading.Event()
    calls: list[tuple[pathlib.Path, str, str]] = []

    def append_query(
        path: pathlib.Path,
        text: str,
        *,
        scope: str = "",
        now: float | None = None,
        dedup_last: str = "",
    ) -> bool:
        _runtime.assert_off_pump("history append")
        calls.append((path, text, scope))
        appended.set()
        return True

    monkeypatch.setattr(_history, "append_query", append_query)
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._record_history(case.text)
        assert await asyncio.to_thread(appended.wait, 2.0)
        assert calls == [
            (_history.history_path(app.screen.home), case.text, app.screen._user_scope)
        ]
        assert any(entry.text == case.text for entry in app.screen._history)


@pytest.mark.slow
async def test_search_submit_records_history(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submitting a search records the query to history (memory + disk)."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._search_input.focus()
        await pilot.pause()
        for char in "tmux":
            await pilot.press(char)
        await pilot.press("enter")
        await pilot.pause()
        assert any(entry.text == "tmux" for entry in app.screen._history)
        await _wait_for_history_text(app.screen.home, "tmux")


@pytest.mark.slow
async def test_history_opt_out_records_nothing(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``AGENTGREP_NO_HISTORY`` set, a submitted search is not recorded."""
    from agentgrep.ui import _history

    monkeypatch.setenv("AGENTGREP_NO_HISTORY", "1")
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._search_input.focus()
        await pilot.pause()
        for char in "tmux":
            await pilot.press(char)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert app.screen._history == []
        assert not _history.history_path(app.screen.home).exists()


@pytest.mark.slow
async def test_ctrl_r_opens_history_modal(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-R from the focused search box opens the recall modal."""
    from agentgrep.ui._history import HistoryEntry
    from agentgrep.ui.widgets.history import HistoryRecall

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._history = [HistoryEntry(text="agent:codex refactor", ts=10)]
        app.screen._search_input.focus()
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, HistoryRecall)


def _search_requested(text: str) -> object:
    """Build a ``SearchRequested`` message carrying ``text`` (Enter-submit stand-in)."""
    from agentgrep.progress import SearchRequestedPayload
    from agentgrep.ui.widgets import SearchRequested

    return SearchRequested(payload=SearchRequestedPayload(text=text))


class PassiveSlashCommandCase(t.NamedTuple):
    """Passive slash command that must not interrupt an active search."""

    test_id: str
    text: str


PASSIVE_SLASH_COMMAND_CASES = (PassiveSlashCommandCase(test_id="help", text="/help"),)


class LiteralSlashSearchCase(t.NamedTuple):
    """Leading-slash text that should remain a normal search."""

    test_id: str
    text: str


LITERAL_SLASH_SEARCH_CASES = (
    LiteralSlashSearchCase(test_id="absolute-path", text="/usr/local/bin"),
    LiteralSlashSearchCase(test_id="unknown-token", text="/foo"),
    LiteralSlashSearchCase(test_id="command-plus-args", text="/help find prompts"),
)


class EnterCommandCase(t.NamedTuple):
    """A partial slash input and the command Enter should run from the menu."""

    test_id: str
    typed: str
    expected: str


ENTER_COMMAND_CASES = (
    EnterCommandCase(test_id="clear-prefix", typed="/c", expected="clear"),
    EnterCommandCase(test_id="exit-prefix", typed="/e", expected="exit"),
    EnterCommandCase(test_id="help-prefix", typed="/h", expected="help"),
    EnterCommandCase(test_id="keys-prefix", typed="/k", expected="keys"),
    EnterCommandCase(test_id="theme-prefix", typed="/t", expected="theme"),
    EnterCommandCase(test_id="alias-prefix", typed="/re", expected="clear"),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    ENTER_COMMAND_CASES,
    ids=[case.test_id for case in ENTER_COMMAND_CASES],
)
async def test_enter_runs_highlighted_slash_command(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: EnterCommandCase,
) -> None:
    """Enter on a partial command runs the highlighted command, not the literal text."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        ran: list[str] = []
        monkeypatch.setattr(
            app.screen,
            "_run_command_at",
            lambda index: ran.append(app.screen._command_matches[index].name),
        )
        app.screen._search_input.focus()
        await pilot.pause()
        for char in case.typed:
            await pilot.press(char)
        await pilot.pause()
        assert app.screen._enum_dropdown.display is True
        await pilot.press("enter")
        await pilot.pause()
        assert ran == [case.expected]


class CommandPlusArgsCase(t.NamedTuple):
    """A command token followed by args that must run a literal search."""

    test_id: str
    typed: str


COMMAND_PLUS_ARGS_CASES = (
    CommandPlusArgsCase(test_id="help-with-args", typed="/help find prompts"),
    CommandPlusArgsCase(test_id="clear-with-args", typed="/clear stale"),
    CommandPlusArgsCase(test_id="alias-with-args", typed="/quit now"),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    COMMAND_PLUS_ARGS_CASES,
    ids=[case.test_id for case in COMMAND_PLUS_ARGS_CASES],
)
async def test_command_with_args_runs_literal_search(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: CommandPlusArgsCase,
) -> None:
    """A command token plus args is literal text — Enter searches, not runs it."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        ran: list[str] = []
        searches: list[object] = []
        monkeypatch.setattr(
            app.screen,
            "_run_command_at",
            lambda index: ran.append(app.screen._command_matches[index].name),
        )
        monkeypatch.setattr(app.screen, "_start_search_worker", searches.append)
        app.screen._search_input.focus()
        await pilot.pause()
        app.screen._search_input.value = case.typed
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert ran == []
        assert len(searches) == 1
        assert app.screen._enum_dropdown.display is False


@pytest.mark.slow
async def test_slash_opens_and_filters_command_menu(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typing ``/`` opens the command menu; typing more prefix-filters it."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._search_input.focus()
        await pilot.pause()
        await pilot.press("/")
        await pilot.pause()
        dropdown = app.screen._enum_dropdown
        assert dropdown.display is True
        assert {cmd.name for cmd in app.screen._command_matches} == {
            "clear",
            "exit",
            "help",
            "keys",
            "maximize",
            "minimize",
            "screenshot",
            "theme",
        }
        assert dropdown.option_count == len(app.screen._command_matches)
        await pilot.press("c")  # value is now "/c"
        await pilot.pause()
        assert [cmd.name for cmd in app.screen._command_matches] == ["clear"]
        assert dropdown.option_count == 1


@pytest.mark.slow
async def test_slash_menu_selection_uses_canonical_text_dispatch(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A menu choice re-enters the shared exact-command dispatcher by text."""
    from agentgrep.ui import commands

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        clear = commands.resolve_command("clear")
        assert clear is not None
        dispatched: list[str] = []
        monkeypatch.setattr(app.screen, "_dispatch_slash_text", dispatched.append)
        app.screen._command_matches = (clear,)

        app.screen._run_command_at(0)

        assert dispatched == ["/clear"]


@pytest.mark.slow
async def test_slash_clear_resets_and_is_not_recorded(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/clear`` returns the explorer to the bare canvas and is not recorded."""
    from agentgrep.ui import _history

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        # A real search first: records history, leaves a non-empty state.
        app.screen._search_input.focus()
        await pilot.pause()
        for char in "tmux":
            await pilot.press(char)
        await pilot.press("enter")
        await pilot.pause(0.1)
        # Now dispatch /clear.
        app.screen._search_input.value = "/clear"
        app.screen.on_search_requested(_search_requested("/clear"))
        await pilot.pause()
        assert app.screen.query_one("#body").has_class("-empty")
        assert app.screen._search_input.value == ""
        on_disk = _history.load_history(_history.history_path(app.screen.home))
        assert all(entry.text != "/clear" for entry in on_disk)
        assert any(entry.text == "tmux" for entry in on_disk)


@pytest.mark.slow
async def test_slash_menu_clear_cancels_active_search(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting ``clear`` from the slash menu signals the old search control."""
    from agentgrep.ui import commands

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app.screen, "run_worker", lambda *a, **kw: None)
        app.screen._search_input.focus()
        await pilot.pause()
        app.screen._search_input.value = "tmux"
        await pilot.press("enter")
        await pilot.pause(0.1)
        first_control = app.screen.control
        clear = commands.resolve_command("clear")
        assert clear is not None
        app.screen._command_matches = (clear,)
        app.screen._run_command_at(0)
        await pilot.pause()
        assert first_control.answer_now_requested() is True
        assert app.screen.control is not first_control
        assert app.screen.control.answer_now_requested() is False


@pytest.mark.slow
async def test_slash_help_notifies_the_command_list(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/help`` shows the registry as a notification."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        notes: list[tuple[tuple[object, ...], dict[str, object]]] = []
        monkeypatch.setattr(app.screen, "notify", lambda *a, **k: notes.append((a, k)))
        app.screen.on_search_requested(_search_requested("/help"))
        await pilot.pause()
        assert len(notes) == 1
        message = str(notes[0][0][0])
        assert "/clear" in message
        assert "/help" in message


@pytest.mark.slow
async def test_slash_exit_quits_the_app(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/exit`` (and its ``/quit`` alias) quits the app."""
    for text in ("/exit", "/quit"):
        app = _build_empty_ui_app(tmp_path, monkeypatch)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            exits: list[object] = []
            monkeypatch.setattr(app, "exit", lambda *a, _sink=exits, **k: _sink.append((a, k)))
            app.screen.on_search_requested(_search_requested(text))
            await pilot.pause()
            assert len(exits) == 1, f"{text} should quit"


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    LITERAL_SLASH_SEARCH_CASES,
    ids=[case.test_id for case in LITERAL_SLASH_SEARCH_CASES],
)
async def test_literal_leading_slash_text_runs_search(
    case: LiteralSlashSearchCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leading slash is a command only for exact registered command tokens."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        spawned: list[tuple[tuple[object, ...], dict[str, object]]] = []
        notes: list[tuple[tuple[object, ...], dict[str, object]]] = []
        monkeypatch.setattr(
            app.screen,
            "run_worker",
            lambda *a, **k: spawned.append((a, k)),
        )
        monkeypatch.setattr(app.screen, "notify", lambda *a, **k: notes.append((a, k)))
        app.screen.on_search_requested(_search_requested(case.text))
        await pilot.pause()
        search_workers = [kwargs for _, kwargs in spawned if kwargs.get("name") == "search"]
        assert len(search_workers) == 1
        assert notes == []
        assert not app.screen._search_input.has_class("-error")
        assert app.screen.search_query.terms == tuple(case.text.split())


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    PASSIVE_SLASH_COMMAND_CASES,
    ids=[case.test_id for case in PASSIVE_SLASH_COMMAND_CASES],
)
async def test_passive_slash_command_preserves_active_search(
    case: PassiveSlashCommandCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passive commands do not cancel or replace an active search."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    spawned: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_worker(*args: object, **kwargs: object) -> None:
        spawned.append((args, kwargs))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app.screen, "run_worker", fake_worker)
        notes: list[tuple[tuple[object, ...], dict[str, object]]] = []
        monkeypatch.setattr(app.screen, "notify", lambda *a, **k: notes.append((a, k)))
        app.screen._search_input.focus()
        await pilot.pause()
        app.screen._search_input.value = "tmux"
        await pilot.press("enter")
        await pilot.pause(0.1)
        first_control = app.screen.control
        assert first_control.answer_now_requested() is False
        search_workers = [kwargs for _, kwargs in spawned if kwargs.get("name") == "search"]
        assert len(search_workers) == 1
        app.screen.on_search_requested(_search_requested(case.text))
        await pilot.pause()
        assert app.screen.control is first_control
        assert first_control.answer_now_requested() is False
        search_workers = [kwargs for _, kwargs in spawned if kwargs.get("name") == "search"]
        assert len(search_workers) == 1
        assert len(notes) == 1


@pytest.mark.slow
async def test_slash_menu_pushes_body_down_and_reflows_back(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command menu is in-flow (``overlay: none``): it pushes the body down.

    Unlike the keyword picker (which floats via ``overlay: screen``), the slash
    menu takes real layout height, reflowing the content below it — the pi/ink
    way — and the body returns when the slash is cleared.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        await pilot.pause()
        body = app.screen.query_one("#body")
        app.screen._search_input.focus()
        await pilot.pause()
        y_closed = body.region.y
        await pilot.press("/")
        await pilot.pause()
        assert app.screen._enum_dropdown.styles.overlay == "none"
        assert body.region.y > y_closed
        # Clearing the slash collapses the menu and reflows the body back.
        await pilot.press("backspace")
        await pilot.pause()
        assert body.region.y == y_closed


@pytest.mark.slow
async def test_history_modal_background_is_transparent(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recall modal screen is transparent, so the explorer shows through.

    The dialog's own chrome (border + fill) stays crisp, but the screen around
    it is transparent (``a == 0``) — Textual renders the explorer below via
    ``app.render``, giving full context behind the modal by preference.
    """
    from agentgrep.ui._history import HistoryEntry

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._history = [HistoryEntry(text="agent:codex refactor", ts=10)]
        app.screen.action_recall_history()
        await pilot.pause()
        assert app.screen.styles.background.a == 0
        # Close the modal so the screen stack is clean at teardown.
        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.slow
async def test_ctrl_c_in_history_modal_does_not_quit_app(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C inside the modal clears then closes it — it never quits the app."""
    from textual.widgets import Input

    from agentgrep.ui._history import HistoryEntry
    from agentgrep.ui.widgets.history import HistoryRecall

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._history = [HistoryEntry(text="agent:codex refactor", ts=10)]
        app.screen.action_recall_history()
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, HistoryRecall)
        modal.query_one("#history-filter", Input).value = "zzz"
        await pilot.pause()
        # First Ctrl-C clears (does not quit the app via smart_quit).
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert modal.query_one("#history-filter", Input).value == ""
        assert isinstance(app.screen, HistoryRecall)
        # Second Ctrl-C closes the modal back to the explorer — app still alive.
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not isinstance(app.screen, HistoryRecall)


@pytest.mark.slow
async def test_apply_recalled_query_fills_box_without_running(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choosing a history entry fills the search box but does not auto-run it."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._apply_recalled_query("agent:codex refactor")
        await pilot.pause()
        assert app.screen._search_input.value == "agent:codex refactor"
        # Filling the box is not a submit — no results were loaded.
        assert app.screen.all_records == []


@pytest.mark.slow
@pytest.mark.parametrize("input_id", ["search", "filter", "detail-find"])
@pytest.mark.parametrize(
    ("key", "cursor", "expected"),
    [("shift+backspace", 3, "ab"), ("shift+delete", 1, "ac")],
)
async def test_shift_delete_aliases_edit_all_inputs(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    input_id: str,
    key: str,
    cursor: int,
    expected: str,
) -> None:
    """Shift-modified delete keys retain their ordinary editing behavior."""
    from textual.widgets import Input

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        if input_id == "detail-find":
            app.screen.show_detail(
                _agentgrep_module.SearchRecord(
                    kind="prompt",
                    agent="codex",
                    store="codex.sessions",
                    adapter_id="codex.sessions_jsonl.v1",
                    path=tmp_path / "a.jsonl",
                    text="detail",
                ),
            )
            app.screen.action_open_detail_find()
            await pilot.pause()
        input_widget = app.screen.query_one(f"#{input_id}", Input)
        input_widget.value = "abc"
        input_widget.cursor_position = cursor
        input_widget.focus()
        await pilot.press(key)
        assert input_widget.value == expected


@pytest.mark.slow
async def test_streaming_ui_result_row_title_not_always_bold(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows bake no always-on bold; weight is a selection signal applied via CSS.

    pi reserves bold for the selected line, so the row builder leaves every
    span at regular weight and the highlighted-row CSS supplies the emphasis.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        results = app.screen.query_one("#results")
        record = _agentgrep_module.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.history",
            adapter_id="codex.history_jsonl.v1",
            path=tmp_path / "history.jsonl",
            text="error handling",
            title="error handling notes",
        )
        rendered = results._render_record(record)
        assert all("bold" not in str(span.style) for span in rendered.spans)


@pytest.mark.slow
async def test_pane_headers_left_label_embedded_in_rule(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pane headers embed a left label in a width-filling rule."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        await pilot.pause()
        header = app.screen.query_one("#results-header")
        header.set_right("")
        plain = header.render().plain
        assert plain.startswith("─results")  # a rule cell sits before the label
        assert plain.endswith("─")  # rule fills to the edge — no trailing margin
        assert "  " not in plain  # no confusing double-space gap


@pytest.mark.slow
async def test_results_header_right_slot_stays_anchored(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Growing position/percentage digits repaint inside one fixed-width slot."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        await pilot.pause()
        header = app.screen._results_header

        rendered: list[str] = []
        for status in (" 1/40    9%", "10/40   10%", "40/40  100%"):
            header.set_right(status)
            rendered.append(header.render().plain)

        assert {len(line) for line in rendered} == {header.size.width}
        assert len({line.index("/") for line in rendered}) == 1
        assert all(line.endswith("─") for line in rendered)


def test_update_pane_focus_without_active_screen_is_safe(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pane-focus recolor must no-op when no screen is on the stack.

    On teardown a descendant blur fires ``on_descendant_blur`` ->
    ``_update_pane_focus``; once the screen stack is empty ``self.focused``
    raises ``ScreenStackError``, so the handler must guard against it. An
    un-run app reproduces the empty-stack state deterministically.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    app.get_default_screen()._update_pane_focus()  # must not raise (unmounted layout)


@pytest.mark.slow
async def test_streaming_ui_app_enum_dropdown_opens_and_closes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typing an enum field predicate opens the value dropdown; other text hides it."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        dropdown = app.screen.query_one("#enum-dropdown")

        # An enum field token opens the dropdown with one option per value.
        search.value = "scope:"
        await pilot.pause()
        assert dropdown.display is True
        assert dropdown.option_count == 3  # prompts, conversations, all

        # A partial filters the values.
        search.value = "agent:cu"
        await pilot.pause()
        assert dropdown.display is True
        assert dropdown.option_count == 2  # cursor-cli, cursor-ide

        # The dropdown tracks the input cursor: a long prefix pushes it right.
        search.value = "ruff codex review notes scope:"
        search.cursor_position = len(search.value)
        await pilot.pause()
        assert dropdown.display is True
        # Left edge is anchored near the cursor column, not pinned at 0.
        cursor_x = search.cursor_screen_offset.x
        assert abs(dropdown.region.x - (cursor_x - 1)) <= 1
        assert dropdown.region.x > 10

        # Non-enum / bare text hides it.
        search.value = "ruff"
        await pilot.pause()
        assert dropdown.display is False


@pytest.mark.slow
async def test_streaming_ui_app_filter_dropdown_and_query_aware(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filter box gets a keyword dropdown and a query-aware matcher."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        filter_input = app.screen.query_one("#filter")
        dropdown = app.screen.query_one("#filter-dropdown")

        # A bare token lists field-name keywords (no record vocabulary).
        filter_input.value = "agent"
        filter_input.cursor_position = len("agent")
        await pilot.pause()
        assert dropdown.display is True
        assert app.screen._filter_dropdown_values[0] == "agent:"

        # A field token lists the enum values.
        filter_input.value = "scope:"
        filter_input.cursor_position = len("scope:")
        await pilot.pause()
        assert app.screen._filter_dropdown_values == ("prompts", "conversations", "all")

        # The filter executes the query language: a predicate compiles to a
        # matcher; empty/whitespace yields no matcher (all records pass).
        assert app.screen._build_filter_matcher("agent:codex") is not None
        assert app.screen._build_filter_matcher("   ") is None

        # A free-text term that isn't a keyword shows no dropdown.
        filter_input.value = "zzznomatch"
        filter_input.cursor_position = len("zzznomatch")
        await pilot.pause()
        assert dropdown.display is False


@pytest.mark.slow
async def test_dropdown_accept_leaves_cursor_at_end_without_selecting(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepting a dropdown choice places the cursor at the end, not select-all."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        search.value = "agent:co"
        search.cursor_position = len("agent:co")
        await pilot.pause()
        assert app.screen._enum_values == ("codex",)

        app.screen._accept_dropdown_choice(
            search, app.screen._enum_dropdown, app.screen._enum_values, 0
        )
        await pilot.pause()

        assert search.value == "agent:codex"
        assert search.cursor_position == len("agent:codex")
        assert search.selection.is_empty


def test_dropdown_accept_uses_validated_value_length(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completion places the cursor after any input-side value clamping."""

    class ClampedInput:
        def __init__(self) -> None:
            self._value = "a"
            self.cursor_position = 0
            self.focused = False

        @property
        def value(self) -> str:
            return self._value

        @value.setter
        def value(self, value: str) -> None:
            self._value = value[:5]

        def focus(self) -> None:
            self.focused = True

    class Dropdown:
        display = True

    target = ClampedInput()
    dropdown = Dropdown()
    hud = _build_empty_ui_app(tmp_path, monkeypatch).get_default_screen()

    hud._accept_dropdown_choice(target, dropdown, ("abcdef",), 0)

    assert target.value == "abcde"
    assert target.cursor_position == len(target.value)
    assert dropdown.display is False
    assert target.focused is True


@pytest.mark.slow
async def test_detail_pane_highlights_filter_terms_distinctly(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filter terms are highlighted in the detail body in a distinct style."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._filter_terms = ("mobx",)
        body = "use biome and mobx here"
        renderable, _ = app.screen._build_detail_body(body, ("biome",))

        spans = [(s.start, s.end, str(s.style)) for s in renderable.spans]
        biome = body.index("biome")
        mobx = body.index("mobx")
        # Search and filter terms get distinct, theme-aware styles: the search
        # term carries the gold foreground token, the filter term the accent
        # background token.
        search_hex = app.theme_variables["ag-match-search"]
        filter_bg_hex = app.theme_variables["ag-match-filter-bg"]
        assert any(
            s == biome and e == biome + len("biome") and search_hex in style
            for s, e, style in spans
        )
        assert any(
            s == mobx and e == mobx + len("mobx") and filter_bg_hex in style
            for s, e, style in spans
        )


@pytest.mark.slow
async def test_large_detail_body_builds_off_thread(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large, uncached detail body is built by a worker, not on the UI thread."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        big = "x" * (app.screen._DETAIL_ASYNC_BODY_THRESHOLD + 1000)
        record = _ui_record(agentgrep, tmp_path / "big.jsonl", big, "big")
        terms = list(app.screen.search_query.terms)

        app.screen.show_detail(record)
        # show_detail returns immediately; the heavy body is deferred.
        assert not app.screen._detail_body_is_cached(terms)

        await app.workers.wait_for_complete()
        await pilot.pause()

        # The worker built and applied the body off the UI thread.
        assert app.screen._detail_body_is_cached(terms)
        assert len(list(_static_content(app.screen._detail).renderables)) == 2


@pytest.mark.slow
async def test_large_detail_body_resolves_match_styles_on_pump(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large detail worker uses styles resolved on the pump thread."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        pump_thread_id = threading.get_ident()
        style_threads: list[int] = []

        def record_style(kind: str) -> str:
            style_threads.append(threading.get_ident())
            return "bold yellow" if kind == "search" else "bold black on cyan"

        monkeypatch.setattr(app.screen, "_match_style", record_style)
        app.screen._filter_terms = ("needle",)
        big = "needle " + ("x" * (app.screen._DETAIL_ASYNC_BODY_THRESHOLD + 1000))
        record = _ui_record(agentgrep, tmp_path / "big.jsonl", big, "big")

        app.screen.show_detail(record)
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert style_threads
        assert set(style_threads) == {pump_thread_id}


@pytest.mark.slow
async def test_present_detail_discards_superseded_record(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished build whose record the cursor has left is not rendered."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        current = _ui_record(agentgrep, tmp_path / "cur.jsonl", "current body", "cur")
        stale = _ui_record(agentgrep, tmp_path / "old.jsonl", "stale body", "old")
        app.screen._current_detail_record = current
        updates: list[object] = []
        monkeypatch.setattr(app.screen._detail, "update", updates.append)

        app.screen._present_detail(
            stale, "HEADER", app.screen._build_detail_body("stale body", ()), ()
        )
        assert updates == []  # superseded record is dropped

        app.screen._present_detail(
            current, "HEADER", app.screen._build_detail_body("current body", ()), ()
        )
        assert len(updates) == 1  # current record is rendered


@pytest.mark.slow
async def test_present_detail_rejects_stale_same_record_generation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older same-record worker cannot overwrite a newer detail build."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        record = _ui_record(agentgrep, tmp_path / "same.jsonl", "body", "same")
        app.screen._current_detail_record = record
        app.screen._detail_build_generation = 2
        cache_key = app.screen._detail_cache_key((), record)
        updates: list[object] = []
        monkeypatch.setattr(app.screen._detail, "update", updates.append)

        app.screen._present_detail(
            record,
            "OLD",
            (object(), "old body"),
            (),
            generation=1,
            cache_key=cache_key,
        )
        assert updates == []
        assert cache_key not in app.screen._detail_body_cache

        app.screen._present_detail(
            record,
            "NEW",
            (object(), "new body"),
            (),
            generation=2,
            cache_key=cache_key,
        )
        assert len(updates) == 1
        assert app.screen._detail_body_cache[cache_key][0] is record


@pytest.mark.slow
async def test_present_detail_retains_captured_highlight_key(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed detail build cannot label stale spans with live filter state."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    source = "before needle after"
    async with app.run_test() as pilot:
        await pilot.pause()
        record = _ui_record(agentgrep, tmp_path / "race.jsonl", source, "race")
        app.screen._current_detail_record = record
        app.screen._filter_terms = ("before",)
        cache_key = app.screen._detail_cache_key((), record)
        assert cache_key is not None
        body = app.screen._build_detail_body(source, (), filter_terms=("before",))

        app.screen._filter_terms = ("after",)
        app.screen._present_detail(record, "HEADER", body, (), cache_key=cache_key)

        assert app.screen._detail_find_base_key is not None
        assert app.screen._detail_find_base_key[-1] == ("before",)
        assert app.screen._detail_find_base_for(source) is not body[0]


@pytest.mark.slow
async def test_detail_body_builder_does_not_mutate_shared_cache(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detached worker computation leaves cache ownership on the pump."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._current_detail_record = _ui_record(
            agentgrep,
            tmp_path / "a.jsonl",
            "body",
            "a",
        )
        app.screen._build_detail_body("body", ())
        assert app.screen._detail_body_cache == {}


@pytest.mark.slow
async def test_expanding_json_detail_is_offloaded_and_bounded(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compact deeply nested JSON never expands on the pump or past the cap."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    nested = "[" * 3000 + "0" + "]" * 3000
    record = _ui_record(agentgrep, tmp_path / "nested.jsonl", nested, "nested")
    async with app.run_test() as pilot:
        await pilot.pause()
        scheduled: list[object] = []

        def capture_worker(worker: object, **_: object) -> None:
            scheduled.append(worker)

        monkeypatch.setattr(app.screen, "run_worker", capture_worker)
        app.screen.show_detail(record)
        assert len(scheduled) == 1

        _renderable, formatted = await asyncio.to_thread(
            app.screen._build_detail_body,
            app.screen._detail_body_text,
            (),
        )
        assert len(formatted) <= agentgrep.DETAIL_BODY_MAX_CHARS + 32


@pytest.mark.slow
async def test_regex_detail_omits_untrusted_pattern_highlighting(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regex search semantics never run Python pattern matching in detail chrome."""
    from agentgrep.ui.layouts import hud as hud_module

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    pattern = "(a+)+$"
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.search_query = dataclasses.replace(
            app.screen.search_query,
            terms=(pattern,),
            regex=True,
        )
        renderable, _source = app.screen._build_detail_body("a" * 23 + "!", (pattern,))
        assert isinstance(renderable, hud_module.Text)
        assert renderable.spans == []


@pytest.mark.slow
async def test_detail_highlight_spans_have_fixed_budget(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated literal matches cannot create an unbounded Rich span list."""
    from agentgrep.ui import _streaming
    from agentgrep.ui.layouts import hud as hud_module

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderable, _source = app.screen._build_detail_body("a" * 19_000, ("a",))
        assert isinstance(renderable, hud_module.Text)
        assert len(renderable.spans) == _streaming._DETAIL_HIGHLIGHT_MAX_MATCHES
        assert (
            _streaming._bounded_literal_terms(
                ("x" * (_streaming._DETAIL_HIGHLIGHT_MAX_TERM_CHARS + 1),),
                case_sensitive=False,
            )
            == ()
        )


@pytest.mark.slow
async def test_large_markdown_detail_uses_plain_text_rendering(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long Markdown avoids Rich's lazy syntax work on the message pump."""
    from agentgrep.ui.layouts import hud as hud_module

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    body = "# Heading\n\n" + "x" * (2049 - len("# Heading\n\n"))
    assert len(body) == 2049
    async with app.run_test() as pilot:
        await pilot.pause()
        renderable, source = app.screen._build_detail_body(body, ())
        assert isinstance(renderable, hud_module.Text)
        assert source == body


@pytest.mark.slow
async def test_dropdown_dismissal_keys_close_without_accepting(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Esc, Enter, and Ctrl+C dismiss an open dropdown without auto-accepting."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        dropdown = app.screen.query_one("#enum-dropdown")
        search.focus()
        await pilot.pause()

        # Each block uses a distinct value so the reactive fires Changed and
        # the dropdown reopens.
        #
        # Esc dismisses and keeps focus in the input (still editing).
        search.value = "agent:"
        search.cursor_position = len(search.value)
        await pilot.pause()
        assert dropdown.display is True
        await pilot.press("escape")
        await pilot.pause()
        assert dropdown.display is False
        assert app.focused is search

        # Enter closes the dropdown without accepting a value.
        search.value = "scope:"
        search.cursor_position = len(search.value)
        await pilot.pause()
        assert dropdown.display is True
        await pilot.press("enter")
        await pilot.pause()
        assert dropdown.display is False
        assert search.value == "scope:"

        # Ctrl+C dismisses the dropdown instead of quitting the app.
        search.value = "agent:cu"
        search.cursor_position = len(search.value)
        await pilot.pause()
        assert dropdown.display is True
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert dropdown.display is False
        # The app is still running (Ctrl+C was consumed by the dropdown).
        assert app.screen.query_one("#search") is search


@pytest.mark.slow
async def test_empty_query_focuses_search_input_and_marks_search_done(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no initial query, the search bar takes focus and chrome is idle."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "search"
        assert app.screen._search_done is True


@pytest.mark.slow
async def test_search_and_filter_inputs_carry_query_highlighter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both query inputs are wired with the Rich query-syntax highlighter."""
    from agentgrep.ui.highlighter import QueryHighlighter

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        filter_input = app.screen.query_one("#filter")
        assert isinstance(search.highlighter, QueryHighlighter)
        assert isinstance(filter_input.highlighter, QueryHighlighter)


def test_scope_predicate_widening_does_not_persist(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``scope:`` predicate widens scope for its own search only; bare queries revert.

    Regression: after ``scope:conversations bliss`` widened discovery to "all"
    and that query became ``self.query``, a follow-up ``bliss`` (no ``scope:``)
    used to inherit the widened "all" and keep scanning conversations.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    hud = app.get_default_screen()
    assert hud.search_query.scope == "prompts"

    widened = hud._build_search_query("scope:conversations bliss")
    assert widened.scope == "all"
    hud.search_query = widened

    reverted = hud._build_search_query("bliss")
    assert reverted.scope == "prompts"
    assert hud._build_search_query("scope:").scope == "prompts"
    assert hud._build_search_query("agent:").scope == "prompts"


@pytest.mark.parametrize("layout", ["hud", "greplog"])
def test_launch_scope_predicate_preserves_base_scope(
    layout: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compiled launch predicate cannot become the layout's plain-query scope."""
    from agentgrep.query import build_query_from_input, default_registry

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    base = agentgrep.SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    result = build_query_from_input(
        "scope:conversations bliss",
        base,
        default_registry(),
    )
    assert result.query is not None
    assert result.query.scope == "all"
    app = agentgrep.build_streaming_ui_app(
        home,
        result.query,
        control=agentgrep.SearchControl(),
        initial_search_text="scope:conversations bliss",
        base_scope="prompts",
        layout=layout,
    )
    screen = app.get_default_screen()

    assert screen.build_query("plain").scope == "prompts"


@pytest.mark.parametrize("layout", ["hud", "greplog"])
def test_malformed_ui_query_preserves_launch_invariants(
    layout: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Literal fallback keeps overlay fields and drops stale compiled syntax."""
    from agentgrep.query import compile_query, default_registry, parse_query

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    registry = default_registry()
    launch = agentgrep.SearchQuery(
        terms=("launch",),
        scope="prompts",
        any_term=True,
        regex=True,
        case_sensitive=True,
        agents=("claude",),
        limit=7,
        dedupe=False,
        compiled=compile_query(parse_query("agent:codex", registry), registry),
        match_surface="text",
        origin_filter=RecordOrigin(repo="example/repo"),
    )
    app = agentgrep.build_streaming_ui_app(
        home,
        launch,
        control=agentgrep.SearchControl(),
        initial_search_text="agent:codex",
        base_scope="prompts",
        layout=layout,
    )

    fallback = app.get_default_screen().build_query("agent:")

    assert fallback == dataclasses.replace(
        launch,
        terms=("agent:",),
        compiled=None,
    )


def test_streaming_ui_app_passes_runtime_to_search_worker(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TUI owns one runtime and passes it to backend searches."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    runtimes: list[object] = []

    def record_runtime(*_args: object, **kwargs: object) -> list[object]:
        runtimes.append(kwargs.get("runtime"))
        return []

    # ``run_search_query`` is called from ``EngineSearchInvoker`` (ui/_seams.py).
    monkeypatch.setattr(orchestration, "run_search_query", record_runtime)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    query = agentgrep.SearchQuery(
        terms=("bliss",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    app = agentgrep.build_streaming_ui_app(home, query, control=agentgrep.SearchControl())

    hud = app.get_default_screen()
    hud._search_emit = lambda _event: None
    hud._run_search()
    hud._run_search()

    assert len(runtimes) == 2
    assert isinstance(runtimes[0], agentgrep.SearchRuntime)
    assert runtimes[0] is runtimes[1]
    assert runtimes[0].source_scan_cache is not None


def test_streaming_ui_search_worker_emits_failure(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker exception remains a typed, generation-gated terminal event."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    hud = app.get_default_screen()
    error = RuntimeError("search failed")

    def fail_search(*_args: object, **_kwargs: object) -> t.NoReturn:
        raise error

    monkeypatch.setattr(hud._invoker, "run", fail_search)
    events: list[object] = []
    hud._search_emit = events.append

    hud._run_search()

    assert events == [
        agentgrep.StreamingSearchFinished(
            outcome="error",
            total=0,
            elapsed=0.0,
            error=error,
        ),
    ]


@pytest.mark.slow
async def test_search_input_posts_search_requested_only_on_enter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typing alone posts nothing; pressing Enter posts one ``SearchRequested``.

    The ``SearchRequested`` class lives inside the streaming-app factory
    closure, so the test sniffs every posted message and filters to ones
    whose payload type matches :class:`SearchRequestedPayload`.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    posts: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._search_input.focus()
        await pilot.pause()
        original_post_message = app.screen._search_input.post_message

        def capture(message: object) -> bool:
            payload = getattr(message, "payload", None)
            if isinstance(payload, agentgrep.SearchRequestedPayload):
                posts.append(payload.text)
            return original_post_message(message)

        monkeypatch.setattr(app.screen._search_input, "post_message", capture)
        await pilot.press("b")
        await pilot.press("l")
        await pilot.press("i")
        await pilot.pause(0.4)
        assert posts == [], f"keystrokes should not auto-post; got {posts}"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert posts == ["bli"], f"expected one post on Enter, got {posts}"


@pytest.mark.slow
async def test_search_input_dispatch_spawns_search_group_worker(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing Enter on a non-empty search bar spawns a ``search`` worker."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    spawned: list[dict[str, object]] = []

    def fake_worker(*args: object, **kwargs: object) -> None:
        spawned.append({"args": args, "kwargs": kwargs})

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app.screen, "run_worker", fake_worker)
        app.screen._search_input.focus()
        await pilot.pause()
        app.screen._search_input.value = "bliss"
        await pilot.pause(0.1)
        assert spawned == [], f"value change alone should not spawn; got {spawned}"
        await pilot.press("enter")
        await pilot.pause(0.1)
        groups = [t.cast("dict[str, object]", entry["kwargs"]).get("group") for entry in spawned]
        assert "search" in groups, f"expected a search-group worker, got {spawned}"


@pytest.mark.slow
async def test_search_input_enter_replaces_control_to_cancel_prior_search(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each new search signals the prior control and installs a fresh one.

    The cooperative cancel contract is: the old worker thread keeps its
    (now-signaled) ``SearchControl`` reference and bails out; the new
    worker gets a fresh, un-signaled control.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)

    async with app.run_test() as pilot:
        await pilot.pause()
        # Stub run_worker so the app's worker bookkeeping doesn't fight us.
        monkeypatch.setattr(app.screen, "run_worker", lambda *a, **kw: None)
        app.screen._search_input.focus()
        await pilot.pause()
        app.screen._search_input.value = "first"
        await pilot.press("enter")
        await pilot.pause(0.1)
        first_control = app.screen.control
        assert first_control.answer_now_requested() is False
        app.screen._search_input.value = "second"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert app.screen.control is not first_control, "control should be replaced on new search"
        assert first_control.answer_now_requested() is True, (
            "prior control should be signaled to cancel"
        )
        assert app.screen.control.answer_now_requested() is False, (
            "fresh control should not carry over the cancel flag"
        )


@pytest.mark.slow
async def test_tab_moves_focus_from_filter_to_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tab on the filter input moves focus to the DataTable below it."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Leave the pre-search bare canvas so the body chrome (filter/results)
        # is present to focus.
        app.screen._set_empty_state(empty=False)
        await pilot.pause()
        # On empty initial query the search bar takes initial focus, so
        # manually move focus to the filter input for this test.
        app.screen._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "filter"
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "results"


@pytest.mark.slow
async def test_down_at_empty_filter_releases_focus_to_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``down`` arrow on an empty filter moves focus to the results table."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


@pytest.mark.slow
async def test_up_at_results_top_row_releases_focus_to_filter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``up`` when the results-list cursor is at row 0 moves focus to the filter."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="seed row",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        # Seed one record so the list has a row 0 to be on.
        app.screen.all_records.append(record)
        app.screen.filtered_records.append(record)
        app.screen._results.append_records([record])
        await pilot.pause()
        # Land focus on the filter and tab to the results.
        app.screen._filter_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"
        # Ensure highlight is on row 0 before pressing up.
        assert app.screen._results.highlighted in (None, 0)
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


@pytest.mark.slow
async def test_l_from_results_focuses_detail_pane(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vim-style ``l`` (and right-arrow) from the results list focuses the detail pane."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="seed row",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.append(record)
        app.screen.filtered_records.append(record)
        app.screen._results.append_records([record])
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"
        await pilot.press("l")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"


@pytest.mark.slow
async def test_k_at_detail_top_focuses_filter_input(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``k`` / ``up`` on the detail pane at scroll_y=0 releases focus to the filter input."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="seed row",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.append(record)
        app.screen.filtered_records.append(record)
        app.screen._results.append_records([record])
        await pilot.pause()
        app.screen._detail_scroll.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        # Pre-condition: at the top of the (short) detail body.
        assert app.screen._detail_scroll.scroll_y <= 0
        await pilot.press("k")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


@pytest.mark.slow
async def test_h_from_detail_focuses_results_pane(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vim-style ``h`` (and left-arrow) from the detail pane focuses the results list."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="seed row",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.append(record)
        app.screen.filtered_records.append(record)
        app.screen._results.append_records([record])
        await pilot.pause()
        # Focus the detail-scroll widget directly, then bounce back via ``h``.
        app.screen._detail_scroll.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        await pilot.press("h")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


@pytest.mark.slow
async def test_g_on_results_jumps_to_top(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``g`` while the results list is focused snaps the cursor to row 0."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        app.screen._results.highlighted = 3
        await pilot.pause()
        assert app.screen._results.highlighted == 3
        await pilot.press("g")
        await pilot.pause()
        assert app.screen._results.highlighted == 0


@pytest.mark.slow
async def test_G_on_results_jumps_to_bottom(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``G`` while the results list is focused snaps the cursor to the last row."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("G")
        await pilot.pause()
        assert app.screen._results.highlighted == 4


@pytest.mark.slow
async def test_ctrl_d_on_results_advances_half_page(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-D`` on the results list advances the highlight by at least one row."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 20)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        app.screen._results.highlighted = 0
        await pilot.pause()
        await pilot.press("ctrl+d")
        await pilot.pause()
        # Robust against tiny viewports during ``run_test`` — half-page may be
        # as small as 1 if the simulated screen is shallow. Either way, the
        # cursor must have moved forward and stayed within bounds.
        assert app.screen._results.highlighted is not None
        assert app.screen._results.highlighted > 0
        assert app.screen._results.highlighted <= len(records) - 1


@pytest.mark.slow
async def test_g_on_detail_scrolls_to_top(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``g`` on the detail pane jumps scroll_y back to 0."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    long_body = "\n".join(f"line {idx}" for idx in range(200))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "long.jsonl",
        text=long_body,
    )
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.append(record)
        app.screen.filtered_records.append(record)
        app.screen._results.append_records([record])
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        app.screen._detail_scroll.scroll_to(y=50, animate=False)
        await pilot.pause()
        assert app.screen._detail_scroll.scroll_y > 0
        app.screen._detail_scroll.focus()
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        assert app.screen._detail_scroll.scroll_y == 0


@pytest.mark.slow
async def test_G_on_detail_scrolls_to_bottom(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``G`` on the detail pane snaps scroll_y to (near) the maximum."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    long_body = "\n".join(f"line {idx}" for idx in range(200))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "long.jsonl",
        text=long_body,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.append(record)
        app.screen.filtered_records.append(record)
        app.screen._results.append_records([record])
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        app.screen._detail_scroll.focus()
        await pilot.pause()
        await pilot.press("G")
        await pilot.pause()
        assert app.screen._detail_scroll.scroll_y >= app.screen._detail_scroll.max_scroll_y - 0.5


@pytest.mark.slow
async def test_ctrl_f_on_detail_opens_find(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-F`` (and ``/``) on the detail pane opens the find-in-detail bar."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    long_body = "\n".join(f"line {idx}" for idx in range(200))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "long.jsonl",
        text=long_body,
    )
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.append(record)
        app.screen.filtered_records.append(record)
        app.screen._results.append_records([record])
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        app.screen._detail_scroll.focus()
        await pilot.pause()
        assert app.screen._detail_find_input.display is False
        await pilot.press("ctrl+f")
        await pilot.pause()
        assert app.screen._detail_find_input.display is True
        assert app.screen._detail_find_active is True
        assert getattr(app.focused, "id", None) == "detail-find"
