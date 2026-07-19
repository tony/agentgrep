"""Functional tests for the legacy ``agentgrep`` Textual surface."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import pathlib
import threading
import time
import typing as t

import pytest

import agentgrep as _agentgrep_module
from agentgrep._engine import orchestration
from agentgrep.records import RecordOrigin
from agentgrep.ui._source_diagnostics import (
    SourceScanFinished,
    SourceScanStarted,
    UiProgressSnapshot,
)

pytestmark = pytest.mark.tui


def load_agentgrep_module() -> object:
    """Return the installed ``agentgrep`` package."""
    return _agentgrep_module


def _build_empty_ui_app(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> t.Any:
    """Build a streaming UI app with the search worker stubbed to a no-op."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    # Isolate the search-history state file under tmp so tests never read or
    # trim the developer's real ~/.local/state/agentgrep/history.jsonl.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Keep persisted UI preferences away from the developer's real config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        agentgrep,
        "run_search_query",
        lambda *args, **kwargs: [],
    )
    query = agentgrep.SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    return agentgrep.build_streaming_ui_app(home, query, control=agentgrep.SearchControl())


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


def _ui_record(agentgrep: t.Any, path: pathlib.Path, text: str, session_id: str) -> t.Any:
    """Build a minimal prompt :class:`SearchRecord` for detail-pane tests."""
    return agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        text=text,
        session_id=session_id,
    )


def _static_content(widget: t.Any) -> t.Any:
    """Return Static content across Textual's supported inspection APIs."""
    content = getattr(widget, "content", None)
    return content if content is not None else widget._content


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


def _seed_records(
    agentgrep: t.Any,
    tmp_path: pathlib.Path,
    count: int,
) -> list[t.Any]:
    """Build ``count`` ``SearchRecord`` instances under ``tmp_path``."""
    return [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(count)
    ]


def _set_result_records(results: t.Any, records: t.Iterable[t.Any]) -> None:
    """Adopt one test-prepared result model."""
    prepared = list(records)
    results.set_records(
        prepared,
        record_ids={id(record) for record in prepared},
    )


def _filter_completed(app: t.Any, records: t.Iterable[t.Any], *, text: str = "") -> t.Any:
    """Build a generation-scoped filter completion for a mounted test app."""
    from agentgrep.ui.widgets import FilterCompleted

    prepared = list(records)
    return FilterCompleted(
        text=text,
        records=prepared,
        record_ids={id(record) for record in prepared},
        generation=app.screen._filter_generation,
        records_generation=app.screen._records_generation,
    )


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


def _detail_find_record(agentgrep: t.Any, path: pathlib.Path) -> t.Any:
    """Build a record whose body has several 'needle' matches across lines."""
    body = "\n".join(
        f"line {i} has a needle here" if i % 3 == 0 else f"line {i} is plain" for i in range(30)
    )
    return _ui_record(agentgrep, path, body, "find")


async def _open_detail_with_find(app: t.Any, record: t.Any, pilot: t.Any) -> None:
    """Show ``record`` in the detail pane and reveal the find bar."""
    app.screen._set_empty_state(empty=False)
    app.screen.show_detail(record)
    await pilot.pause()
    app.screen.action_open_detail_find()
    await pilot.pause()


class DetailFindStaleRequestCase(t.NamedTuple):
    """A stale debounced find request scenario."""

    test_id: str
    live_text: str
    message_text: str
    close_first: bool


DETAIL_FIND_STALE_REQUEST_CASES = [
    DetailFindStaleRequestCase(
        test_id="closed-find-ignores-pending-request",
        live_text="needle",
        message_text="needle",
        close_first=True,
    ),
    DetailFindStaleRequestCase(
        test_id="changed-input-ignores-old-request",
        live_text="nomatch",
        message_text="needle",
        close_first=False,
    ),
]


class DetailFindStepLiveQueryCase(t.NamedTuple):
    """An immediate find navigation key scenario."""

    test_id: str
    key: str
    expected_index: int


DETAIL_FIND_STEP_LIVE_QUERY_CASES = [
    DetailFindStepLiveQueryCase(test_id="enter-steps-live-query", key="enter", expected_index=1),
    DetailFindStepLiveQueryCase(test_id="down-steps-live-query", key="down", expected_index=1),
    DetailFindStepLiveQueryCase(test_id="up-steps-live-query", key="up", expected_index=9),
]


class DetailFindPendingRenderCase(t.NamedTuple):
    """A detail-find query while the selected large record is still rendering."""

    test_id: str
    query: str
    expected_matches: int


DETAIL_FIND_PENDING_RENDER_CASES = [
    DetailFindPendingRenderCase(
        test_id="does-not-search-old-source",
        query="oldneedle",
        expected_matches=0,
    ),
    DetailFindPendingRenderCase(
        test_id="searches-new-body-fallback",
        query="newneedle",
        expected_matches=1,
    ),
]


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    DETAIL_FIND_STALE_REQUEST_CASES,
    ids=[case.test_id for case in DETAIL_FIND_STALE_REQUEST_CASES],
)
async def test_detail_find_ignores_stale_debounce_requests(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: DetailFindStaleRequestCase,
) -> None:
    """Stale debounced find requests do not repaint hidden or superseded find state."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    from agentgrep.ui.widgets.messages import DetailFindRequested

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, record, pilot)
        app.screen._detail_find_input.load_query(case.live_text)
        if case.close_first:
            app.screen._close_detail_find()
            await pilot.pause()

        app.screen.on_detail_find_requested(DetailFindRequested(case.message_text))
        await pilot.pause()

        assert app.screen._detail_find_query == ""
        assert app.screen._detail_find_matches == []
        if case.close_first:
            assert app.screen._detail_find_active is False
            assert app.screen._detail_find_input.display is False


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    DETAIL_FIND_STEP_LIVE_QUERY_CASES,
    ids=[case.test_id for case in DETAIL_FIND_STEP_LIVE_QUERY_CASES],
)
async def test_detail_find_steps_live_query_before_navigation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: DetailFindStepLiveQueryCase,
) -> None:
    """Find navigation keys search the live input before stepping matches."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    from textual import events

    from agentgrep.ui.widgets.messages import DetailFindRequested

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, record, pilot)
        app.screen._detail_find_input.value = "needle"

        await app.screen._detail_find_input.on_key(events.Key(case.key, None))
        app.screen.on_detail_find_requested(DetailFindRequested("needle"))
        await pilot.pause()

        assert app.screen._detail_find_query == "needle"
        assert len(app.screen._detail_find_matches) == 10
        assert app.screen._detail_find_current == case.expected_index


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    DETAIL_FIND_PENDING_RENDER_CASES,
    ids=[case.test_id for case in DETAIL_FIND_PENDING_RENDER_CASES],
)
async def test_detail_find_uses_new_body_while_large_render_is_pending(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: DetailFindPendingRenderCase,
) -> None:
    """Opening find before a large render finishes searches the new record body."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    old_record = _ui_record(
        agentgrep,
        tmp_path / "old.jsonl",
        "oldneedle only lives in the previous record",
        "old",
    )
    new_body = "newneedle lives here\n" + (
        "x" * (app.get_default_screen()._DETAIL_ASYNC_BODY_THRESHOLD + 1000)
    )
    new_record = _ui_record(agentgrep, tmp_path / "new.jsonl", new_body, "new")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen.show_detail(old_record)
        await pilot.pause()
        assert "oldneedle" in app.screen._detail_find_source

        scheduled_workers: list[object] = []

        def capture_worker(worker: object, **_: object) -> None:
            scheduled_workers.append(worker)

        monkeypatch.setattr(app.screen, "run_worker", capture_worker)
        app.screen.show_detail(new_record)
        assert scheduled_workers

        app.screen.action_open_detail_find()
        app.screen._detail_find_input.load_query(case.query)
        app.screen._run_detail_find(case.query, reset_cursor=True)
        await pilot.pause()

        assert len(app.screen._detail_find_matches) == case.expected_matches


@pytest.mark.slow
async def test_detail_find_searches_navigates_and_counts(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typing in the find bar matches the body, counts N/M, and steps the cursor."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, record, pilot)
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        await pilot.pause()
        assert len(app.screen._detail_find_matches) == 10
        assert app.screen._detail_find_current == 0
        assert "1/10" in str(app.screen._detail_statusline.render())
        # Next match advances the cursor and scrolls the body.
        before = app.screen._detail_scroll.scroll_y
        app.screen._detail_find_step(1)
        await pilot.pause()
        assert app.screen._detail_find_current == 1
        assert app.screen._detail_scroll.scroll_y > before
        # Wrap-around: previous from match 1 -> 0, previous again -> last (9).
        app.screen._detail_find_step(-1)
        app.screen._detail_find_step(-1)
        assert app.screen._detail_find_current == 9


@pytest.mark.slow
async def test_detail_find_step_reuses_syntax_base(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stepping find matches must not re-tokenize the JSON body each press (NB-9).

    ``_present_detail_find`` re-overlays only the find-match spans; the
    syntax+search+filter base is identical across a find session, so a cached
    base keeps the per-keystroke cost off a full-body ``Syntax`` re-highlight.
    """
    from agentgrep.ui.layouts import hud

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    body = json.dumps({"notes": [f"needle {i}" for i in range(12)]}, indent=2)
    record = _ui_record(agentgrep, tmp_path / "j.jsonl", body, "json")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        syntax_calls = 0
        real_syntax = hud._RichSyntax

        def counting_syntax(*args: t.Any, **kwargs: t.Any) -> t.Any:  # forwarding spy
            nonlocal syntax_calls
            syntax_calls += 1
            return real_syntax(*args, **kwargs)

        monkeypatch.setattr(hud, "_RichSyntax", counting_syntax)
        await _open_detail_with_find(app, record, pilot)
        app.screen._run_detail_find("needle", reset_cursor=True)
        await pilot.pause()
        assert app.screen._detail_find_matches  # the JSON body really was matched
        after_find = syntax_calls
        assert after_find >= 1  # the JSON base was tokenized at least once
        app.screen._detail_find_step(1)
        await pilot.pause()
        assert syntax_calls == after_find  # the step reused the cached base


@pytest.mark.slow
async def test_detail_find_open_reuses_presented_text_highlights(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening find reuses the long plain-text body already highlighted off-pump.

    Patch the defining module: hud calls
    ``_streaming._apply_bounded_literal_highlights`` through the module
    namespace, so patching ``ui._streaming`` intercepts every caller;
    patching a ``hud`` alias would intercept nothing.
    """
    from agentgrep.ui import _streaming

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _ui_record(
        agentgrep,
        tmp_path / "long.jsonl",
        "needle " + ("plain text " * 6000),
        "long",
    )
    highlight_calls = 0
    real_highlight = _streaming._apply_bounded_literal_highlights

    def counting_highlight(*args: t.Any, **kwargs: t.Any) -> None:
        nonlocal highlight_calls
        highlight_calls += 1
        real_highlight(*args, **kwargs)

    monkeypatch.setattr(_streaming, "_apply_bounded_literal_highlights", counting_highlight)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen.search_query = dataclasses.replace(
            app.screen.search_query,
            terms=("needle",),
        )
        app.screen._set_empty_state(empty=False)
        app.screen.show_detail(record)
        await app.workers.wait_for_complete()
        await pilot.pause()
        after_present = highlight_calls
        assert after_present >= 1

        app.screen.action_open_detail_find()
        await pilot.pause()

        assert highlight_calls == after_present


class DetailFindFilterRefreshCase(t.NamedTuple):
    """A same-record filter change while detail find stays open."""

    test_id: str
    initial_filter: str
    updated_filter: str
    find_query: str


DETAIL_FIND_FILTER_REFRESH_CASES: tuple[DetailFindFilterRefreshCase, ...] = (
    DetailFindFilterRefreshCase(
        test_id="same-record-filter-change",
        initial_filter="before",
        updated_filter="after",
        find_query="needle",
    ),
)


@pytest.mark.slow
async def test_filter_completion_refreshes_same_record_detail_highlights(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new filter repaints decoration even when the selected record is unchanged."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    body = "before needle after"
    record = _ui_record(agentgrep, tmp_path / "filter.jsonl", body, "filter")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._filter_terms = ("before",)
        app.screen.show_detail(record)
        await app.workers.wait_for_complete()
        await pilot.pause()

        app.screen._filter_terms = ("after",)
        app.screen._filter_input.value = "after"
        app.screen.on_filter_completed(
            _filter_completed(app, [record], text="after"),
        )
        await app.workers.wait_for_complete()
        await pilot.pause()

        detail_body = _static_content(app.screen._detail).renderables[1]
        spans = [(span.start, span.end, str(span.style)) for span in detail_body.spans]
        filter_bg = app.theme_variables["ag-match-filter-bg"]
        assert not any(
            start == 0 and end == len("before") and filter_bg in style
            for start, end, style in spans
        )
        after_start = body.index("after")
        assert any(
            start == after_start and end == after_start + len("after") and filter_bg in style
            for start, end, style in spans
        )


@pytest.mark.slow
async def test_empty_filter_completion_clears_detail_find_selection(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty filter cannot retain or repaint the excluded detail record."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _ui_record(agentgrep, tmp_path / "excluded.jsonl", "needle body", "excluded")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, record, pilot)
        app.screen._detail_opened = True
        app.screen._apply_responsive_layout()
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        app.screen._search_done = True
        app.screen._filter_input.value = "absent"
        await pilot.pause()

        app.screen.on_filter_completed(
            _filter_completed(app, [], text="absent"),
        )
        await pilot.pause()

        assert str(app.screen._detail.render()) == "No results."
        assert app.screen._current_detail_record is None
        assert app.screen._detail_find_active is False
        assert app.screen._detail_find_input.display is False
        assert app.screen._detail_find_matches == []
        assert str(app.screen._detail_statusline.render()) == ""
        assert getattr(app.focused, "id", None) == "filter"
        assert app.screen._detail_opened is False
        assert app.screen._detail_column.has_class("-collapsed")

        app.screen._detail_find_step(1)

        assert str(app.screen._detail.render()) == "No results."


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    DETAIL_FIND_FILTER_REFRESH_CASES,
    ids=[case.test_id for case in DETAIL_FIND_FILTER_REFRESH_CASES],
)
async def test_detail_find_base_refreshes_filter_highlights_when_filter_changes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: DetailFindFilterRefreshCase,
) -> None:
    """A same-record filter change refreshes the cached find-highlight base."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    body = f"{case.initial_filter} {case.find_query} {case.updated_filter}"
    record = _ui_record(agentgrep, tmp_path / "filter.jsonl", body, "filter")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, record, pilot)
        app.screen._filter_terms = (case.initial_filter,)
        app.screen._detail_find_input.load_query(case.find_query)
        app.screen._run_detail_find(case.find_query, reset_cursor=True)
        await pilot.pause()

        app.screen._filter_terms = (case.updated_filter,)
        app.screen._filter_input.value = case.updated_filter
        app.screen.on_filter_completed(
            _filter_completed(
                app,
                [record],
                text=case.updated_filter,
            ),
        )
        app.screen._detail_find_step(1)
        await pilot.pause()

        detail_body = _static_content(app.screen._detail).renderables[1]
        spans = [(span.start, span.end, str(span.style)) for span in detail_body.spans]
        filter_bg = app.theme_variables["ag-match-filter-bg"]
        initial_start = body.index(case.initial_filter)
        updated_start = body.index(case.updated_filter)
        assert not any(
            start == initial_start
            and end == initial_start + len(case.initial_filter)
            and filter_bg in style
            for start, end, style in spans
        )
        assert any(
            start == updated_start
            and end == updated_start + len(case.updated_filter)
            and filter_bg in style
            for start, end, style in spans
        )


@pytest.mark.slow
async def test_new_search_clears_results_render_cache(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh search releases rendered rows so a reused record id can't go stale.

    The row cache is keyed by ``id(record)`` (like cached_haystack); when a new
    search empties ``all_records`` the rows must be released with them.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        _ui_record(agentgrep, tmp_path / f"r{i}.jsonl", f"row {i}", f"s{i}") for i in range(6)
    ]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        _set_result_records(app.screen._results, records)
        assert app.screen._results._render_cache == {}  # model replacement stays lazy
        app.screen._results._render_record(records[0])  # one requested row populates the LRU
        assert app.screen._results._render_cache  # non-empty
        app.screen._reset_search_chrome()  # a fresh search releases the old records
        assert app.screen._results._render_cache == {}  # cache released with them


@pytest.mark.slow
async def test_detail_find_only_opens_with_a_record(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The find bar stays hidden when no detail record is loaded (gated)."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert app.screen._current_detail_record is None
        app.screen.action_open_detail_find()
        await pilot.pause()
        assert app.screen._detail_find_input.display is False
        assert app.screen._detail_find_active is False


@pytest.mark.slow
async def test_detail_find_escape_closes_without_quitting(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Esc closes the find bar and refocuses the detail body without exiting."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, record, pilot)
        assert app.screen._detail_find_input.display is True
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen._detail_find_input.display is False
        assert app.screen._detail_find_active is False
        assert getattr(app.focused, "id", None) == "detail-scroll"
        assert app.is_running  # esc closed find, did not quit the app


@pytest.mark.slow
async def test_detail_find_memory_restores_per_record(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing find saves the query+cursor per record; revisiting restores them."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    rec_a = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    rec_b = _ui_record(agentgrep, tmp_path / "b.jsonl", "no matches at all\n" * 8, "b")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, rec_a, pilot)
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        app.screen._detail_find_step(1)  # land on match index 1
        await pilot.pause()
        app.screen._close_detail_find()
        await pilot.pause()
        assert app.screen._detail_find_state[id(rec_a)][:2] == ("needle", 1)
        # Visit another record, come back, reopen -> the query + cursor restore.
        app.screen.show_detail(rec_b)
        await pilot.pause()
        app.screen.show_detail(rec_a)
        await pilot.pause()
        app.screen.action_open_detail_find()
        await pilot.pause()
        assert app.screen._detail_find_input.value == "needle"
        assert app.screen._detail_find_current == 1
        assert len(app.screen._detail_find_matches) == 10


@pytest.mark.slow
async def test_detail_find_resets_on_record_switch_while_open(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching records with the find bar open closes it (no stale matches/count).

    Regression: leaving the bar open across a record switch otherwise applied
    the old record's match offsets to the new body and showed a stale N/M. The
    outgoing record's find is saved, so a revisit restores it.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    rec_a = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    rec_b = _ui_record(agentgrep, tmp_path / "b.jsonl", "no matches at all\n" * 8, "b")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, rec_a, pilot)
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        app.screen._detail_find_step(1)
        await pilot.pause()
        # Switch to B WITHOUT closing find first (the bug path).
        app.screen.show_detail(rec_b)
        await pilot.pause()
        assert app.screen._detail_find_active is False
        assert app.screen._detail_find_input.display is False
        assert app.screen._detail_find_matches == []
        # A's find survived in per-record memory for a later revisit.
        assert app.screen._detail_find_state[id(rec_a)][:2] == ("needle", 1)


@pytest.mark.slow
async def test_detail_find_survives_theme_switch(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A theme switch re-renders the same record but keeps the find active+highlighted."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    from agentgrep.ui import theme as ui_theme

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    rec_a = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, rec_a, pilot)
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        await pilot.pause()
        app.theme = ui_theme.LIGHT_THEME_NAME  # same record re-render
        await pilot.pause()
        # Find stays active with valid matches (not closed by the re-render),
        # and _present_detail re-overlays the highlights via _present_detail_find.
        assert app.screen._detail_find_active is True
        assert app.screen._detail_find_input.display is True
        assert len(app.screen._detail_find_matches) == 10


@pytest.mark.slow
async def test_theme_switch_refreshes_the_searching_panel(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A theme switch re-bakes the SearchingPanel's hex spans, like the header."""
    from agentgrep.ui import theme as ui_theme

    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        calls: list[int] = []
        monkeypatch.setattr(app.screen._searching_panel, "refresh_theme", lambda: calls.append(1))
        app.theme = ui_theme.LIGHT_THEME_NAME
        await pilot.pause()
        assert calls == [1]


@pytest.mark.slow
async def test_input_ctrl_c_clears_then_arms_confirm_exit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C in the search input clears the text first, then arms confirm-exit."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        search.focus()
        search.value = "hello"
        await pilot.pause()
        await pilot.press("ctrl+c")  # text present -> clear, no exit, no arm
        await pilot.pause()
        assert search.value == ""
        assert app.is_running
        assert app.screen._confirm_exit_pending is False
        await pilot.press("ctrl+c")  # empty box -> arm confirm-exit (gutter shown)
        await pilot.pause()
        assert app.screen._confirm_exit_pending is True
        assert app.is_running
        assert app.screen._ctrlc_gutter.has_class("-shown")
        await pilot.press("x")  # any other key disarms
        await pilot.pause()
        assert app.screen._confirm_exit_pending is False
        assert app.screen._ctrlc_gutter.has_class("-shown") is False


@pytest.mark.slow
async def test_input_second_ctrl_c_on_empty_exits(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second ctrl-c on an empty input within the window exits the app."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        search = app.screen.query_one("#search")
        search.focus()
        await pilot.press("ctrl+c")  # arm
        await pilot.pause()
        assert app.screen._confirm_exit_pending is True
        await pilot.press("ctrl+c")  # exit
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.slow
@pytest.mark.parametrize("input_id", ["search", "filter"])
async def test_empty_input_ctrl_c_cancels_active_search(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    input_id: str,
) -> None:
    """An empty focused input cancels active work before arming exit."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        target = app.screen.query_one(f"#{input_id}")
        target.focus()
        app.screen._search_done = False

        await pilot.press("ctrl+c")
        await pilot.pause()

        assert app.screen.control.answer_now_requested() is True
        assert app.screen._confirm_exit_pending is False
        assert app.is_running


@pytest.mark.slow
async def test_ctrl_c_on_detail_pane_arms_confirm_exit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C with a non-input pane focused arms confirm-exit, like the inputs.

    Regression: the staged "press ctrl-c again to exit" gutter only fired from a
    focused input; on the detail scroll (a non-input widget) the first ctrl-c
    quit outright with no warning. ``action_smart_quit`` now routes through the
    same arm-then-confirm flow.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        app.screen.show_detail(record)
        await pilot.pause()
        app.screen._detail_scroll.focus()
        await pilot.pause()
        assert getattr(app.focused, "id", None) == "detail-scroll"
        assert app.screen._has_active_actions() is False
        await pilot.press("ctrl+c")  # non-input focus -> arm, do not quit
        await pilot.pause()
        assert app.is_running
        assert app.screen._confirm_exit_pending is True
        assert app.screen._ctrlc_gutter.has_class("-shown")
        await pilot.press("ctrl+c")  # second press within the window -> exit
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.slow
async def test_find_input_ctrl_c_clears_then_closes_bar(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C in the find input clears the query, then closes the bar (never quits)."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _detail_find_record(agentgrep, tmp_path / "a.jsonl")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_detail_with_find(app, record, pilot)
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        app.screen._detail_find_input.focus()
        await pilot.pause()
        await pilot.press("ctrl+c")  # query present -> clear, bar stays open
        await pilot.pause()
        assert app.screen._detail_find_input.value == ""
        assert app.screen._detail_find_active is True
        assert app.is_running
        await pilot.press("ctrl+c")  # empty -> close the bar (not quit)
        await pilot.pause()
        assert app.screen._detail_find_active is False
        assert app.screen._detail_find_input.display is False
        assert app.is_running


@pytest.mark.slow
async def test_detail_find_scrolls_wrapped_match_into_view(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scroll-to-match brings a match on a wrapped line into the viewport.

    A logical newline count would land the match far above the viewport when
    long lines wrap; the wrap-aware row computation puts it on screen.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    body = "\n".join(
        ["x" * 220 for _ in range(6)] + ["a needle to find"] + ["y" * 220 for _ in range(6)],
    )
    record = _ui_record(agentgrep, tmp_path / "wrap.jsonl", body, "wrap")
    async with app.run_test(size=(140, 24)) as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        app.screen.show_detail(record)
        await pilot.pause()
        app.screen.action_open_detail_find()
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        await pilot.pause()
        scroll = app.screen._detail_scroll
        # The match's true visual row (read off the rendered wrap cache) lies in
        # the scrolled viewport; a logical-line count would land it off-screen.
        app.screen._detail._render_content()
        rows = [
            i
            for i, strip in enumerate(app.screen._detail._render_cache.lines)
            if "needle" in strip.text
        ]
        assert rows, "match should be in the rendered output"
        viewport = range(int(scroll.scroll_y), int(scroll.scroll_y) + scroll.size.height)
        assert any(row in viewport for row in rows)


@pytest.mark.slow
async def test_detail_find_keeps_json_syntax_colors(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Find on a JSON body keeps syntax token colors and layers the find highlight."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    body = '{"role": "user", "needle": "a", "items": [{"needle": "b"}], "x": "no"}'
    record = _ui_record(agentgrep, tmp_path / "j.jsonl", body, "j")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        app.screen.show_detail(record)
        await pilot.pause()
        # The find source is the pretty-printed (multiline) JSON, so offsets and
        # matches line up with what is displayed.
        assert "\n" in app.screen._detail_find_source
        app.screen.action_open_detail_find()
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        await pilot.pause()
        assert len(app.screen._detail_find_matches) == 2
        body_text = _static_content(app.screen._detail).renderables[1]
        styles = {str(span.style) for span in body_text.spans}
        assert any("on " in s for s in styles)  # find-match background spans
        assert any(s and "on " not in s and s != "none" for s in styles)  # JSON token colors


@pytest.mark.slow
async def test_ctrl_j_from_filter_focuses_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-J`` while the filter input has focus moves focus to the results list."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("ctrl+j")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


@pytest.mark.slow
async def test_ctrl_l_from_results_focuses_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-L`` from the results list moves focus rightward to the detail pane."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
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
        assert app.focused is not None and app.focused.id == "results"
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"


@pytest.mark.slow
async def test_ctrl_h_from_detail_focuses_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-H`` from the detail pane moves focus leftward to the results list."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        app.screen._detail_scroll.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        await pilot.press("ctrl+h")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


@pytest.mark.slow
async def test_ctrl_k_from_results_focuses_filter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-K`` from the results list moves focus up to the filter input."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
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
        assert app.focused is not None and app.focused.id == "results"
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


@pytest.mark.slow
async def test_ctrl_k_from_detail_focuses_filter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-K`` from the detail pane jumps focus all the way back to the filter."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        app.screen._detail_scroll.focus()
        await pilot.pause()
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


@pytest.mark.slow
async def test_backspace_from_detail_focuses_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backspace aliases ``Ctrl-H`` in many terminals — should focus results from detail."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        app.screen._detail_scroll.focus()
        await pilot.pause()
        await pilot.press("backspace")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


@pytest.mark.slow
async def test_backspace_in_filter_still_deletes_a_character(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backspace alias must NOT steal backspace from the filter input."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("a")
        await pilot.press("b")
        await pilot.press("c")
        await pilot.pause()
        assert app.screen._filter_input.value == "abc"
        await pilot.press("backspace")
        await pilot.pause()
        # Backspace deleted the last character; focus stayed on filter.
        assert app.screen._filter_input.value == "ab"
        assert app.focused.id == "filter"


@pytest.mark.slow
async def test_ctrl_h_from_filter_is_a_noop(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Ctrl-H`` on the filter does nothing (no pane to the left)."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("ctrl+h")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"


@pytest.mark.slow
async def test_up_on_empty_filter_releases_focus_to_search(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain ``up`` on an empty filter input lifts focus to the top search bar."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "filter"
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "search"


@pytest.mark.slow
async def test_up_on_filter_with_cursor_at_start_releases_focus_to_search(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``up`` on a non-empty filter whose cursor is at position 0 still escapes upward."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        # Type something, then move cursor back to start.
        app.screen._filter_input.value = "abc"
        app.screen._filter_input.cursor_position = 0
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "search"


class FocusDetailRevealCase(t.NamedTuple):
    """One width scenario for ``right``/``l`` focusing the detail pane."""

    test_id: str
    size: tuple[int, int]
    expect_opened: bool


FOCUS_DETAIL_REVEAL_CASES: tuple[FocusDetailRevealCase, ...] = (
    FocusDetailRevealCase(
        test_id="wide-records-explicit-focus", size=(120, 24), expect_opened=True
    ),
    FocusDetailRevealCase(test_id="narrow-opens-on-focus", size=(80, 24), expect_opened=True),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    FOCUS_DETAIL_REVEAL_CASES,
    ids=[case.test_id for case in FOCUS_DETAIL_REVEAL_CASES],
)
async def test_right_on_empty_filter_focuses_and_opens_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: FocusDetailRevealCase,
) -> None:
    """``right`` on an empty filter focuses the detail — opening it when stacked.

    On a narrow terminal the detail starts collapsed (``display: none``);
    focusing it must reveal it first, not move focus into a hidden pane.
    """
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=case.size) as pilot:
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        assert app.screen._filter_input.value == ""
        await pilot.press("right")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        assert not app.screen._detail_column.has_class("-collapsed")
        # Explicit detail focus records the user's reader intent even when
        # wide mode already has the pane visible.
        assert app.screen._detail_opened is case.expect_opened


class DetailFocusResizeCase(t.NamedTuple):
    """One explicit detail-focus route before a wide-to-narrow resize."""

    test_id: str
    key: str


DETAIL_FOCUS_RESIZE_CASES: tuple[DetailFocusResizeCase, ...] = (
    DetailFocusResizeCase(test_id="l-from-results", key="l"),
    DetailFocusResizeCase(test_id="right-from-results", key="right"),
    DetailFocusResizeCase(test_id="ctrl-l-from-results", key="ctrl+l"),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    DETAIL_FOCUS_RESIZE_CASES,
    ids=[case.test_id for case in DETAIL_FOCUS_RESIZE_CASES],
)
async def test_explicit_wide_detail_focus_survives_narrow_resize(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: DetailFocusResizeCase,
) -> None:
    """Explicit reader focus in wide mode remains visible after stacking."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._apply_responsive_layout()
        app.screen._results.focus()
        await pilot.pause()

        await pilot.press(case.key)
        await pilot.pause()
        assert app.screen._stacked is False
        assert app.focused is not None and app.focused.id == "detail-scroll"
        assert app.screen._detail_opened is True

        await pilot.resize_terminal(80, 24)
        await pilot.pause(0.1)
        assert app.screen._stacked is True
        assert app.screen._detail_opened is True
        assert not app.screen._detail_column.has_class("-collapsed")
        assert app.focused is not None and app.focused.id == "detail-scroll"


@pytest.mark.slow
async def test_l_from_results_opens_stacked_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing ``l`` in the results list opens + focuses the stacked detail."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._apply_responsive_layout()
        await pilot.pause()
        assert app.screen._detail_column.has_class("-collapsed")
        app.screen._results.focus()
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "detail-scroll"
        assert not app.screen._detail_column.has_class("-collapsed")
        assert app.screen._detail_opened is True


class FocusDetailRenderCase(t.NamedTuple):
    """One explicit-detail focus scenario and the record it should render."""

    test_id: str
    highlighted: int | None
    expected_index: int


FOCUS_DETAIL_RENDER_CASES: tuple[FocusDetailRenderCase, ...] = (
    FocusDetailRenderCase(
        test_id="no-highlight-falls-back-to-first-record",
        highlighted=None,
        expected_index=0,
    ),
    FocusDetailRenderCase(
        test_id="highlighted-record-wins",
        highlighted=2,
        expected_index=2,
    ),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    FOCUS_DETAIL_RENDER_CASES,
    ids=[case.test_id for case in FOCUS_DETAIL_RENDER_CASES],
)
async def test_focus_detail_renders_record_when_opening_stacked_streaming_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: FocusDetailRenderCase,
) -> None:
    """Opening a stacked streaming result renders a readable detail body."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"prefix\nVISIBLEPROBE record {idx}\nsuffix",
        )
        for idx in range(3)
    ]
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        app.screen._results.append_records(records)
        if case.highlighted is not None:
            # Seed Textual's reactive storage directly so this case can
            # model a highlighted row without dispatching the same genuine
            # cursor-move event that normally opens the stacked detail.
            app.screen._results._reactive_highlighted = case.highlighted
            app.screen._current_detail_record = records[0]
            app.screen._detail_opened = False
        app.screen._apply_responsive_layout()
        await pilot.pause()
        assert app.screen._detail_column.has_class("-collapsed")
        app.screen._results.focus()
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        expected = records[case.expected_index]
        assert app.focused is not None and app.focused.id == "detail-scroll"
        assert app.screen._current_detail_record is expected
        assert not app.screen._detail_column.has_class("-collapsed")
        # Records open at the top now (per-record scroll memory), so in the
        # short stacked viewport the matched body line sits below the metadata
        # header — scroll down to bring it into view before asserting it renders.
        app.screen._detail_scroll.scroll_end(animate=False)
        await pilot.pause()
        screenshot = app.export_screenshot(simplify=True)
        assert "VISIBLEPROBE" in screenshot
        assert f"record&#160;{case.expected_index}" in screenshot


@pytest.mark.slow
async def test_detail_focus_membership_uses_ids_maintained_at_all_mutation_seams(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current-detail visibility is O(1) after reset, append, and replace."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    first, second = _seed_records(agentgrep, tmp_path, 2)
    iteration_error = "current-detail membership scanned the result list"

    class NoIdentityIteration(list[t.Any]):
        def __iter__(self) -> t.NoReturn:
            raise AssertionError(iteration_error)

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen._results.append_records((first,))
        app.screen._reset_search_chrome()
        assert app.screen._results.contains_record(first) is False

        await app.screen._apply_records_batch((first, second), total=2)
        assert app.screen._results.contains_record(first) is True
        assert app.screen._results.contains_record(second) is True

        app.screen.on_filter_completed(_filter_completed(app, [second]))
        assert app.screen._results.contains_record(first) is False
        assert app.screen._results.contains_record(second) is True

        app.screen._results._reactive_highlighted = None
        app.screen._current_detail_record = second
        app.screen.filtered_records = NoIdentityIteration((second,))
        assert app.screen._record_for_detail_focus() is second


class AutohighlightQueueCase(t.NamedTuple):
    """One filter-result scenario for queued programmatic highlights."""

    test_id: str
    record_count: int
    matching_count: int
    initial_highlighted: int | None
    expect_programmatic: int


AUTOHIGHLIGHT_QUEUE_CASES: tuple[AutohighlightQueueCase, ...] = (
    AutohighlightQueueCase(
        test_id="streamed-results-without-highlight",
        record_count=3,
        matching_count=3,
        initial_highlighted=None,
        expect_programmatic=0,
    ),
    AutohighlightQueueCase(
        test_id="empty-leaves-it-disarmed",
        record_count=3,
        matching_count=0,
        initial_highlighted=None,
        expect_programmatic=0,
    ),
    AutohighlightQueueCase(
        test_id="single-clamp-highlight",
        record_count=3,
        matching_count=2,
        initial_highlighted=2,
        expect_programmatic=1,
    ),
    AutohighlightQueueCase(
        test_id="far-clamp-is-one-programmatic-move",
        record_count=10,
        matching_count=5,
        initial_highlighted=9,
        expect_programmatic=1,
    ),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    AUTOHIGHLIGHT_QUEUE_CASES,
    ids=[case.test_id for case in AUTOHIGHLIGHT_QUEUE_CASES],
)
async def test_filter_completion_marks_only_model_highlights_programmatic(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: AutohighlightQueueCase,
) -> None:
    """Only an existing cursor emits a programmatic model-change highlight."""
    from agentgrep.ui.widgets import ResultHighlighted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, case.record_count)
    messages: dict[int, ResultHighlighted] = {}

    def capture(message: object) -> None:
        if isinstance(message, ResultHighlighted) and message.programmatic:
            messages[id(message)] = message

    async with app.run_test(size=(80, 24), message_hook=capture) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        app.screen._results.append_records(records)
        if case.initial_highlighted is not None:
            app.screen._results._reactive_highlighted = case.initial_highlighted
        app.screen.on_filter_completed(
            _filter_completed(
                app,
                records[: case.matching_count],
            ),
        )
        await pilot.pause()
        assert len(messages) == case.expect_programmatic


class FilterUserMoveCase(t.NamedTuple):
    """One filter path and the first genuine cursor move after it."""

    test_id: str
    record_count: int
    matching_count: int
    initial_highlighted: int | None
    first_user_key: str


FILTER_USER_MOVE_CASES: tuple[FilterUserMoveCase, ...] = (
    FilterUserMoveCase(
        test_id="streamed-results-without-highlight",
        record_count=3,
        matching_count=3,
        initial_highlighted=None,
        first_user_key="j",
    ),
    FilterUserMoveCase(
        test_id="narrowing-keeps-highlight-index",
        record_count=3,
        matching_count=2,
        initial_highlighted=0,
        first_user_key="j",
    ),
    FilterUserMoveCase(
        test_id="single-clamp-highlight-is-programmatic",
        record_count=3,
        matching_count=2,
        initial_highlighted=2,
        first_user_key="k",
    ),
    FilterUserMoveCase(
        test_id="multi-clamp-highlights-are-programmatic",
        record_count=10,
        matching_count=5,
        initial_highlighted=9,
        first_user_key="k",
    ),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    FILTER_USER_MOVE_CASES,
    ids=[case.test_id for case in FILTER_USER_MOVE_CASES],
)
async def test_filter_completion_does_not_swallow_first_real_cursor_move(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: FilterUserMoveCase,
) -> None:
    """Only queued programmatic highlights may keep stacked detail collapsed."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, case.record_count)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        app.screen._results.append_records(records)
        if case.initial_highlighted is not None:
            app.screen._results._reactive_highlighted = case.initial_highlighted
        app.screen._detail_opened = False
        app.screen._apply_responsive_layout()
        app.screen._results.focus()
        await pilot.pause()

        app.screen.on_filter_completed(
            _filter_completed(
                app,
                records[: case.matching_count],
            ),
        )
        await pilot.pause()
        await pilot.pause()
        assert app.screen._detail_opened is False
        assert app.screen._detail_column.has_class("-collapsed")

        await pilot.press(case.first_user_key)
        await pilot.pause()
        assert app.screen._detail_opened is True
        assert not app.screen._detail_column.has_class("-collapsed")


@pytest.mark.slow
async def test_filter_completion_keeps_detail_on_unchanged_cursor_index(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing the record under a stable cursor also replaces its detail."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._results.highlighted = 1
        await pilot.pause()

        matching = [records[2], records[4]]
        app.screen.on_filter_completed(_filter_completed(app, matching))
        await pilot.pause()

        assert app.screen._results.highlighted == 1
        assert app.screen._current_detail_record is records[4]


@pytest.mark.slow
async def test_filter_completion_adopts_worker_model_without_iteration(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pump adopts worker-prepared lists and identity indexes in O(1)."""
    from agentgrep.ui.widgets import FilterCompleted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    iteration_error = "prepared filter records were scanned on the pump"

    class PreparedRecords(list[t.Any]):
        def __iter__(self) -> t.NoReturn:
            raise AssertionError(iteration_error)

    prepared = PreparedRecords(records)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen.on_filter_completed(
            FilterCompleted(
                text="",
                records=prepared,
                record_ids={id(record) for record in records},
                generation=app.screen._filter_generation,
                records_generation=app.screen._records_generation,
            ),
        )

        assert app.screen.filtered_records is prepared
        assert app.screen._results.uses_records(prepared)
        assert app.screen._results.contains_record(records[2])


@pytest.mark.slow
async def test_filter_completion_drops_superseded_filter_generation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older same-text filter worker cannot replace the current model."""
    from agentgrep.ui.widgets import FilterCompleted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    first, second = _seed_records(agentgrep, tmp_path, 2)
    async with app.run_test(size=(120, 24)):
        app.screen.filtered_records = [first]
        _set_result_records(app.screen._results, [first])
        completion = FilterCompleted(
            text="",
            records=[second],
            record_ids={id(second)},
            generation=app.screen._filter_generation - 1,
            records_generation=app.screen._records_generation,
        )

        app.screen.on_filter_completed(completion)

        assert app.screen.filtered_records == [first]
        assert app.screen._results.contains_record(first)
        assert not app.screen._results.contains_record(second)


@pytest.mark.slow
async def test_filter_completion_retries_after_streamed_records(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker snapshot cannot replace records streamed after it started."""
    from agentgrep.ui.widgets import FilterCompleted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    first, second = _seed_records(agentgrep, tmp_path, 2)
    async with app.run_test(size=(120, 24)):
        await app.screen._apply_records_batch((first,), total=1)
        stale_revision = app.screen._records_generation
        completion = FilterCompleted(
            text="",
            records=[first],
            record_ids={id(first)},
            generation=app.screen._filter_generation,
            records_generation=stale_revision,
        )
        await app.screen._apply_records_batch((second,), total=2)
        retries: list[str] = []
        monkeypatch.setattr(app.screen, "filter_loaded", retries.append)

        app.screen.on_filter_completed(completion)

        assert app.screen.filtered_records == [first, second]
        assert app.screen._results.contains_record(second)
        assert retries == [""]


@pytest.mark.slow
async def test_filter_retry_supersedes_inflight_stream_projection(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry cannot duplicate chunks left in a superseded batch projection."""
    from agentgrep.ui import _runtime
    from agentgrep.ui.widgets import FilterCompleted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    entered_yield = asyncio.Event()
    release_yield = asyncio.Event()

    async def pause_between_chunks() -> None:
        entered_yield.set()
        await release_yield.wait()

    monkeypatch.setattr(_runtime, "_sleep_zero", pause_between_chunks)
    async with app.run_test(size=(120, 24)) as pilot:
        records = _seed_records(
            agentgrep,
            tmp_path,
            app.screen._APPLY_CHUNK_SIZE * 2 + 2,
        )
        stale_completion = FilterCompleted(
            text="",
            records=[],
            record_ids=set(),
            generation=app.screen._filter_generation,
            records_generation=app.screen._records_generation,
        )
        apply_task = asyncio.create_task(
            app.screen._apply_records_batch(records, total=len(records)),
        )
        await asyncio.wait_for(entered_yield.wait(), timeout=2)

        app.screen.on_filter_completed(stale_completion)
        async with asyncio.timeout(2):
            while app.screen._results.option_count != len(records):
                await pilot.pause()

        release_yield.set()
        await apply_task

        assert len(app.screen.filtered_records) == len(records)
        assert app.screen._results.option_count == len(records)
        assert len({id(record) for record in app.screen.filtered_records}) == len(records)


@pytest.mark.slow
async def test_right_on_non_empty_filter_moves_cursor(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``right`` on a non-empty filter walks the cursor — does not release focus."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._filter_input.focus()
        await pilot.pause()
        app.screen._filter_input.value = "abc"
        app.screen._filter_input.cursor_position = 0
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        # Focus stays on the filter; cursor advances by one.
        assert app.focused is not None and app.focused.id == "filter"
        assert app.screen._filter_input.cursor_position == 1


@pytest.mark.slow
async def test_search_results_list_append_under_load(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Appending 1000 records to the results list completes within a generous bound.

    Smoke test against accidental O(N²) regressions in the virtual model update.
    The row renderables themselves remain lazy and are covered separately.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(1000)
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        start = time.monotonic()
        app.screen._results.append_records(records)
        elapsed = time.monotonic() - start
        await pilot.pause()
        assert len(app.screen._results._records) == 1000
        assert elapsed < 2.0, f"append_records(1000) took {elapsed:.3f}s; expected < 2.0s"


@pytest.mark.slow
async def test_set_records_narrowing_preserves_order(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A narrowing filter swaps the model without eagerly rebuilding rows."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(10)
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._results.append_records(records)
        await pilot.pause()
        rendered = 0
        original_build = app.screen._results._build_row

        def counting_build(record: t.Any) -> t.Any:
            nonlocal rendered
            rendered += 1
            return original_build(record)

        monkeypatch.setattr(app.screen._results, "_build_row", counting_build)
        _set_result_records(app.screen._results, records[:7])
        assert rendered == 0
        await pilot.pause()
        assert len(app.screen._results._records) == 7
        assert [id(r) for r in app.screen._results._records] == [id(r) for r in records[:7]]


@pytest.mark.slow
async def test_set_records_widening_preserves_order(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Widening publishes the complete requested order without Option materialization."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(5)
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen._results.append_records(records[:3])
        await pilot.pause()
        _set_result_records(app.screen._results, records)
        await pilot.pause()
        assert len(app.screen._results._records) == 5
        assert [id(r) for r in app.screen._results._records] == [id(r) for r in records]


@pytest.mark.slow
async def test_apply_records_batch_yields_between_chunks(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Applying a large batch yields to the event loop every chunk_size records."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    chunk = app.get_default_screen()._APPLY_CHUNK_SIZE
    # Three chunks worth — should yield twice (between chunk 0/1 and 1/2).
    record_count = chunk * 3
    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(record_count)
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        sleep_calls = 0
        real_sleep = asyncio.sleep

        async def counting_sleep(delay: float) -> None:
            nonlocal sleep_calls
            if delay == 0:
                sleep_calls += 1
            await real_sleep(delay)

        monkeypatch.setattr(asyncio, "sleep", counting_sleep)
        await app.screen._apply_records_batch(records, record_count)
        assert sleep_calls >= 2, (
            f"expected >= 2 yields for {record_count} records in chunks of {chunk}, "
            f"got {sleep_calls}"
        )
        assert len(app.screen._results._records) == record_count


@pytest.mark.slow
async def test_apply_records_batch_filters_off_pump_in_bounded_chunks(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming filter projection stays off-pump, bounded, and ordered."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 401)
    pump_thread = threading.get_ident()
    match_threads: list[int] = []
    worker_chunks: list[int] = []
    repr_error = "worker description rendered matcher data"

    class EvenMatcher:
        def __repr__(self) -> t.NoReturn:
            raise AssertionError(repr_error)

        def matches(self, record: t.Any) -> bool:
            match_threads.append(threading.get_ident())
            return int(record.path.stem.removeprefix("r")) % 2 == 0

    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen._filter_matcher = EvenMatcher()
        app.screen._filter_generation += 1
        original_run_worker = app.screen.run_worker

        def capture_worker(work: t.Any, **kwargs: t.Any) -> t.Any:
            if kwargs.get("group") == "stream-filter":
                assert kwargs.get("description") == "match streamed records"
                worker_chunks.append(len(work.args[-1]))
            return original_run_worker(work, **kwargs)

        monkeypatch.setattr(app.screen, "run_worker", capture_worker)
        await app.screen._apply_records_batch(records, total=len(records))

        expected = records[::2]
        assert worker_chunks == [200, 200, 1]
        assert match_threads
        assert all(thread_id != pump_thread for thread_id in match_threads)
        assert app.screen.filtered_records == expected
        assert app.screen._results._records == expected


def test_stream_filter_chunks_bound_body_work(
    tmp_path: pathlib.Path,
) -> None:
    """Worker slices also cap projected body characters, not only rows."""
    from agentgrep.ui._streaming import (
        _STREAM_FILTER_MAX_TEXT_CHARS,
        _stream_filter_chunks,
    )

    records = tuple(
        _agentgrep_module.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{index}",
            text=text,
        )
        for index, text in enumerate(
            ("x" * _STREAM_FILTER_MAX_TEXT_CHARS, "y", ""),
        )
    )

    chunks = tuple(
        _stream_filter_chunks(
            records,
            max_records=200,
            max_chars=_STREAM_FILTER_MAX_TEXT_CHARS,
        ),
    )

    assert tuple(map(len, chunks)) == (1, 2)


@pytest.mark.slow
async def test_apply_records_batch_drops_stale_worker_projection(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filter change cannot publish a worker slice from the old matcher."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _seed_records(agentgrep, tmp_path, 1)[0]
    worker_started = threading.Event()
    release_worker = threading.Event()

    class BlockingMatcher:
        def matches(self, _record: t.Any) -> bool:
            worker_started.set()
            assert release_worker.wait(timeout=2)
            return True

    async with app.run_test(size=(120, 24)):
        app.screen._filter_matcher = BlockingMatcher()
        app.screen._filter_generation += 1
        apply_task = asyncio.create_task(
            app.screen._apply_records_batch((record,), total=1),
        )
        assert await asyncio.to_thread(worker_started.wait, 2)

        app.screen._filter_generation += 1
        app.screen._filter_matcher = None
        release_worker.set()
        await apply_task

        assert app.screen.all_records == [record]
        assert app.screen.filtered_records == []
        assert app.screen._results.option_count == 0


async def test_stream_filter_worker_does_not_hold_message_dispatch(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked filter slice cannot hold keystrokes behind its pump callback."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = _seed_records(agentgrep, tmp_path, 1)[0]
    worker_started = threading.Event()
    release_worker = threading.Event()

    class BlockingMatcher:
        def matches(self, _record: t.Any) -> bool:
            worker_started.set()
            assert release_worker.wait(timeout=2)
            return True

    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen._filter_matcher = BlockingMatcher()
        app.screen._filter_generation += 1
        batch = agentgrep.StreamingRecordsBatch(records=(record,), total=1)
        apply_task = asyncio.create_task(
            asyncio.to_thread(
                app.call_from_thread,
                app.screen._apply_streaming_event,
                app.screen._chrome_generation,
                batch,
            ),
        )
        assert await asyncio.to_thread(worker_started.wait, 2)

        try:
            await asyncio.wait_for(pilot.press("a"), timeout=1)
            assert app.screen._search_input.value == "a"
        finally:
            release_worker.set()
            await apply_task


@pytest.mark.slow
async def test_set_records_majority_removal_clamps_cursor_once(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large narrowing clamps the global cursor with one programmatic move."""
    from agentgrep.ui.widgets import ResultHighlighted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    messages: list[ResultHighlighted] = []

    def capture(message: object) -> None:
        if isinstance(message, ResultHighlighted):
            messages.append(message)

    records = [
        agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"r{idx}.jsonl",
            text=f"row {idx}",
        )
        for idx in range(10)
    ]
    async with app.run_test(message_hook=capture) as pilot:
        await pilot.pause()
        app.screen._results.append_records(records)
        app.screen._results._reactive_highlighted = 9
        await pilot.pause()
        messages.clear()
        result = _set_result_records(app.screen._results, records[:2])
        await pilot.pause()
        assert result is None
        generation = app.screen._results.generation
        current = {
            id(message): (message.index, message.programmatic)
            for message in messages
            if message.generation == generation
        }
        assert list(current.values()) == [(1, True)]
        assert app.screen._results.highlighted == 1
        assert len(app.screen._results._records) == 2


def test_scroll_percent_returns_full_when_nothing_scrolls() -> None:
    """A pane that fits its viewport reports ``100%`` (tig convention)."""
    from agentgrep.ui.format import scroll_percent

    assert scroll_percent(0.0, 0.0) == 100


def test_scroll_percent_clamps_to_bounds() -> None:
    """Scroll percent is clamped to ``[0, 100]`` even for nonsense inputs."""
    from agentgrep.ui.format import scroll_percent

    assert scroll_percent(0.0, 100.0) == 0
    assert scroll_percent(50.0, 100.0) == 50
    assert scroll_percent(100.0, 100.0) == 100
    # Overshoot past max — clamped to 100.
    assert scroll_percent(500.0, 100.0) == 100
    # Negative scroll — clamped to 0.
    assert scroll_percent(-10.0, 100.0) == 0


@pytest.mark.slow
async def test_results_status_right_shows_position_or_count(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The results rule combines item position/count with list scroll percent.

    Before a cursor exists the bare match count renders; the denominator
    carries the count afterwards, so the two never appear together. Both
    numeric fields keep a stable width while their values advance.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        # No streaming results yet — empty right slot regardless of args.
        assert app.screen._format_results_right(cursor=None, visible=None, percent=100) == ""
        # Seed streaming totals so the match count segment renders.
        app.screen.all_records.extend(_seed_records(agentgrep, tmp_path, 10))
        # No cursor yet — bare match count.
        assert (
            app.screen._format_results_right(cursor=None, visible=10, percent=100)
            == "10 matches  100%"
        )
        # A local filter owns this rule, so its visible count wins over the
        # larger unfiltered search total.
        assert (
            app.screen._format_results_right(cursor=None, visible=4, percent=100)
            == "4 matches  100%"
        )
        assert (
            app.screen._format_results_right(cursor=None, visible=0, percent=100)
            == "0 matches  100%"
        )
        # Cursor at row 0 of all 10 — position plus list scroll percentage.
        assert app.screen._format_results_right(cursor=0, visible=10, percent=0) == " 1/10    0%"


@pytest.mark.slow
async def test_detail_statusline_shows_path_and_scroll_percent(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``show_detail`` populates the detail status line with path + scroll %."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path("[red]x[/red]"),
        text="hello",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        updates: list[str] = []
        real_update = app.screen._detail_statusline.update

        def spy(content: t.Any = "", *args: t.Any, **kwargs: t.Any) -> None:
            updates.append(str(content))
            real_update(content, *args, **kwargs)

        monkeypatch.setattr(app.screen._detail_statusline, "update", spy)
        app.screen.show_detail(record)
        await pilot.pause()
        # Latest update should carry both the path's basename and a trailing ``%``.
        rendered = updates[-1] if updates else ""
        assert "[red]x[/red]" in rendered
        assert rendered.rstrip().endswith("%")
        assert "[red]x[/red]" in str(app.screen._detail_statusline.render())


@pytest.mark.slow
async def test_results_scroll_changed_updates_status_right(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app handler updates the results rule when cursor or scroll changes."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 40)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        updates: list[str] = []
        real_set = app.screen._results_header.set_right

        def spy(text: str) -> None:
            updates.append(text)
            real_set(text)

        monkeypatch.setattr(app.screen._results_header, "set_right", spy)
        # Pre-seed streaming records so the match count is non-zero.
        app.screen.all_records.extend(records)
        app.screen._results.append_records(records)
        await pilot.pause()
        # Explicitly land focus and move cursor to row 0 — the reactive
        # ``highlighted`` watcher fires on change, so set it directly.
        app.screen._results.focus()
        await pilot.pause()
        app.screen._results.highlighted = 0
        await pilot.pause()
        # The ``highlighted`` watcher posts the top position and percentage.
        assert any(u.strip().startswith("1/40") and u.endswith("0%") for u in updates), updates

        await pilot.press("G")
        await pilot.pause()

        assert app.screen._results_header._right == "40/40  100%"


@pytest.mark.slow
async def test_filter_completion_refreshes_unchanged_cursor_denominator(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Narrowing a filter refreshes ``1/N`` even when row 1 stays selected."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 10)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._results.highlighted = 0
        app.screen._refresh_results_status_right()
        await pilot.pause()
        assert "1/10" in app.screen._results_header._right

        app.screen.on_filter_completed(_filter_completed(app, records[:5]))
        await pilot.pause()

        assert app.screen._results.highlighted == 0
        assert "1/5" in app.screen._results_header._right


@pytest.mark.slow
async def test_stale_results_scroll_message_cannot_repaint_reset_rule(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued pre-reset scroll snapshot is only a live-state invalidation."""
    from agentgrep.ui.widgets import ResultsScrollChanged

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._results.highlighted = 0
        app.screen._refresh_results_status_right()
        await pilot.pause()
        assert app.screen._results_header._right

        app.screen._reset_search_chrome()
        await pilot.pause()
        assert app.screen._results_header._right == ""
        app.screen.on_results_scroll_changed(
            ResultsScrollChanged(cursor=0, total=5, percent=0),
        )

        assert app.screen._results_header._right == ""


class RightSlotCase(t.NamedTuple):
    """One position/scroll scenario for the results-status right slot."""

    test_id: str
    cursor: int | None
    visible: int
    percent: int
    expected: str


RIGHT_SLOT_CASES: tuple[RightSlotCase, ...] = (
    RightSlotCase(
        test_id="first-of-five-at-top",
        cursor=0,
        visible=5,
        percent=0,
        expected="1/5    0%",
    ),
    RightSlotCase(
        test_id="first-of-forty-pads-numerator",
        cursor=0,
        visible=40,
        percent=9,
        expected=" 1/40    9%",
    ),
    RightSlotCase(
        test_id="tenth-of-forty-keeps-width",
        cursor=9,
        visible=40,
        percent=10,
        expected="10/40   10%",
    ),
    RightSlotCase(
        test_id="last-of-forty-at-bottom",
        cursor=39,
        visible=40,
        percent=100,
        expected="40/40  100%",
    ),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    RIGHT_SLOT_CASES,
    ids=[case.test_id for case in RIGHT_SLOT_CASES],
)
async def test_results_status_right_has_stable_numeric_width(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: RightSlotCase,
) -> None:
    """Right slots keep fixed-width position and scroll fields."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen._set_empty_state(empty=False)
        await pilot.pause()
        assert (
            app.screen._format_results_right(
                case.cursor,
                case.visible,
                percent=case.percent,
            )
            == case.expected
        )


def _make_progress_snapshot(agentgrep: t.Any, **overrides: t.Any) -> t.Any:
    """Build a scanning-phase ``ProgressSnapshot`` with overridable fields."""
    fields: dict[str, t.Any] = {
        "query_label": "tmux",
        "phase": "scanning",
        "current": 5662,
        "total": 6748,
        "detail": "2176 records, 354 source matches",
        "matches": 2176,
        "elapsed": 32.0,
        "source_records_seen": 2176,
    }
    fields.update(overrides)
    return agentgrep.ProgressSnapshot(**fields)


@pytest.mark.slow
async def test_apply_progress_shows_indeterminate_source_heartbeat(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scanning snapshot shows source facts and heartbeat without a bar."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        # The folded header rule shows once results stream in; seed one so the
        # hybrid is past its centered-panel phase.
        app.screen.all_records.extend(_seed_records(agentgrep, tmp_path, 1))
        app.screen._set_empty_state(empty=False)
        app.screen._filter_header.begin()
        app.screen._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        rendered = app.screen._filter_header.render().plain
        assert "source 5662 of 6748" in rendered
        assert "2176 records" in rendered
        assert "▰" not in rendered
        assert "%" not in rendered


@pytest.mark.slow
async def test_header_indeterminate_before_total_shows_no_bar(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a source total the header shows no bar — the spinner carries motion."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        # Seed a result so the folded header rule (not the centered panel) is
        # the visible chrome whose payload we assert on.
        app.screen.all_records.extend(_seed_records(agentgrep, tmp_path, 1))
        app.screen._set_empty_state(empty=False)
        app.screen._filter_header.begin()
        app.screen._apply_progress(
            _make_progress_snapshot(
                agentgrep,
                phase="discovering",
                current=None,
                total=None,
                detail=None,
            ),
        )
        await pilot.pause()
        rendered = app.screen._filter_header.render().plain
        assert "Discovering" in rendered
        assert "▰" not in rendered
        assert "%" not in rendered


@pytest.mark.slow
async def test_ctrl_backslash_toggles_scanning_detail_row(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""``Ctrl-\`` does not duplicate the already-visible scan status."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        app.screen._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        detail_row = app.screen.query_one("#status-detail")
        assert not detail_row.has_class("visible")
        await pilot.press("ctrl+backslash")
        await pilot.pause()
        assert app.screen._detail_visible is True
        assert not detail_row.has_class("visible")
        assert app.screen._last_detail_text == ""
        await pilot.press("ctrl+backslash")
        await pilot.pause()
        assert app.screen._detail_visible is False
        assert not detail_row.has_class("visible")


@pytest.mark.slow
async def test_detail_row_does_not_label_planning_counts_as_sources(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planner-group counters stay distinct from active source ordinals."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        app.screen._apply_progress(
            _make_progress_snapshot(
                agentgrep,
                phase="planning",
                current=7,
                total=10,
                detail="candidate sources",
                source_records_seen=None,
            ),
        )
        await pilot.press("ctrl+backslash")
        await pilot.pause()

        assert app.screen._last_detail_text == ""
        assert not app.screen._detail_row.has_class("visible")


@pytest.mark.slow
async def test_detail_row_surfaces_only_a_thresholded_concurrent_source(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The expanded row ignores a fast tail and paints the true slow store."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        detail_row = app.screen._detail_row
        detail_row.begin()
        assert not detail_row.has_class("visible")

        updates: list[tuple[str, bool]] = []
        real_update = detail_row.update

        def spy(content: t.Any = "", *, layout: bool = True) -> None:
            updates.append((str(content), layout))
            real_update(content, layout=layout)

        monkeypatch.setattr(detail_row, "update", spy)
        now = time.monotonic()
        snapshot = _make_progress_snapshot(agentgrep, current=3, total=82)
        generation = app.screen._chrome_generation
        await app.screen._apply_streaming_event(
            generation,
            UiProgressSnapshot(
                snapshot=snapshot,
                lifecycle=SourceScanStarted(
                    source_id=3,
                    store="cursor-ide.state_vscdb",
                ),
            ),
        )
        for source_id in range(4, 83):
            fast_store = f"fast.store.{source_id}"
            await app.screen._apply_streaming_event(
                generation,
                UiProgressSnapshot(
                    snapshot=snapshot,
                    lifecycle=SourceScanStarted(
                        source_id=source_id,
                        store=fast_store,
                    ),
                ),
            )
            await app.screen._apply_streaming_event(
                generation,
                UiProgressSnapshot(
                    snapshot=snapshot,
                    lifecycle=SourceScanFinished(
                        source_id=source_id,
                        finished_at=now,
                    ),
                ),
            )

        await pilot.press("ctrl+backslash")
        await pilot.pause(0.55)
        assert detail_row.has_class("visible")
        assert app.screen._body.has_class("-searching")
        assert detail_row.display is True
        assert updates == [
            ("Slow source\ncursor-ide.state_vscdb · 500ms+", False),
        ]

        await app.screen._apply_streaming_event(
            generation,
            UiProgressSnapshot(
                snapshot=snapshot,
                lifecycle=SourceScanFinished(
                    source_id=3,
                    finished_at=time.monotonic(),
                ),
            ),
        )
        app.screen._apply_finished("complete", 40, 69.4, None)
        terminal, layout = updates[-1]
        assert terminal.startswith(
            "Search complete: 40 matches in 69.4s\nSlow source: cursor-ide.state_vscdb · ",
        )
        assert layout is False
        assert detail_row._sample_timer is None
        assert all("fast.store" not in content for content, _layout in updates)


@pytest.mark.slow
async def test_finished_source_selects_remaining_active_search_chrome(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed source yields the chrome to a remaining active source."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen._search_done = False
        generation = screen._chrome_generation

        for source_id in (1, 2):
            await screen._apply_streaming_event(
                generation,
                UiProgressSnapshot(
                    snapshot=_make_progress_snapshot(
                        agentgrep,
                        current=source_id,
                        total=2,
                        source_records_seen=0,
                    ),
                    lifecycle=SourceScanStarted(
                        source_id=source_id,
                        store=f"store.{source_id}",
                    ),
                ),
            )

        await screen._apply_streaming_event(
            generation,
            _make_progress_snapshot(
                agentgrep,
                current=1,
                total=2,
                source_records_seen=128,
            ),
        )
        await screen._apply_streaming_event(
            generation,
            UiProgressSnapshot(
                snapshot=_make_progress_snapshot(
                    agentgrep,
                    current=1,
                    total=2,
                    source_records_seen=128,
                ),
                lifecycle=SourceScanFinished(
                    source_id=1,
                    finished_at=time.monotonic(),
                ),
            ),
        )

        assert screen._last_snapshot.current == 2
        assert screen._filter_header._current == 2
        screen._apply_finished("interrupted", 0, 0.5, None)
        assert "source 1 of 2" not in screen._last_detail_text
        assert "while scanning source 2 of 2" in screen._last_detail_text


@pytest.mark.slow
async def test_detail_row_visibility_sticky_across_search_reset(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new search keeps the detail row visible but wipes its stale content."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        app.screen._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        await pilot.press("ctrl+backslash")
        await pilot.pause()
        assert app.screen._detail_visible is True
        app.screen._reset_search_chrome()
        await pilot.pause()
        assert app.screen._detail_visible is True
        assert app.screen._last_detail_text == ""
        assert not app.screen._detail_row.has_class("visible")


@pytest.mark.slow
async def test_finish_complete_freezes_header_to_done_text(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finishing freezes the header to ``Done`` and stops the timer."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        # Results present → the folded header rule (not the centered panel) is
        # the chrome that freezes and carries the outcome.
        app.screen.all_records.extend(_seed_records(agentgrep, tmp_path, 1))
        app.screen._set_empty_state(empty=False)
        app.screen._filter_header.begin()
        app.screen._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        app.screen._apply_finished("complete", 100, 12.3, None)
        await pilot.pause()
        header = app.screen._filter_header
        assert header._outcome == "complete"
        assert header.auto_refresh is None  # the spinner timer stopped
        rendered = header.render().plain
        assert "Done" in rendered
        assert "%" not in rendered
        assert "▰" not in rendered
        assert "▱" not in rendered
        assert "✓" not in rendered
        # The data summary lands in the toggleable detail row.
        assert app.screen._last_detail_text == "Search complete: 100 matches in 12.3s"


class FinishOutcomeCase(t.NamedTuple):
    """One post-search outcome scenario for the filter header."""

    test_id: str
    size: tuple[int, int]
    outcome: str
    glyph: str  # the frozen marker stored on the widget
    marker: str
    seed_scanning: bool


FINISH_OUTCOME_CASES: tuple[FinishOutcomeCase, ...] = (
    FinishOutcomeCase(
        test_id="complete-wide-done-no-bar",
        size=(160, 24),
        outcome="complete",
        glyph="✓",
        marker="Done",
        seed_scanning=True,
    ),
    FinishOutcomeCase(
        test_id="complete-narrow-done-no-bar",
        size=(40, 24),
        outcome="complete",
        glyph="✓",
        marker="Done",
        seed_scanning=True,
    ),
    FinishOutcomeCase(
        test_id="interrupted-wide-stopped-no-bar",
        size=(160, 24),
        outcome="interrupted",
        glyph="■",
        marker="■",
        seed_scanning=True,
    ),
    FinishOutcomeCase(
        # Interrupted before the first scanning snapshot: explicit stopped text,
        # no fabricated fraction or bar.
        test_id="interrupted-no-scan-square-no-bar",
        size=(160, 24),
        outcome="interrupted",
        glyph="■",
        marker="■",
        seed_scanning=False,
    ),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    FINISH_OUTCOME_CASES,
    ids=[case.test_id for case in FINISH_OUTCOME_CASES],
)
async def test_finish_outcome_freezes_header_glyph(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: FinishOutcomeCase,
) -> None:
    """The frozen filter header carries every outcome as bounded text."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=case.size) as pilot:
        await pilot.pause()
        # Reveal + lay out the chrome so the header has a real width before the
        # narrow/wide payload is computed.
        app.screen._set_empty_state(empty=False)
        await pilot.pause()
        app.screen._search_done = False
        app.screen.all_records.extend(_seed_records(agentgrep, tmp_path, 5))
        app.screen._filter_header.begin()
        if case.seed_scanning:
            app.screen._apply_progress(_make_progress_snapshot(agentgrep))
            await pilot.pause()
        app.screen._apply_finished(case.outcome, 100, 12.3, None)
        await pilot.pause()
        header = app.screen._filter_header
        assert header._outcome == case.outcome
        assert header._final_glyph == case.glyph
        rendered = header.render().plain
        assert case.marker in rendered
        assert "▰" not in rendered
        assert "▱" not in rendered
        assert "%" not in rendered
        if case.outcome == "interrupted":
            assert "Stopped" in rendered
            assert "%" not in rendered


@pytest.mark.slow
async def test_detail_row_shows_summary_after_finish(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Toggling the detail row after a finished search shows the data summary."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        app.screen._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        app.screen._apply_finished("interrupted", 2976, 2.1, None)
        await pilot.pause()
        await pilot.press("ctrl+backslash")
        await pilot.pause()
        assert app.screen._detail_visible is True
        assert app.screen._last_detail_text == (
            "Stopped at 2976 matches while scanning source 5662 of 6748 in 2.1s"
        )


@pytest.mark.slow
async def test_interrupted_planning_summary_omits_source_counts(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopping during planning never presents group counters as sources."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        app.screen._apply_progress(
            _make_progress_snapshot(
                agentgrep,
                phase="planning",
                current=7,
                total=10,
                detail="candidate sources",
                source_records_seen=None,
            ),
        )
        app.screen._apply_finished("interrupted", 0, 0.5, None)
        await pilot.pause()

        assert app.screen._last_detail_text == "Stopped at 0 matches in 0.5s"


@pytest.mark.slow
async def test_header_snapshot_setter_does_not_repaint(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """During a search, set_snapshot stores heartbeat state without repainting.

    The 2 Hz spinner timer drives the header, so thousands of per-source
    progress events never thrash the rule with extra refreshes.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        header = app.screen._filter_header
        header.begin()  # arms the self-refresh timer (drives repaints)
        refreshes: list[None] = []
        real_refresh = header.refresh

        def spy(*args: t.Any, **kwargs: t.Any) -> t.Any:
            refreshes.append(None)
            return real_refresh(*args, **kwargs)

        monkeypatch.setattr(header, "refresh", spy)
        header.set_snapshot(_make_progress_snapshot(agentgrep, source_records_seen=128))
        header.set_snapshot(_make_progress_snapshot(agentgrep, source_records_seen=256))
        assert refreshes == []  # setters store only; the timer repaints


class StaleGenerationCase(t.NamedTuple):
    """One generation-gate scenario for ``_apply_streaming_event``."""

    test_id: str
    use_current_generation: bool
    expect_applied: bool


STALE_GENERATION_CASES: tuple[StaleGenerationCase, ...] = (
    StaleGenerationCase(
        test_id="current-generation-applies",
        use_current_generation=True,
        expect_applied=True,
    ),
    StaleGenerationCase(
        test_id="stale-generation-dropped",
        use_current_generation=False,
        expect_applied=False,
    ),
)


@pytest.mark.parametrize(
    "case",
    STALE_GENERATION_CASES,
    ids=[case.test_id for case in STALE_GENERATION_CASES],
)
async def test_streaming_events_gated_by_generation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: StaleGenerationCase,
) -> None:
    """Events from a cancelled worker's generation never touch the chrome.

    A cancelled worker keeps draining its queued events after the user
    starts a new search; the un-gated form repainted the new search's
    chrome with stale "Stopped" states and old bar fills.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        stale_generation = app.screen._chrome_generation
        # A new search bumps the generation; the old reporter's events
        # still carry the previous one.
        app.screen._reset_search_chrome()
        await pilot.pause()
        generation = (
            app.screen._chrome_generation if case.use_current_generation else stale_generation
        )
        await app.screen._apply_streaming_event(generation, _make_progress_snapshot(agentgrep))
        await pilot.pause()
        assert (app.screen._last_snapshot is not None) is case.expect_applied
        assert (app.screen._filter_header._current is not None) is case.expect_applied


async def test_streaming_records_batch_lands_in_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A records batch routed through the generation gate populates the list.

    Regression guard: the records handler is a coroutine — the gate must
    await it, not drop the un-awaited coroutine on the floor (which left
    the results list silently empty).
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        app.screen._search_done = False
        batch = agentgrep.StreamingRecordsBatch(records=tuple(records), total=3)
        await app.screen._apply_streaming_event(app.screen._chrome_generation, batch)
        await pilot.pause()
        assert len(app.screen.all_records) == 3
        assert len(app.screen._results._records) == 3


@pytest.mark.slow
async def test_narrow_header_keeps_source_without_bar(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below the breakpoint the header keeps source state without fake percent."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(40, 24)) as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        await pilot.pause()
        app.screen._search_done = False
        app.screen.all_records.extend(_seed_records(agentgrep, tmp_path, 5))
        app.screen._filter_header.begin()
        app.screen._apply_progress(_make_progress_snapshot(agentgrep))
        await pilot.pause()
        rendered = app.screen._filter_header.render().plain
        assert "5662/6748" in rendered
        assert "▰" not in rendered
        assert "%" not in rendered


class SplitOrientationCase(t.NamedTuple):
    """One terminal-width scenario for the responsive detail split."""

    test_id: str
    size: tuple[int, int]
    expect_stacked: bool


SPLIT_ORIENTATION_CASES: tuple[SplitOrientationCase, ...] = (
    SplitOrientationCase(test_id="wide-side-by-side", size=(120, 24), expect_stacked=False),
    SplitOrientationCase(test_id="narrow-stacked", size=(80, 24), expect_stacked=True),
)


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    SPLIT_ORIENTATION_CASES,
    ids=[case.test_id for case in SPLIT_ORIENTATION_CASES],
)
async def test_body_stacks_below_split_breakpoint(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: SplitOrientationCase,
) -> None:
    """The body flips to a stacked layout below 100 cols, side-by-side above."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=case.size) as pilot:
        await pilot.pause()
        assert app.screen._stacked is case.expect_stacked
        assert app.screen._body.has_class("-stacked") is case.expect_stacked


@pytest.mark.slow
async def test_narrow_detail_opens_on_user_selection_not_autohighlight(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stacked detail stays collapsed until a genuine cursor move (tig-style)."""
    from agentgrep.ui.widgets import ResultHighlighted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._apply_responsive_layout()
        await pilot.pause()
        # Narrow + nothing opened → detail collapsed.
        assert app.screen._stacked is True
        assert app.screen._detail_column.has_class("-collapsed")
        # The programmatic row-0 highlight must NOT open it.
        app.screen.on_result_highlighted(
            ResultHighlighted(
                record=records[0],
                index=0,
                generation=app.screen._results.generation,
                programmatic=True,
            ),
        )
        await pilot.pause()
        assert app.screen._detail_opened is False
        assert app.screen._detail_column.has_class("-collapsed")
        # A real cursor move opens it and keeps it open.
        app.screen.on_result_highlighted(
            ResultHighlighted(
                record=records[1],
                index=1,
                generation=app.screen._results.generation,
                programmatic=False,
            ),
        )
        await pilot.pause()
        assert app.screen._detail_opened is True
        assert not app.screen._detail_column.has_class("-collapsed")


@pytest.mark.slow
async def test_clicking_programmatically_highlighted_row_opens_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Click intent opens stacked detail even when the cursor value is unchanged."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 3)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._results._reactive_highlighted = 2
        app.screen.on_filter_completed(_filter_completed(app, records[:1]))
        await pilot.pause()

        assert app.screen._results.highlighted == 0
        assert app.screen._detail_column.has_class("-collapsed")

        clicked = await pilot.click(app.screen._results, offset=(4, 0))
        await pilot.pause()

        assert clicked is True
        assert app.screen._detail_opened is True
        assert not app.screen._detail_column.has_class("-collapsed")


@pytest.mark.slow
async def test_stale_result_highlight_cannot_open_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued highlight from an older model is rejected by generation."""
    from agentgrep.ui.widgets import ResultHighlighted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 2)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._detail_opened = False

        app.screen.on_result_highlighted(
            ResultHighlighted(
                record=records[0],
                index=0,
                generation=app.screen._results.generation - 1,
                programmatic=True,
            ),
        )
        await pilot.pause()

        assert app.screen._detail_opened is False
        assert app.screen._current_detail_record is not records[0]


@pytest.mark.slow
async def test_wide_detail_always_visible(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Side-by-side keeps the detail pane visible regardless of selection."""
    from agentgrep.ui.widgets import ResultHighlighted

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._apply_responsive_layout()
        await pilot.pause()
        assert app.screen._stacked is False
        # Visible before any selection.
        assert app.screen._detail_opened is False
        assert not app.screen._detail_column.has_class("-collapsed")
        # ...and still visible after a genuine selection (the "regardless
        # of selection" property the docstring promises).
        app.screen.on_result_highlighted(
            ResultHighlighted(
                record=records[0],
                index=0,
                generation=app.screen._results.generation,
                programmatic=False,
            ),
        )
        await pilot.pause()
        assert not app.screen._detail_column.has_class("-collapsed")


@pytest.mark.slow
async def test_responsive_layout_classes_stay_orthogonal_to_detail_zoom(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Responsive recomputation leaves logical zoom and collapse state independent."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 2)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen._set_empty_state(empty=False)
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._detail_opened = False
        app.screen._apply_responsive_layout()

        app.screen._search_input.value = "/maximize detail"
        app.screen._search_input.focus()
        await pilot.press("enter")
        await pilot.pause()
        app.screen._apply_responsive_layout()
        await pilot.pause()

        assert app.screen._body.has_class("-zoom-detail")
        assert app.screen._body.has_class("-stacked")
        assert app.screen._detail_column.has_class("-collapsed")
        assert app.screen._detail_opened is False


@pytest.mark.slow
async def test_new_search_recollapses_narrow_detail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_reset_search_chrome`` re-collapses the stacked detail pane."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._detail_opened = True
        app.screen._apply_responsive_layout()
        await pilot.pause()
        assert not app.screen._detail_column.has_class("-collapsed")
        app.screen._reset_search_chrome()
        await pilot.pause()
        assert app.screen._detail_opened is False
        assert app.screen._detail_column.has_class("-collapsed")


@pytest.mark.slow
async def test_stacked_focus_routes_results_and_detail_vertically(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When stacked, ctrl+j reaches the detail below and ctrl+k returns up."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    records = _seed_records(agentgrep, tmp_path, 5)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.all_records.extend(records)
        app.screen.filtered_records = list(records)
        _set_result_records(app.screen._results, records)
        app.screen._apply_responsive_layout()
        await pilot.pause()
        app.screen._results.focus()
        await pilot.pause()
        # Down from results opens + focuses the detail below.
        app.screen.action_focus_pane_down()
        await pilot.pause()
        assert app.screen._detail_opened is True
        assert app.focused is not None and app.focused.id == "detail-scroll"
        # Up from the detail returns to the results.
        app.screen.action_focus_pane_up()
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "results"


def test_format_compact_path_passes_short_paths_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Paths that already fit the width budget are returned unchanged."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(agentgrep.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    short = tmp_path / "a" / "b.txt"
    assert agentgrep.format_compact_path(short, max_width=80) == "~/a/b.txt"


def test_format_compact_path_middle_elides_long_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Long paths get a ``…/`` middle elide, preserving the hidden-dir root."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(agentgrep.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    long_path = tmp_path / ".codex" / "sessions" / "2024" / "02" / "14" / "uuid.jsonl"
    result = agentgrep.format_compact_path(long_path, max_width=30)
    assert result == "~/.codex/…/14/uuid.jsonl"
    assert len(result) <= 30


def test_format_compact_path_drops_root_when_tight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """When even the rooted elide doesn't fit, drop the root: ``…/parent/file``."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    monkeypatch.setattr(agentgrep.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    long_path = tmp_path / ".codex" / "sessions" / "2024" / "02" / "14" / "verylongfilename.jsonl"
    result = agentgrep.format_compact_path(long_path, max_width=20)
    # Either tier-2 (root dropped) or tier-3 (filename only) — whichever fits.
    assert len(result) <= 20
    assert "verylongfilename" in result or "…" in result


def test_truncate_lines_passes_short_text_through() -> None:
    """Short text is returned unchanged."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "a\nb\nc"
    assert agentgrep.truncate_lines(text, max_lines=10) == text


def test_truncate_lines_appends_overflow_marker() -> None:
    """Long text is truncated and a ``+N more`` marker is appended."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "\n".join(f"line {i}" for i in range(50))
    result = agentgrep.truncate_lines(text, max_lines=5)
    assert result.startswith("line 0\nline 1\nline 2\nline 3\nline 4\n")
    assert "(+45 more lines)" in result


def test_truncate_lines_caps_single_line_by_characters() -> None:
    """A newline-free body cannot bypass the detail rendering budget."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    cap = agentgrep.DETAIL_BODY_MAX_CHARS
    text = "x" * (cap + 1000)
    result = agentgrep.truncate_lines(
        text,
        max_lines=agentgrep.DETAIL_BODY_MAX_LINES,
        max_chars=cap,
    )
    assert result.startswith("x" * cap)
    assert result.endswith("… (more content)")
    assert len(result) < len(text)


@pytest.mark.slow
async def test_show_detail_caps_single_line_at_max_chars(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``show_detail`` bounds one huge line before any Rich rendering."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    cap = agentgrep.DETAIL_BODY_MAX_CHARS
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text="x" * (cap + 1000),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        assert app.screen._detail_body_text.endswith("… (more content)")
        assert len(app.screen._detail_body_text) < len(record.text)


@pytest.mark.slow
async def test_show_detail_caps_body_at_max_lines(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``show_detail`` caps the body so giant records render instantly.

    The body is now wrapped in a ``VerticalScroll`` so the cap is a generous
    sanity bound (default 1000 lines), not the visible-height. Test the cap.
    """
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    cap = agentgrep.DETAIL_BODY_MAX_LINES
    huge_body = "\n".join(f"body line {i}" for i in range(cap + 1000))
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "a.jsonl",
        text=huge_body,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        # The compatibility helper returns the Group passed to ``update()``;
        # for this plain-text body, its body renderable is a ``Text``.
        group = _static_content(app.screen._detail)
        body_text = next(
            item
            for item in group.renderables
            if hasattr(item, "plain") and "body line" in item.plain
        )
        assert "more lines" in body_text.plain
        assert body_text.plain.count("body line") == cap


def test_format_timestamp_tig_renders_iso_with_offset_in_local_tz() -> None:
    """ISO inputs with explicit offsets are localized to the system timezone."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    result = agentgrep.format_timestamp_tig("2026-05-17T11:59:12+00:00")
    # Shape: ``YYYY-MM-DD HH:MM ±HHMM`` (22 chars)
    assert len(result) == 22
    assert result[4] == "-" and result[7] == "-"
    assert result[10] == " "
    assert result[13] == ":"
    assert result[16] == " "
    assert result[17] in {"+", "-"}


def test_format_timestamp_tig_renders_zulu_input() -> None:
    """``Z`` suffix is treated as ``+00:00`` (Python's ``fromisoformat`` requires the swap)."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    result = agentgrep.format_timestamp_tig("2026-05-17T11:59:12Z")
    assert len(result) == 22


def test_format_timestamp_tig_returns_empty_string_for_missing_input() -> None:
    """``None`` / empty inputs render as the empty string so callers can pad."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.format_timestamp_tig(None) == ""
    assert agentgrep.format_timestamp_tig("") == ""


def test_format_timestamp_tig_falls_back_to_raw_on_parse_error() -> None:
    """Unparseable inputs return the original string clipped to 22 chars."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.format_timestamp_tig("not-an-iso-timestamp") == "not-an-iso-timestamp"
    # Long unparseable input is clipped.
    long_input = "this-is-not-a-timestamp-but-it-is-too-long-anyway"
    assert agentgrep.format_timestamp_tig(long_input) == long_input[:22]


def test_find_first_match_line_returns_index_of_first_match() -> None:
    """Returns the line index of the first matching line; case-insensitive by default."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "alpha\nbeta\nFOO bar\nbaz"
    assert agentgrep.find_first_match_line(text, ("foo",)) == 2
    assert agentgrep.find_first_match_line(text, ("foo",), case_sensitive=True) is None
    assert agentgrep.find_first_match_line(text, ("FOO",), case_sensitive=True) == 2
    assert agentgrep.find_first_match_line("", ("foo",)) is None
    assert agentgrep.find_first_match_line(text, ()) is None
    # Regex mode
    assert agentgrep.find_first_match_line(text, (r"b\w+",), regex=True) == 1


def test_find_first_match_line_skips_malformed_regex() -> None:
    """Malformed regex patterns are silently skipped; valid siblings still match."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    text = "alpha\nbeta gamma\ndelta"
    # ``[`` is unbalanced; should be ignored. ``gamma`` should still match.
    assert agentgrep.find_first_match_line(text, ("[", "gamma"), regex=True) == 1


def test_highlight_matches_styles_each_occurrence() -> None:
    """``highlight_matches`` adds a styled span for every occurrence of every term."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_text = agentgrep.highlight_matches("foo foo bar", ("foo",))
    # Two spans for two occurrences.
    assert sum(1 for span in rich_text.spans if "bold yellow" in str(span.style)) == 2


def test_highlight_matches_combines_terms() -> None:
    """Multiple terms each get their own styled spans."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_text = agentgrep.highlight_matches("alpha beta alpha gamma", ("alpha", "gamma"))
    styled = [str(span.style) for span in rich_text.spans if "bold yellow" in str(span.style)]
    assert len(styled) == 3  # 2 alpha + 1 gamma


@pytest.mark.slow
async def test_show_detail_memoizes_body_formatting(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-rendering the same record + query reuses the cached body renderable."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    json_body = '{"alpha": 1, "beta": 2, "gamma": 3}'
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "j.jsonl",
        text=json_body,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        # Replace json.loads so a real cache miss would explode loudly.
        load_calls = 0
        real_loads = json.loads

        def counting_loads(*args: t.Any, **kwargs: t.Any) -> t.Any:
            nonlocal load_calls
            load_calls += 1
            return real_loads(*args, **kwargs)

        monkeypatch.setattr(json, "loads", counting_loads)
        app.screen.show_detail(record)
        await pilot.pause()
        assert load_calls == 0, "JSON should not be re-parsed for the same record + query"


@pytest.mark.slow
async def test_reset_search_chrome_invalidates_detail_caches(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starting a new search clears any stale detail-pane caches."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "x.jsonl",
        text='{"x": 1}',
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        assert len(app.screen._detail_body_cache) >= 1
        app.screen._reset_search_chrome()
        assert len(app.screen._detail_body_cache) == 0
        assert len(app.screen._detail_scroll_positions) == 0


@pytest.mark.slow
async def test_detail_scroll_memory(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New records open at the top; revisiting a record restores its scroll."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    big = "\n".join(f"line {i}" for i in range(200))

    def _record(name: str) -> t.Any:
        return agentgrep.SearchRecord(
            kind="prompt",
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=tmp_path / f"{name}.jsonl",
            text=big,
        )

    rec_a, rec_b = _record("a"), _record("b")
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        # A fresh record opens at the top.
        app.screen.show_detail(rec_a)
        await pilot.pause()
        assert app.screen._detail_scroll.scroll_y == 0
        # Scroll down — the position is remembered for rec_a.
        app.screen._detail_scroll.scroll_to(y=20, animate=False)
        await pilot.pause()
        # A different, never-seen record opens at the top.
        app.screen.show_detail(rec_b)
        await pilot.pause()
        assert app.screen._detail_scroll.scroll_y == 0
        # Returning to rec_a restores its remembered scroll.
        app.screen.show_detail(rec_a)
        await pilot.pause()
        assert app.screen._detail_scroll.scroll_y > 0


def test_detect_content_format_recognizes_json() -> None:
    """``detect_content_format`` returns ``"json"`` for parseable JSON objects/arrays."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format('{"a": 1, "b": 2}') == "json"
    assert agentgrep.detect_content_format("[1, 2, 3]") == "json"
    # Whitespace + pretty-printed JSON.
    assert agentgrep.detect_content_format('  {\n  "x": 1\n}') == "json"


def test_detect_content_format_falls_back_to_text_for_malformed_json() -> None:
    """A leading ``{`` that doesn't parse falls through to ``"text"``, not ``"json"``."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format('{"missing": ') == "text"
    assert agentgrep.detect_content_format("{not even json}") == "text"


def test_detect_content_format_falls_back_for_excessive_json_depth() -> None:
    """A deeply nested JSON-looking body cannot overflow format detection."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    nested = "[" * 50000 + "0" + "]" * 1000
    assert agentgrep.detect_content_format(nested) == "text"


def test_detect_content_format_recognizes_markdown() -> None:
    """ATX headings and fenced code blocks at line-start trip markdown mode."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format("# Heading\n\nbody") == "markdown"
    assert agentgrep.detect_content_format("intro\n\n## Subhead\n\nrest") == "markdown"
    assert agentgrep.detect_content_format("intro\n\n```python\nprint(1)\n```") == "markdown"


def test_detect_content_format_leans_false_negative_for_weak_markdown() -> None:
    """Bullet-style or inline-bold chat content is intentionally NOT classified as markdown."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    # A chat message starting with "- " should keep its match highlight.
    assert agentgrep.detect_content_format("- not really markdown") == "text"
    # Inline **bold** alone isn't enough either.
    assert agentgrep.detect_content_format("plain message with **emphasis** inline") == "text"


def test_detect_content_format_handles_empty_and_plain_text() -> None:
    """Empty body and plain chat prose both return ``"text"``."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    assert agentgrep.detect_content_format("") == "text"
    assert agentgrep.detect_content_format("just a plain prompt") == "text"
    assert agentgrep.detect_content_format("multi\nline\nplain\nbody") == "text"


@pytest.mark.slow
async def test_show_detail_renders_json_with_syntax(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON record body produces a ``Syntax`` renderable in the detail Group."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_syntax = importlib.import_module("rich.syntax")
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "json.jsonl",
        text='{"alpha": 1, "beta": "two"}',
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        rendered = _static_content(app.screen._detail)
        renderables = list(rendered.renderables)
        assert any(isinstance(item, rich_syntax.Syntax) for item in renderables)


@pytest.mark.slow
async def test_light_theme_selects_light_syntax_for_detail_renderers(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON, Markdown code, and JSON find share the light Rich syntax theme."""
    from agentgrep.ui import theme as ui_theme
    from agentgrep.ui.layouts import hud

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    json_record = _ui_record(agentgrep, tmp_path / "json.jsonl", '{"alpha": 1}', "json")
    markdown_record = _ui_record(
        agentgrep,
        tmp_path / "markdown.jsonl",
        "# Heading\n\n```json\n{}\n```\n",
        "markdown",
    )
    syntax_themes: list[str] = []
    markdown_themes: list[str] = []
    real_syntax = hud._RichSyntax
    real_markdown = hud._RichMarkdown

    def recording_syntax(*args: t.Any, **kwargs: t.Any) -> t.Any:
        syntax_themes.append(kwargs["theme"])
        return real_syntax(*args, **kwargs)

    def recording_markdown(*args: t.Any, **kwargs: t.Any) -> t.Any:
        markdown_themes.append(kwargs["code_theme"])
        return real_markdown(*args, **kwargs)

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.theme = ui_theme.LIGHT_THEME_NAME
        await pilot.pause()
        monkeypatch.setattr(hud, "_RichSyntax", recording_syntax)
        monkeypatch.setattr(hud, "_RichMarkdown", recording_markdown)

        app.screen.show_detail(json_record)
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.screen._detail_find_base_for(app.screen._detail_find_source)
        app.screen.show_detail(markdown_record)
        await pilot.pause()

    assert syntax_themes == ["ansi_light", "ansi_light"]
    assert markdown_themes == ["ansi_light"]


@pytest.mark.slow
async def test_show_detail_renders_markdown_with_markdown(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A markdown body produces a ``Markdown`` renderable in the detail Group."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_markdown = importlib.import_module("rich.markdown")
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "md.jsonl",
        text="# Heading\n\nbody paragraph\n",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        rendered = _static_content(app.screen._detail)
        renderables = list(rendered.renderables)
        assert any(isinstance(item, rich_markdown.Markdown) for item in renderables)


@pytest.mark.slow
async def test_show_detail_keeps_text_highlighting_for_plain_body(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain bodies still get bounded literal spans for search matches."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_text_module = importlib.import_module("rich.text")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agentgrep, "run_search_query", lambda *args, **kwargs: [])
    query = agentgrep.SearchQuery(
        terms=("libtmux",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    control = agentgrep.SearchControl()
    app = agentgrep.build_streaming_ui_app(home, query, control=control)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "plain.jsonl",
        text="plain prose mentioning libtmux exactly once",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        rendered = _static_content(app.screen._detail)
        renderables = list(rendered.renderables)
        # Two Text instances: the header and the body. The body is the one
        # carrying the highlight spans (header is bold labels only).
        text_bodies = [
            item
            for item in renderables
            if isinstance(item, rich_text_module.Text) and "libtmux" in item.plain
        ]
        assert text_bodies, "expected the body Text containing 'libtmux'"
        styled = [str(span.style) for span in text_bodies[0].spans]
        # Search matches carry the theme's gold foreground token, bold.
        search_hex = app.theme_variables["ag-match-search"]
        assert any("bold" in style and search_hex in style for style in styled)


@pytest.mark.slow
async def test_show_detail_includes_record_origin_without_io(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TUI detail header surfaces origin fields already on the record."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    rich_text_module = importlib.import_module("rich.text")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(agentgrep, "run_search_query", lambda *args, **kwargs: [])
    query = agentgrep.SearchQuery(
        terms=("origin",),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    control = agentgrep.SearchControl()
    app = agentgrep.build_streaming_ui_app(home, query, control=control)
    record = agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=home / ".codex" / "sessions" / "rollout.jsonl",
        text="plain origin detail",
        origin=agentgrep.RecordOrigin(
            cwd=str(home / "work" / "agentgrep"),
            branch="project-context",
        ),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.show_detail(record)
        await pilot.pause()
        rendered = _static_content(app.screen._detail)
        header = next(
            item
            for item in rendered.renderables
            if isinstance(item, rich_text_module.Text) and "Agent:" in item.plain
        )

    assert "Cwd: ~/work/agentgrep/" in header.plain
    assert "Branch: project-context" in header.plain
