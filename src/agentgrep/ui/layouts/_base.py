"""``LayoutScreen`` — the base for pluggable explorer layouts (ADR 0013).

A layout is a Textual ``Screen`` injected with the shared
:class:`~agentgrep.ui._context.UiContext`. Subclasses own their ``compose``, CSS,
bindings, and presentation; they reach the engine only through
``context.invoker`` (ADR 0012 RW-1) and run all blocking work off the pump
(ADR 0011). The App shell mounts one subclass as the active layout.
"""

from __future__ import annotations

import functools
import io
import typing as t

from rich.console import Console
from rich.segment import Segment, Segments
from textual.app import generate_datetime_filename
from textual.screen import Screen

from agentgrep.ui import _runtime, commands, theme as ui_theme

if t.TYPE_CHECKING:
    from agentgrep.ui._context import UiContext
    from agentgrep.ui.workflows import Workflow

__all__ = ["LayoutScreen"]

#: The ``Screen`` base, kept opaque to the type checker exactly as the former
#: ``ExplorerApp`` base was: the large relocated view bodies are not yet fully
#: typed against Textual, and ``DOMNode.query`` (the DOM query) would otherwise
#: collide with view helpers. The search-query state is ``self.search_query``
#: precisely to avoid that collision; fully typing the views is a follow-up.
_SCREEN_BASE: t.Any = Screen


class _ScreenshotFrame(t.NamedTuple):
    """Detached Rich recording data for one visible Textual frame."""

    width: int
    height: int
    title: str
    filename: str
    segments: tuple[Segment, ...]


def _screenshot_console(width: int, height: int) -> Console:
    """Build the recording console Textual uses for SVG screenshots.

    Parameters
    ----------
    width : int
        Captured terminal width in cells.
    height : int
        Captured terminal height in cells.

    Returns
    -------
    Console
        A truecolor Rich recording console with Textual's screenshot options.
    """
    return Console(
        width=width,
        height=height,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        record=True,
        legacy_windows=False,
        safe_box=False,
    )


@_runtime.offload
def _export_screenshot_frame(
    frame: _ScreenshotFrame,
    call_from_thread: t.Callable[..., object],
    register_delivery: t.Callable[[t.TextIO, str], None],
) -> None:
    """Serialize a detached Rich frame and register pump-side delivery.

    Parameters
    ----------
    frame : _ScreenshotFrame
        Immutable dimensions, title, and recorded segments captured on the pump.
    call_from_thread : typing.Callable
        Textual's worker-to-pump call gate captured before offload.
    register_delivery : typing.Callable
        Pump-only callback that validates and registers the finished SVG.
    """
    console = _screenshot_console(frame.width, frame.height)
    console.print(Segments(frame.segments))
    screenshot = io.StringIO(console.export_svg(title=frame.title))
    try:
        call_from_thread(register_delivery, screenshot, frame.filename)
    except BaseException:
        screenshot.close()
        raise


class LayoutScreen(_SCREEN_BASE):
    """A swappable explorer layout that consumes a shared :class:`UiContext`.

    Parameters
    ----------
    ctx : UiContext
        Session-fixed dependencies (home, engine seam, launch query, control)
        the App shell injects. Reachable to subclasses via :attr:`context`.
    workflow : Workflow
        The active interaction strategy (search vs. filter). The layout
        implements ``WorkflowHost`` and the workflow drives it; it is attached
        on mount and re-attached when swapped via :meth:`set_workflow`.
    """

    EXTRA_SLASH_COMMANDS: t.ClassVar[tuple[commands.SlashCommand, ...]] = ()
    """Layout-specific commands appended to the common slash surface."""

    ZOOM_ARGUMENT_HINT: t.ClassVar[str | None] = None
    """Layout-specific logical pane targets, or ``None`` when unsupported."""

    def __init__(self, ctx: UiContext, workflow: Workflow) -> None:
        super().__init__()
        self._ctx = ctx
        self._workflow = workflow
        self._workflow_attach_pending = False
        self._command_matches: tuple[commands.SlashCommand, ...] = ()
        self._enum_dropdown: t.Any = None

    @property
    def context(self) -> UiContext:
        """The session-fixed dependencies injected by the App shell."""
        return self._ctx

    @property
    def slash_commands(self) -> tuple[commands.SlashCommand, ...]:
        """Return common commands plus this layout's extension commands."""
        zoom = commands.zoom_commands(self.ZOOM_ARGUMENT_HINT) if self.ZOOM_ARGUMENT_HINT else ()
        return (*commands.SLASH_COMMANDS, *zoom, *self.EXTRA_SLASH_COMMANDS)

    @property
    def workflow(self) -> Workflow:
        """The currently active workflow strategy."""
        return self._workflow

    def on_mount(self) -> None:
        """Attach the active workflow once the layout is mounted.

        Subclasses cache their widgets in their own ``on_mount`` and call
        ``super().on_mount()`` last, so the workflow's initial dispatch (which
        may start a search and paint chrome) runs after the widgets exist.
        """
        self._workflow.on_attach(t.cast("t.Any", self))
        self._workflow_attach_pending = False

    def set_workflow(self, workflow: Workflow, *, attach: bool = True) -> None:
        """Swap the active workflow, optionally re-seeding its initial dispatch."""
        if attach:
            t.cast("t.Any", self).request_cancel()
        self._workflow = workflow
        self._workflow_attach_pending = not attach
        if attach:
            self._workflow.on_attach(t.cast("t.Any", self))
            self._workflow_attach_pending = False

    def attach_pending_workflow(self) -> None:
        """Attach a suspended workflow swap when the layout is resumed."""
        if not self._workflow_attach_pending:
            return
        self._workflow_attach_pending = False
        t.cast("t.Any", self).request_cancel()
        self._workflow.on_attach(t.cast("t.Any", self))

    @_runtime.pump_only
    def _dispatch_slash_text(self, text: str) -> bool | None:
        """Run one recognized exact slash command.

        ``None`` means ``text`` is not dispatchable and should retain literal
        search behavior. A handler's ``False`` result means the command was
        recognized but invalid, so callers must not route it to search.
        """
        if not text.startswith("/"):
            return None
        token, args = commands.parse_command(text)
        command = commands.resolve_command(token, self.slash_commands)
        if command is None or (args and not command.accepts_args):
            return None
        succeeded = command.run(self, args)
        if succeeded:
            self._clear_command_input()
            self._hide_command_completion()
        return succeeded

    def _clear_command_input(self) -> None:
        """Clear and refocus the shared search input after command success."""
        search_input = getattr(self, "_search_input", None)
        if search_input is None:
            return
        search_input.value = ""
        search_input.cursor_position = 0
        search_input.focus()

    def _hide_command_completion(self) -> None:
        """Hide the shared slash-command dropdown after execution."""
        if self._enum_dropdown is not None:
            self._enum_dropdown.display = False
        self._command_matches = ()

    def _update_command_completion(self, value: str) -> bool:
        """Update slash-command completion and report whether it owns ``value``."""
        if not value.lstrip().startswith("/"):
            self._command_matches = ()
            if self._enum_dropdown is not None:
                self._enum_dropdown.remove_class("-commands")
            return False
        self._update_command_dropdown(value)
        return True

    def _update_command_dropdown(self, value: str) -> None:
        """Show the shared pi-style command menu filtered by ``value``."""
        from textual.content import Content
        from textual.widgets.option_list import Option

        token, args = commands.parse_command(value)
        matches = () if args else commands.command_matches(token, self.slash_commands)
        self._command_matches = matches
        dropdown = self._enum_dropdown
        if dropdown is None:
            return
        if not matches:
            dropdown.display = False
            return
        dropdown.add_class("-commands")
        dropdown.clear_options()
        name_width = max(len(commands.command_menu_label(command)) for command in matches) + 2
        for command in matches:
            label = commands.command_menu_label(command)
            prompt = Content.assemble(
                (label.ljust(name_width), ""),
                (command.description, "dim"),
            )
            dropdown.add_option(Option(prompt))
        dropdown.styles.offset = (0, 0)
        dropdown.display = True
        dropdown.highlighted = 0

    def _select_command_option(self, event: object) -> bool:
        """Dispatch a selected slash-menu row and report whether it was one."""
        option_list = getattr(event, "option_list", None)
        if option_list is not self._enum_dropdown or not self._command_matches:
            return False
        index = int(getattr(event, "option_index", 0) or 0)
        self._run_command_at(index)
        return True

    def _run_command_at(self, index: int) -> None:
        """Dispatch the slash command at ``index`` in the open command menu."""
        if not (0 <= index < len(self._command_matches)):
            return
        command = self._command_matches[index]
        self._dispatch_slash_text(f"/{command.name}")

    @_runtime.pump_only
    def request_screenshot(self) -> bool:
        """Deliver this layout after command chrome changes have refreshed."""
        self.refresh()
        return bool(self.call_after_refresh(self._deliver_screenshot_after_refresh))

    @_runtime.pump_only
    def _deliver_screenshot_after_refresh(self) -> None:
        """Deliver only while this layout remains mounted and active."""
        if not self.is_mounted or not self.is_attached:
            return
        app = self.app
        stack = app.screen_stack
        if not stack or stack[-1] is not self:
            return
        frame = self._capture_screenshot_frame()
        self.run_worker(
            functools.partial(
                _export_screenshot_frame,
                frame,
                app.call_from_thread,
                self._register_screenshot_delivery,
            ),
            name="screenshot",
            group="screenshot",
            thread=True,
            exclusive=True,
        )

    @_runtime.pump_only
    def _register_screenshot_delivery(self, screenshot: t.TextIO, filename: str) -> None:
        """Deliver a worker-built SVG while its originating layout is active."""
        if not self.is_mounted or not self.is_attached:
            screenshot.close()
            return
        stack = self.app.screen_stack
        if not stack or stack[-1] is not self:
            screenshot.close()
            return
        self.app.deliver_text(
            screenshot,
            save_directory=None,
            save_filename=filename,
            open_method="browser",
            mime_type="image/svg+xml",
            name="screenshot",
        )

    @_runtime.pump_only
    def _capture_screenshot_frame(self) -> _ScreenshotFrame:
        """Detach the active compositor frame into immutable Rich segments."""
        app = self.app
        width, height = app.size
        console = _screenshot_console(width, height)
        screen_render = self._compositor.render_update(
            full=True,
            screen_stack=app._background_screens,
            simplify=False,
        )
        assert screen_render is not None
        title = app.title
        return _ScreenshotFrame(
            width=width,
            height=height,
            title=title,
            filename=generate_datetime_filename(title, ".svg"),
            segments=tuple(console.render(screen_render)),
        )

    @_runtime.pump_only
    def toggle_help_panel(self) -> None:
        """Toggle Textual's singleton key-help panel on the active layout."""
        if self.query("HelpPanel"):
            self.app.action_hide_help_panel()
        else:
            self.app.action_show_help_panel()

    @_runtime.pump_only
    def select_theme(self, argument: str) -> bool:
        """Toggle or directly select agentgrep's dark/light theme."""
        choice = argument.strip().lower()
        if not choice:
            target = (
                ui_theme.LIGHT_THEME_NAME
                if self.app.theme == ui_theme.DARK_THEME_NAME
                else ui_theme.DARK_THEME_NAME
            )
        elif choice == "dark":
            target = ui_theme.DARK_THEME_NAME
        elif choice == "light":
            target = ui_theme.LIGHT_THEME_NAME
        else:
            self.notify(
                "Theme must be dark or light.",
                title="Theme",
                severity="warning",
            )
            return False
        self.app.theme = target
        return True

    # --- input control defaults (the shared SearchInput reaches these) --------
    # SearchInput.on_key routes ctrl-c and the non-ctrl-c "disarm" through
    # ``self.screen``, so every layout that hosts it needs these. The HUD
    # overrides them with its staged confirm-exit gutter; other layouts get a
    # sane default (clear the box, then quit on an empty box).
    def _handle_input_ctrl_c(self, widget: object) -> None:
        """Default ctrl-c inside an input: clear it, else quit on an empty box."""
        target = t.cast("t.Any", widget)
        if str(getattr(target, "value", "")):
            target.value = ""
            return
        self.app.exit()

    def _disarm_confirm_exit(self) -> None:
        """No-op by default; the HUD overrides this to cancel its confirm gutter."""
