"""Status-line widgets: the self-driving spinner and the progress meter.

Both are ``Static`` subclasses. Neither touches the app or the message bus; they
are pure presentation driven by ``set_*`` calls, so they are directly testable
in isolation.
"""

from __future__ import annotations

import time
import typing as t

from rich.text import Text
from textual.widgets import Static

from agentgrep.ui.format import format_progress_percent, render_progress_meter

__all__ = ["MeterWidget", "PaneHeader", "SpinnerWidget"]


class PaneHeader(Static):
    """A pi-style section header: a bold label followed by a width-filling rule.

    Mirrors pi's ``DynamicBorder`` (``"─".repeat(width)``) sitting under an
    indented bold label (``tree-selector.ts``), compacted onto one row:
    ``results ────────────``. The label keeps its bold weight; the line color is
    driven entirely by CSS (``$ag-muted`` at rest, ``$accent`` via the
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
        """Return ``<label> <rule>`` filling the widget width."""
        width = int(getattr(self.size, "width", 0) or 0)
        rule_len = max(0, width - len(self._label) - 1)
        text = Text(no_wrap=True, overflow="crop")
        text.append(self._label, style="bold")
        if rule_len:
            text.append(" " + "─" * rule_len)
        return text


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
