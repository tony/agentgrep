"""Pump-placement contract for the explorer's ``/status`` command.

``/status`` reports the git ref the running code came from, and resolving that
ref spawns ``git describe``. A subprocess on the UI thread freezes the whole
interface, so these contracts pin *where* the probe runs: never on the
handler's own thread, always inside the ``thread=True`` worker whose body the
``@offload`` guard refuses to run on the UI thread.
"""

from __future__ import annotations

import collections.abc as cabc
import logging
import pathlib
import threading
import typing as t

import pytest
from textual.app import App

from agentgrep import _version
from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery
from agentgrep.ui import _runtime, commands, registry
from agentgrep.ui._context import UiContext
from agentgrep.ui.layouts import _base

pytestmark = pytest.mark.tui


class _IdleInvoker:
    """Search-invoker double that never touches a store."""

    def run(
        self,
        query: SearchQuery,
        *,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Accept a request and do nothing with it."""
        del query, control, emit


def _idle_query() -> SearchQuery:
    """Return the empty launch plan the explorer opens in browse mode with."""
    return SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )


def _build_layout(home: pathlib.Path, layout_name: str) -> t.Any:
    """Construct one unmounted layout parented to an app that owns the DOM."""
    ctx = UiContext(
        home=home,
        invoker=t.cast("t.Any", _IdleInvoker()),
        query=_idle_query(),
        control=SearchControl(),
        base_scope="prompts",
        base_effort="prompt",
    )
    layout_spec = registry.layout_spec(layout_name)
    workflow_spec = registry.workflow_spec("search")
    assert layout_spec is not None
    assert workflow_spec is not None
    layout = t.cast("t.Any", layout_spec.loader()(ctx, workflow_spec.loader()()))
    # ``report_build_status`` captures ``self.app.call_from_thread`` on the
    # pump, exactly as the screenshot worker does. Parenting the layout is how
    # Textual resolves ``.app`` for a node that is not mounted.
    layout._parent = App[None]()
    return layout


def test_status_is_registered_with_a_version_alias() -> None:
    """``/status`` and ``/version`` resolve to the same handler."""
    status = commands.resolve_command("status")
    assert status is not None
    assert status.run is commands._run_status
    assert commands.resolve_command("version") is status
    assert not status.accepts_args


@pytest.mark.parametrize("layout_name", registry.layout_names())
def test_status_starts_an_offloaded_worker_instead_of_probing_git(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    layout_name: str,
) -> None:
    """The handler resolves nothing itself; it hands the probe to a worker.

    Every layout registers the shared command, so every layout has to route it
    off the pump.
    """
    monkeypatch.setattr(_version, "_cached_provenance", None)
    layout = _build_layout(tmp_path, layout_name)
    spawned: list[tuple[cabc.Callable[[], object], dict[str, object]]] = []
    monkeypatch.setattr(
        layout,
        "run_worker",
        lambda target, **kwargs: spawned.append((target, kwargs)),
    )
    presented: list[_version.BuildProvenance] = []
    monkeypatch.setattr(layout, "_present_build_status", presented.append)

    assert commands._run_status(layout, "") is True

    assert len(spawned) == 1
    _target, kwargs = spawned[0]
    assert kwargs["thread"] is True
    assert kwargs["exclusive"] is True
    assert kwargs["group"] == "build-status"
    # Nothing was reported yet, and — the point of the contract — nothing was
    # resolved on the calling thread.
    assert presented == []
    assert _version.cached_build_provenance() is None


def test_status_initiates_no_blocking_io_on_the_bound_pump_thread(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The armed audit hook sees no covered blocking event from the handler.

    ``sys.addaudithook`` fires on the acting thread regardless of how a call was
    spelled or dispatched, so an armed raising hook converts a pump-side
    ``subprocess.Popen`` into :class:`~agentgrep.ui._runtime.BlockingOnPumpError`
    before the process is spawned. Running the real handler under it is the
    strongest available proof that no aliasing hides one.
    """
    monkeypatch.setattr(_version, "_cached_provenance", None)
    layout = _build_layout(tmp_path, registry.DEFAULT_LAYOUT)
    monkeypatch.setattr(layout, "run_worker", lambda _target, **_kwargs: None)
    monkeypatch.setattr(layout, "_present_build_status", lambda _provenance: None)

    _runtime.bind_pump_thread()
    _runtime.arm_pump_audit(raising=True)
    try:
        assert commands._run_status(layout, "") is True
    finally:
        _runtime.disarm_pump_audit()
        _runtime.unbind_pump_thread()

    assert _version.cached_build_provenance() is None


def test_the_probe_itself_trips_the_audit_hook_on_the_pump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: the guard the previous test relies on is not vacuous.

    Calling the probe on the bound pump thread must abort — otherwise "the
    handler tripped nothing" would prove nothing about the handler.
    """
    monkeypatch.setattr(_version, "_cached_provenance", None)
    monkeypatch.setattr(_version, "_cached_release", _version.release_version())

    _runtime.bind_pump_thread()
    _runtime.arm_pump_audit(raising=True)
    try:
        with pytest.raises(_runtime.BlockingOnPumpError):
            _ = _version.build_provenance()
    finally:
        _runtime.disarm_pump_audit()
        _runtime.unbind_pump_thread()


def test_status_worker_body_refuses_to_run_on_the_pump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``@offload`` fails the worker body if a later edit calls it inline."""
    monkeypatch.setattr(_version, "_cached_provenance", None)

    _runtime.bind_pump_thread()
    try:
        with pytest.raises(AssertionError, match="off the pump thread"):
            _base._resolve_build_provenance(
                lambda *_args: None,
                lambda _provenance: None,
            )
    finally:
        _runtime.unbind_pump_thread()


def test_status_worker_hands_the_resolved_provenance_back_to_the_pump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker probes off the pump and marshals the result back."""
    monkeypatch.setattr(_version, "_cached_provenance", None)
    monkeypatch.setattr(_version, "_git_describe", lambda: "v9.9.9-3-gfeedface")
    marshalled: list[tuple[object, tuple[object, ...]]] = []
    presented: list[_version.BuildProvenance] = []

    def _call_from_thread(callback: object, *args: object) -> None:
        marshalled.append((callback, args))

    worker = threading.Thread(
        target=lambda: _base._resolve_build_provenance(
            _call_from_thread,
            presented.append,
        ),
    )
    _runtime.bind_pump_thread()
    try:
        worker.start()
        worker.join(timeout=5)
    finally:
        _runtime.unbind_pump_thread()

    assert not worker.is_alive()
    assert len(marshalled) == 1
    callback, args = marshalled[0]
    assert callback == presented.append
    # Presentation happens on the pump, where the marshalled callback lands.
    t.cast("cabc.Callable[..., None]", callback)(*args)
    assert presented == [
        _version.BuildProvenance(_version.release_version(), "v9.9.9-3-gfeedface"),
    ]


def test_status_reports_a_cached_provenance_without_a_worker(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second ``/status`` reads the cache instead of spawning git again."""
    cached = _version.BuildProvenance("9.9.9", "v9.9.9-6-gcab6f56b")
    monkeypatch.setattr(_version, "_cached_provenance", cached)
    layout = _build_layout(tmp_path, registry.DEFAULT_LAYOUT)
    spawned: list[object] = []
    monkeypatch.setattr(layout, "run_worker", lambda target, **_kwargs: spawned.append(target))
    presented: list[_version.BuildProvenance] = []
    monkeypatch.setattr(layout, "_present_build_status", presented.append)

    assert commands._run_status(layout, "") is True

    assert spawned == []
    assert presented == [cached]


@pytest.mark.slow
async def test_mounted_status_command_notifies_the_build_report(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Drive ``/status`` through a running app with the audit hook watching.

    The handler runs on the real pump thread here, so a covered blocking-I/O
    initiation would be logged against it. The hook is armed log-only: a raising
    hook inside a live Textual driver would abort the app rather than report.
    """
    from agentgrep.ui.app import build_streaming_ui_app

    monkeypatch.setattr(_version, "_cached_provenance", None)
    app = t.cast(
        "t.Any",
        build_streaming_ui_app(
            tmp_path,
            _idle_query(),
            control=SearchControl(),
        ),
    )

    with caplog.at_level(logging.WARNING, logger="agentgrep.ui._runtime"):
        async with app.run_test(size=(100, 30)) as pilot:
            _runtime.arm_pump_audit(raising=False)
            try:
                assert app.screen._dispatch_slash_text("/status") is True
                _ = await app.workers.wait_for_complete()
                await pilot.pause()
            finally:
                _runtime.disarm_pump_audit()

    blocking = [
        record
        for record in caplog.records
        if getattr(record, "agentgrep_pump_blocking_event", None) is not None
    ]
    assert blocking == []

    resolved = _version.cached_build_provenance()
    assert resolved is not None
    messages = [str(notification.message) for notification in app._notifications]
    assert _version.format_build_status(resolved) in messages
