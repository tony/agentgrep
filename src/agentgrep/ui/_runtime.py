"""Non-blocking runtime guards and helpers for the Textual explorer.

This module encodes ADR 0011's NB-1..NB-10 invariants as reusable, testable
primitives so the rules are structural rather than conventions a future edit can
silently drop:

- :func:`pump_only` / :func:`offload` decorators assert (in dev/test builds)
  that a callable runs on / off the event-loop thread (NB-1, NB-2, NB-8).
- :func:`stream_apply` applies a worker-produced collection to the UI in bounded
  chunks that yield between slices, so an unbounded apply is impossible (NB-4).
- :func:`make_gated_emitter` centralizes the "results bypass the message bus and
  carry a generation token" transport (NB-3, NB-10).
- A heartbeat watchdog (:func:`start_pump_watchdog`), default-on for an
  interactive TTY, logs when the pump stalls past a threshold; the deterministic
  runtime-oracle test pins its warning and structured threshold fields.

It is Textual-free and imports only the standard library, so it sits below the
application shell in the layering (ADR 0010), where focused runtime tests can
reach it directly. The guards are no-ops
unless enabled (under pytest, or when ``AGENTGREP_TUI_WATCHDOG`` is truthy), so
production pays at most one boolean check per guarded call; the log-only
watchdog additionally defaults on for an interactive TTY.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import functools
import inspect
import logging
import os
import sys
import threading
import time
import typing as t

logger = logging.getLogger(__name__)

__all__ = [
    "HEARTBEAT_INTERVAL",
    "STALL_THRESHOLD_MS",
    "BlockingOnPumpError",
    "arm_pump_audit",
    "assert_off_pump",
    "assert_on_pump",
    "audit_hook_enabled",
    "bind_pump_thread",
    "disarm_pump_audit",
    "guards_enabled",
    "install_pump_audit_hook",
    "make_gated_emitter",
    "offload",
    "pump_only",
    "record_heartbeat",
    "set_guards_enabled",
    "start_pump_watchdog",
    "stop_pump_watchdog",
    "stream_apply",
    "unbind_pump_thread",
    "watchdog_enabled",
]

#: Heartbeat cadence (seconds): 10x faster than the stall threshold so a single
#: missed beat is unambiguous.
HEARTBEAT_INTERVAL = 0.5
#: A pump that has not beaten for this long is considered wedged.
STALL_THRESHOLD_MS = 1000


def _truthy(value: str | None) -> bool:
    """Return whether an env-var string is a truthy opt-in."""
    return bool(value) and value.strip().lower() not in {"", "0", "false", "no", "off"}


def _stdout_isatty() -> bool:
    """Return whether stdout is an interactive terminal."""
    try:
        return bool(sys.stdout.isatty())
    except AttributeError, ValueError:  # detached / closed stream
        return False


def _watchdog_explicitly_enabled() -> bool:
    """Return whether ``AGENTGREP_TUI_WATCHDOG`` is set to a truthy value."""
    return _truthy(os.environ.get("AGENTGREP_TUI_WATCHDOG"))


def watchdog_enabled() -> bool:
    """Return whether the heartbeat watchdog should run.

    The watchdog is log-only (it never raises), so it defaults *on* for an
    interactive terminal session — the case where a frozen pump actually hurts a
    user. ``AGENTGREP_TUI_WATCHDOG`` is an explicit override in either direction;
    set it to a falsey value to force the watchdog off. It never auto-starts
    under pytest (tests opt in explicitly), so a captured-stdout run stays quiet.

    Returns
    -------
    bool
        ``True`` when ``AGENTGREP_TUI_WATCHDOG`` is truthy, or — when the
        variable is unset and not under pytest — when stdout is a TTY.
    """
    raw = os.environ.get("AGENTGREP_TUI_WATCHDOG")
    if raw is not None:
        return _truthy(raw)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return _stdout_isatty()


def audit_hook_enabled() -> bool:
    """Return whether the pump audit hook should be installed and armed.

    Opt-in via ``AGENTGREP_TUI_WATCHDOG`` (the same debug switch as the heartbeat
    watchdog). It is *not* enabled for a bare interactive run: the audit hook is
    process-wide and, under pytest, escalates a pump-thread blocking-I/O
    initiation to a raised :class:`BlockingOnPumpError`, so a normal user's
    session is never converted into an exception.

    Returns
    -------
    bool
        ``True`` when ``AGENTGREP_TUI_WATCHDOG`` is truthy.
    """
    return _watchdog_explicitly_enabled()


def _compute_guards_enabled() -> bool:
    """Return whether the pump-thread assertions should fire.

    Active under pytest (so violations fail CI) or when ``AGENTGREP_TUI_WATCHDOG``
    is explicitly truthy; off otherwise so production pays only a boolean check.
    Deliberately *not* tied to :func:`watchdog_enabled`: the assertions can raise,
    so a bare interactive TTY (where the watchdog runs log-only) must not arm
    them and risk crashing a user on a latent violation.
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or _watchdog_explicitly_enabled()


_GUARDS_ENABLED: bool = _compute_guards_enabled()
_pump_thread_id: int | None = None


def guards_enabled() -> bool:
    """Return whether the ``@pump_only`` / ``@offload`` assertions are active."""
    return _GUARDS_ENABLED


def set_guards_enabled(value: bool) -> None:
    """Override whether the pump-thread assertions fire (test hook).

    Parameters
    ----------
    value : bool
        ``True`` to activate the assertions, ``False`` to disable them.
    """
    global _GUARDS_ENABLED
    _GUARDS_ENABLED = value


def bind_pump_thread() -> None:
    """Record the calling thread as the event-loop/pump thread.

    Call once from ``App.on_mount`` (which Textual runs on the pump thread) so
    :func:`assert_on_pump` / :func:`assert_off_pump` have a reference identity.
    """
    global _GUARDS_ENABLED, _pump_thread_id
    _GUARDS_ENABLED = _compute_guards_enabled()
    _pump_thread_id = threading.get_ident()


def unbind_pump_thread() -> None:
    """Clear the recorded pump thread (call from ``App.on_unmount``).

    Scopes the binding to a mounted app so the guards stay inert outside an app
    run — e.g. a unit test that calls a worker body directly on the main thread
    is not a real NB-2 violation and must not trip ``assert_off_pump``.
    """
    global _pump_thread_id
    _pump_thread_id = None


def assert_on_pump(where: str) -> None:
    """Raise if not running on the bound pump thread (NB-1/NB-8).

    No-op when the pump thread is unbound. Callers gate this on
    :func:`guards_enabled`, so it never runs in production.

    Parameters
    ----------
    where : str
        Identifier for the offending callable, used in the error message.
    """
    if _pump_thread_id is None:
        return
    if threading.get_ident() != _pump_thread_id:
        msg = f"{where} must run on the pump thread (NB-1/NB-8)"
        raise AssertionError(msg)


def assert_off_pump(where: str) -> None:
    """Raise if running ON the bound pump thread (NB-2).

    No-op when the pump thread is unbound. Callers gate this on
    :func:`guards_enabled`, so it never runs in production.

    Parameters
    ----------
    where : str
        Identifier for the offending callable, used in the error message.
    """
    if _pump_thread_id is None:
        return
    if threading.get_ident() == _pump_thread_id:
        msg = f"{where} must run off the pump thread (NB-2)"
        raise AssertionError(msg)


def _guarded[F: cabc.Callable[..., t.Any]](fn: F, check: cabc.Callable[[str], None]) -> F:
    """Wrap ``fn`` so ``check`` runs at call entry when guards are enabled.

    Preserves coroutine-function identity so Textual's ``call_from_thread`` /
    direct ``await`` still see an awaitable.
    """
    where = getattr(fn, "__qualname__", getattr(fn, "__name__", repr(fn)))
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: t.Any, **kwargs: t.Any) -> t.Any:
            if _GUARDS_ENABLED:
                check(where)
            return await fn(*args, **kwargs)

        return t.cast("F", async_wrapper)

    @functools.wraps(fn)
    def sync_wrapper(*args: t.Any, **kwargs: t.Any) -> t.Any:
        if _GUARDS_ENABLED:
            check(where)
        return fn(*args, **kwargs)

    return t.cast("F", sync_wrapper)


def pump_only[F: cabc.Callable[..., t.Any]](fn: F) -> F:
    """Mark a callable that must run on the pump thread (NB-1/NB-5/NB-8).

    In dev/test builds the wrapper asserts it runs on the bound pump thread;
    in production it is a single boolean check.
    """
    return _guarded(fn, assert_on_pump)


def offload[F: cabc.Callable[..., t.Any]](fn: F) -> F:
    """Mark a worker-body callable that must run off the pump thread (NB-2).

    In dev/test builds the wrapper asserts it does *not* run on the pump
    thread; in production it is a single boolean check.
    """
    return _guarded(fn, assert_off_pump)


async def stream_apply[T](
    items: cabc.Sequence[T],
    apply_chunk: cabc.Callable[[cabc.Sequence[T]], None],
    *,
    chunk_size: int = 200,
    yield_between: cabc.Callable[[], cabc.Awaitable[None]] | None = None,
) -> None:
    """Apply ``items`` to the UI in bounded chunks, yielding between slices (NB-4).

    Encodes the chunk cap and the inter-slice ``await`` structurally so a
    single large batch can never freeze the pump.

    Parameters
    ----------
    items : collections.abc.Sequence
        The worker-produced collection to apply.
    apply_chunk : collections.abc.Callable
        Applies one bounded slice to the UI (called on the pump thread).
    chunk_size : int, optional
        Maximum slice length. Must be positive, by default ``200``.
    yield_between : collections.abc.Callable, optional
        Awaitable factory used to yield to the event loop between slices; by
        default ``asyncio.sleep(0)``.

    Raises
    ------
    ValueError
        If ``chunk_size`` is not positive — an unbounded apply is impossible.
    """
    if chunk_size <= 0:
        msg = "chunk_size must be positive"
        raise ValueError(msg)
    yield_fn = yield_between if yield_between is not None else _sleep_zero
    total = len(items)
    for start in range(0, total, chunk_size):
        apply_chunk(items[start : start + chunk_size])
        if start + chunk_size < total:
            await yield_fn()


async def _sleep_zero() -> None:
    """Yield one event-loop turn (the default :func:`stream_apply` yield)."""
    await asyncio.sleep(0)


def make_gated_emitter(
    call_from_thread: cabc.Callable[..., t.Any],
    apply: cabc.Callable[..., t.Any],
    generation: int,
) -> cabc.Callable[[object], None]:
    """Return an ``emit(event)`` that forwards via ``call_from_thread`` (NB-3/NB-10).

    Centralizes the streaming transport: results bypass the message bus and
    carry the captured ``generation`` so a draining superseded worker's events
    are dropped on the pump.

    Parameters
    ----------
    call_from_thread : collections.abc.Callable
        The app's ``call_from_thread`` (schedules ``apply`` on the pump).
    apply : collections.abc.Callable
        The pump-side handler, invoked as ``apply(generation, event)``.
    generation : int
        Chrome generation captured at emitter creation.

    Returns
    -------
    collections.abc.Callable
        A worker-thread ``emit(event)`` callable.
    """

    def emit(event: object) -> None:
        call_from_thread(apply, generation, event)

    return emit


# --- heartbeat watchdog ----------------------------------------------------

_last_heartbeat: float = 0.0
_watchdog_thread: threading.Thread | None = None
_watchdog_stop: threading.Event | None = None
_WATCHDOG_NAME = "agentgrep-pump-watchdog"


def record_heartbeat() -> None:
    """Stamp the current time as the latest pump heartbeat.

    Armed via ``App.set_interval`` so a wedged pump stops stamping and the
    watchdog notices.
    """
    global _last_heartbeat
    _last_heartbeat = time.monotonic()


def start_pump_watchdog(
    *,
    stall_threshold_ms: int = STALL_THRESHOLD_MS,
    poll_seconds: float = 0.25,
) -> None:
    """Start the daemon thread that logs when the pump stalls.

    Idempotent: a second call while running is a no-op. The thread is a daemon
    and is stopped/joined by :func:`stop_pump_watchdog`.

    Parameters
    ----------
    stall_threshold_ms : int, optional
        A gap larger than this between heartbeats logs a stall, by default
        :data:`STALL_THRESHOLD_MS`.
    poll_seconds : float, optional
        How often the watcher samples the heartbeat, by default ``0.25``.
    """
    global _watchdog_thread, _watchdog_stop
    if _watchdog_thread is not None and _watchdog_thread.is_alive():
        return
    record_heartbeat()
    stop = threading.Event()
    _watchdog_stop = stop
    threshold_s = stall_threshold_ms / 1000.0

    def _watch() -> None:
        warned = False
        while not stop.wait(poll_seconds):
            stall = time.monotonic() - _last_heartbeat
            if stall > threshold_s:
                if not warned:
                    warned = True
                    logger.warning(
                        "pump heartbeat stalled",
                        extra={
                            "agentgrep_pump_stall_ms": int(stall * 1000),
                            "agentgrep_pump_thread_id": _pump_thread_id or 0,
                            "agentgrep_pump_heartbeat_interval_ms": int(
                                HEARTBEAT_INTERVAL * 1000,
                            ),
                            "agentgrep_pump_stall_threshold_ms": stall_threshold_ms,
                        },
                    )
            elif warned:
                warned = False
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "pump heartbeat resumed",
                        extra={"agentgrep_pump_stall_ms": int(stall * 1000)},
                    )

    thread = threading.Thread(target=_watch, name=_WATCHDOG_NAME, daemon=True)
    _watchdog_thread = thread
    thread.start()


def stop_pump_watchdog(timeout: float = 1.0) -> None:
    """Stop and join the watchdog thread (idempotent).

    Parameters
    ----------
    timeout : float, optional
        Seconds to wait for the thread to exit, by default ``1.0``.
    """
    global _watchdog_thread, _watchdog_stop
    if _watchdog_stop is not None:
        _watchdog_stop.set()
    if _watchdog_thread is not None:
        _watchdog_thread.join(timeout=timeout)
    _watchdog_thread = None
    _watchdog_stop = None


# --- pump-thread audit hook ------------------------------------------------
#
# ``sys.addaudithook`` (PEP 578) fires synchronously on the *acting* thread, so
# a hook can detect — and, by raising, abort — a blocking I/O *initiation* on
# the pump thread regardless of how the call was spelled, aliased, or
# dynamically dispatched. This is the denylist-free complement to manual static
# review: it sees the event, not the source text. It is blind to pure CPU
# spin, byte-transfer on already-open handles (``socket.recv``, ``cursor.execute``
# on a live connection), and native code that issues syscalls without
# ``PySys_Audit`` — the heartbeat watchdog is the cause-agnostic backstop for
# that residue (ADR 0011).


class BlockingOnPumpError(RuntimeError):
    """A blocking-I/O initiation was attempted on the pump thread (NB-1)."""


#: Audit events that signal a blocking I/O *initiation*. Deliberately excludes
#: ``open`` and ``import`` — Textual/Rich read theme and syntax files on the
#: pump during render and lazy imports are legitimate — and byte-transfer events
#: (``socket.recv`` etc. carry no audit event at all).
_AUDIT_BLOCKING_EVENTS = frozenset(
    {
        "socket.connect",
        "socket.getaddrinfo",
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.spawn",
        "time.sleep",
        "sqlite3.connect",
    },
)

_audit_hook_installed: bool = False
_audit_armed: bool = False
_audit_raises: bool = False


def _pump_audit_hook(event: str, _args: tuple[object, ...]) -> None:
    """Flag a blocking-I/O initiation on the pump thread.

    Installed process-wide but inert unless :func:`arm_pump_audit` has armed it.
    On the bound pump thread it logs a warning carrying
    ``agentgrep_pump_blocking_event``; when armed in raising mode it additionally
    raises :class:`BlockingOnPumpError`, aborting the syscall before it runs.
    """
    if not _audit_armed:
        return
    pump_id = _pump_thread_id
    if pump_id is None or threading.get_ident() != pump_id:
        return
    if event not in _AUDIT_BLOCKING_EVENTS:
        return
    logger.warning(
        "blocking io initiated on pump",
        extra={
            "agentgrep_pump_blocking_event": event,
            "agentgrep_pump_thread_id": pump_id,
        },
    )
    if _audit_raises:
        msg = f"{event} initiated on the pump thread (NB-1)"
        raise BlockingOnPumpError(msg)


def install_pump_audit_hook() -> None:
    """Install the process-wide pump audit hook (idempotent).

    ``sys.addaudithook`` cannot be removed, so the hook stays installed for the
    process lifetime; it is a single boolean check per audit event until
    :func:`arm_pump_audit` arms it.
    """
    global _audit_hook_installed
    if _audit_hook_installed:
        return
    sys.addaudithook(_pump_audit_hook)
    _audit_hook_installed = True


def arm_pump_audit(*, raising: bool | None = None) -> None:
    """Arm the pump audit hook, installing it first if needed.

    Parameters
    ----------
    raising : bool, optional
        Whether a flagged event raises :class:`BlockingOnPumpError`. Defaults to
        raising under pytest (so a violation fails CI) and logging only otherwise
        (so an interactive session is not converted into a crash).
    """
    global _audit_armed, _audit_raises
    install_pump_audit_hook()
    _audit_raises = bool(os.environ.get("PYTEST_CURRENT_TEST")) if raising is None else raising
    _audit_armed = True


def disarm_pump_audit() -> None:
    """Make the pump audit hook inert (the hook itself stays installed)."""
    global _audit_armed
    _audit_armed = False
