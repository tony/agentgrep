"""Compact first-run and runtime picker for agentgrep-owned themes."""

from __future__ import annotations

import typing as t

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.timer import Timer
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from agentgrep.ui import _runtime, theme as ui_theme
from agentgrep.ui.highlighter import QueryHighlighter

__all__ = ["ThemePicker"]


class ThemePicker(ModalScreen[None]):
    """Preview and choose from agentgrep's bounded owned profile catalog."""

    AUTO_FOCUS = "#theme-picker-options"
    HORIZONTAL_BREAKPOINTS: list[tuple[int, str]] | None = [  # noqa: RUF012
        (0, "-compact"),
        (68, "-wide"),
    ]
    BINDINGS: t.ClassVar[list[Binding]] = [
        Binding("j", "next_theme", show=False),
        Binding("k", "previous_theme", show=False),
        Binding("escape", "cancel", show=False, priority=True),
        Binding("ctrl+c", "cancel", show=False, priority=True),
    ]
    DEFAULT_CSS = """
    ThemePicker {
        align: center middle;
        background: $ag-canvas;
        color: $text;
    }
    #theme-picker-dialog {
        width: 76;
        max-width: 100%;
        height: 22;
        max-height: 100%;
        padding: 1 2;
        background: $ag-canvas;
    }
    #theme-picker-title {
        height: 2;
        text-align: center;
        color: $text-muted;
    }
    #theme-picker-controls {
        height: 5;
        align: center middle;
    }
    #theme-picker-navigation,
    #theme-picker-commit {
        width: 1fr;
        height: 1;
        color: $text;
    }
    #theme-picker-navigation { text-align: right; padding-right: 2; }
    #theme-picker-commit { text-align: left; padding-left: 2; }
    #theme-picker-options {
        width: 32;
        height: 5;
        padding: 0 1;
        text-align: center;
        border: round $ag-faint;
        background: $ag-canvas;
        scrollbar-size: 0 0;
    }
    #theme-picker-options:focus { border: round $accent; }
    #theme-picker-options > .option-list--option-highlighted {
        background: $ag-state-selected-bg;
        color: auto;
        text-style: bold;
    }
    #theme-picker-footer,
    #theme-picker-footer-compact {
        height: 1;
        text-align: center;
        color: $text-muted;
    }
    #theme-picker-footer-compact { display: none; }
    #theme-picker-preview-label {
        height: 1;
        text-align: center;
        color: $text-muted;
    }
    #theme-picker-preview {
        height: 9;
        padding: 1 2;
        border: round $ag-faint;
        background: transparent;
        color: $text;
    }
    ThemePicker.-compact #theme-picker-navigation,
    ThemePicker.-compact #theme-picker-commit { display: none; }
    ThemePicker.-compact #theme-picker-options { width: 100%; }
    ThemePicker.-compact #theme-picker-footer { display: none; }
    ThemePicker.-compact #theme-picker-footer-compact { display: block; }
    ThemePicker.-compact #theme-picker-dialog { padding: 0 1; height: 20; }
    ThemePicker.-compact #theme-picker-preview { height: 8; padding: 0 1; }
    """

    def __init__(self, selected_theme: str, *, initial_setup: bool) -> None:
        super().__init__(id="theme-picker")
        self._original_theme = selected_theme
        self.initial_setup = initial_setup
        self._index = next(
            (
                index
                for index, profile in enumerate(ui_theme.THEME_PROFILES)
                if profile.name == selected_theme
            ),
            0,
        )
        self._preview_generation = 0
        self._pending_preview: tuple[int, str] | None = None
        self._preview_timer: Timer | None = None
        self._committing = False

    @_runtime.pump_only
    def compose(self) -> ComposeResult:
        """Compose a native list, explicit controls, and semantic preview."""
        built = self._active_theme()
        with Vertical(id="theme-picker-dialog"):
            yield Static("Select your preferred theme", id="theme-picker-title")
            with Horizontal(id="theme-picker-controls"):
                yield Static(self._navigation_hint(built), id="theme-picker-navigation")
                yield OptionList(
                    *(
                        Option(profile.label, id=profile.name)
                        for profile in ui_theme.THEME_PROFILES
                    ),
                    id="theme-picker-options",
                    markup=False,
                    compact=True,
                )
                yield Static(self._commit_hint(built), id="theme-picker-commit")
            yield Static(self._cancel_hint(), id="theme-picker-footer")
            yield Static(self._footer_hint(), id="theme-picker-footer-compact")
            yield Static("Preview", id="theme-picker-preview-label")
            yield Static(self._render_preview(built), id="theme-picker-preview")

    @_runtime.pump_only
    def on_mount(self) -> None:
        """Focus and align the native list with the active profile."""
        options = self.query_one("#theme-picker-options", OptionList)
        options.highlighted = self._index
        options.focus()

    def _active_theme(self) -> Theme:
        """Build the currently highlighted profile for bounded rendering."""
        return ui_theme.THEME_PROFILES[self._index].build()

    def _navigation_hint(self, built: Theme | None = None) -> Text:
        """Render the left-side navigation hint."""
        built = self._active_theme() if built is None else built
        hint = Text("Navigate ")
        hint.append("↑↓", style=f"bold {built.accent}")
        return hint

    def _commit_hint(self, built: Theme | None = None) -> Text:
        """Render the right-side commit hint."""
        built = self._active_theme() if built is None else built
        hint = Text("Press ")
        hint.append("Enter", style=f"bold {built.accent}")
        hint.append(" ↵")
        return hint

    def _footer_hint(self) -> str:
        """Return a plain structural fallback that survives NO_COLOR."""
        action = "not now" if self.initial_setup else "cancel"
        return f"↑↓ / j k navigate · Enter select · Esc {action}"

    def _cancel_hint(self) -> str:
        """Return the one non-obvious wide-layout action."""
        action = "not now" if self.initial_setup else "cancel"
        return f"Esc {action}"

    def _render_preview(self, built: Theme | None = None) -> Text:
        """Render representative agentgrep syntax and match roles."""
        built = self._active_theme() if built is None else built
        variables = built.variables
        preview = Text("Heading", style=f"bold {built.accent}")
        preview.append("\n")
        preview.append("Bold", style="bold")
        preview.append(", ")
        preview.append("italic", style="italic")
        preview.append(", and ")
        preview.append("inline code", style=variables["ag-query-keyword"])
        preview.append(".\n")
        preview.append("• ", style=built.accent)
        preview.append("agent:claude  model:gpt*", style=variables["ag-muted"])
        preview.append("\n")
        query = Text('agent:claude OR model:gpt* "exact phrase"')
        QueryHighlighter(theme_variables=variables, dark=built.dark).highlight(query)
        preview.append_text(query)
        preview.append("\n")
        preview.append("search", style=f"bold {variables['ag-match-search']}")
        preview.append("  ")
        preview.append(
            "filter",
            style=(f"bold {variables['ag-match-filter-fg']} on {variables['ag-match-filter-bg']}"),
        )
        preview.append("  ")
        preview.append(
            "find",
            style=f"{variables['ag-match-find-fg']} on {variables['ag-match-find-bg']}",
        )
        preview.append("  ")
        preview.append(
            "current",
            style=(
                f"bold {variables['ag-match-find-current-fg']} "
                f"on {variables['ag-match-find-current-bg']}"
            ),
        )
        return preview

    def _refresh_copy(self) -> None:
        """Refresh profile-derived hints and preview after navigation."""
        built = self._active_theme()
        self.query_one("#theme-picker-navigation", Static).update(
            self._navigation_hint(built),
        )
        self.query_one("#theme-picker-commit", Static).update(self._commit_hint(built))
        self.query_one("#theme-picker-preview", Static).update(self._render_preview(built))

    def _schedule_preview(self) -> None:
        """Debounce global restyling while list navigation is in flight."""
        self._preview_generation += 1
        profile = ui_theme.THEME_PROFILES[self._index]
        self._pending_preview = (self._preview_generation, profile.name)
        if self._preview_timer is not None:
            self._preview_timer.stop()
        self._preview_timer = self.set_timer(0.05, self._apply_pending_preview)

    @_runtime.pump_only
    def _apply_pending_preview(self) -> None:
        """Apply only the newest scheduled profile preview."""
        pending = self._pending_preview
        if pending is None:
            return
        generation, theme_name = pending
        if generation != self._preview_generation:
            return
        self.app.theme = theme_name
        self._pending_preview = None

    def _set_index(self, index: int) -> None:
        """Adopt one bounded profile index and schedule its preview."""
        if self._committing or not 0 <= index < len(ui_theme.THEME_PROFILES):
            return
        self._index = index
        self._refresh_copy()
        self._schedule_preview()

    @_runtime.pump_only
    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Preview the highlighted keyboard or mouse row."""
        if event.option_list.id == "theme-picker-options":
            self._set_index(event.option_index)

    @_runtime.pump_only
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Commit the selected keyboard or mouse row."""
        if event.option_list.id != "theme-picker-options" or self._committing:
            return
        self._set_index(event.option_index)
        theme_name = ui_theme.THEME_PROFILES[self._index].name
        self._committing = True
        if not t.cast("t.Any", self.app).commit_theme_picker(self, theme_name):
            self._committing = False

    @_runtime.pump_only
    def action_next_theme(self) -> None:
        """Move to the next profile, wrapping at the catalog edge."""
        options = self.query_one("#theme-picker-options", OptionList)
        options.highlighted = (self._index + 1) % len(ui_theme.THEME_PROFILES)

    @_runtime.pump_only
    def action_previous_theme(self) -> None:
        """Move to the previous profile, wrapping at the catalog edge."""
        options = self.query_one("#theme-picker-options", OptionList)
        options.highlighted = (self._index - 1) % len(ui_theme.THEME_PROFILES)

    @_runtime.pump_only
    def action_cancel(self) -> None:
        """Roll back preview and close or skip without persistence."""
        if self._committing:
            return
        self._cancel_preview()
        self.app.theme = self._original_theme
        t.cast("t.Any", self.app).cancel_theme_picker(self)

    def _cancel_preview(self) -> None:
        """Invalidate and stop the pending debounce callback."""
        self._preview_generation += 1
        self._pending_preview = None
        if self._preview_timer is not None:
            self._preview_timer.stop()
            self._preview_timer = None

    @_runtime.pump_only
    def on_unmount(self) -> None:
        """Invalidate a pending preview as the picker leaves the stack."""
        self._cancel_preview()
