"""``LayoutScreen`` — the base for pluggable explorer layouts (ADR 0013).

A layout is a Textual ``Screen`` injected with the shared
:class:`~agentgrep.ui._context.UiContext`. Subclasses own their ``compose``, CSS,
bindings, and presentation; they reach the engine only through
``context.invoker`` (ADR 0012 RW-1) and run all blocking work off the pump
(ADR 0011). The App shell mounts one subclass as the active layout.
"""

from __future__ import annotations

import dataclasses
import functools
import io
import typing as t

from rich.console import Console
from rich.segment import Segment, Segments
from textual.app import generate_datetime_filename
from textual.screen import Screen

from agentgrep import _version
from agentgrep.results import apply_search_request_patch, offered_depth_actions
from agentgrep.ui import _runtime, commands, theme as ui_theme

if t.TYPE_CHECKING:
    from agentgrep.records import SearchQuery
    from agentgrep.results import NextAction, RunSummary
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
    generation: int,
    call_from_thread: t.Callable[..., object],
    register_delivery: t.Callable[[int, t.TextIO, str], None],
) -> None:
    """Serialize a detached Rich frame and register pump-side delivery.

    Parameters
    ----------
    frame : _ScreenshotFrame
        Immutable dimensions, title, and recorded segments captured on the pump.
    generation : int
        Screenshot generation accepted by the originating layout.
    call_from_thread : typing.Callable
        Textual's worker-to-pump call gate captured before offload.
    register_delivery : typing.Callable
        Pump-only callback that validates and registers the finished SVG.
    """
    console = _screenshot_console(frame.width, frame.height)
    console.print(Segments(frame.segments))
    screenshot = io.StringIO(console.export_svg(title=frame.title))
    try:
        call_from_thread(register_delivery, generation, screenshot, frame.filename)
    except BaseException:
        screenshot.close()
        raise


@_runtime.offload
def _resolve_build_provenance(
    call_from_thread: t.Callable[..., object],
    present: t.Callable[[_version.BuildProvenance], None],
) -> None:
    """Probe the build provenance off the pump, then hand it back to present.

    :func:`agentgrep._version.build_provenance` spawns ``git describe``, which
    must not happen on the UI thread. This worker body is its only caller
    inside the explorer, and ``@offload`` asserts it never runs on that thread.

    Parameters
    ----------
    call_from_thread : typing.Callable
        Textual's worker-to-pump call gate, captured before offload.
    present : typing.Callable
        Pump-only presenter invoked with the resolved provenance.
    """
    call_from_thread(present, _version.build_provenance())


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
        self._command_matches: tuple[commands.SlashCommand, ...] = ()
        self._dispatching_command = False
        self._enum_dropdown: t.Any = None
        self._screenshot_generation: int = 0
        self._run_summary: RunSummary | None = None
        self._active_search_text = (
            ctx.initial_search_text
            if ctx.initial_search_text is not None
            else " ".join(ctx.query.terms)
        )

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

    def set_workflow(self, workflow: Workflow) -> None:
        """Replace the active workflow and seed its initial dispatch."""
        t.cast("t.Any", self).request_cancel()
        self._workflow = workflow
        self._workflow.on_attach(t.cast("t.Any", self))

    def _resolve_slash_line(self, text: str) -> tuple[commands.SlashCommand, str] | None:
        """Resolve one command line into its command and argument remainder.

        ``None`` means ``text`` is not dispatchable and retains literal search
        behavior — which is exactly what makes a ``/``-prefixed path query such
        as ``/usr/local`` searchable.

        Parameters
        ----------
        text : str
            Trimmed primary-input text.

        Returns
        -------
        tuple[commands.SlashCommand, str] | None
            The resolved command and its trimmed remainder, or ``None`` when
            ``text`` is not a command line this layout dispatches.
        """
        if not text.startswith("/"):
            return None
        token, args = commands.parse_command(text)
        command = commands.resolve_command(token, self.slash_commands)
        if command is None or (args and not command.accepts_args):
            return None
        return command, args

    @_runtime.pump_only
    def _dispatch_slash_text(self, text: str) -> bool | None:
        """Run one recognized exact slash command.

        ``None`` means ``text`` is not dispatchable and should retain literal
        search behavior. A handler's ``False`` result means the command was
        recognized but invalid, so callers must not route it to search.
        """
        resolved = self._resolve_slash_line(text)
        if resolved is None:
            return None
        command, args = resolved
        # A running handler is the one condition under which the primary input
        # is known to hold a command line rather than the query being
        # escalated: the command menu dispatches a completed ``/deep`` while
        # the box still holds the partial ``/de`` the user typed.
        self._dispatching_command = True
        try:
            succeeded = command.run(self, args)
        finally:
            self._dispatching_command = False
        if command.clears_input:
            if succeeded:
                self._clear_command_input()
        else:
            self._restore_active_search_input()
        if succeeded or not command.clears_input:
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

    def _restore_active_search_input(self) -> None:
        """Restore the query that an action command is escalating."""
        search_input = getattr(self, "_search_input", None)
        if search_input is None:
            return
        search_input.load_query(self._active_search_text)
        search_input.focus()

    def _remember_active_search_text(self, text: str) -> None:
        """Retain the query a slash command escalates while ``/`` occupies the input."""
        self._active_search_text = text

    def pending_depth_actions(self) -> tuple[NextAction, ...]:
        """Return the engine's depth offer for the request not yet submitted.

        The offer is derived from the layout's *launch* policy through
        ``build_query``, not from a previous run, so it never carries a
        widened effort forward. It describes what a run would read; it
        asserts nothing about coverage.
        """
        return offered_depth_actions(self._pending_depth_query())

    def _pending_depth_query(self) -> SearchQuery:
        """Return the request the primary input would submit right now."""
        return t.cast("t.Any", self).build_query(self._pending_search_text())

    def _pending_search_text(self) -> str:
        """Return live input text, ignoring a command line occupying the box.

        An emptied box means the user cleared the query, so it escalates
        nothing. The box falls back to the retained draft only when it is
        genuinely holding a command — while a handler runs, or when its text
        resolves to one — so escalation reads exactly the text Enter would
        search. An unresolved ``/`` token such as ``/usr/local`` searches as a
        literal, so it escalates as that same literal.
        """
        if self._dispatching_command:
            return self._active_search_text.strip()
        search_input = getattr(self, "_search_input", None)
        value = "" if search_input is None else str(getattr(search_input, "value", "") or "")
        text = value.strip()
        if self._resolve_slash_line(text) is not None:
            return self._active_search_text.strip()
        return text

    @_runtime.pump_only
    def run_next_action(self, action_id: str, argument: str = "") -> bool:
        """Apply one engine-authored depth action to the active request.

        After a run the action set comes from that run's ``RunSummary``. Before
        any run it comes from :func:`~agentgrep.results.offered_depth_actions`
        for the query the primary input currently holds, so the ladder is
        reachable from a cold session. Both paths apply the engine's own
        :class:`~agentgrep.results.SearchRequestPatch` and neither makes the
        escalated effort sticky.

        Parameters
        ----------
        action_id : str
            Engine action naming the rung, such as ``search.targeted``.
        argument : str
            Raw slash remainder. A positive integer bounds this request's
            targeted conversation attempts (``/deep 50``); an empty remainder
            keeps the cap the engine's own patch carries. Anything else, and
            any argument to a rung that reads every conversation, is refused.

        Returns
        -------
        bool
            Whether a search was started.
        """
        bound = argument.strip()
        if bound and not self._accepts_depth_bound(action_id, bound):
            return False
        summary = self._run_summary
        base_query = (
            t.cast("t.Any", self).search_query
            if summary is not None
            else self._pending_depth_query()
        )
        actions = summary.next_actions if summary is not None else offered_depth_actions(base_query)
        action = next(
            (candidate for candidate in actions if candidate.action_id == action_id),
            None,
        )
        if action is None:
            self.notify(
                "The engine offers no deeper coverage for this request.",
                title="Search depth",
                severity="warning",
            )
            return False
        if action.requires_confirmation:
            self.notify(
                "Change the explicit scope to all before searching conversations.",
                title="Search depth",
                severity="warning",
            )
            return False
        if summary is None and not self._has_searchable_request(base_query):
            self.notify(
                "Type a query before choosing its conversation coverage.",
                title="Search depth",
                severity="warning",
            )
            return False
        query = apply_search_request_patch(base_query, action.patch)
        if bound:
            # The engine still authors the escalation; the user only replaces
            # the cap it declared, exactly as ``--deep N`` does at launch.
            query = dataclasses.replace(query, conversation_limit=int(bound))
        self._run_summary = None
        host = t.cast("t.Any", self)
        # An escalation replaces the active search, so it owes the same
        # lifecycle a submitted query gets in ``SearchWorkflow.on_query``:
        # signal the in-flight run before ``run_search`` swaps in a fresh
        # ``SearchControl`` the old worker can never see. A post-run
        # escalation re-runs text the history already holds from its
        # submission; a pre-run one submits a draft for the first time.
        host.request_cancel()
        if summary is None:
            escalated_text = self._pending_search_text()
            self._remember_active_search_text(escalated_text)
            host.record_history(escalated_text)
        host.run_search(query)
        return True

    def _accepts_depth_bound(self, action_id: str, bound: str) -> bool:
        """Report whether ``bound`` is a conversation cap this rung can honor.

        Warns about the refusal it reports, so a mistyped count never silently
        starts a search at a different depth than the user asked for.

        Parameters
        ----------
        action_id : str
            Engine action naming the rung the bound was typed for.
        bound : str
            Non-empty slash remainder, already trimmed.

        Returns
        -------
        bool
            Whether ``bound`` is a positive integer on a rung that reads a
            bounded set of conversations.
        """
        if action_id != "search.targeted":
            self.notify(
                "Searching all conversations reads every one of them, so it takes no count.",
                title="Search depth",
                severity="warning",
            )
            return False
        # isdecimal, not isdigit: superscripts satisfy isdigit but raise in int().
        if not bound.isdecimal() or int(bound) < 1:
            self.notify(
                "Deep search takes an optional conversation count, such as /deep 50.",
                title="Search depth",
                severity="warning",
            )
            return False
        return True

    @staticmethod
    def _has_searchable_request(query: SearchQuery) -> bool:
        """Report whether ``query`` asks for anything (mirrors ``SearchWorkflow``)."""
        origin_filter = query.origin_filter
        return bool(
            query.terms
            or query.compiled is not None
            or (origin_filter is not None and not origin_filter.is_empty()),
        )

    def _is_command_draft(self, value: str) -> bool:
        """Report whether ``value`` is a command line the user is still typing.

        Prefix matching, not resolution: ``/de`` is a draft of ``/deep`` even
        though it dispatches nothing yet, while ``/usr/local`` completes to no
        command and is therefore a query — the same query Enter would search.

        Parameters
        ----------
        value : str
            Raw primary-input text.

        Returns
        -------
        bool
            Whether the command menu owns ``value``.
        """
        text = value.lstrip()
        if not text.startswith("/"):
            return False
        token, _args = commands.parse_command(text)
        return bool(commands.command_matches(token, self.slash_commands))

    def _update_command_completion(self, value: str) -> bool:
        """Update slash-command completion and report whether it owns ``value``.

        A non-command edit doubles as the record of what a later depth command
        escalates. Typing ``/deep`` means emptying the box first, so a query
        retained only on submit would already be gone by the time the command
        runs. Retention is verbatim and deliberately not prefix-aware among
        queries: the last non-empty one wins, so narrowing ``deployment`` to
        ``deploy`` escalates ``deploy``.
        """
        if not self._is_command_draft(value):
            text = value.strip()
            if text:
                self._remember_active_search_text(text)
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
        generation = self._screenshot_generation + 1
        self.refresh()
        scheduled = bool(
            self.call_after_refresh(
                self._deliver_screenshot_after_refresh,
                generation,
            ),
        )
        if scheduled:
            self._screenshot_generation = generation
        return scheduled

    @_runtime.pump_only
    def _deliver_screenshot_after_refresh(self, generation: int) -> None:
        """Deliver only while this layout remains mounted and active."""
        if generation != self._screenshot_generation:
            return
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
                generation,
                app.call_from_thread,
                self._register_screenshot_delivery,
            ),
            name="screenshot",
            group="screenshot",
            description="export screenshot",
            thread=True,
            exclusive=True,
        )

    @_runtime.pump_only
    def _register_screenshot_delivery(
        self,
        generation: int,
        screenshot: t.TextIO,
        filename: str,
    ) -> None:
        """Deliver a worker-built SVG while its originating layout is active."""
        if generation != self._screenshot_generation:
            screenshot.close()
            return
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
    def report_build_status(self) -> bool:
        """Report the running version and how it was built.

        The version is cheap, but the ``git describe`` that turns it into a
        build ref is a subprocess, so this never resolves one: a provenance
        another ``/status`` already resolved is reported straight from the
        process-wide cache, and an unresolved one is handed to a ``thread=True``
        worker whose notification arrives when the probe returns.

        Returns
        -------
        bool
            Whether the report was shown or its resolution was started.
        """
        resolved = _version.cached_build_provenance()
        if resolved is not None:
            self._present_build_status(resolved)
            return True
        try:
            _ = self.run_worker(
                functools.partial(
                    _resolve_build_provenance,
                    self.app.call_from_thread,
                    self._present_build_status,
                ),
                name="build-status",
                group="build-status",
                description="resolve build provenance",
                exit_on_error=False,
                thread=True,
                exclusive=True,
            )
        except RuntimeError:
            return False
        return True

    @_runtime.pump_only
    def _present_build_status(self, provenance: _version.BuildProvenance) -> None:
        """Show one build-status notification while this layout is still live.

        Streaming chrome needs a generation token so a stale result cannot
        overwrite a newer one; this does not, because the provenance is fixed
        for the life of the process and a late notification carries the same
        text a fresh one would. The mount check exists only so a torn-down
        layout does not notify.

        Parameters
        ----------
        provenance : agentgrep._version.BuildProvenance
            Release version and git ref resolved off the pump.
        """
        if not self.is_mounted or not self.is_attached:
            return
        self.notify(
            _version.format_build_status(provenance),
            title="Build",
            timeout=10,
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
        """Open the picker or directly select one owned theme profile."""
        choice = argument.strip().lower()
        if not choice:
            return bool(self.app.open_theme_picker())
        aliases = {
            "dark": ui_theme.DARK_THEME_NAME,
            "light": ui_theme.LIGHT_THEME_NAME,
            "tokyo": ui_theme.TOKYO_NIGHT_THEME_NAME,
            "tokyo-night": ui_theme.TOKYO_NIGHT_THEME_NAME,
        }
        target = aliases.get(choice, choice)
        if target not in ui_theme.THEME_PROFILE_BY_NAME:
            self.notify(
                "Theme must be dark or light, or tokyo-night.",
                title="Theme",
                severity="warning",
            )
            return False
        return bool(self.app.select_theme(target))

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
