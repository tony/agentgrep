"""Status-line widgets: the self-driving spinner and the progress meter.

Both are ``Static`` subclasses. Neither touches the app or the message bus; they
are pure presentation driven by ``set_*`` calls, so they are directly testable
in isolation.
"""

from __future__ import annotations

import time
import typing as t

from rich.cells import cell_len
from rich.text import Text
from textual.widgets import Static

from agentgrep.progress import format_match_count
from agentgrep.ui import theme as ui_theme
from agentgrep.ui.format import (
    format_elapsed_compact,
    format_progress_percent,
    phase_label,
    render_progress_meter,
)

__all__ = [
    "MeterWidget",
    "PaneHeader",
    "ResultsHeader",
    "SearchingPanel",
    "SpinnerWidget",
]


class PaneHeader(Static):
    """A pi-style section header: a left-positioned label embedded in a full rule.

    Mirrors the filter input's rule — a label set into a rule that runs the
    section's full width — but left-positioned (the filter is right-aligned).
    One leading ``─`` cell sits before the bold label, then the rule fills to
    the right edge: ``─results────────``. No trailing margin. The line color is
    driven entirely by CSS (``$ag-faint`` at rest, ``$accent`` via the
    ``-active`` class), so recoloring the focused pane's header is paint-only —
    no inline color is baked in. The rule length is recomputed on resize.
    """

    def __init__(self, label: str, *, id: str | None = None) -> None:  # noqa: A002 -- Textual ``id`` kwarg
        super().__init__(id=id)
        self._label = label

    def on_resize(self) -> None:
        """Recompute the rule length when the column width changes."""
        self.refresh()

    def render(self) -> Text:
        """Return ``─<label><rule>`` filling the widget width.

        The single leading rule cell is the left mirror of the filter input's
        one trailing cap dash; the remaining rule runs to the full width with
        no margin.
        """
        width = int(getattr(self.size, "width", 0) or 0)
        fill = max(0, width - 1 - cell_len(self._label))
        text = Text(no_wrap=True, overflow="crop")
        text.append("─")
        text.append(self._label, style="bold")
        text.append("─" * fill)
        return text


class ResultsHeader(PaneHeader):
    """Results section header with the live search status folded into the rule.

    Extends :class:`PaneHeader`: idle, it renders the plain ``─results────``
    rule; while a search runs (and after it finishes) it folds the status
    payload — an animated spinner, a ``▰▱`` progress bar, the percent, and the
    match count — into the right of the same rule (pi's ``fitBorder`` shape), so
    the results column spends one row instead of two. The payload is dropped
    right-to-left (matches, then bar, then percent) as the width tightens; the
    spinner is always kept.

    The spinner self-drives off ``time.monotonic`` via ``auto_refresh`` while a
    search is active, so it ticks regardless of event-loop load; the worker
    thread only calls store-only setters, and the next timer frame repaints.
    On finish the timer stops and the frozen outcome glyph (``✓``/``■``/``✗``)
    holds. ``begin``/``freeze``/``go_idle`` mirror the old spinner+meter
    lifecycle.
    """

    _FRAMES: t.ClassVar[str] = "·✢✽✻"
    _SEQUENCE: t.ClassVar[str] = _FRAMES + _FRAMES[::-1]
    _FPS: t.ClassVar[float] = 2.0
    _MIN_BAR: t.ClassVar[int] = 4
    # Cap the bar so the label keeps a visible run of rule before the status,
    # rather than the bar swallowing the whole width on a wide terminal.
    _MAX_BAR: t.ClassVar[int] = 16

    def __init__(self, label: str, *, id: str | None = None) -> None:  # noqa: A002 -- Textual ``id`` kwarg
        super().__init__(label, id=id)
        self._active = False
        self._fraction: float | None = None
        self._phase = ""
        self._matches_text = ""
        self._final_glyph: str | None = None
        self._outcome = ""
        self._error = ""
        self._narrow = False
        self._started_at = time.monotonic()
        self._c_accent = ""
        self._c_success = ""
        self._c_muted = ""

    def on_mount(self) -> None:
        """Resolve the payload colors from the active theme (no timer until active)."""
        self.refresh_theme()

    def refresh_theme(self) -> None:
        """Re-resolve the payload hexes (called on theme switch)."""
        theme_vars = t.cast("t.Any", self.app).theme_variables
        self._c_accent = ui_theme.resolve(theme_vars, "accent")
        self._c_success = ui_theme.resolve(theme_vars, "success")
        self._c_muted = ui_theme.resolve(theme_vars, "ag-muted")
        self.refresh()

    # --- lifecycle (driven by the app's search flow) ----------------------
    def begin(self) -> None:
        """Activate on search start: clear state and arm the spinner timer."""
        self._active = True
        self._final_glyph = None
        self._outcome = ""
        self._error = ""
        self._fraction = None
        self._phase = ""
        self._matches_text = ""
        self._started_at = time.monotonic()
        self.auto_refresh = 1.0 / self._FPS
        self.refresh()

    def set_progress(self, fraction: float | None, phase: str = "") -> None:
        r"""Store the bar fraction and the phase verb.

        While scanning the rule shows the spinner, the phase verb, and the
        bar+percent only; the spinner timer repaints the stored state on its
        next frame. The N/M source count and per-source detail live in the
        ``Ctrl-\`` row, not here.
        """
        self._fraction = fraction
        self._phase = phase

    def set_matches(self, text: str) -> None:
        """Store the right-slot match/cursor text."""
        self._matches_text = text
        if self.auto_refresh is None:
            self.refresh()

    def set_narrow(self, narrow: bool) -> None:
        """Record whether the row is too narrow to also carry the match count."""
        self._narrow = narrow
        if self.auto_refresh is None:
            self.refresh()

    def freeze(self, outcome: str, message: str = "") -> None:
        """Search finished: stop the timer and lock the final state.

        A complete scan drops its glyph and word entirely — the full bar at
        100%% is enough. Interrupted/error keep a marker (``■`` / ``✗`` + the
        error message), since those outcomes aren't self-evident from the bar.
        """
        self._outcome = outcome
        # ``_final_glyph`` only flags "frozen"; the rendered marker is chosen
        # from ``_outcome`` in ``_payload`` (complete shows none).
        self._final_glyph = {"complete": "✓", "interrupted": "■", "error": "✗"}.get(
            outcome,
            "·",
        )
        self._error = message if outcome == "error" else ""
        if outcome == "complete":
            self._fraction = 1.0
        self.auto_refresh = None
        self.refresh()

    def go_idle(self) -> None:
        """Collapse to the clean plain rule (no search active)."""
        self._active = False
        self._final_glyph = None
        self._outcome = ""
        self._error = ""
        self._fraction = None
        self._phase = ""
        self._matches_text = ""
        self.auto_refresh = None
        self.refresh()

    def invalidate(self) -> None:
        """Repaint (e.g. after a resize changed the available width)."""
        self.refresh()

    # --- rendering --------------------------------------------------------
    def _spinner(self) -> str:
        """Return the spinner glyph: the frozen outcome, else the wall-clock frame."""
        if self._final_glyph is not None:
            return self._final_glyph
        elapsed = time.monotonic() - self._started_at
        return self._SEQUENCE[int(elapsed * self._FPS) % len(self._SEQUENCE)]

    def render(self) -> Text:
        """Idle → plain ``─results────``; active → fold the payload into the rule."""
        if not self._active:
            return super().render()
        width = int(getattr(self.size, "width", 0) or 0)
        label_cost = 1 + cell_len(self._label)  # leading ─ + label
        # Reserve the trailing cap dash and a 2-cell minimum gap; the payload
        # is fit into whatever remains and right-anchored against the cap.
        avail = max(0, width - label_cost - 1 - 2)
        payload = self._payload(avail)
        gap = max(2, width - label_cost - payload.cell_len - 1)
        text = Text(no_wrap=True, overflow="crop")
        # The rule frame (leading dash, label, gap dashes, trailing cap) carries
        # NO inline color so it inherits the widget's CSS `color` — $ag-faint at
        # rest, $accent via `-active` on focus, like the plain idle rule and the
        # filter's rule. Only the payload segments get inline hues.
        text.append("─")
        text.append(self._label, style="bold")
        text.append("─" * gap)
        text.append_text(payload)
        text.append("─")
        return text

    def _payload(self, avail: int) -> Text:
        r"""Build the right-of-gap status fragment, fit to ``avail`` cells.

        Scanning shows ``✽ Scanning ▰▰▱ 5%`` — spinner, phase verb, bar, and
        percent. A completed scan drops the spinner and verb entirely (a full
        ``▰▰▰▰▰ 100%`` says it); interrupted/error keep a ``■``/``✗`` marker.
        The match/cursor count appears only once the scan has finished. The N/M
        source count, per-source detail, and elapsed time live in the
        ``Ctrl-\`` row, never here.
        """
        payload = Text(no_wrap=True, overflow="crop")
        frozen = self._final_glyph is not None
        # Leading marker: the animated spinner while scanning; on finish, only
        # the stopped/error markers — a completed scan needs none.
        if not frozen:
            glyph, glyph_style = self._spinner(), self._c_accent
        elif self._outcome == "interrupted":
            glyph, glyph_style = "■", self._c_muted
        elif self._outcome == "error":
            glyph, glyph_style = "✗", self._c_muted
        else:
            glyph, glyph_style = "", ""
        if glyph:
            payload.append(" ")
            payload.append(glyph, style=glyph_style or None)
        used = payload.cell_len
        # Phase verb — only while scanning; the finished states drop the word.
        if not frozen:
            verb = phase_label(self._phase)
            if verb and used + 1 + cell_len(verb) <= avail:
                payload.append(" ")
                payload.append(verb, style=self._c_muted or None)
                used = payload.cell_len
        if frozen and self._outcome == "error":
            room = max(0, avail - used - 1)
            message = self._error
            if cell_len(message) > room:
                message = (message[: max(0, room - 1)] + "…") if room > 1 else ""
            if message:
                payload.append(" ")
                payload.append(message, style=self._c_muted or None)
            return payload
        # The progress bar + percent (the "scrollbar"), plus — only after the
        # scan finishes — the match/cursor count.
        percent = format_progress_percent(self._fraction) if self._fraction is not None else ""
        matches = self._matches_text or ""
        show_matches = frozen and bool(matches) and not self._narrow
        percent_cost = 1 + cell_len(percent) if percent else 0
        matches_cost = 2 + cell_len(matches) if show_matches else 0
        bar_room = avail - used - percent_cost - matches_cost - 1
        if bar_room < self._MIN_BAR and show_matches:
            show_matches = False
            bar_room = avail - used - percent_cost - 1
        if bar_room >= self._MIN_BAR and self._fraction is not None:
            bar_cells = min(bar_room, self._MAX_BAR)
        else:
            bar_cells = 0
        if bar_cells > 0 and self._fraction is not None:
            bar = render_progress_meter(self._fraction, bar_cells)
            filled = bar.count("▰")
            fill_hex = self._c_muted if self._outcome == "interrupted" else self._c_success
            payload.append(" ")
            payload.append("▰" * filled, style=fill_hex or None)
            payload.append("▱" * (len(bar) - filled), style=self._c_muted or None)
        if percent:
            payload.append(" ")
            payload.append(percent, style=self._c_accent or None)
        if show_matches:
            payload.append("  ")
            payload.append(matches, style=f"{self._c_accent} bold".strip())
        return payload


class SpinnerWidget(Static):
    """Self-driving star spinner that animates regardless of event-loop load.

    The widget pulls its frame index from ``time.monotonic()`` on every
    ``render`` and lets Textual's per-widget ``auto_refresh`` reactor drive
    the redraw. This decouples the spinner from any main-thread timer or
    message handler — even if record-batch dispatch backs up, the spinner
    keeps ticking.

    Frames ping-pong through the star glyphs — inspired by Claude
    Code's compaction-spinner aesthetic. The endpoints are doubled
    (forward then full reverse) so the breathe holds briefly at the
    dot and at full bloom instead of bouncing straight back.

    Every frame must stay off the Unicode emoji table — glyphs like
    ``✳`` (U+2733 EIGHT SPOKED ASTERISK) carry an emoji presentation
    that terminal fonts substitute with a colored bitmap. The
    teardrop-spoked asterisks below have text presentation only.
    """

    _FRAMES: t.ClassVar[str] = "·✢✽✻"
    _SEQUENCE: t.ClassVar[str] = _FRAMES + _FRAMES[::-1]
    _FPS: t.ClassVar[float] = 2.0

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 -- forwarded to Textual's ``id`` kwarg
        super().__init__("", id=id)
        self._final_glyph: str | None = None
        self._started_at: float = time.monotonic()

    def on_mount(self) -> None:
        """Arm the per-widget refresh timer (Textual reads this after mount)."""
        self.auto_refresh = 1.0 / self._FPS

    def render(self) -> str:
        """Return the current star frame from elapsed wall-clock time."""
        if self._final_glyph is not None:
            return self._final_glyph
        elapsed = time.monotonic() - self._started_at
        frame_index = int(elapsed * self._FPS) % len(self._SEQUENCE)
        return self._SEQUENCE[frame_index]

    def freeze(self, glyph: str) -> None:
        """Stop animating and lock the displayed glyph (called on terminal events)."""
        self._final_glyph = glyph
        self.auto_refresh = None
        self.refresh()

    def unfreeze(self) -> None:
        """Resume animation (called when a fresh search restarts)."""
        self._final_glyph = None
        self._started_at = time.monotonic()
        self.auto_refresh = 1.0 / self._FPS
        self.refresh()


class MeterWidget(Static):
    """Inline ``▰▱`` progress meter with change-gated repaints.

    ``set_progress`` recomputes the rendered string and only calls
    ``refresh()`` when the visible cells actually change — a 17-cell
    bar has 18 fill states plus ~100 integer percents, so thousands
    of per-source progress callbacks collapse to ~120 repaints.

    Width adaptation happens at render time: with enough room the
    meter shows ``▰▰▰▱▱ 52%``; below ``_MIN_BAR_CELLS`` of bar room
    it renders nothing — on narrow statuslines the search percent
    moves to the right slot instead, next to the match count.
    While the source total is unknown (discovery / planning phases)
    it shows the phase word instead of a bar — the spinner next
    door already supplies motion, so no second animation timer.
    No ``auto_refresh`` is armed; the widget costs nothing when idle.
    """

    _MIN_BAR_CELLS: t.ClassVar[int] = 4

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 -- forwarded to Textual's ``id`` kwarg
        super().__init__("", id=id)
        self._fraction: float | None = None
        self._indeterminate_phase: str = ""
        self._frozen: bool = False
        self._frozen_blank: bool = False
        self._narrow: bool = False
        self._last_render: str | None = None

    def set_narrow(self, narrow: bool) -> None:
        """Suppress the meter on narrow statuslines.

        The right slot carries the search percent there; squeezing a
        bar in as well made it pop in and out whenever the growing
        match count nudged the meter across its fits-a-bar threshold.
        """
        self._narrow = narrow
        self._maybe_refresh()

    def set_progress(
        self,
        fraction: float | None,
        indeterminate_phase: str = "",
    ) -> None:
        """Store new progress state; repaint only when the output changes."""
        self._fraction = fraction
        self._indeterminate_phase = indeterminate_phase
        self._maybe_refresh()

    def freeze(self, outcome: str) -> None:
        """Lock the meter into its post-search look — the bar IS the summary.

        ``"complete"`` fills the bar and recolors it green;
        ``"interrupted"`` keeps the bar at its last fill in gray.
        Errors blank the meter — the status text carries the
        failure message.
        """
        self._frozen = True
        self._frozen_blank = outcome == "error"
        if outcome == "complete":
            self._fraction = 1.0
            self.add_class("-done")
        elif outcome == "interrupted":
            self.add_class("-stopped")
        self._maybe_refresh()

    def reset(self) -> None:
        """Clear all state for a fresh search."""
        self._frozen = False
        self._frozen_blank = False
        self._fraction = None
        self._indeterminate_phase = ""
        self.remove_class("-done", "-stopped")
        self._maybe_refresh()

    def invalidate(self) -> None:
        """Drop the change-gate cache and repaint (e.g. after a resize)."""
        self._last_render = None
        self.refresh()

    def shows_bar(self) -> bool:
        """Whether the meter will render a bar (vs. nothing).

        False when there is no fraction yet (e.g. a search frozen
        before the first scanning snapshot), on narrow statuslines,
        or for the blanked error state — cases where the post-search
        left text must carry the outcome word instead.
        """
        return self._fraction is not None and not self._narrow and not self._frozen_blank

    def _compose_text(self) -> str:
        """Build the meter text for the current state and available width."""
        if self._frozen_blank or self._narrow:
            return ""
        width = int(getattr(self.size, "width", 0) or 0)
        if width <= 0:
            return ""
        if self._fraction is None:
            # A search frozen before any source total (e.g. cancelled
            # during discovery) has no bar to show.
            if self._frozen:
                return ""
            return self._indeterminate_phase[:width]
        percent = format_progress_percent(self._fraction)
        # Exact fit: one space between bar and percent, one trailing
        # cell — the percent hugs the bar and the gap to the right
        # slot stays constant while the percent grows in digits.
        bar_width = width - len(percent) - 2
        if bar_width >= self._MIN_BAR_CELLS:
            bar = render_progress_meter(self._fraction, bar_width)
            return f"{bar} {percent}"
        return ""

    def _maybe_refresh(self) -> None:
        """Repaint only when the composed text differs from the last paint."""
        text = self._compose_text()
        if text == self._last_render:
            return
        self._last_render = text
        self.refresh()

    def render(self) -> str:
        """Return the meter text; keeps the change-gate cache in sync."""
        text = self._compose_text()
        self._last_render = text
        return text


class SearchingPanel(Static):
    """Centered, self-driving search status for the empty-canvas moment.

    While a search runs and no results have arrived yet, the explorer hosts
    this panel in the centered ``#searching-panel`` slot — a spinner, the
    phase verb, the source progress, the match count, and elapsed time. The
    instant the first record batch lands the app swaps it for the results
    list and the folded :class:`ResultsHeader` rule carries the phase from
    there; a search that finds nothing freezes the panel into its terminal
    ``No matches`` state instead.

    Like :class:`ResultsHeader` the spinner self-drives off ``time.monotonic``
    via ``auto_refresh`` while active, so it ticks regardless of event-loop
    load; the worker thread only calls store-only setters (ADR 0011). The
    centering is paint-free CSS (``content-align: center middle``); this
    widget only composes the multi-line Rich ``Text``.
    """

    _FRAMES: t.ClassVar[str] = "·✢✽✻"
    _SEQUENCE: t.ClassVar[str] = _FRAMES + _FRAMES[::-1]
    _FPS: t.ClassVar[float] = 2.0

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 -- Textual ``id`` kwarg
        super().__init__("", id=id)
        self._active = False
        self._phase = ""
        self._current: int | None = None
        self._total: int | None = None
        self._matches = 0
        self._final_glyph: str | None = None
        self._outcome = ""
        self._error = ""
        self._frozen_total = 0
        self._frozen_elapsed: float | None = None
        self._started_at = time.monotonic()
        self._c_accent = ""
        self._c_success = ""
        self._c_muted = ""
        self._c_dim = ""

    def on_mount(self) -> None:
        """Resolve the payload colors from the active theme (no timer until active)."""
        self.refresh_theme()

    def refresh_theme(self) -> None:
        """Re-resolve the payload hexes (called on theme switch)."""
        theme_vars = t.cast("t.Any", self.app).theme_variables
        self._c_accent = ui_theme.resolve(theme_vars, "accent")
        self._c_success = ui_theme.resolve(theme_vars, "success")
        self._c_muted = ui_theme.resolve(theme_vars, "ag-muted")
        self._c_dim = ui_theme.resolve(theme_vars, "ag-dim")
        self.refresh()

    # --- lifecycle (driven by the app's search flow) ----------------------
    def begin(self) -> None:
        """Activate on search start: clear state and arm the spinner timer."""
        self._active = True
        self._final_glyph = None
        self._outcome = ""
        self._error = ""
        self._phase = ""
        self._current = None
        self._total = None
        self._matches = 0
        self._frozen_total = 0
        self._frozen_elapsed = None
        self._started_at = time.monotonic()
        self.auto_refresh = 1.0 / self._FPS
        self.refresh()

    def set_snapshot(self, snapshot: t.Any) -> None:
        """Store the latest progress snapshot; the timer repaints it next frame."""
        self._phase = snapshot.phase
        self._current = snapshot.current
        self._total = snapshot.total
        self._matches = snapshot.matches

    def freeze(
        self,
        outcome: str,
        total: int = 0,
        elapsed: float | None = None,
        message: str = "",
    ) -> None:
        """Lock the panel into its terminal state and stop the spinner timer."""
        self._outcome = outcome
        self._error = message if outcome == "error" else ""
        self._frozen_total = total
        self._frozen_elapsed = elapsed
        self._final_glyph = {"complete": "✓", "interrupted": "■", "error": "✗"}.get(
            outcome,
            "·",
        )
        self.auto_refresh = None
        self.refresh()

    def go_idle(self) -> None:
        """Stop the timer and clear active state (the panel is hidden by CSS)."""
        self._active = False
        self._final_glyph = None
        self.auto_refresh = None
        self.refresh()

    # --- rendering --------------------------------------------------------
    def _spinner(self) -> str:
        """Return the frozen outcome glyph, else the wall-clock spinner frame."""
        if self._final_glyph is not None:
            return self._final_glyph
        elapsed = time.monotonic() - self._started_at
        return self._SEQUENCE[int(elapsed * self._FPS) % len(self._SEQUENCE)]

    def render(self) -> Text:
        """Compose the centered two-line status block (CSS does the centering)."""
        glyph = self._spinner()
        text = Text(no_wrap=True, overflow="ellipsis")
        if self._final_glyph is not None:
            return self._render_frozen(text, glyph)
        text.append(glyph, style=self._c_accent or None)
        text.append(" ")
        text.append(phase_label(self._phase) or "Searching")
        if self._current is not None and self._total is not None:
            text.append(f" {self._current}/{self._total} sources", style=self._c_muted or None)
        byline = self._byline()
        if byline:
            text.append("\n")
            text.append(byline, style=self._c_dim or None)
        return text

    def _byline(self) -> str:
        """Build the dim second line: match count + elapsed, or a discovery hint."""
        parts: list[str] = []
        if self._matches > 0:
            parts.append(format_match_count(self._matches))
        seconds = int(time.monotonic() - self._started_at)
        if seconds >= 1:
            parts.append(format_elapsed_compact(seconds))
        return " · ".join(parts) if parts else "searching your stores…"

    def _render_frozen(self, text: Text, glyph: str) -> Text:
        """Compose the post-search terminal block."""
        if self._outcome == "error":
            text.append(glyph, style=self._c_muted or None)
            text.append(" ")
            text.append(self._error or "Search failed", style=self._c_muted or None)
            return text
        hue = self._c_success if self._outcome == "complete" else self._c_muted
        text.append(glyph, style=hue or None)
        text.append(" ")
        if self._frozen_total <= 0 and self._outcome == "complete":
            text.append("No matches", style=self._c_muted or None)
        else:
            prefix = "Stopped · " if self._outcome == "interrupted" else ""
            text.append(
                f"{prefix}{format_match_count(self._frozen_total)}", style=self._c_muted or None
            )
        if self._frozen_elapsed is not None and self._frozen_elapsed >= 1:
            text.append("\n")
            text.append(format_elapsed_compact(self._frozen_elapsed), style=self._c_dim or None)
        return text
