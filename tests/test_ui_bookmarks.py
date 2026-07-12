"""Tests for the in-memory bookmark recall modal."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import pathlib
import threading
import typing as t

import pytest
from textual.app import App
from textual.widgets import Input, OptionList, Static

import agentgrep.bookmarks as bookmarks
from agentgrep.bookmarks import BookmarkEntry, BookmarkMutation, BookmarkStore
from agentgrep.identity import record_identity
from agentgrep.progress import SearchControl, StreamingRecordsBatch, StreamingSearchFinished
from agentgrep.records import AGENT_CHOICES, RecordPosition, SearchQuery, SearchRecord

pytestmark = [pytest.mark.tui, pytest.mark.slow]

_CONTENT_ID = "agc1:00000000000000000000000000"
_RECORD_ID = "agr1:11111111111111111111111111"
_THREAD_ID = "agt1:22222222222222222222222222"
_MISSING_ID = "agc1:33333333333333333333333333"
_CREATED_AT = "2026-07-12T12:00:00Z"


def _bookmark_widgets() -> tuple[type[t.Any], type[t.Any]]:
    """Import the bookmark widget API lazily for modal and HUD tests."""
    from agentgrep.ui.widgets.bookmarks import BookmarkChoice, BookmarkRecall

    return BookmarkChoice, BookmarkRecall


def _record(
    *,
    suffix: str = "resolved",
    text: str = "calm bookmark preview\nsecond line",
    title: str | None = "calm bookmark preview",
    native: bool = True,
) -> SearchRecord:
    """Return one synthetic resolved modal record."""
    return SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=pathlib.Path(f"synthetic-{suffix}.jsonl"),
        text=text,
        title=title,
        session_id=f"session-{suffix}" if native else None,
        conversation_id=f"session-{suffix}" if native else None,
        identity_namespace="codex.session" if native else None,
        position=(
            RecordPosition(native_id=f"message-{suffix}", quality="native") if native else None
        ),
    )


def _choices() -> list[t.Any]:
    """Return resolved and unresolved choices in stable display order."""
    bookmark_choice, _bookmark_recall = _bookmark_widgets()
    return [
        bookmark_choice(
            BookmarkEntry(_RECORD_ID, "record", _CONTENT_ID, _CREATED_AT),
            _record(),
        ),
        bookmark_choice(
            BookmarkEntry(_THREAD_ID, "thread", None, _CREATED_AT),
            _record(),
        ),
        bookmark_choice(
            BookmarkEntry(_MISSING_ID, "content", None, _CREATED_AT),
            None,
        ),
    ]


class _BookmarkHostApp(App[None]):
    """Minimal host that pushes bookmark recall and captures dismissal."""

    def __init__(self, choices: list[t.Any]) -> None:
        super().__init__()
        self._choices = choices
        self.result: object = "UNSET"

    def on_mount(self) -> None:
        _bookmark_choice, bookmark_recall = _bookmark_widgets()
        self.push_screen(bookmark_recall(self._choices), self._capture)

    def _capture(self, value: object) -> None:
        self.result = value


def _status_text(app: App[None]) -> str:
    """Return the bookmark modal's one-line preview/status text."""
    status = app.screen.query_one("#bookmark-preview", Static)
    content = getattr(status, "_Static__content", "")
    return getattr(content, "plain", str(content))


async def test_modal_empty_bookmarks_shows_hint_and_accepts_none() -> None:
    """An empty store shows one disabled hint row and Enter dismisses ``None``."""
    app = _BookmarkHostApp([])
    async with app.run_test() as pilot:
        await pilot.pause()
        option_list = app.screen.query_one("#bookmark-list", OptionList)
        assert option_list.option_count == 1
        assert "No bookmarks yet" in str(option_list.get_option_at_index(0).prompt)
        await pilot.press("enter")
        await pilot.pause()
        assert app.result is None


async def test_modal_rows_distinguish_resolved_and_unresolved() -> None:
    """Rows show canonical targets and status without inventing unresolved paths."""
    app = _BookmarkHostApp(_choices())
    async with app.run_test() as pilot:
        await pilot.pause()
        option_list = app.screen.query_one("#bookmark-list", OptionList)
        rows = [
            getattr(option_list.get_option_at_index(index).prompt, "plain", "")
            for index in range(3)
        ]
        assert _RECORD_ID in rows[0]
        assert "resolved" in rows[0].lower()
        assert _MISSING_ID in rows[2]
        assert "unresolved" in rows[2].lower()
        assert "synthetic.jsonl" not in rows[2]


async def test_modal_filter_narrows_then_accepts() -> None:
    """Typing filters in memory and Enter accepts the surviving choice."""
    choices = _choices()
    app = _BookmarkHostApp(choices)
    async with app.run_test() as pilot:
        await pilot.pause()
        for char in "thread":
            await pilot.press(char)
        await pilot.pause()
        assert app.screen.query_one("#bookmark-filter", Input).value == "thread"
        assert app.screen.query_one("#bookmark-list", OptionList).option_count == 1
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == choices[1]


async def test_modal_navigation_updates_one_line_preview() -> None:
    """Arrow navigation moves the list while the filter retains focus."""
    app = _BookmarkHostApp(_choices())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "calm bookmark preview" in _status_text(app)
        await pilot.press("end")
        await pilot.pause()
        assert app.screen.query_one("#bookmark-list", OptionList).highlighted == 2
        status = _status_text(app)
        assert _MISSING_ID in status
        assert "unavailable" in status.lower()
        assert "\n" not in status


async def test_modal_escape_cancels() -> None:
    """Escape dismisses with ``None`` and never accepts the highlighted row."""
    app = _BookmarkHostApp(_choices())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.result is None


async def test_modal_option_selection_returns_choice() -> None:
    """Selecting a list option returns the exact resolved choice object."""
    choices = _choices()
    app = _BookmarkHostApp(choices)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert app.result is choices[1]


def test_modal_bounds_large_single_line_preview_and_filter_text() -> None:
    """Modal pump work slices a huge body/title before newline search or casefold."""
    bookmark_choice, bookmark_recall = _bookmark_widgets()
    huge = "x" * 1_000_000
    for title in (huge, None):
        record = _record(text=huge, title=title)
        choice = bookmark_choice(
            BookmarkEntry(_RECORD_ID, "record", _CONTENT_ID, _CREATED_AT),
            record,
        )
        modal = bookmark_recall([choice])

        assert len(modal._search_text(choice)) <= len("record  ") + len(_RECORD_ID) + 160
        assert len(modal._preview(choice)) <= len("Ready · ") + 160


class _NoopInvoker:
    """Search seam fake used by HUD tests that do not resolve bookmarks."""

    def run(
        self,
        query: SearchQuery,
        *,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        del query, control, emit


class _ResolutionInvoker:
    """Search seam fake that streams a captured candidate batch."""

    def __init__(self, records: cabc.Sequence[SearchRecord]) -> None:
        self.records = tuple(records)
        self.queries: list[SearchQuery] = []
        self.controls: list[SearchControl] = []
        self.thread_ids: list[int] = []

    def run(
        self,
        query: SearchQuery,
        *,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        from agentgrep.ui import _runtime

        _runtime.assert_off_pump("bookmark resolver invoker")
        self.queries.append(query)
        self.controls.append(control)
        self.thread_ids.append(threading.get_ident())
        emit(StreamingRecordsBatch(records=self.records, total=len(self.records)))
        emit(
            StreamingSearchFinished(
                outcome="interrupted" if control.answer_now_requested() else "complete",
                total=len(self.records),
                elapsed=0.01,
            ),
        )


def _bookmark_app(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    invoker: object | None = None,
) -> t.Any:
    """Build an isolated HUD with an injectable search seam."""
    from agentgrep.ui import registry
    from agentgrep.ui._context import UiContext
    from agentgrep.ui._shell import ExplorerApp

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
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
            invoker=t.cast("t.Any", invoker or _NoopInvoker()),
            query=query,
            control=SearchControl(),
            base_scope=query.scope,
        ),
        composition=registry._UiComposition(
            layout_type=layout_spec.loader(),
            workflow_type=workflow_spec.loader(),
        ),
    )


async def _settle_workers(app: t.Any, pilot: t.Any) -> None:
    """Wait for Textual workers and their pump-side callbacks."""
    await app.workers.wait_for_complete()
    await pilot.pause()


def _seed_record(screen: t.Any, record: SearchRecord) -> None:
    """Install one selected record without replacing HUD result lists."""
    screen.all_records.append(record)
    screen.filtered_records.append(record)
    screen._results.append_records((record,))
    screen._results.highlighted = 0


async def test_bookmark_store_load_runs_off_pump(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot read and JSON validation happen in a bookmark-load worker."""
    from agentgrep.ui import _runtime

    entry = BookmarkEntry(_THREAD_ID, "thread", None, _CREATED_AT)
    calls: list[int] = []

    def guarded_list(_store: BookmarkStore) -> list[BookmarkEntry]:
        _runtime.assert_off_pump("bookmark store load")
        calls.append(threading.get_ident())
        return [entry]

    monkeypatch.setattr(BookmarkStore, "list", guarded_list)
    app = _bookmark_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        pump_thread = threading.get_ident()
        await _settle_workers(app, pilot)
        assert calls and all(thread_id != pump_thread for thread_id in calls)
        assert app.screen._bookmarks_loaded is True
        assert app.screen._bookmarked_ids == {_THREAD_ID}


async def test_toggle_hash_and_transaction_run_off_pump(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity preparation and the complete toggle transaction share one worker."""
    from agentgrep.ui import _runtime

    record = _record()
    prepared = record_identity(record)
    calls: list[tuple[str, int]] = []
    real_entry = bookmarks.bookmark_entry_for_record

    def guarded_entry(
        candidate: SearchRecord,
        *,
        scope: bookmarks.BookmarkScope = "record",
        created_at: str | None = None,
    ) -> BookmarkEntry:
        _runtime.assert_off_pump("bookmark identity")
        calls.append(("identity", threading.get_ident()))
        return real_entry(candidate, scope=scope, created_at=created_at)

    def guarded_toggle(_store: BookmarkStore, entry: BookmarkEntry) -> BookmarkMutation:
        _runtime.assert_off_pump("bookmark transaction")
        calls.append(("transaction", threading.get_ident()))
        return BookmarkMutation("added", entry)

    monkeypatch.setattr(bookmarks, "bookmark_entry_for_record", guarded_entry)
    monkeypatch.setattr(BookmarkStore, "toggle", guarded_toggle)
    app = _bookmark_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle_workers(app, pilot)
        pump_thread = threading.get_ident()
        _seed_record(app.screen, record)
        app.screen.toggle_bookmark("record")
        await _settle_workers(app, pilot)

        assert [name for name, _thread in calls] == ["identity", "transaction"]
        assert all(thread_id != pump_thread for _name, thread_id in calls)
        assert prepared.record_id in app.screen._bookmarked_ids
        assert app.screen._bookmark_write_pending is False


async def test_loaded_snapshot_refreshes_star_from_cached_identity(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late load repaints the current header without hashing again on the pump."""
    import agentgrep.identity as identity
    from agentgrep.ui.layouts import hud

    record = _record(suffix="late-load")
    prepared = record_identity(record)
    assert prepared.record_id is not None
    entry = BookmarkEntry(prepared.record_id, "record", prepared.content_id, _CREATED_AT)
    app = _bookmark_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle_workers(app, pilot)
        _seed_record(app.screen, record)
        app.screen.show_detail(record)
        await _settle_workers(app, pilot)
        monkeypatch.setattr(
            identity,
            "record_identity",
            lambda _record: pytest.fail("load callback must reuse the identity cache"),
        )

        app.screen._apply_loaded_bookmarks(
            app.screen._bookmark_load_generation,
            hud._LoadedBookmarks(entries=(entry,), error=None),
        )

        live_header = next(
            renderable
            for renderable in app.screen._detail.content.renderables
            if hasattr(renderable, "plain") and "Agent:" in renderable.plain
        )
        assert f"Record: ★ {prepared.record_id}" in live_header.plain


async def test_rapid_double_b_accepts_one_mutation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pending gate serializes rapid bookmark toggles without supersession."""
    record = _record()
    spawned: list[tuple[t.Callable[[], None], dict[str, object]]] = []
    app = _bookmark_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle_workers(app, pilot)
        _seed_record(app.screen, record)
        app.screen._results.focus()
        await pilot.pause()

        real_run_worker = app.screen.run_worker

        def capture(target: t.Callable[[], None], **kwargs: object) -> object:
            if kwargs.get("group") == "bookmark-write":
                spawned.append((target, kwargs))
                return None
            return real_run_worker(target, **kwargs)

        monkeypatch.setattr(app.screen, "run_worker", capture)
        await pilot.press("b")
        await pilot.press("b")
        await pilot.pause()

        assert len(spawned) == 1
        assert spawned[0][1] == {
            "name": "bookmark-write",
            "group": "bookmark-write",
            "thread": True,
            "exclusive": True,
        }
        assert app.screen._bookmark_write_pending is True


async def test_b_binding_is_focus_safe_for_search_input(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A focused input receives ``b`` as text instead of toggling a record."""
    app = _bookmark_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle_workers(app, pilot)
        calls: list[str] = []
        monkeypatch.setattr(app.screen, "toggle_bookmark", calls.append)
        app.screen._search_input.focus()
        await pilot.press("b")
        await pilot.pause()
        assert app.screen._search_input.value == "b"
        assert calls == []


async def test_b_from_detail_toggles_the_visible_recalled_record(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detail-owned ``b`` targets its visible record, not an old result cursor."""
    first = _record(suffix="result")
    recalled = _record(suffix="recalled")
    spawned: list[t.Callable[[], None]] = []
    app = _bookmark_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle_workers(app, pilot)
        _seed_record(app.screen, first)
        await pilot.pause()
        app.screen.show_detail(recalled)
        app.screen._detail_scroll.focus()
        monkeypatch.setattr(
            app.screen,
            "run_worker",
            lambda target, **_kwargs: spawned.append(target),
        )

        await pilot.press("b")
        await pilot.pause()

        assert len(spawned) == 1
        assert t.cast("t.Any", spawned[0]).args[1] is recalled


@pytest.mark.parametrize("scope", ["record", "thread"])
async def test_toggle_reports_null_identity_without_transaction(
    scope: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Record/thread scopes with null handles fail safely before storage mutation."""
    record = _record(native=False)
    transactions: list[BookmarkEntry] = []
    notes: list[str] = []
    monkeypatch.setattr(
        BookmarkStore,
        "toggle",
        lambda _store, entry: transactions.append(entry),
    )
    app = _bookmark_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle_workers(app, pilot)
        _seed_record(app.screen, record)
        monkeypatch.setattr(app.screen, "notify", lambda message, **_kwargs: notes.append(message))
        app.screen.toggle_bookmark(scope)
        await _settle_workers(app, pilot)
        assert transactions == []
        assert notes and "identity" in notes[-1].lower()
        assert str(record.path) not in notes[-1]
        assert record.text not in notes[-1]


async def test_toggle_without_selection_is_path_free(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bookmark command with no selected result reports a generic prompt."""
    app = _bookmark_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle_workers(app, pilot)
        notes: list[str] = []
        monkeypatch.setattr(app.screen, "notify", lambda message, **_kwargs: notes.append(message))
        app.screen.toggle_bookmark("record")
        assert notes == ["Select a record to bookmark."]


async def test_record_switch_during_mutation_does_not_repaint_old_record(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted mutation updates canonical state but not a newer selection."""
    first = _record(suffix="first")
    second = _record(suffix="second")
    first_identity = record_identity(first)
    second_identity = record_identity(second)
    app = _bookmark_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle_workers(app, pilot)
        _seed_record(app.screen, first)
        app.screen.all_records.append(second)
        app.screen.filtered_records.append(second)
        app.screen._results.append_records((second,))
        await pilot.pause()
        spawned: list[tuple[t.Callable[[], None], dict[str, object]]] = []
        monkeypatch.setattr(
            app.screen,
            "run_worker",
            lambda target, **kwargs: spawned.append((target, kwargs)),
        )

        app.screen.show_detail(first)
        app.screen.toggle_bookmark("record")
        app.screen._results.highlighted = 1
        await pilot.pause()
        bookmark_workers = [
            target for target, kwargs in spawned if kwargs.get("group") == "bookmark-write"
        ]
        assert len(bookmark_workers) == 1
        assert app.screen._current_detail_record is second
        await asyncio.to_thread(bookmark_workers[0])
        await pilot.pause()

        assert app.screen._current_detail_record is second
        assert first_identity.record_id in app.screen._bookmarked_ids
        header = app.screen._build_detail_header(second, second_identity, width=120).plain
        assert "★" not in header


async def test_resolution_uses_scope_all_and_stops_when_targets_resolve(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolution searches all stores, hashes off-pump, and cancels after a hit."""
    import agentgrep.identity as identity
    from agentgrep.ui import _runtime

    target = _record(suffix="target")
    prepared = record_identity(target)
    assert prepared.record_id is not None
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    BookmarkStore().add(
        prepared.record_id,
        content_id=prepared.content_id,
        created_at=_CREATED_AT,
    )
    many = [target, *(_record(suffix=f"extra-{index}") for index in range(1_000))]
    invoker = _ResolutionInvoker(many)
    calls: list[tuple[SearchRecord, int]] = []
    original = identity.record_identity

    def guarded_identity(record: SearchRecord) -> identity.RecordIdentity:
        _runtime.assert_off_pump("bookmark resolution identity")
        calls.append((record, threading.get_ident()))
        return original(record)

    monkeypatch.setattr(identity, "record_identity", guarded_identity)
    app = _bookmark_app(tmp_path, monkeypatch, invoker=invoker)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle_workers(app, pilot)
        app.screen.open_bookmarks()
        await _settle_workers(app, pilot)

        assert len(invoker.queries) == 1
        query = invoker.queries[0]
        assert query.scope == "all"
        assert query.agents == AGENT_CHOICES
        assert query.terms == ()
        assert invoker.controls[0].answer_now_requested() is True
        assert [record for record, _thread in calls] == [target]
        assert all(thread_id != threading.get_ident() for _record, thread_id in calls)
        _bookmark_choice, bookmark_recall = _bookmark_widgets()
        assert isinstance(app.screen, bookmark_recall)


async def test_record_resolution_requires_matching_content_validation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact-record target never resolves when its stored content ID differs."""
    _bookmark_choice, bookmark_recall = _bookmark_widgets()
    candidate = _record(suffix="validation")
    prepared = record_identity(candidate)
    assert prepared.record_id is not None
    assert prepared.content_id != _CONTENT_ID
    entry = BookmarkEntry(prepared.record_id, "record", _CONTENT_ID, _CREATED_AT)
    invoker = _ResolutionInvoker((candidate,))
    app = _bookmark_app(tmp_path, monkeypatch, invoker=invoker)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle_workers(app, pilot)
        app.screen._bookmark_entries = [entry]
        app.screen._bookmarked_ids = {entry.target_id}
        app.screen.open_bookmarks()
        await _settle_workers(app, pilot)

        assert isinstance(app.screen, bookmark_recall)
        assert app.screen._matches[0].entry is entry
        assert app.screen._matches[0].record is None


async def test_resolved_choice_preserves_current_result_lists(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reopening a bookmark only presents detail; loaded search lists stay intact."""
    bookmark_choice, _bookmark_recall = _bookmark_widgets()
    first = _record(suffix="first")
    recalled = _record(suffix="recalled")
    prepared = record_identity(recalled)
    assert prepared.record_id is not None
    choice = bookmark_choice(
        BookmarkEntry(prepared.record_id, "record", prepared.content_id, _CREATED_AT),
        recalled,
    )
    app = _bookmark_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle_workers(app, pilot)
        _seed_record(app.screen, first)
        await pilot.pause()
        all_records = app.screen.all_records
        filtered_records = app.screen.filtered_records
        all_before = list(all_records)
        filtered_before = list(filtered_records)

        app.screen._apply_bookmark_choice(choice)
        await pilot.pause()

        assert app.screen.all_records is all_records
        assert app.screen.filtered_records is filtered_records
        assert app.screen.all_records == all_before
        assert app.screen.filtered_records == filtered_before
        assert app.screen._current_detail_record is recalled


async def test_unresolved_choice_notification_contains_only_target(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolved selection reports its canonical target and a generic reason."""
    bookmark_choice, _bookmark_recall = _bookmark_widgets()
    choice = bookmark_choice(
        BookmarkEntry(_MISSING_ID, "content", None, _CREATED_AT),
        None,
    )
    app = _bookmark_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle_workers(app, pilot)
        notes: list[str] = []
        monkeypatch.setattr(app.screen, "notify", lambda message, **_kwargs: notes.append(message))
        app.screen._apply_bookmark_choice(choice)
        assert notes == [f"{_MISSING_ID} is unavailable in the current stores."]


async def test_stale_resolution_and_empty_stack_callbacks_are_ignored(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the live resolver generation may push a modal, including at teardown."""
    from agentgrep.ui.layouts import hud

    app = _bookmark_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle_workers(app, pilot)
        screen = app.screen
        pushes: list[object] = []
        monkeypatch.setattr(app, "push_screen", lambda *args, **_kwargs: pushes.append(args))
        screen._bookmark_resolution_generation = 4
        payload = hud._BookmarkResolution(choices=(), error=None)

        screen._apply_bookmark_resolution(3, payload)
        assert pushes == []

        stack = app._screen_stacks[app.current_mode]
        saved = list(stack)
        stack.clear()
        try:
            screen._apply_bookmark_resolution(4, payload)
        finally:
            stack.extend(saved)
        assert pushes == []


async def test_large_body_toggle_keeps_identity_marker_single_line(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large record toggles off-pump and the starred record handle stays no-wrap."""
    from rich.console import Console

    record = _record(text="x" * 100_000)
    prepared = record_identity(record)
    assert prepared.record_id is not None
    app = _bookmark_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle_workers(app, pilot)
        _seed_record(app.screen, record)
        app.screen.show_detail(record)
        app.screen.toggle_bookmark("record")
        await _settle_workers(app, pilot)

        live_header = next(
            renderable
            for renderable in app.screen._detail.content.renderables
            if hasattr(renderable, "plain") and "Agent:" in renderable.plain
        )
        assert f"Record: ★ {prepared.record_id}" in live_header.plain
        header = app.screen._build_detail_header(record, prepared, width=40)
        lines = [line.plain for line in header.wrap(Console(), 38)]
        record_lines = [line for line in lines if line.startswith("R:")]
        assert record_lines == [f"R: ★ {prepared.record_id}"]
