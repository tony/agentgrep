"""``LayoutScreen`` — the base for pluggable explorer layouts (ADR 0013).

A layout is a Textual ``Screen`` injected with the shared
:class:`~agentgrep.ui._context.UiContext`. Subclasses own their ``compose``, CSS,
bindings, and presentation; they reach the engine only through
``context.invoker`` (ADR 0012 RW-1) and run all blocking work off the pump
(ADR 0011). The App shell mounts one subclass as the active layout.
"""

from __future__ import annotations

import typing as t

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

    def __init__(self, ctx: UiContext, workflow: Workflow) -> None:
        super().__init__()
        self._ctx = ctx
        self._workflow = workflow
        self._workflow_attach_pending = False

    @property
    def context(self) -> UiContext:
        """The session-fixed dependencies injected by the App shell."""
        return self._ctx

    @property
    def slash_commands(self) -> tuple[commands.SlashCommand, ...]:
        """Return common commands plus this layout's extension commands."""
        return (*commands.SLASH_COMMANDS, *self.EXTRA_SLASH_COMMANDS)

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
        """Hide command-completion chrome when a layout provides it."""

    @_runtime.pump_only
    def notify_key_bindings(self) -> None:
        """Notify enabled, visible App/Screen bindings for the active layout."""
        lines: list[str] = []
        seen: set[tuple[int, str, str]] = set()
        for active in self.app.active_bindings.values():
            binding = active.binding
            if (
                (active.node is not self and active.node is not self.app)
                or not active.enabled
                or not binding.show
                or not binding.description
            ):
                continue
            marker = (id(active.node), binding.action, binding.description)
            if marker in seen:
                continue
            seen.add(marker)
            key = binding.key_display or binding.key
            lines.append(f"{key} — {binding.description}")
        self.notify("\n".join(lines), title="Keys", timeout=10)

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
