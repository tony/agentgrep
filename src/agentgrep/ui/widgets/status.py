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

from agentgrep.progress import ProgressSnapshot, format_match_count
from agentgrep.ui import theme as ui_theme
from agentgrep.ui.format import (
    format_elapsed_compact,
    format_progress_percent,
    phase_label,
    render_progress_meter,
)

__all__ = [
    "FilterHeader",
    "MeterWidget",
    "PaneHeader",
    "ResultsHeader",
    "SearchingPanel",
    "SpinnerWidget",
]


class PaneHeader(Static):
    """A pi-style section header: a left-positioned label embedded in a full rule.

    One leading ``─`` cell sits before the bold label, then the rule fills to
    the right edge: ``─results────────``. An optional status is right-anchored
    without moving the label. The line color is driven entirely by CSS
    (``$ag-faint`` at rest, ``$accent`` via the ``-active`` class), so
    recoloring the focused pane's header is paint-only. The rule length is
    recomputed on resize.
    """

    def __init__(self, label: str, *, id: str | None = None) -> None:  # noqa: A002 -- Textual ``id`` kwarg
        super().__init__(id=id)
        self._label = label
        self._right = ""

    def set_right(self, text: str) -> None:
        """Right-anchor ``text`` in the rule, repainting only on change."""
        if text == self._right:
            return
        self._right = text
        self.refresh()

    def on_resize(self) -> None:
        """Recompute the rule length when the column width changes."""
        self.refresh()

    def render(self) -> Text:
        """Return ``─<label><rule>`` filling the widget width.

        The single leading rule cell anchors the label. When a right status is
        present, a trailing rule cell anchors that status against the edge.
        """
        width = int(getattr(self.size, "width", 0) or 0)
        label_cost = 1 + cell_len(self._label)
        right = self._fit_right(max(0, width - label_cost - 4))
        text = Text(no_wrap=True, overflow="crop")
        text.append("─")
        text.append(self._label, style="bold")
        if not right:
            text.append("─" * max(0, width - label_cost))
            return text
        gap = max(2, width - label_cost - cell_len(right) - 2)
        text.append("─" * gap)
        text.append(" ")
        text.append(right)
        text.append("─")
        return text

    def _fit_right(self, avail: int) -> str:
        """Return the widest whole right-slot variant that fits ``avail``."""
        if cell_len(self._right) <= avail:
            return self._right
        compact = self._right.rsplit("  ", 1)[0].strip()
        return compact if cell_len(compact) <= avail else ""


class ResultsHeader(PaneHeader):
    """Rule separating the filter input from its navigable result list."""


class FilterHeader(PaneHeader):
    """Filter section header with live search status folded into the rule.

    Extends :class:`PaneHeader`: idle, it renders the plain ``─filter─────``
    rule; while a search runs it folds a compact indeterminate status into the
    right of the same rule (pi's ``fitBorder`` shape): spinner, phase, source,
    and record heartbeat. Segments shed right-to-left as the width tightens.

    The spinner self-drives off ``time.monotonic`` via ``auto_refresh`` while a
    search is active; progress updates only store while it runs, and the next
    timer frame repaints. On finish the timer stops and every outcome remains
    explicit text.
    """

    _FRAMES: t.ClassVar[str] = "·✢✽✻"
    _SEQUENCE: t.ClassVar[str] = _FRAMES + _FRAMES[::-1]
    _FPS: t.ClassVar[float] = 2.0

    def __init__(self, label: str, *, id: str | None = None) -> None:  # noqa: A002 -- Textual ``id`` kwarg
        super().__init__(label, id=id)
        self._active = False
        self._phase = ""
        self._current: int | None = None
        self._total: int | None = None
        self._source_records_seen: int | None = None
        self._final_glyph: str | None = None
        self._outcome = ""
        self._error = ""
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
        self._phase = ""
        self._current = None
        self._total = None
        self._source_records_seen = None
        self._started_at = time.monotonic()
        self.auto_refresh = 1.0 / self._FPS
        self.refresh()

    def set_snapshot(self, snapshot: ProgressSnapshot) -> None:
        """Store typed live-search facts without forcing an event repaint.

        Source ordinals are not comparable work units, so active scans remain
        indeterminate. The timer repaints the stored source-local heartbeat on
        its next frame.
        """
        self._phase = snapshot.phase
        scanning = snapshot.phase == "scanning"
        self._current = snapshot.current if scanning else None
        self._total = snapshot.total if scanning else None
        self._source_records_seen = snapshot.source_records_seen if scanning else None

    def freeze(self, outcome: str, message: str = "") -> None:
        """Search finished: stop the timer and lock the final state.

        Every terminal state is textual. Completed scans say ``Done`` rather
        than fabricating determinate progress from heterogeneous sources.
        """
        self._outcome = outcome
        # ``_final_glyph`` is only a "frozen" flag; its glyph value is unused —
        # ``_payload`` derives the rendered marker from ``_outcome`` (complete
        # shows none).
        self._final_glyph = {"complete": "✓", "interrupted": "■", "error": "✗"}.get(
            outcome,
            "·",
        )
        self._error = message if outcome == "error" else ""
        self.auto_refresh = None
        self.refresh()

    def go_idle(self) -> None:
        """Collapse to the clean plain rule (no search active)."""
        self._active = False
        self._final_glyph = None
        self._outcome = ""
        self._error = ""
        self._phase = ""
        self._current = None
        self._total = None
        self._source_records_seen = None
        self.auto_refresh = None
        self.refresh()

    def invalidate(self) -> None:
        """Repaint (e.g. after a resize changed the available width)."""
        self.refresh()

    # --- rendering --------------------------------------------------------
    def _spinner(self) -> str:
        """Return the wall-clock spinner frame (called only while not frozen)."""
        elapsed = time.monotonic() - self._started_at
        return self._SEQUENCE[int(elapsed * self._FPS) % len(self._SEQUENCE)]

    def render(self) -> Text:
        """Idle → plain rule; active → fold the search status into it."""
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

        Active scans are indeterminate and show bounded source-local facts.
        Finished scans use explicit textual outcomes. Result navigation lives
        on the separate results rule below the filter input.
        """
        payload = Text(no_wrap=True, overflow="crop")
        frozen = self._final_glyph is not None
        # Leading marker: the animated spinner while scanning; on finish, only
        # stopped/error need a glyph because completion has the word ``Done``.
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
        if not frozen:
            verb = phase_label(self._phase)
            self._append_active_progress(payload, avail, verb)
            return payload
        if frozen and self._outcome == "error":
            room = max(0, avail - used - 1)
            message = self._error
            if cell_len(message) > room:
                message = (message[: max(0, room - 1)] + "…") if room > 1 else ""
            if message:
                payload.append(" ")
                payload.append(message, style=self._c_muted or None)
            return payload
        if frozen and self._outcome == "interrupted":
            if used + len(" Stopped") <= avail:
                payload.append(" Stopped", style=self._c_muted or None)
            return payload
        if frozen and self._outcome == "complete":
            if used + len(" Done") <= avail:
                payload.append(" Done", style=self._c_success or None)
            return payload
        return payload

    def _append_active_progress(self, payload: Text, avail: int, verb: str) -> None:
        """Append one whole status variant so wider rows never lose facts."""
        current = self._current
        total = self._total
        records = self._source_records_seen
        if current is None or total is None:
            if verb and payload.cell_len + 1 + cell_len(verb) <= avail:
                payload.append(" ")
                payload.append(verb, style=self._c_muted or None)
            return
        wide_source = f"source {current} of {total}"
        compact_source = f"{current}/{total}"
        record_text = ""
        if records is not None and records > 0:
            suffix = "record" if records == 1 else "records"
            record_text = f" · {records} {suffix}"
        variants = tuple(
            variant
            for variant in (
                f"{verb} {wide_source}{record_text}" if verb else "",
                f"{verb} {wide_source}" if verb else "",
                f"{verb} {compact_source}{record_text}" if verb else "",
                f"{verb} {compact_source}" if verb else "",
                f"{compact_source}{record_text}",
                compact_source,
                verb,
            )
            if variant
        )
        for variant in variants:
            if payload.cell_len + 1 + cell_len(variant) <= avail:
                payload.append(" ")
                payload.append(variant, style=self._c_muted or None)
                return


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
    it renders nothing rather than squeezing an unreadable bar into narrow
    chrome.
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

        Squeezing a bar in made it pop in and out whenever adjacent content
        nudged the meter across its fits-a-bar threshold.
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
    list and the folded :class:`FilterHeader` rule carries the phase from
    there; a search that finds nothing freezes the panel into its terminal
    ``No matches`` state instead.

    Like :class:`FilterHeader`, the spinner uses ``time.monotonic`` with
    ``auto_refresh`` while active. The worker thread only calls store-only
    setters (ADR 0011); the pump performs bounded string rendering. Centering
    is paint-free CSS (``content-align: center middle``).
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
        self._source_records_seen: int | None = None
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
        self._source_records_seen = None
        self._matches = 0
        self._frozen_total = 0
        self._frozen_elapsed = None
        self._started_at = time.monotonic()
        self.auto_refresh = 1.0 / self._FPS
        self.refresh()

    def set_snapshot(self, snapshot: ProgressSnapshot) -> None:
        """Store the latest progress snapshot; the timer repaints it next frame."""
        self._phase = snapshot.phase
        scanning = snapshot.phase == "scanning"
        self._current = snapshot.current if scanning else None
        self._total = snapshot.total if scanning else None
        self._source_records_seen = snapshot.source_records_seen if scanning else None
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
            text.append(
                f" source {self._current} of {self._total}",
                style=self._c_muted or None,
            )
        byline = self._byline()
        if byline:
            text.append("\n")
            text.append(byline, style=self._c_dim or None)
        return text

    def _byline(self) -> str:
        """Build the dim second line from source-local facts and elapsed time."""
        parts: list[str] = []
        if self._source_records_seen is not None and self._source_records_seen > 0:
            suffix = "record" if self._source_records_seen == 1 else "records"
            parts.append(f"{self._source_records_seen} {suffix}")
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
