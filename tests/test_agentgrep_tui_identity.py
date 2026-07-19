"""Identity rendering and cache tests for the legacy Textual detail pane."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import pathlib
import threading
import typing as t

import pytest

import agentgrep as _agentgrep_module
from agentgrep.identity import record_identity

pytestmark = pytest.mark.tui


def load_agentgrep_module() -> object:
    """Return the installed ``agentgrep`` package."""
    return _agentgrep_module


def _identity_ui_record(
    agentgrep: t.Any,
    path: pathlib.Path,
    text: str,
    *,
    native: bool = True,
) -> t.Any:
    """Build one detail record with either native or nullable identity."""
    from agentgrep.records import RecordPosition

    return agentgrep.SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        text=text,
        session_id="session-1" if native else None,
        conversation_id="session-1" if native else None,
        identity_namespace="codex.session" if native else None,
        position=(RecordPosition(native_id="message-1", quality="native") if native else None),
    )


def _build_empty_ui_app(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> t.Any:
    """Build a streaming UI app with its search worker stubbed."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(agentgrep, "run_search_query", lambda *args, **kwargs: [])
    query = agentgrep.SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=("codex",),
        limit=None,
    )
    return agentgrep.build_streaming_ui_app(
        home,
        query,
        control=agentgrep.SearchControl(),
    )


@contextlib.asynccontextmanager
async def _mounted_detail_app(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> t.AsyncIterator[tuple[t.Any, t.Any]]:
    """Yield one mounted wide HUD and its Pilot."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        yield app, pilot


def _detail_header_renderable(screen: t.Any) -> t.Any:
    """Return the current Rich detail-header text."""
    return next(
        renderable
        for renderable in screen._detail.content.renderables
        if hasattr(renderable, "plain") and "Agent:" in renderable.plain
    )


def _text_range_has_style(text: t.Any, value: str, style_fragment: str) -> bool:
    """Return whether one exact text range carries ``style_fragment``."""
    start = text.plain.index(value)
    end = start + len(value)
    return any(
        span.start <= start and span.end >= end and style_fragment in str(span.style)
        for span in text.spans
    )


async def _drain_detail_workers(app: t.Any, pilot: t.Any) -> None:
    """Wait for detail preparation and let its pump apply settle."""
    await app.workers.wait_for_complete()
    await pilot.pause()


class DetailIdentityBodyCase(t.NamedTuple):
    """One bounded or worker-built body used by detail identity tests."""

    test_id: str
    body: str


DETAIL_IDENTITY_BODY_CASES: tuple[DetailIdentityBodyCase, ...] = (
    DetailIdentityBodyCase("small", "serenity and bliss"),
    DetailIdentityBodyCase("large", "x" * 21_000),
)


@pytest.mark.parametrize(
    "case",
    DETAIL_IDENTITY_BODY_CASES,
    ids=[case.test_id for case in DETAIL_IDENTITY_BODY_CASES],
)
@pytest.mark.slow
async def test_detail_identity_handles_prepare_off_pump(
    case: DetailIdentityBodyCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Small and large detail records hash off-pump and expose exact handles."""
    import agentgrep.identity as identity

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    record = _identity_ui_record(agentgrep, tmp_path / f"{case.test_id}.jsonl", case.body)
    expected = identity.record_identity(record)
    call_threads: list[int] = []
    original = identity.record_identity

    def guarded_identity(value: object) -> identity.RecordIdentity:
        call_threads.append(threading.get_ident())
        return original(t.cast("t.Any", value))

    monkeypatch.setattr(identity, "record_identity", guarded_identity)

    async with _mounted_detail_app(tmp_path, monkeypatch) as (app, pilot):
        pump_thread = threading.get_ident()
        app.screen.show_detail(record)

        pending = _detail_header_renderable(app.screen)
        pending_lines = pending.plain.splitlines()
        assert pending_lines[4:7] == ["Record: …", "Content: …", "Thread: …"]
        for label in ("Record:", "Content:", "Thread:"):
            assert _text_range_has_style(pending, label, "dim")
        assert _text_range_has_style(pending, "…", "dim")

        await _drain_detail_workers(app, pilot)

        header = _detail_header_renderable(app.screen)
        lines = header.plain.splitlines()
        assert lines[3:8] == [
            "Adapter: codex.sessions_jsonl.v1",
            f"Record: {expected.record_id}",
            f"Content: {expected.content_id}",
            f"Thread: {expected.thread_id}",
            "Timestamp: unknown",
        ]
        assert len(call_threads) == 1
        assert call_threads[0] != pump_thread


@pytest.mark.slow
async def test_detail_identity_handles_do_not_wrap_in_narrow_hud(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Narrow detail rows keep each complete identity on one visual line."""
    from rich.console import Console

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    record = _identity_ui_record(agentgrep, tmp_path / "narrow.jsonl", "serenity")
    expected = record_identity(record)

    async with _mounted_detail_app(tmp_path, monkeypatch) as (app, pilot):
        app.screen.show_detail(record)
        await _drain_detail_workers(app, pilot)
        original_renderables = tuple(app.screen._detail.content.renderables)
        original_body = original_renderables[1]
        original_generation = app.screen._detail_generation

        await pilot.resize_terminal(40, 30)
        await pilot.pause(0.1)

        # A 40-column pane leaves 38 content cells after #detail's horizontal
        # padding. Render at that effective width to reproduce Textual's wrap.
        content_width = 38
        header = _detail_header_renderable(app.screen)
        lines = [line.plain for line in header.wrap(Console(), content_width)]
        assert lines[4:7] == [
            f"R: {expected.record_id}",
            f"C: {expected.content_id}",
            f"T: {expected.thread_id}",
        ]
        assert tuple(app.screen._detail.content.renderables)[1] is original_body
        assert app.screen._detail_generation == original_generation

        await pilot.resize_terminal(120, 30)
        await pilot.pause(0.1)
        wide_lines = _detail_header_renderable(app.screen).plain.splitlines()
        assert wide_lines[4:7] == [
            f"Record: {expected.record_id}",
            f"Content: {expected.content_id}",
            f"Thread: {expected.thread_id}",
        ]


@pytest.mark.slow
async def test_detail_identity_null_handles_use_em_dash(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threadless details keep content identity and render nullable handles as dashes."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    record = _identity_ui_record(
        agentgrep,
        tmp_path / "threadless.jsonl",
        "positionless serenity",
        native=False,
    )
    expected = record_identity(record)

    async with _mounted_detail_app(tmp_path, monkeypatch) as (app, pilot):
        app.screen.show_detail(record)
        await _drain_detail_workers(app, pilot)

        lines = _detail_header_renderable(app.screen).plain.splitlines()
        assert lines[4:7] == [
            "Record: —",
            f"Content: {expected.content_id}",
            "Thread: —",
        ]


def test_detail_find_visual_row_does_not_wrap_header() -> None:
    """No-wrap metadata contributes logical rows, even when a value is long."""
    from agentgrep.ui.layouts.hud import HudLayout

    header = f"Branch: {'x' * 100}\n"

    assert HudLayout._wrap_aware_row(0, 38, header, "needle") == 1


class DetailWorkerMatrixCase(t.NamedTuple):
    """One identity/body cache topology and expected detail-worker count."""

    test_id: str
    identity_cached: bool
    large_body: bool
    expected_workers: int


DETAIL_WORKER_MATRIX_CASES: tuple[DetailWorkerMatrixCase, ...] = (
    DetailWorkerMatrixCase("cached-small", True, False, 0),
    DetailWorkerMatrixCase("identity-only", False, False, 1),
    DetailWorkerMatrixCase("body-only", True, True, 1),
    DetailWorkerMatrixCase("combined", False, True, 1),
)


@pytest.mark.parametrize(
    "case",
    DETAIL_WORKER_MATRIX_CASES,
    ids=[case.test_id for case in DETAIL_WORKER_MATRIX_CASES],
)
@pytest.mark.slow
async def test_detail_uses_exact_one_worker_matrix(
    case: DetailWorkerMatrixCase,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity/body cache topology launches at most one stable detail worker."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    body = "x" * 21_000 if case.large_body else "small detail body"
    record = _identity_ui_record(agentgrep, tmp_path / f"{case.test_id}.jsonl", body)
    prepared = record_identity(record)
    spawned: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async with _mounted_detail_app(tmp_path, monkeypatch) as (app, _pilot):
        if case.identity_cached:
            cache = collections.OrderedDict({id(record): (record, prepared)})
            app.screen._detail_identity_cache = cache

        def capture_worker(*args: object, **kwargs: object) -> None:
            spawned.append((args, kwargs))

        monkeypatch.setattr(app.screen, "run_worker", capture_worker)
        app.screen.show_detail(record)

        assert len(spawned) == case.expected_workers
        if spawned:
            _args, kwargs = spawned[0]
            assert kwargs == {
                "name": "detail",
                "group": "detail",
                "description": "prepare record detail",
                "thread": True,
                "exclusive": True,
            }


@pytest.mark.slow
async def test_detail_cached_fast_path_cancels_and_advances_generation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every selection and reset invalidates draining detail workers before fast paths."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    record = _identity_ui_record(agentgrep, tmp_path / "cached.jsonl", "cached body")
    prepared = record_identity(record)
    cancellations: list[tuple[object, str]] = []
    spawned: list[object] = []

    async with _mounted_detail_app(tmp_path, monkeypatch) as (app, _pilot):
        app.screen._detail_identity_cache = collections.OrderedDict(
            {id(record): (record, prepared)},
        )
        before = getattr(app.screen, "_detail_generation", 0)
        monkeypatch.setattr(
            app.workers,
            "cancel_group",
            lambda node, group: cancellations.append((node, group)) or [],
        )
        monkeypatch.setattr(
            app.screen,
            "run_worker",
            lambda *args, **kwargs: spawned.append((args, kwargs)),
        )

        app.screen.show_detail(record)

        assert cancellations == [(app.screen, "detail")]
        assert getattr(app.screen, "_detail_generation", 0) == before + 1
        assert spawned == []

        app.screen._reset_search_chrome()

        assert cancellations == [(app.screen, "detail"), (app.screen, "detail")]
        assert getattr(app.screen, "_detail_generation", 0) == before + 2
        assert app.screen._detail_identity_cache == collections.OrderedDict()


@pytest.mark.slow
async def test_detail_identity_only_apply_preserves_find_body_object(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity completion swaps only the header and retains the live find body."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    record = _identity_ui_record(
        agentgrep,
        tmp_path / "find.jsonl",
        "needle before needle after",
    )
    spawned: list[t.Callable[[], None]] = []

    async with _mounted_detail_app(tmp_path, monkeypatch) as (app, pilot):

        def capture_worker(target: t.Callable[[], None], **_kwargs: object) -> None:
            spawned.append(target)

        monkeypatch.setattr(app.screen, "run_worker", capture_worker)
        app.screen.show_detail(record)
        assert len(spawned) == 1
        app.screen.action_open_detail_find()
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        before = list(app.screen._detail.content.renderables)[1]

        await asyncio.to_thread(spawned[0])
        await pilot.pause()

        after = list(app.screen._detail.content.renderables)[1]
        assert after is before
        assert f"Content: {record_identity(record).content_id}" in (
            _detail_header_renderable(app.screen).plain
        )


@pytest.mark.slow
async def test_detail_generation_rejects_a_to_b_to_a_stale_result(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old A worker cannot repaint a newer A selection after A-to-B-to-A."""
    import agentgrep.identity as identity

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    body = "x" * 21_000
    first = _identity_ui_record(agentgrep, tmp_path / "a.jsonl", body)
    second = _identity_ui_record(agentgrep, tmp_path / "b.jsonl", body)
    base = identity.record_identity(first)
    fresh = dataclasses.replace(base, content_id="agc1:" + ("n" * 26))
    stale = dataclasses.replace(base, content_id="agc1:" + ("o" * 26))
    prepared = iter((fresh, stale))
    spawned: list[t.Callable[[], None]] = []

    monkeypatch.setattr(identity, "record_identity", lambda _record: next(prepared))

    async with _mounted_detail_app(tmp_path, monkeypatch) as (app, pilot):
        monkeypatch.setattr(
            app.screen,
            "run_worker",
            lambda target, **_kwargs: spawned.append(target),
        )

        app.screen.show_detail(first)
        app.screen.show_detail(second)
        app.screen.show_detail(first)
        assert len(spawned) == 3

        await asyncio.to_thread(spawned[2])
        await pilot.pause()
        await asyncio.to_thread(spawned[0])
        await pilot.pause()

        header = _detail_header_renderable(app.screen).plain
        assert f"Content: {fresh.content_id}" in header
        assert stale.content_id not in header


@pytest.mark.slow
async def test_detail_worker_does_not_mutate_caches_before_gated_apply(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Combined worker returns prepared data; pump apply owns both cache writes."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    record = _identity_ui_record(agentgrep, tmp_path / "large.jsonl", "x" * 21_000)
    spawned: list[t.Callable[[], None]] = []
    scheduled: list[tuple[t.Callable[..., None], tuple[object, ...]]] = []

    async with _mounted_detail_app(tmp_path, monkeypatch) as (app, _pilot):
        monkeypatch.setattr(
            app.screen,
            "run_worker",
            lambda target, **_kwargs: spawned.append(target),
        )
        monkeypatch.setattr(
            app,
            "call_from_thread",
            lambda callback, *args: scheduled.append((callback, args)),
        )

        app.screen.show_detail(record)
        assert len(spawned) == 1
        assert app.screen._detail_body_cache == collections.OrderedDict()
        assert getattr(app.screen, "_detail_identity_cache", {}) == collections.OrderedDict()

        await asyncio.to_thread(spawned[0])

        assert app.screen._detail_body_cache == collections.OrderedDict()
        assert getattr(app.screen, "_detail_identity_cache", {}) == collections.OrderedDict()
        assert len(scheduled) == 1

        callback, args = scheduled[0]
        callback(*args)

        assert len(app.screen._detail_body_cache) == 1
        assert app.screen._detail_identity_cache[id(record)][0] is record


@pytest.mark.slow
async def test_detail_identity_cache_rejects_same_key_for_different_record(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching integer key is not a hit unless the retained record is identical."""
    import agentgrep.identity as identity

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    selected = _identity_ui_record(agentgrep, tmp_path / "selected.jsonl", "selected")
    other = _identity_ui_record(agentgrep, tmp_path / "other.jsonl", "other")
    other_identity = identity.record_identity(other)
    calls: list[object] = []
    original = identity.record_identity

    def count_identity(record: object) -> identity.RecordIdentity:
        calls.append(record)
        return original(t.cast("t.Any", record))

    monkeypatch.setattr(identity, "record_identity", count_identity)

    async with _mounted_detail_app(tmp_path, monkeypatch) as (app, pilot):
        app.screen._detail_identity_cache = collections.OrderedDict(
            {id(selected): (other, other_identity)},
        )
        app.screen.show_detail(selected)
        await _drain_detail_workers(app, pilot)

        cached_record, cached_identity = app.screen._detail_identity_cache[id(selected)]
        assert calls == [selected]
        assert cached_record is selected
        assert cached_identity == original(selected)


@pytest.mark.slow
async def test_detail_identity_cache_evicts_oldest_at_exact_cap(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retained-record identity cache stays capped and evicts its oldest entry."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    selected = _identity_ui_record(agentgrep, tmp_path / "selected.jsonl", "selected")
    seed_identity = record_identity(selected)

    async with _mounted_detail_app(tmp_path, monkeypatch) as (app, pilot):
        assert app.screen._DETAIL_CACHE_MAX == 1024
        retained = [
            _identity_ui_record(agentgrep, tmp_path / f"seed-{index}.jsonl", str(index))
            for index in range(app.screen._DETAIL_CACHE_MAX)
        ]
        oldest_key = id(retained[0])
        app.screen._detail_identity_cache = collections.OrderedDict(
            (id(record), (record, seed_identity)) for record in retained
        )

        app.screen.show_detail(selected)
        await _drain_detail_workers(app, pilot)

        assert len(app.screen._detail_identity_cache) == app.screen._DETAIL_CACHE_MAX
        assert oldest_key not in app.screen._detail_identity_cache
        assert app.screen._detail_identity_cache[id(selected)][0] is selected


@pytest.mark.parametrize("cache_kind", ["identity", "body"])
@pytest.mark.slow
async def test_detail_cache_hits_refresh_lru_before_worker_completion(
    cache_kind: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selected cache hit becomes newest before pending worker work returns."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    body = "x" * 21_000 if cache_kind == "identity" else "small"
    selected = _identity_ui_record(agentgrep, tmp_path / "selected.jsonl", body)
    other = _identity_ui_record(agentgrep, tmp_path / "other.jsonl", body)

    async with _mounted_detail_app(tmp_path, monkeypatch) as (app, _pilot):
        monkeypatch.setattr(app.screen, "run_worker", lambda _target, **_kwargs: None)
        if cache_kind == "identity":
            cache = app.screen._detail_identity_cache = collections.OrderedDict(
                (
                    (id(selected), (selected, record_identity(selected))),
                    (id(other), (other, record_identity(other))),
                ),
            )
            expected_key = id(selected)
        else:
            query_terms = tuple(app.screen.search_query.terms)
            selected_key = app.screen._detail_cache_key_for(
                selected,
                query_terms,
                case_sensitive=app.screen.search_query.case_sensitive,
                regex=app.screen.search_query.regex,
                filter_terms=app.screen._filter_terms,
            )
            other_key = app.screen._detail_cache_key_for(
                other,
                query_terms,
                case_sensitive=app.screen.search_query.case_sensitive,
                regex=app.screen.search_query.regex,
                filter_terms=app.screen._filter_terms,
            )
            cache = app.screen._detail_body_cache = collections.OrderedDict(
                (
                    (selected_key, (selected, "selected body", "selected body")),
                    (other_key, (other, "other body", "other body")),
                ),
            )
            expected_key = selected_key

        app.screen.show_detail(selected)

        assert next(reversed(cache)) == expected_key


@pytest.mark.slow
async def test_detail_theme_reuses_identity_and_preserves_find_state(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Theme rerender reuses cached identity without closing or resetting find."""
    import agentgrep.identity as identity

    agentgrep = t.cast("t.Any", load_agentgrep_module())
    record = _identity_ui_record(
        agentgrep,
        tmp_path / "theme.jsonl",
        "needle before needle after",
    )
    calls: list[object] = []
    original = identity.record_identity

    def count_identity(value: object) -> identity.RecordIdentity:
        calls.append(value)
        return original(t.cast("t.Any", value))

    monkeypatch.setattr(identity, "record_identity", count_identity)

    async with _mounted_detail_app(tmp_path, monkeypatch) as (app, pilot):
        app.screen.show_detail(record)
        await _drain_detail_workers(app, pilot)
        app.screen.action_open_detail_find()
        app.screen._detail_find_input.load_query("needle")
        app.screen._run_detail_find("needle", reset_cursor=True)
        expected_matches = list(app.screen._detail_find_matches)

        app.screen._on_theme_changed(object())
        await _drain_detail_workers(app, pilot)

        assert calls == [record]
        assert app.screen._detail_find_active is True
        assert app.screen._detail_find_query == "needle"
        assert app.screen._detail_find_matches == expected_matches
