"""Thin, fixed-composition Textual application shell (ADR 0013).

``ExplorerApp`` owns app lifecycle, the immutable layout/workflow composition,
theme registration, onboarding, and serialized UI-preference persistence. The
search engine and MCP surfaces remain outside this module.

Textual is imported at module scope, so this module is reached only lazily via
:func:`agentgrep.ui.app.build_streaming_ui_app`.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import pathlib
import queue
import threading
import typing as t
from dataclasses import dataclass

from textual.app import App
from textual.binding import BindingType
from textual.worker import Worker, WorkerState

from agentgrep.ui import _runtime, preferences, registry, theme as ui_theme
from agentgrep.ui.widgets.theme_picker import ThemePicker

if t.TYPE_CHECKING:
    from textual.screen import Screen

    from agentgrep.ui._context import UiContext

__all__ = ["ExplorerApp"]

_EXPLORER_MODE = "explorer"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ThemeSaveRequest:
    """One serialized preference write."""

    generation: int
    theme_name: str


class _ThemeSaveMailbox:
    """Coalesce theme requests behind one off-pump writer."""

    def __init__(self) -> None:
        self._requests: queue.SimpleQueue[_ThemeSaveRequest] = queue.SimpleQueue()
        self._write_lock = threading.Lock()
        self._latest: _ThemeSaveRequest | None = None
        self._persisted_generation = 0

    def enqueue(self, request: _ThemeSaveRequest) -> None:
        """Add a request without waiting for the active filesystem write."""
        self._requests.put(request)

    def _drain(self) -> None:
        """Record the newest queued request while holding the writer lock."""
        while True:
            try:
                self._latest = self._requests.get_nowait()
            except queue.Empty:
                return

    def _save(self, request: _ThemeSaveRequest, config_path: pathlib.Path) -> bool:
        """Persist one current request while holding the writer lock."""
        try:
            saved = preferences.save_theme_name(request.theme_name, config_path)
        except Exception:
            return False
        if saved:
            self._persisted_generation = request.generation
        return saved

    def save(
        self,
        config_path: pathlib.Path,
        request: _ThemeSaveRequest | None = None,
    ) -> bool:
        """Persist a current request, or flush the newest request at teardown."""
        with self._write_lock:
            self._drain()
            latest = self._latest
            if latest is None:
                return True
            selected = latest if request is None else request
            if selected.generation < latest.generation:
                return True
            if selected.generation <= self._persisted_generation:
                return True
            return self._save(selected, config_path)


@_runtime.offload
def _save_theme_selection(
    mailbox: _ThemeSaveMailbox,
    config_path: pathlib.Path,
    request: _ThemeSaveRequest | None = None,
) -> bool:
    """Flush the newest detached theme choice away from the Textual pump."""
    return mailbox.save(config_path, request)


class ExplorerApp(App[None]):
    """Layout-agnostic shell with one validated immutable composition."""

    ENABLE_COMMAND_PALETTE: t.ClassVar[bool] = False
    COMMANDS: t.ClassVar[set[t.Any]] = set()
    CSS_PATH: t.ClassVar[str] = "styles.tcss"
    BINDINGS: t.ClassVar[list[BindingType]] = []

    def __init__(
        self,
        ctx: UiContext,
        *,
        composition: registry._UiComposition,
        selected_theme: str | None = None,
        config_path: pathlib.Path | None = None,
        offer_theme_setup: bool = False,
    ) -> None:
        super().__init__()
        for profile in ui_theme.THEME_PROFILES:
            self.register_theme(profile.build())
        valid_selection = (
            selected_theme if selected_theme in ui_theme.THEME_PROFILE_BY_NAME else None
        )
        self.theme = valid_selection or ui_theme.DARK_THEME_NAME
        self.ansi_color = True
        self._ctx = ctx
        self._composition = composition
        self._theme_config_path = config_path or preferences.theme_config_path(home=ctx.home)
        self._needs_theme_setup = offer_theme_setup and valid_selection is None
        if self._needs_theme_setup:
            self.add_mode(_EXPLORER_MODE, self._build_layout_screen)
        self._theme_save_generation = 0
        self._theme_save_pending: _ThemeSaveRequest | None = None
        self._theme_save_active: _ThemeSaveRequest | None = None
        self._theme_save_worker: Worker[bool] | None = None
        self._theme_save_mailbox = _ThemeSaveMailbox()

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """Merge agentgrep tokens so unowned Textual themes remain safe."""
        base = super().get_theme_variable_defaults()
        return {**base, **ui_theme.ag_variable_defaults()}

    @_runtime.pump_only
    def _build_layout_screen(self) -> Screen:
        """Construct the immutable layout/workflow pair on demand."""
        workflow = self._composition.workflow_type()
        return t.cast("Screen", self._composition.layout_type(self._ctx, workflow))

    @_runtime.pump_only
    def get_default_screen(self) -> Screen:
        """Return onboarding first, otherwise the selected explorer layout."""
        if self._needs_theme_setup:
            return t.cast(
                "Screen",
                ThemePicker(self.theme, initial_setup=True),
            )
        return self._build_layout_screen()

    @_runtime.pump_only
    def on_mount(self) -> None:
        """Bind pump guards and start the optional watchdog/audit hooks."""
        _runtime.bind_pump_thread()
        self.theme_changed_signal.subscribe(self, self._on_theme_changed)
        if _runtime.watchdog_enabled():
            self.set_interval(_runtime.HEARTBEAT_INTERVAL, _runtime.record_heartbeat)
            _runtime.start_pump_watchdog()
        if _runtime.audit_hook_enabled():
            _runtime.arm_pump_audit()

    @_runtime.pump_only
    def _on_theme_changed(self, _selected_theme: object) -> None:
        """Preserve Textual's ANSI-aware rendering across profile changes."""
        if self.ansi_color is True:
            return
        self.ansi_color = True
        self.refresh_css(animate=False)

    @_runtime.pump_only
    def open_theme_picker(self) -> bool:
        """Open one runtime picker, returning whether it was pushed."""
        if isinstance(self.screen, ThemePicker):
            return False
        self.push_screen(ThemePicker(self.theme, initial_setup=False))
        return True

    @_runtime.pump_only
    def commit_theme_picker(self, picker: ThemePicker, theme_name: str) -> bool:
        """Apply a picker selection, close setup, and persist in background."""
        if self.screen is not picker or theme_name not in ui_theme.THEME_PROFILE_BY_NAME:
            return False
        self.theme = theme_name
        if picker.initial_setup:
            self._needs_theme_setup = False
            self.switch_mode(_EXPLORER_MODE)
        else:
            self.pop_screen()
        self._queue_theme_save(theme_name)
        return True

    @_runtime.pump_only
    def cancel_theme_picker(self, picker: ThemePicker) -> None:
        """Close a runtime picker or continue setup without persisting."""
        if self.screen is not picker:
            return
        if picker.initial_setup:
            self._needs_theme_setup = False
            self.switch_mode(_EXPLORER_MODE)
        else:
            self.pop_screen()

    @_runtime.pump_only
    def select_theme(self, theme_name: str) -> bool:
        """Apply and persist one valid profile name."""
        if theme_name not in ui_theme.THEME_PROFILE_BY_NAME:
            return False
        self.theme = theme_name
        self._queue_theme_save(theme_name)
        return True

    def _queue_theme_save(self, theme_name: str) -> None:
        """Coalesce preference writes while preserving final-choice ordering."""
        self._theme_save_generation += 1
        request = _ThemeSaveRequest(self._theme_save_generation, theme_name)
        self._theme_save_pending = request
        self._theme_save_mailbox.enqueue(request)
        if self._theme_save_worker is None:
            self._start_theme_save(request)

    def _start_theme_save(self, request: _ThemeSaveRequest) -> None:
        """Start one exclusive worker after the preceding write has finished."""
        self._theme_save_active = request
        try:
            self._theme_save_worker = self.run_worker(
                functools.partial(
                    _save_theme_selection,
                    self._theme_save_mailbox,
                    self._theme_config_path,
                    request,
                ),
                name="theme-config",
                group="theme-config",
                description="save theme",
                exit_on_error=False,
                thread=True,
                exclusive=True,
            )
        except RuntimeError:
            self._theme_save_active = None
            self._finish_theme_save(saved=False)

    @_runtime.pump_only
    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Advance the serialized preference queue after its worker terminates."""
        if event.worker is not self._theme_save_worker:
            return
        if event.state not in {WorkerState.CANCELLED, WorkerState.ERROR, WorkerState.SUCCESS}:
            return
        request = self._theme_save_active
        worker = self._theme_save_worker
        self._theme_save_active = None
        self._theme_save_worker = None
        if request is None:
            return
        pending = self._theme_save_pending
        if pending is not None and pending.generation != request.generation:
            self._start_theme_save(pending)
            return
        self._theme_save_pending = None
        saved = event.state is WorkerState.SUCCESS and bool(worker.result)
        self._finish_theme_save(saved=saved)

    def _finish_theme_save(self, *, saved: bool) -> None:
        """Surface a durable-write failure without rolling back the session."""
        if not saved:
            self.notify(
                "Theme is active for this session, but the preference could not be saved.",
                title="Theme",
                severity="warning",
            )

    @_runtime.pump_only
    async def on_unmount(self) -> None:
        """Flush the latest theme off-pump, then release runtime resources."""
        pending = self._theme_save_pending
        _runtime.stop_pump_watchdog()
        _runtime.disarm_pump_audit()
        try:
            if pending is not None:
                saved = await asyncio.to_thread(
                    _save_theme_selection,
                    self._theme_save_mailbox,
                    self._theme_config_path,
                )
                self._theme_save_pending = None
                if not saved:
                    logger.warning(
                        "theme preference save failed during shutdown",
                        extra={"agentgrep_theme": pending.theme_name},
                    )
        finally:
            _runtime.unbind_pump_thread()
