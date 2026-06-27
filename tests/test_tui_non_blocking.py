"""Enforcement tests for the ADR 0011 non-blocking TUI invariants.

Three layers guard the rules:

- An AST scan of ``ui/app_screen.py`` and every ``ui/widgets/*.py`` module
  proves no pump-thread method contains a blocking call (NB-1/NB-8), that JSON
  parsing is confined to the one bounded fast-path method (NB-9), and that the
  batch applier routes through the bounded ``stream_apply`` (NB-4). A scan of
  ``ui/app_screen.py`` proves every worker launch is exclusive and grouped
  (NB-6).
- Unit tests of the ``@pump_only`` / ``@offload`` guards and ``stream_apply``
  confirm the runtime assertions and the chunk cap.
- Pilot/behavioral tests confirm cooperative cancel (NB-7) and the opt-in
  heartbeat watchdog (the fuzz harness's oracle).
"""

from __future__ import annotations

import ast
import inspect
import logging
import pathlib
import threading
import time
import typing as t

import pytest

from agentgrep.ui import _runtime, app_screen
from tests.test_agentgrep import _build_empty_ui_app

_APP_PATH = pathlib.Path(app_screen.__file__)
_APP_TREE = ast.parse(_APP_PATH.read_text(encoding="utf-8"))


def _ui_source_trees() -> list[ast.AST]:
    """Parse ``ui/app_screen.py`` plus every extracted ``ui/widgets/*.py`` module.

    The widgets moved out of the app closure into factory modules, so the
    no-blocking-calls guard must scan their pump methods (``watch_*`` /
    ``_on_key`` / ``render``) too — not just the app's.
    """
    widget_paths = sorted((_APP_PATH.parent / "widgets").glob("*.py"))
    return [ast.parse(path.read_text(encoding="utf-8")) for path in (_APP_PATH, *widget_paths)]


_UI_TREES = _ui_source_trees()

# A method Textual invokes on the pump thread: event/action/watch/compute
# handlers, render/compose, the input key/value overrides — plus anything
# explicitly tagged @pump_only.
_PUMP_PREFIXES = ("on_", "action_", "watch_", "compute_", "_watch_")
_PUMP_EXACT = {"render", "compose", "_on_key"}

# Blocking calls forbidden in a pump-thread body (NB-1). JSON parsing is checked
# separately (NB-9) because it has one sanctioned, bounded home.
_FORBIDDEN_CALL_NAMES = {"open", "run_search_query"}
_FORBIDDEN_ATTRS = {"read_text", "read_bytes"}
_FORBIDDEN_DOTTED_ROOTS = {"subprocess", "sqlite3"}
_FORBIDDEN_DOTTED = {"os.close", "os.open", "os.write", "time.sleep"}

#: The single method allowed to call ``json.loads`` / ``json.dumps`` (NB-9):
#: the inline-bounded / worker detail-body builder. A new offender must be
#: added here deliberately, with a bound, not silently.
_JSON_EXEMPT = {"_build_detail_body"}


class _Method(t.NamedTuple):
    """A class method discovered in ``ui/app_screen.py``."""

    cls: str
    name: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    decorators: tuple[str, ...]


def _decorator_name(node: ast.expr) -> str:
    """Return the rightmost identifier of a decorator expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ""


def _all_methods() -> list[_Method]:
    """Return every method defined on a class in the app or widget modules."""
    methods: list[_Method] = []
    for tree in _UI_TREES:
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for item in cls.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    decorators = tuple(_decorator_name(d) for d in item.decorator_list)
                    methods.append(_Method(cls.name, item.name, item, decorators))
    return methods


def _is_pump_method(method: _Method) -> bool:
    """Classify whether Textual would invoke ``method`` on the pump thread."""
    if "pump_only" in method.decorators:
        return True
    if method.name in _PUMP_EXACT:
        return True
    return method.name.startswith(_PUMP_PREFIXES)


def _dotted(node: ast.expr) -> str:
    """Return a dotted name for an attribute chain, else ``""``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        root = _dotted(node.value)
        return f"{root}.{node.attr}" if root else node.attr
    return ""


def _forbidden_calls(node: ast.AST) -> list[str]:
    """Return the blocking-call names found anywhere under ``node`` (NB-1)."""
    found: list[str] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALL_NAMES:
            found.append(func.id)
        elif isinstance(func, ast.Attribute):
            if func.attr in _FORBIDDEN_ATTRS:
                found.append(func.attr)
            dotted = _dotted(func)
            root = dotted.split(".", 1)[0]
            if root in _FORBIDDEN_DOTTED_ROOTS or dotted in _FORBIDDEN_DOTTED:
                found.append(dotted)
    return found


def _json_calls(node: ast.AST) -> bool:
    """Return whether ``node`` calls ``json.loads`` or ``json.dumps``."""
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in {"loads", "dumps"}
            and isinstance(func.value, ast.Name)
            and func.value.id == "json"
        ):
            return True
    return False


# --- NB-1 / NB-8: no blocking calls on the pump ----------------------------


def test_pump_methods_have_no_blocking_calls() -> None:
    """No pump-thread method opens files, spawns processes, or sleeps (NB-1)."""
    offenders = {
        f"{m.cls}.{m.name}": calls
        for m in _all_methods()
        if _is_pump_method(m) and (calls := _forbidden_calls(m.node))
    }
    assert not offenders, f"blocking calls in pump methods (NB-1/NB-8): {offenders}"


def test_forbidden_call_detector_flags_blocking_calls() -> None:
    """The detector itself is not a no-op — it flags real blocking calls.

    Without this anchor, ``test_pump_methods_have_no_blocking_calls`` could pass
    only because the detector never matches anything.
    """
    blocking = ast.parse(
        "def handler(self):\n"
        "    subprocess.run(['rg'])\n"
        "    open('x')\n"
        "    fd = os.open('x', os.O_RDONLY)\n"
        "    os.write(fd, b'x')\n"
        "    os.close(fd)\n"
        "    self.path.read_text()\n"
        "    time.sleep(1)\n",
    )
    clean = ast.parse("def handler(self):\n    self.refresh()\n    await asyncio.sleep(0)\n")
    assert set(_forbidden_calls(blocking)) >= {
        "subprocess.run",
        "open",
        "os.open",
        "os.write",
        "os.close",
        "read_text",
        "time.sleep",
    }
    assert _forbidden_calls(clean) == []


def test_json_parsing_confined_to_detail_body() -> None:
    """``json.loads`` / ``json.dumps`` live only in the bounded builder (NB-9)."""
    json_methods = {m.name for m in _all_methods() if _json_calls(m.node)}
    assert json_methods == _JSON_EXEMPT, (
        f"json parsing must stay in {_JSON_EXEMPT}; found in {json_methods}"
    )


# --- NB-4 / NB-6: bounded apply, exclusive grouped workers -----------------


def _run_worker_calls() -> list[ast.Call]:
    """Return every ``*.run_worker(...)`` call in ``ui/app_screen.py``."""
    return [
        node
        for node in ast.walk(_APP_TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_worker"
    ]


def test_workers_are_thread_exclusive_and_grouped() -> None:
    """Every worker is ``thread=True`` and grouped (NB-6).

    Supersedable groups are ``exclusive=True``; the ``history`` append group is
    the sole exception — each append must complete, never supersede an earlier
    one.
    """
    calls = _run_worker_calls()
    assert calls, "expected at least one run_worker call"
    for call in calls:
        kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        thread = kwargs.get("thread")
        exclusive = kwargs.get("exclusive")
        group = kwargs.get("group")
        assert isinstance(thread, ast.Constant) and thread.value is True
        assert "group" in kwargs, "worker launch missing a stable group="
        # The history-append group is non-supersedable; all others are exclusive.
        non_supersedable = isinstance(group, ast.Constant) and group.value == "history"
        assert isinstance(exclusive, ast.Constant) and exclusive.value is not non_supersedable


def test_apply_records_batch_uses_bounded_stream_apply() -> None:
    """The batch applier routes through the bounded ``stream_apply`` (NB-4)."""
    batch = next(m for m in _all_methods() if m.name == "_apply_records_batch")
    calls = {
        node.func.attr
        for node in ast.walk(batch.node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "stream_apply" in calls


# --- runtime guards --------------------------------------------------------


def test_pump_only_raises_off_pump(monkeypatch: pytest.MonkeyPatch) -> None:
    """``@pump_only`` raises when invoked off the bound pump thread."""
    monkeypatch.setattr(_runtime, "_GUARDS_ENABLED", True)
    monkeypatch.setattr(_runtime, "_pump_thread_id", -1)  # never the current thread

    @_runtime.pump_only
    def guarded() -> str:
        return "ran"

    with pytest.raises(AssertionError):
        guarded()


def test_offload_raises_on_pump(monkeypatch: pytest.MonkeyPatch) -> None:
    """``@offload`` raises when invoked on the bound pump thread."""
    monkeypatch.setattr(_runtime, "_GUARDS_ENABLED", True)
    monkeypatch.setattr(_runtime, "_pump_thread_id", threading.get_ident())

    @_runtime.offload
    def guarded() -> str:
        return "ran"

    with pytest.raises(AssertionError):
        guarded()


def test_bind_pump_thread_refreshes_guard_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Binding the app pump enables guards even after an early import."""
    monkeypatch.setattr(_runtime, "_GUARDS_ENABLED", False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_tui_non_blocking.py::case (call)")
    _runtime.bind_pump_thread()

    @_runtime.offload
    def guarded() -> str:
        return "ran"

    try:
        with pytest.raises(AssertionError):
            guarded()
    finally:
        _runtime.unbind_pump_thread()


def test_guards_are_noops_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With guards off, the decorators are transparent passthroughs."""
    monkeypatch.setattr(_runtime, "_GUARDS_ENABLED", False)
    monkeypatch.setattr(_runtime, "_pump_thread_id", -1)

    @_runtime.pump_only
    def pump() -> str:
        return "pump"

    @_runtime.offload
    def work() -> str:
        return "work"

    assert pump() == "pump"
    assert work() == "work"


async def test_offload_preserves_coroutine_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``@offload`` async function stays a coroutine function."""
    monkeypatch.setattr(_runtime, "_GUARDS_ENABLED", False)

    @_runtime.offload
    async def work() -> int:
        return 5

    assert inspect.iscoroutinefunction(work)
    assert await work() == 5


# --- stream_apply ----------------------------------------------------------


async def test_stream_apply_chunks_and_yields() -> None:
    """``stream_apply`` applies all items in chunks and yields between slices."""
    applied: list[int] = []
    yields = 0

    async def count_yield() -> None:
        nonlocal yields
        yields += 1

    await _runtime.stream_apply(
        list(range(450)),
        applied.extend,
        chunk_size=200,
        yield_between=count_yield,
    )
    assert applied == list(range(450))
    assert yields == 2  # slices at 0, 200, 400 -> yield after the first two


async def test_stream_apply_rejects_nonpositive_chunk() -> None:
    """A non-positive chunk size is rejected so an unbounded apply is impossible."""
    with pytest.raises(ValueError, match="chunk_size"):
        await _runtime.stream_apply([1, 2, 3], lambda _chunk: None, chunk_size=0)


# --- NB-7: cooperative cancel ----------------------------------------------


async def test_stop_search_requests_cooperative_cancel(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopping a search flags the control; the next search swaps a fresh one."""
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app._search_done = False
        old_control = app.control
        app.action_stop_search()
        assert app.control.answer_now_requested()
        app._reset_search_chrome()
        assert app.control is not old_control
        assert not app.control.answer_now_requested()


# --- heartbeat watchdog ----------------------------------------------------


def test_watchdog_logs_on_stall(caplog: pytest.LogCaptureFixture) -> None:
    """A stalled heartbeat logs a warning carrying ``agentgrep_pump_stall_ms``."""
    try:
        with caplog.at_level(logging.WARNING, logger="agentgrep.ui._runtime"):
            _runtime.start_pump_watchdog(stall_threshold_ms=20, poll_seconds=0.01)
            # Do not beat again; the watcher must notice the stall.
            deadline = time.monotonic() + 1.0
            stalls = []
            while time.monotonic() < deadline:
                stalls = [r for r in caplog.records if hasattr(r, "agentgrep_pump_stall_ms")]
                if stalls:
                    break
                time.sleep(0.02)
    finally:
        _runtime.stop_pump_watchdog()
    assert stalls, "watchdog did not log a stall"
    assert t.cast("int", stalls[0].agentgrep_pump_stall_ms) >= 20


async def test_watchdog_not_started_without_env(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Booting the app with the env unset spawns no watchdog thread."""
    monkeypatch.delenv("AGENTGREP_TUI_WATCHDOG", raising=False)
    app = _build_empty_ui_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        names = {thread.name for thread in threading.enumerate()}
        assert "agentgrep-pump-watchdog" not in names
