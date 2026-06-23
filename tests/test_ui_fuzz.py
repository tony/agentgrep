"""Fast, deterministic hang/clog fuzzer for the Textual explorer.

A seeded sequence of UI moves (type into search/filter, submit synthetic
searches, navigate, select rows, scroll detail, resize across the responsive
breakpoint, open dropdowns, stop mid-search) is replayed inside one
``run_test`` session. Each move is wrapped in a tight ``asyncio.wait_for`` so a
wedged pump fails in ~seconds (a :class:`UiHangError`) instead of Textual's
internal 30 s ``_wait_for_screen`` timeout, and a set of liveness invariants is
asserted after every move.

The synthetic engine drives the *real* streaming path — the app's
:class:`~agentgrep.progress.StreamingSearchProgress` reporter, ``call_from_thread``
backpressure, and the chunked applier — through pathological shapes (empty,
single, large batch, many tiny batches, progress-heavy, interrupted,
error-midstream) and pathological record bodies (JSON, Markdown, Rich-markup
metacharacters, unicode, multi-megabyte, long single line).

Two meta-tests prove the detector is not a no-op: a deliberately blocking pump
handler and a worker leak must both be *caught*.

The fuzzer never emits app-quitting keys (``q`` / quitting ``ctrl+c``); a clean
quit is covered elsewhere.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import random
import time
import typing as t

import pytest

from agentgrep.ui import app as ui_app
from tests.test_agentgrep import _build_empty_ui_app, load_agentgrep_module

# Tight per-move budgets (seconds). A real wedge blows these; legitimate work
# (a large synthetic batch, an off-thread detail build) stays well under them.
# Scale up on slow CI without editing call sites.
_BUDGET_SCALE = float(os.environ.get("AGENTGREP_FUZZ_BUDGET_SCALE", "1.0"))
_LIGHT_BUDGET = 2.0 * _BUDGET_SCALE
_HEAVY_BUDGET = 5.0 * _BUDGET_SCALE

#: Worst-case live workers: 3 exclusive groups (search/filter/detail) + slack.
_MAX_WORKERS = 8
#: A batch large enough to exercise several chunk slices without slowing the gate.
_LARGE_BATCH = 400
#: Default fuzz run: a few fixed seeds so a real hang fails every run.
_SEEDS = (0, 1, 7)
_MOVES_PER_SESSION = 12

#: Adversarial query strings — unterminated quotes, unbalanced parens, bare
#: field predicates, booleans, wildcards, unicode. None may wedge the pump.
_QUERIES: tuple[str, ...] = (
    "",
    "   ",
    "bliss",
    "agent:codex",
    "agent:",
    "scope:",
    "(agent:codex OR agent:cursor-cli) AND bliss",
    'bliss "deploy',
    "'agent:codex",
    "NOT agent:claude",
    "model:*",
    "-agent:claude",
    "(((((",
    ")))))",
    "a AND OR NOT (",
    "中文 поиск",
    "path:~/.codex",
)
_NONEMPTY_QUERIES = tuple(q for q in _QUERIES if q.strip())


class UiHangError(AssertionError):
    """Raised when a fuzz move fails to settle within its budget."""


# --- synthetic records -----------------------------------------------------


def _record(
    agentgrep: t.Any, tmp_path: pathlib.Path, idx: int, text: str, title: str | None
) -> t.Any:
    """Build a minimal prompt :class:`SearchRecord` for the fuzzer."""
    agent = agentgrep.AGENT_CHOICES[idx % len(agentgrep.AGENT_CHOICES)]
    return agentgrep.SearchRecord(
        kind="prompt" if idx % 2 == 0 else "history",
        agent=agent,
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / f"r{idx}.jsonl",
        text=text,
        title=title,
        timestamp="2026-01-01T00:00:00Z" if idx % 3 else None,
    )


def _record_pool(agentgrep: t.Any, tmp_path: pathlib.Path) -> list[t.Any]:
    """Return a small pool spanning every pathological record body shape."""
    bodies: list[tuple[str, str | None]] = [
        ("", None),
        ("row one", "tiny title"),
        ("line\n" * 200, "multiline"),
        ('{"a": 1, "b": [1, 2, 3], "msg": "hello"}', "json body"),
        ("# Heading\n\n- bullet\n\n```py\nx = 1\n```\n", "md body"),
        ("[red]not markup[/] {brace} \\ [bold] $token", "[bold]title[/]"),
        ("中文 🚀 ​ á mixed", "unicode 🎯"),
        ("col\tcol\r\nrow\x1b[0m end", "control chars"),
        ("z" * 20_000, "boundary"),  # exactly the inline/worker threshold
        ("x" * 60_000, "multi-mb -> detail worker"),
        ("y" * 120_000, "long single line"),
    ]
    return [_record(agentgrep, tmp_path, i, text, title) for i, (text, title) in enumerate(bodies)]


def _large_pool(agentgrep: t.Any, tmp_path: pathlib.Path) -> list[t.Any]:
    """Return a large pool of tiny records to stress the chunked applier."""
    return [
        _record(agentgrep, tmp_path, i, f"large row {i} bliss", None) for i in range(_LARGE_BATCH)
    ]


# --- synthetic engine: drive the real reporter path ------------------------


def _drive_zero(progress: t.Any, control: t.Any, records: list[t.Any], query: t.Any) -> None:
    progress.start(query)
    progress.finish(0)


def _drive_pool(progress: t.Any, control: t.Any, records: list[t.Any], query: t.Any) -> None:
    progress.start(query)
    for i, record in enumerate(records, 1):
        progress.record_added(record)
        progress.result_added(i)
    progress.finish(len(records))


def _drive_many_tiny(progress: t.Any, control: t.Any, records: list[t.Any], query: t.Any) -> None:
    progress.start(query)
    for i, record in enumerate(records, 1):
        progress.record_added(record)
        progress.flush()  # force a batch per record -> many round-trips
        progress.result_added(i)
    progress.finish(len(records))


def _drive_progress_heavy(
    progress: t.Any, control: t.Any, records: list[t.Any], query: t.Any
) -> None:
    progress.start(query)
    progress.sources_discovered(8)
    for i in range(8):
        progress.sources_planned(i + 1, 8)
    for i, record in enumerate(records[:3], 1):
        progress.record_added(record)
        progress.result_added(i)
    progress.finish(min(3, len(records)))


def _drive_interrupted(progress: t.Any, control: t.Any, records: list[t.Any], query: t.Any) -> None:
    progress.start(query)
    for i, record in enumerate(records[:6], 1):
        progress.record_added(record)
        progress.result_added(i)
        if control is not None and control.answer_now_requested():
            progress.answer_now(i)
            return
    progress.interrupt()


def _drive_error(progress: t.Any, control: t.Any, records: list[t.Any], query: t.Any) -> None:
    progress.start(query)
    if records:
        progress.record_added(records[0])
        progress.flush()
    msg = "synthetic fuzz error"
    raise RuntimeError(msg)


class _StreamShape(t.NamedTuple):
    """A named way to drive the synthetic engine, with its budget weight."""

    test_id: str
    drive: t.Callable[[t.Any, t.Any, list[t.Any], t.Any], None]
    heavy: bool


_STREAM_SHAPES: tuple[_StreamShape, ...] = (
    _StreamShape("zero", _drive_zero, False),
    _StreamShape("pool", _drive_pool, False),
    _StreamShape("many-tiny", _drive_many_tiny, False),
    _StreamShape("progress-heavy", _drive_progress_heavy, False),
    _StreamShape("interrupted", _drive_interrupted, False),
    _StreamShape("error", _drive_error, False),
    _StreamShape("massive", _drive_pool, True),  # paired with the large pool
)


class _FuzzEngine:
    """Stub ``run_search_query`` that drives the chosen shape with chosen records."""

    def __init__(self) -> None:
        self.drive: t.Callable[[t.Any, t.Any, list[t.Any], t.Any], None] = _drive_zero
        self.records: list[t.Any] = []

    def __call__(
        self,
        home: pathlib.Path,
        query: t.Any,
        *,
        backends: t.Any = None,
        progress: t.Any = None,
        control: t.Any = None,
        runtime: t.Any = None,
    ) -> list[t.Any]:
        if progress is None:
            return []
        self.drive(progress, control, self.records, query)
        return list(self.records)


# --- moves -----------------------------------------------------------------


class _Move(t.NamedTuple):
    """A single fuzzer action."""

    name: str
    heavy: bool
    run: t.Callable[..., t.Awaitable[None]]


async def _m_type_search(pilot: t.Any, app: t.Any, rng: random.Random, ctx: t.Any) -> None:
    app._search_input.value = rng.choice(_QUERIES)
    await pilot.pause()


async def _m_submit_search(pilot: t.Any, app: t.Any, rng: random.Random, ctx: t.Any) -> None:
    shape = rng.choice(_STREAM_SHAPES)
    ctx.engine.drive = shape.drive
    ctx.engine.records = ctx.large_pool if shape.test_id == "massive" else ctx.pool
    app._search_input.focus()
    app._search_input.value = rng.choice(_NONEMPTY_QUERIES)
    await pilot.press("enter")
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _m_type_filter(pilot: t.Any, app: t.Any, rng: random.Random, ctx: t.Any) -> None:
    if not app._filter_input.display:  # hidden until a search loads results
        return
    app._filter_input.value = rng.choice(_QUERIES)
    await pilot.pause(0.2)  # let the 150 ms debounce + filter worker fire
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _m_navigate(pilot: t.Any, app: t.Any, rng: random.Random, ctx: t.Any) -> None:
    if not app._results.display:  # hidden in the pre-search bare-canvas state
        return
    app._results.focus()
    await pilot.press(rng.choice(("j", "k", "g", "G", "ctrl+d", "ctrl+u", "down", "up", "tab")))


async def _m_select_row(pilot: t.Any, app: t.Any, rng: random.Random, ctx: t.Any) -> None:
    count = app._results.option_count
    if count:
        app._results.highlighted = rng.randrange(count)
        await pilot.pause()
        await app.workers.wait_for_complete()  # off-thread detail build for big bodies
        await pilot.pause()


async def _m_scroll_detail(pilot: t.Any, app: t.Any, rng: random.Random, ctx: t.Any) -> None:
    if not app._detail_scroll.display:  # hidden until the detail pane is revealed
        return
    app._detail_scroll.focus()
    await pilot.press(rng.choice(("ctrl+d", "ctrl+u", "g", "G", "ctrl+f", "ctrl+b")))


async def _m_resize(pilot: t.Any, app: t.Any, rng: random.Random, ctx: t.Any) -> None:
    width, height = rng.choice(
        ((120, 30), (99, 30), (100, 30), (80, 24), (40, 20), (200, 50), (30, 12))
    )
    await pilot.resize_terminal(width, height)


async def _m_dropdown(pilot: t.Any, app: t.Any, rng: random.Random, ctx: t.Any) -> None:
    # The filter input is hidden in the pre-search bare-canvas state; only drive
    # inputs that are currently displayed (the search bar always is).
    candidates = [w for w in (app._search_input, app._filter_input) if w.display]
    if not candidates:
        return
    target = rng.choice(candidates)
    target.focus()
    target.value = rng.choice(("agent:", "scope:"))
    await pilot.pause()
    await pilot.press("down")
    await pilot.press(rng.choice(("enter", "escape")))
    await pilot.pause()


async def _m_stop(pilot: t.Any, app: t.Any, rng: random.Random, ctx: t.Any) -> None:
    await pilot.press("escape")
    await pilot.pause()


_MOVES: tuple[_Move, ...] = (
    _Move("type_search", False, _m_type_search),
    _Move("submit_search", True, _m_submit_search),
    _Move("type_filter", False, _m_type_filter),
    _Move("navigate", False, _m_navigate),
    _Move("select_row", True, _m_select_row),
    _Move("scroll_detail", False, _m_scroll_detail),
    _Move("resize", False, _m_resize),
    _Move("dropdown", False, _m_dropdown),
    _Move("stop", False, _m_stop),
)


class _Ctx:
    """Per-session fuzz context: the engine stub and the record pools."""

    def __init__(self, engine: _FuzzEngine, pool: list[t.Any], large_pool: list[t.Any]) -> None:
        self.engine = engine
        self.pool = pool
        self.large_pool = large_pool


def _assert_invariants(app: t.Any, where: str) -> None:
    """Assert the liveness invariants that must hold after every move."""
    assert len(app.workers) <= _MAX_WORKERS, f"{where}: worker leak ({len(app.workers)})"
    focused = app.focused
    if focused is not None:
        # Never strand focus on a hidden widget (the collapsed stacked detail).
        assert focused.display, f"{where}: focus on a non-displayed widget {focused.id}"
    all_ids = {id(record) for record in app.all_records}
    filtered_ids = {id(record) for record in app.filtered_records}
    assert filtered_ids <= all_ids, f"{where}: filtered_records not a subset of all_records"
    assert len(app._detail_body_cache) <= app._DETAIL_CACHE_MAX, f"{where}: detail cache unbounded"
    assert len(app._detail_scroll_positions) <= app._DETAIL_CACHE_MAX, (
        f"{where}: scroll-memory cache unbounded"
    )


async def _bounded(coro: t.Awaitable[None], budget: float, where: str) -> None:
    """Await ``coro`` within ``budget`` seconds or raise :class:`UiHangError`."""
    try:
        await asyncio.wait_for(coro, timeout=budget)
    except TimeoutError as exc:
        msg = f"{where} did not settle within {budget:.1f}s — pump may be wedged"
        raise UiHangError(msg) from exc


async def _run_session(app: t.Any, ctx: _Ctx, *, seed: int, moves: int) -> None:
    """Replay ``moves`` random moves against ``app`` under tight budgets."""
    rng = random.Random(seed)
    async with app.run_test(size=(120, 30)) as pilot:
        await _bounded(pilot.pause(), _LIGHT_BUDGET, f"seed={seed} mount")
        for step in range(moves):
            move = rng.choice(_MOVES)
            budget = _HEAVY_BUDGET if move.heavy else _LIGHT_BUDGET
            where = f"seed={seed} step={step} move={move.name}"
            await _bounded(move.run(pilot, app, rng, ctx), budget, where)
            _assert_invariants(app, where)
        await _bounded(app.workers.wait_for_complete(), _HEAVY_BUDGET, f"seed={seed} drain")
        await _bounded(pilot.pause(), _LIGHT_BUDGET, f"seed={seed} settle")
        # After a full drain the visible list matches the filtered model.
        assert app._results.option_count == len(app.filtered_records)


@pytest.mark.parametrize("seed", _SEEDS, ids=[f"seed-{s}" for s in _SEEDS])
async def test_fuzz_session_stays_responsive(
    seed: int,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No random move sequence wedges the pump or breaks a liveness invariant."""
    agentgrep = t.cast("t.Any", load_agentgrep_module())
    engine = _FuzzEngine()
    monkeypatch.setattr(ui_app, "run_search_query", engine)
    ctx = _Ctx(engine, _record_pool(agentgrep, tmp_path), _large_pool(agentgrep, tmp_path))
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    await _run_session(app, ctx, seed=seed, moves=_MOVES_PER_SESSION)


# --- meta-tests: prove the detector catches real hangs ---------------------


async def test_fuzz_detects_blocking_handler(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocking pump handler is caught as a hang, not silently passed."""
    engine = _FuzzEngine()
    # ``_drive_zero`` emits a single progress snapshot, so the wedged handler
    # fires once (one sleep), keeping teardown fast while still tripping the
    # budget.
    engine.drive = _drive_zero
    monkeypatch.setattr(ui_app, "run_search_query", engine)
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        # Wedge the pump: a synchronous sleep inside a call_from_thread callee.
        monkeypatch.setattr(app, "_apply_progress", lambda _snapshot: time.sleep(1.5))
        app._search_input.value = "bliss"
        with pytest.raises((UiHangError, TimeoutError)):
            await _bounded(_drive_then_wait(pilot, app), 1.0, "blocking-handler")


async def _drive_then_wait(pilot: t.Any, app: t.Any) -> None:
    """Submit a search and wait for it — used by the blocking-handler meta-test."""
    await pilot.press("enter")
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_fuzz_detects_worker_leak() -> None:
    """The worker-count invariant fires when workers are leaked.

    Exercises ``_assert_invariants`` directly against a fake app so the leak
    detector is proven without monkeypatching a live app's ``workers`` property
    (which Textual's teardown relies on).
    """
    import types

    class _FakeWorkers:
        def __len__(self) -> int:
            return _MAX_WORKERS + 5

    fake_app = types.SimpleNamespace(
        workers=_FakeWorkers(),
        focused=None,
        all_records=[],
        filtered_records=[],
        _detail_body_cache={},
        _detail_scroll_positions={},
        _DETAIL_CACHE_MAX=1024,
    )
    with pytest.raises(AssertionError, match="worker leak"):
        _assert_invariants(fake_app, "leak-test")
