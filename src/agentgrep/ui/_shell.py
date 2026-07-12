"""The thin, layout-agnostic Textual App shell (ADR 0013).

``ExplorerApp`` owns *only* App-lifecycle concerns: pi-lite theme registration,
native ANSI-background mode, the non-blocking pump bind / heartbeat watchdog /
audit hook (ADR 0011), and which :class:`~agentgrep.ui.layouts._base.LayoutScreen` and
:class:`~agentgrep.ui.workflows.Workflow` to mount — plus the runtime switch
between them (``f2`` cycles layouts, ``f3`` cycles workflows). All composition,
bindings, and presentation belong to the layout, not here.

Textual is imported at module scope, so this module is reached only lazily (via
:func:`agentgrep.ui.app.build_streaming_ui_app`), keeping ``import agentgrep``
Textual-free (ADR 0010).
"""

from __future__ import annotations

import collections.abc as cabc
import typing as t

from textual.app import App
from textual.binding import Binding

from agentgrep.ui import _runtime, registry, theme as ui_theme
from agentgrep.ui.layouts._base import LayoutScreen

if t.TYPE_CHECKING:
    from textual.screen import Screen

    from agentgrep.ui._context import UiContext
    from agentgrep.ui.workflows import Workflow

__all__ = ["ExplorerApp"]


class ExplorerApp(App[None]):
    """Layout-agnostic shell: theme, pump lifecycle, and pluggable layout switching."""

    ENABLE_COMMAND_PALETTE: t.ClassVar[bool] = False
    COMMANDS: t.ClassVar[set[t.Any]] = set()
    #: The pi-lite global stylesheet (semantic tokens + all-widget rules). The
    #: ``$ag-*`` tokens it references always resolve via
    #: :meth:`get_theme_variable_defaults`, regardless of the active theme.
    CSS_PATH: t.ClassVar[str] = "styles.tcss"
    BINDINGS: t.ClassVar[list[t.Any]] = [
        Binding("f2", "cycle_layout", "Layout", priority=True),
        Binding("f3", "cycle_workflow", "Workflow", priority=True),
    ]

    def __init__(
        self,
        ctx: UiContext,
        *,
        layout: str = registry.DEFAULT_LAYOUT,
        workflow: str = registry.DEFAULT_WORKFLOW,
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
        self._layout_name = layout
        self._workflow_name = workflow
        for name in registry.layout_names():
            self.add_mode(name, self._mode_factory(name))

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

    def get_default_screen(self) -> Screen:
        """Mount the selected layout and workflow as the launch screen."""
        return t.cast("Screen", self._build_layout(self._layout_name, self._workflow_name))

    def _build_layout(self, layout_name: str, workflow_name: str) -> LayoutScreen:
        """Resolve names through the registry and construct the layout (lazy import)."""
        layout = registry.layout_spec(layout_name) or registry.layout_spec(registry.DEFAULT_LAYOUT)
        wf_spec = registry.workflow_spec(workflow_name) or registry.workflow_spec(
            registry.DEFAULT_WORKFLOW,
        )
        assert layout is not None  # the defaults always resolve
        assert wf_spec is not None
        workflow = t.cast("Workflow", wf_spec.loader()())
        return layout.loader()(self._ctx, workflow)

    def _mode_factory(self, layout_name: str) -> cabc.Callable[[], Screen]:
        """Return a zero-arg factory that builds ``layout_name`` with the active workflow.

        ``App.MODES`` factories take no arguments, so this closure injects the
        shared context and reads the *current* workflow name at build time, so a
        layout switched into after an ``F3`` workflow swap carries the new one.
        """

        def factory() -> Screen:
            return t.cast("Screen", self._build_layout(layout_name, self._workflow_name))

        return factory

    def on_mount(self) -> None:
        """Bind the pump thread for the non-blocking guards (ADR 0011 NB-1/NB-8).

        The shell owns the pump, so the bind, the log-only heartbeat watchdog
        (default-on for an interactive TTY), and the opt-in audit hook live here;
        the layouts only carry ``@pump_only`` / ``@offload`` callables.
        """
        self._adopt_launch_mode()
        _runtime.bind_pump_thread()
        if _runtime.watchdog_enabled():
            self.set_interval(_runtime.HEARTBEAT_INTERVAL, _runtime.record_heartbeat)
            _runtime.start_pump_watchdog()
        if _runtime.audit_hook_enabled():
            _runtime.arm_pump_audit()
        self._update_subtitle()

    def on_unmount(self) -> None:
        """Release the pump-thread binding and stop the watchdog on teardown."""
        _runtime.unbind_pump_thread()
        _runtime.stop_pump_watchdog()
        _runtime.disarm_pump_audit()

    def _adopt_launch_mode(self) -> None:
        """Move Textual's startup stack into the selected layout mode."""
        if self._current_mode != self.DEFAULT_MODE:
            return
        stack = self._screen_stacks.get(self.DEFAULT_MODE)
        if not stack:
            return
        self._screen_stacks[self._layout_name] = stack
        self._screen_stacks.pop(self.DEFAULT_MODE, None)
        self._current_mode = self._layout_name

    def action_cycle_layout(self) -> None:
        """``F2``: switch to the next registered layout, keeping the workflow."""
        screen = self.screen
        if not isinstance(screen, LayoutScreen):
            return
        names = registry.layout_names()
        self._layout_name = names[(names.index(self._layout_name) + 1) % len(names)]
        self.switch_mode(self._layout_name)
        screen = self.screen
        if isinstance(screen, LayoutScreen):
            screen.attach_pending_workflow()
        self._update_subtitle()

    def action_cycle_workflow(self) -> None:
        """Cycle every live layout to the next registered workflow."""
        screen = self.screen
        if not isinstance(screen, LayoutScreen):
            return
        names = registry.workflow_names()
        self._workflow_name = names[(names.index(self._workflow_name) + 1) % len(names)]
        wf_spec = registry.workflow_spec(self._workflow_name)
        if wf_spec is None:
            return
        for layout in self._layout_screens():
            layout.set_workflow(
                t.cast("Workflow", wf_spec.loader()()),
                attach=layout is screen,
            )
        self._update_subtitle()

    def _layout_screens(self) -> cabc.Iterator[LayoutScreen]:
        """Yield each live layout screen once, including suspended mode stacks."""
        seen: set[int] = set()
        for stack in self._screen_stacks.values():
            for screen in stack:
                if isinstance(screen, LayoutScreen) and id(screen) not in seen:
                    seen.add(id(screen))
                    yield screen

    def _update_subtitle(self) -> None:
        """Show the active ``layout · workflow`` in the app sub-title."""
        self.sub_title = f"{self._layout_name} · {self._workflow_name}"
