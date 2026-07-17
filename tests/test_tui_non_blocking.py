"""Enforcement tests for the ADR 0011 non-blocking TUI invariants.

Three layers guard the rules:

- An AST scan of ``ui/layouts/hud.py`` and every ``ui/widgets/*.py`` module
  proves no pump-thread method contains a blocking call (NB-1/NB-8), that JSON
  parsing is confined to the one bounded fast-path method (NB-9), and that the
  batch applier routes through the bounded ``stream_apply`` (NB-4). A scan of
  ``ui/layouts/hud.py`` proves every worker launch is exclusive and grouped
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

from agentgrep.ui import _runtime
from agentgrep.ui.layouts import hud
from tests.test_agentgrep import _build_empty_ui_app

#: The HUD layout holds every worker launch and ``_apply_records_batch`` (NB-4/NB-6).
_APP_PATH = pathlib.Path(hud.__file__)
_APP_TREE = ast.parse(_APP_PATH.read_text(encoding="utf-8"))


def _ui_source_trees() -> list[ast.AST]:
    """Parse every ``ui/layouts/*.py``, the App shell, and every ``ui/widgets/*.py``.

    Each pluggable layout (HUD, grep-log, …) carries its own streaming transport,
    so the no-blocking-calls guard scans the pump methods (``watch_*`` /
    ``on_key`` / ``on_mount`` / ``render`` / ``@pump_only``) of all of them, plus
    the App-lifecycle shell and the leaf widgets.
    """
    ui_dir = _APP_PATH.parent.parent
    layout_paths = sorted((ui_dir / "layouts").glob("*.py"))
    widget_paths = sorted((ui_dir / "widgets").glob("*.py"))
    extra = [ui_dir / "_shell.py"]
    return [
        ast.parse(path.read_text(encoding="utf-8"))
        for path in (*layout_paths, *extra, *widget_paths)
    ]


_UI_TREES = _ui_source_trees()

# A method Textual invokes on the pump thread: event/action/watch/compute
# handlers, render/compose, the input key/value overrides, @on-decorated
# handlers (any name), the callables handed to a scheduler/cross-thread/signal
# site (see _SCHEDULED_PUMP_NAMES), plus anything explicitly tagged @pump_only.
_PUMP_PREFIXES = ("on_", "action_", "watch_", "compute_", "_watch_")
_PUMP_EXACT = {"render", "compose", "get_default_screen"}

# Calls that hand a callable to the pump thread; their target methods run there
# even though their names match no prefix (NB-1/NB-8).
_SCHEDULER_FUNCS = {
    "set_timer",
    "set_interval",
    "call_later",
    "call_next",
    "call_after_refresh",
    "call_from_thread",
    "subscribe",
}

# Blocking calls forbidden in a pump-thread body (NB-1). JSON parsing is checked
# separately (NB-9) because it has one sanctioned, bounded home. Generic attrs
# (.get/.join/.wait/.acquire/.result/.read) are deliberately absent — carrying no
# type, they would false-positive on dicts/strings/futures; the wall-clock
# watchdog is the backstop for those (ADR 0011 coverage limits).
_FORBIDDEN_CALL_NAMES = {"open", "input", "run_search_query"}
_FORBIDDEN_ATTRS = {
    "deliver_screenshot",
    "export_screenshot",
    "read_text",
    "read_bytes",
    "iterdir",
    "rglob",
}
_FORBIDDEN_DOTTED_ROOTS = {
    "subprocess",
    "sqlite3",
    "socket",
    "urllib",
    "requests",
    "httpx",
    "ftplib",
}
_FORBIDDEN_DOTTED = {
    "os.close",
    "os.open",
    "os.write",
    "os.read",
    "os.walk",
    "os.listdir",
    "os.scandir",
    "os.stat",
    "time.sleep",
    "json.load",
    "json.dump",
}

#: The single method allowed to call ``json.loads`` / ``json.dumps`` (NB-9):
#: the inline-bounded / worker detail-body builder. A new offender must be
#: added here deliberately, with a bound, not silently.
_JSON_EXEMPT = {"_build_detail_body"}


def _import_map(tree: ast.AST) -> dict[str, str]:
    """Map each imported name/alias to its canonical dotted target.

    Resolves alias/from-import evasion: ``import subprocess as sp`` → sp:
    subprocess; ``from time import sleep`` → sleep: time.sleep.
    """
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mapping[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                mapping[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return mapping


def _scheduled_pump_names() -> frozenset[str]:
    """Return method names handed to a scheduler/cross-thread/signal call site.

    Textual runs these on the pump thread even though their names match no
    prefix (e.g. ``set_timer(0.05, self._after_resize)``); the name classifier
    cannot see them, so seed them from the call sites (NB-8).
    """
    names: set[str] = set()
    for tree in _UI_TREES:
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr not in _SCHEDULER_FUNCS:
                continue
            for arg in call.args:
                # Seed only ``self.method`` targets: the data args passed
                # alongside the callable are bare names, and a lambda/partial
                # target has no name to seed (a known residual, ADR 0011).
                if (
                    isinstance(arg, ast.Attribute)
                    and isinstance(arg.value, ast.Name)
                    and arg.value.id == "self"
                ):
                    names.add(arg.attr)
    return frozenset(names)


_SCHEDULED_PUMP_NAMES = _scheduled_pump_names()


class _Method(t.NamedTuple):
    """A class method discovered in an app or widget module."""

    cls: str
    name: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    decorators: tuple[str, ...]
    imports: tuple[tuple[str, str], ...]  # the module's import map, as sorted items


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
        imports = tuple(sorted(_import_map(tree).items()))
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for item in cls.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    decorators = tuple(_decorator_name(d) for d in item.decorator_list)
                    methods.append(_Method(cls.name, item.name, item, decorators, imports))
    return methods


def _is_pump_method(method: _Method) -> bool:
    """Classify whether Textual would invoke ``method`` on the pump thread."""
    if "pump_only" in method.decorators or "on" in method.decorators:
        return True
    if method.name in _PUMP_EXACT or method.name in _SCHEDULED_PUMP_NAMES:
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


def _forbidden_calls(node: ast.AST, imports: dict[str, str] | None = None) -> list[str]:
    """Return the blocking-call names found anywhere under ``node`` (NB-1).

    ``imports`` (a module's :func:`_import_map`) resolves alias / from-import
    evasion to canonical dotted names before matching.
    """
    resolve = imports or {}
    found: list[str] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in _FORBIDDEN_CALL_NAMES:
                found.append(func.id)
                continue
            canonical = resolve.get(func.id, func.id)
            if (
                canonical in _FORBIDDEN_DOTTED
                or canonical.split(".", 1)[0] in _FORBIDDEN_DOTTED_ROOTS
            ):
                found.append(canonical)
        elif isinstance(func, ast.Attribute):
            if func.attr in _FORBIDDEN_ATTRS:
                found.append(func.attr)
            dotted = _dotted(func)
            if not dotted:
                continue
            root, _, rest = dotted.partition(".")
            canonical_root = resolve.get(root, root)
            canonical = canonical_root + ("." + rest if rest else "")
            if (
                canonical_root in _FORBIDDEN_DOTTED_ROOTS
                or canonical in _FORBIDDEN_DOTTED
                or dotted in _FORBIDDEN_DOTTED
            ):
                found.append(canonical)
    return found


def _self_call_targets(node: ast.AST) -> set[str]:
    """Return method names invoked as ``self.<name>(...)`` under ``node``."""
    targets: set[str] = set()
    for call in ast.walk(node):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
        ):
            targets.add(call.func.attr)
    return targets


def _class_method_map() -> dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Map each class name to its ``{method name: node}`` for closure lookups."""
    mapping: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for tree in _UI_TREES:
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            methods = mapping.setdefault(cls.name, {})
            for item in cls.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[item.name] = item
    return mapping


def _closure_forbidden_calls(
    method: _Method,
    class_methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    imports: dict[str, str],
) -> list[str]:
    """Forbidden calls reachable from ``method`` via same-class ``self.<helper>()``.

    Closes the dominant gap: a blocking call extracted into a helper is invisible
    to an intraprocedural scan. ``@offload`` worker bodies are *passed* to
    ``run_worker`` rather than called as ``self.x()``, so the closure does not
    follow into them (their I/O is correctly off the pump).
    """
    found: list[str] = []
    visited: set[str] = {method.name}
    stack: list[ast.AST] = [method.node]
    while stack:
        node = stack.pop()
        found.extend(_forbidden_calls(node, imports))
        for target in _self_call_targets(node):
            if target not in visited and target in class_methods:
                visited.add(target)
                stack.append(class_methods[target])
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
    """No pump method — or a same-class helper it calls — opens/spawns/sleeps (NB-1)."""
    class_methods = _class_method_map()
    offenders = {
        f"{m.cls}.{m.name}": calls
        for m in _all_methods()
        if _is_pump_method(m)
        and (calls := _closure_forbidden_calls(m, class_methods.get(m.cls, {}), dict(m.imports)))
    }
    assert not offenders, f"blocking calls reachable from pump methods (NB-1/NB-8): {offenders}"


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


def test_forbidden_call_detector_resolves_aliases_and_families() -> None:
    """Import-aware: alias / from-import evasion and the NB-1 families are flagged."""
    tree = ast.parse(
        "import subprocess as sp\n"
        "from time import sleep\n"
        "from subprocess import run\n"
        "def handler(self):\n"
        "    sp.run(['rg'])\n"
        "    sleep(1)\n"
        "    run(['rg'])\n"
        "    input()\n"
        "    os.walk('/')\n"
        "    self.path.iterdir()\n"
        "    json.load(fh)\n"
        "    socket.create_connection(addr)\n",
    )
    handler = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    found = set(_forbidden_calls(handler, _import_map(tree)))
    assert {"subprocess.run", "time.sleep", "input", "os.walk", "iterdir", "json.load"} <= found
    assert any(name.startswith("socket") for name in found)


def test_classifier_sees_scheduled_callables_and_on_handlers() -> None:
    """Scheduler / call_from_thread targets and @on handlers classify as pump."""
    assert {
        "_after_resize",
        "_deliver_screenshot_after_refresh",
        "_on_theme_changed",
    } <= _SCHEDULED_PUMP_NAMES  # real sites
    screenshot_callback = next(
        method
        for method in _all_methods()
        if method.cls == "LayoutScreen" and method.name == "_deliver_screenshot_after_refresh"
    )
    assert "pump_only" in screenshot_callback.decorators
    # Data args passed alongside the callable must NOT be seeded (NB-8 false alarm).
    assert not ({"record", "header", "body", "query_terms", "self"} & _SCHEDULED_PUMP_NAMES)
    node = t.cast("ast.FunctionDef", ast.parse("def f(self): ...").body[0])
    assert _is_pump_method(_Method("A", "_after_resize", node, (), ()))  # set_timer target
    assert _is_pump_method(_Method("W", "_handle", node, ("on",), ()))  # @on handler
    assert not _is_pump_method(_Method("W", "_helper", node, (), ()))  # plain helper


def test_closure_follows_self_helpers() -> None:
    """The NB-1 closure follows ``self.<helper>()`` into a same-class helper."""
    tree = ast.parse(
        "class W:\n"
        "    def watch_x(self):\n"
        "        self._do_io()\n"
        "    def _do_io(self):\n"
        "        open('x')\n",
    )
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
    methods = {
        m.name: m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    watch = _Method("W", "watch_x", methods["watch_x"], (), ())
    assert _forbidden_calls(watch.node) == []  # intraprocedural sees nothing
    assert "open" in _closure_forbidden_calls(watch, methods, {})  # the closure does


def test_json_parsing_confined_to_detail_body() -> None:
    """``json.loads`` / ``json.dumps`` live only in the bounded builder (NB-9)."""
    json_methods = {m.name for m in _all_methods() if _json_calls(m.node)}
    assert json_methods == _JSON_EXEMPT, (
        f"json parsing must stay in {_JSON_EXEMPT}; found in {json_methods}"
    )


# --- NB-4 / NB-6: bounded apply, exclusive grouped workers -----------------


def _run_worker_calls() -> list[ast.Call]:
    """Return every ``*.run_worker(...)`` call across the layout modules."""
    return [
        node
        for tree in _UI_TREES
        for node in ast.walk(tree)
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


def test_results_widget_owns_filter_membership_without_hud_rescan() -> None:
    """Filter replacement reuses the results widget's existing ID delta set."""
    methods = {(method.cls, method.name): method.node for method in _all_methods()}
    apply = methods[("HudLayout", "on_filter_completed")]
    focus = methods[("HudLayout", "_record_for_detail_focus")]
    results_methods = {
        name: methods[("SearchResultsList", name)]
        for name in ("append_records", "set_records", "clear", "contains_record")
    }

    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "id"
        for node in ast.walk(apply)
    )
    assert any(
        isinstance(node, ast.Attribute) and node.attr == "contains_record"
        for node in ast.walk(focus)
    )
    for name, node in results_methods.items():
        assert any(
            isinstance(item, ast.Attribute) and item.attr == "_record_ids"
            for item in ast.walk(node)
        ), name


def test_apply_records_batch_uses_bounded_stream_apply() -> None:
    """The batch applier routes through the bounded ``stream_apply`` (NB-4)."""
    batch = next(m for m in _all_methods() if m.name == "_apply_records_batch")
    calls = {
        node.func.attr
        for node in ast.walk(batch.node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "stream_apply" in calls


def test_search_worker_failures_use_gated_emitters() -> None:
    """Worker exceptions travel through the same NB-10 generation gate."""
    methods = {(item.cls, item.name): item for item in _all_methods()}
    for class_name in ("HudLayout", "GrepLogLayout"):
        worker = methods[(class_name, "_run_search")]
        calls = {
            node.func.attr
            for node in ast.walk(worker.node)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "call_from_thread" not in calls, class_name
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "StreamingSearchFinished"
            for node in ast.walk(worker.node)
        ), class_name


def test_filter_completed_adopts_worker_prepared_model() -> None:
    """The pump callback performs no full-result projection work (NB-4/NB-5)."""
    methods = {(item.cls, item.name): item for item in _all_methods()}
    completed = methods[("HudLayout", "on_filter_completed")]
    filter_loaded = methods[("HudLayout", "filter_loaded")]
    worker = methods[("HudLayout", "_run_filter_worker")]

    assert "pump_only" in completed.decorators
    assert "offload" in worker.decorators
    assert not any(
        isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp))
        for node in ast.walk(completed.node)
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"id", "list", "set", "tuple"}
        for node in ast.walk(completed.node)
    )
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "all_records"
        for node in ast.walk(worker.node)
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "tuple"
        and any(
            isinstance(argument, ast.Attribute) and argument.attr == "all_records"
            for argument in node.args
        )
        for node in ast.walk(filter_loaded.node)
    )


def test_theme_changed_invalidates_the_virtual_viewport() -> None:
    """Theme changes invalidate lazy rows without scanning the result model."""
    method = next(
        item
        for item in _all_methods()
        if item.cls == "HudLayout" and item.name == "_on_theme_changed"
    )
    assert isinstance(method.node, ast.FunctionDef)
    assert "pump_only" in method.decorators
    call_attributes = [
        node.func
        for node in ast.walk(method.node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    results_refreshes = [
        attribute
        for attribute in call_attributes
        if attribute.attr == "refresh_theme"
        and isinstance(attribute.value, ast.Name)
        and attribute.value.id == "results"
    ]
    assert len(results_refreshes) == 1
    assert not any(attribute.attr == "stream_apply" for attribute in call_attributes)
    assert not any(
        isinstance(node, (ast.For, ast.ListComp, ast.SetComp, ast.DictComp))
        for node in ast.walk(method.node)
    )


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
        app.screen._search_done = False
        old_control = app.screen.control
        app.screen.action_stop_search()
        assert app.screen.control.answer_now_requested()
        app.screen._reset_search_chrome()
        assert app.screen.control is not old_control
        assert not app.screen.control.answer_now_requested()


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


# --- pump audit hook (denylist-free I/O-initiation guard) ------------------


class _AuditCase(t.NamedTuple):
    """One ``_pump_audit_hook`` scenario: inputs and the expected reaction."""

    test_id: str
    on_pump: bool
    armed: bool
    raising: bool
    event: str
    expect_raise: bool
    expect_log: bool


_AUDIT_CASES = (
    _AuditCase("raises_on_blocking_event", True, True, True, "subprocess.Popen", True, True),
    _AuditCase("ignores_non_blocking_event", True, True, True, "object.__getattr__", False, False),
    _AuditCase("ignores_off_pump_thread", False, True, True, "subprocess.Popen", False, False),
    _AuditCase("logs_without_raising", True, True, False, "sqlite3.connect", False, True),
    _AuditCase("inert_when_disarmed", True, False, True, "subprocess.Popen", False, False),
)


@pytest.mark.parametrize("case", _AUDIT_CASES, ids=lambda c: c.test_id)
def test_pump_audit_hook_behavior(
    case: _AuditCase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The hook flags a blocking-I/O initiation only when armed and on the pump.

    A non-I/O event — and a pure CPU spin, which emits no audit event — never
    trips it: the I/O-vs-CPU split of ADR 0011.
    """
    pump_id = threading.get_ident() if case.on_pump else -1
    monkeypatch.setattr(_runtime, "_pump_thread_id", pump_id)
    monkeypatch.setattr(_runtime, "_audit_armed", case.armed)
    monkeypatch.setattr(_runtime, "_audit_raises", case.raising)
    with caplog.at_level(logging.WARNING, logger="agentgrep.ui._runtime"):
        if case.expect_raise:
            with pytest.raises(_runtime.BlockingOnPumpError, match=case.event.split(".")[0]):
                _runtime._pump_audit_hook(case.event, ())
        else:
            _runtime._pump_audit_hook(case.event, ())
    logged = [r for r in caplog.records if hasattr(r, "agentgrep_pump_blocking_event")]
    assert bool(logged) is case.expect_log
    if case.expect_log:
        assert t.cast("str", logged[0].agentgrep_pump_blocking_event) == case.event


def test_arm_pump_audit_installs_and_disarms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arming installs and enables the hook; disarming makes it inert again."""
    monkeypatch.setattr(_runtime, "_audit_armed", False)
    monkeypatch.setattr(_runtime, "_audit_raises", False)
    _runtime.arm_pump_audit(raising=True)
    assert _runtime._audit_armed is True
    assert _runtime._audit_raises is True
    assert _runtime._audit_hook_installed is True
    _runtime.disarm_pump_audit()
    assert _runtime._audit_armed is False


# --- watchdog / guards / audit enable-predicate truth-table ----------------


class _PredicateCase(t.NamedTuple):
    """Inputs and the expected, decoupled outputs of the three enable predicates."""

    test_id: str
    env: str | None
    under_pytest: bool
    isatty: bool
    watchdog: bool
    guards: bool
    audit: bool


_PREDICATE_CASES = (
    # bare TTY: the watchdog runs but the (raising) asserts stay off — decoupled.
    _PredicateCase("bare_tty", None, False, True, True, False, False),
    _PredicateCase("bare_non_tty", None, False, False, False, False, False),
    _PredicateCase("env_off_overrides_tty", "0", False, True, False, False, False),
    _PredicateCase("env_on_arms_all", "1", False, False, True, True, True),
    _PredicateCase("under_pytest_no_env", None, True, True, False, True, False),
)


@pytest.mark.parametrize("case", _PREDICATE_CASES, ids=lambda c: c.test_id)
def test_runtime_enable_predicates(case: _PredicateCase, monkeypatch: pytest.MonkeyPatch) -> None:
    """watchdog_enabled / guard-asserts / audit_hook_enabled decouple over env, pytest, TTY."""
    if case.env is None:
        monkeypatch.delenv("AGENTGREP_TUI_WATCHDOG", raising=False)
    else:
        monkeypatch.setenv("AGENTGREP_TUI_WATCHDOG", case.env)
    if case.under_pytest:
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::y (call)")
    else:
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(_runtime, "_stdout_isatty", lambda: case.isatty)
    assert _runtime.watchdog_enabled() is case.watchdog
    assert _runtime._compute_guards_enabled() is case.guards
    assert _runtime.audit_hook_enabled() is case.audit
