"""The thin, fixed-composition Textual App shell (ADR 0013).

``ExplorerApp`` owns *only* App-lifecycle concerns: pi-lite theme registration,
native ANSI-background mode, the non-blocking pump bind / heartbeat watchdog /
audit hook (ADR 0011), and which :class:`~agentgrep.ui.layouts._base.LayoutScreen` and
:class:`~agentgrep.ui.workflows.Workflow` to mount. The validated pair is
injected as one immutable shell composition, which the shell never replaces.
All bindings and presentation belong to the layout, not here.

Textual is imported at module scope, so this module is reached only lazily (via
:func:`agentgrep.ui.app.build_streaming_ui_app`), keeping ``import agentgrep``
Textual-free (ADR 0010).
"""

from __future__ import annotations

import typing as t

from textual.app import App

from agentgrep.ui import _runtime, registry, theme as ui_theme

if t.TYPE_CHECKING:
    from textual.screen import Screen

    from agentgrep.ui._context import UiContext

__all__ = ["ExplorerApp"]


class ExplorerApp(App[None]):
    """Layout-agnostic shell with one validated immutable composition."""

    ENABLE_COMMAND_PALETTE: t.ClassVar[bool] = False
    COMMANDS: t.ClassVar[set[t.Any]] = set()
    #: The pi-lite global stylesheet (semantic tokens + all-widget rules). The
    #: ``$ag-*`` tokens it references always resolve via
    #: :meth:`get_theme_variable_defaults`, regardless of the active theme.
    CSS_PATH: t.ClassVar[str] = "styles.tcss"
    BINDINGS: t.ClassVar[list[t.Any]] = []

    def __init__(
        self,
        ctx: UiContext,
        *,
        composition: registry._UiComposition,
    ) -> None:
        super().__init__()
        # Register and activate the pi-lite themes before the stylesheet loads
        # (CSS is parsed during startup) so the ``$ag-*`` tokens it references
        # resolve from the active theme.
        self.register_theme(ui_theme.agentgrep_dark())
        self.register_theme(ui_theme.agentgrep_light())
        self.theme = ui_theme.DARK_THEME_NAME
        # Native ANSI background handling so the structural panes can use
        # ``ansi_default`` (the terminal's own background, SGR 49) like
        # pi/claude-code instead of a painted color.
        self.ansi_color = True
        self._ctx = ctx
        self._composition = composition

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """Merge the ``$ag-*`` token defaults so the stylesheet always resolves.

        Returns
        -------
        dict[str, str]
            Textual's defaults merged with :func:`agentgrep.ui.theme.ag_variable_defaults`
            so a switch to any built-in theme can't leave an ``$ag-*`` reference
            unresolved.
        """
        base = super().get_theme_variable_defaults()
        return {**base, **ui_theme.ag_variable_defaults()}

    @_runtime.pump_only
    def get_default_screen(self) -> Screen:
        """Mount the selected layout and workflow as the launch screen."""
        workflow = self._composition.workflow_type()
        layout = self._composition.layout_type(self._ctx, workflow)
        return t.cast("Screen", layout)

    def on_mount(self) -> None:
        """Bind the pump thread for the non-blocking guards (ADR 0011 NB-1/NB-8).

        The shell owns the pump, so the bind, the log-only heartbeat watchdog
        (default-on for an interactive TTY), and the opt-in audit hook live here;
        the layouts only carry ``@pump_only`` / ``@offload`` callables.
        """
        _runtime.bind_pump_thread()
        if _runtime.watchdog_enabled():
            self.set_interval(_runtime.HEARTBEAT_INTERVAL, _runtime.record_heartbeat)
            _runtime.start_pump_watchdog()
        if _runtime.audit_hook_enabled():
            _runtime.arm_pump_audit()

    def on_unmount(self) -> None:
        """Release the pump-thread binding and stop the watchdog on teardown."""
        _runtime.unbind_pump_thread()
        _runtime.stop_pump_watchdog()
        _runtime.disarm_pump_audit()
