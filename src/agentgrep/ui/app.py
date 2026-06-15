"""Streaming Textual app — ``run_ui`` and the app factory.

This module holds the Textual widget classes (``AgentGrepApp``,
``SpinnerWidget``, ``FilterInput``), their message subclasses, and
the per-record LRU caches that drive the interactive explorer.

Textual is imported lazily inside :func:`build_streaming_ui_app` (via
``importlib.import_module``) so importing this module by itself does
not require Textual at import time — the import error is deferred to
the moment a UI is actually built.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import importlib
import json
import pathlib
import time
import typing as t
from collections import abc as cabc

from rich.console import Group as _RichGroup
from rich.markdown import Markdown as _RichMarkdown
from rich.syntax import Syntax as _RichSyntax

from agentgrep import (
    DETAIL_BODY_MAX_LINES,
    FilterCompletedPayload,
    FilterRequestedPayload,
    ProgressSnapshot,
    RichTextModule,
    RunnableAppLike,
    SearchControl,
    SearchQuery,
    SearchRecord,
    SearchRequestedPayload,
    SearchRuntime,
    StaticLike,
    StreamingAppLike,
    StreamingRecordsBatch,
    StreamingSearchFinished,
    StreamingSearchProgress,
    TextualAppModule,
    TextualBindingModule,
    TextualContainersModule,
    TextualMessageModule,
    TextualOptionListInternalsModule,
    TextualWidgetsModule,
    build_search_haystack,
    cached_haystack,
    clear_haystack_cache,
    compute_filter_matches,
    detect_content_format,
    find_first_match_line,
    format_compact_path,
    format_match_count,
    format_timestamp_tig,
    highlight_matches,
    run_search_query,
    truncate_lines,
)
from agentgrep.query import default_registry
from agentgrep.ui.completion import FilterSuggester, QuerySuggester


def scroll_percent(scroll_y: float, max_scroll_y: float) -> int:
    """Return an integer scroll percent clamped to ``[0, 100]``.

    Returns ``100`` when there is no scrollable region (everything fits)
    and ``0`` when scrolled to the very top. Mirrors tig's bottom-status
    convention where a fully visible view reads as ``100%``.
    """
    if max_scroll_y <= 0:
        return 100 if scroll_y <= 0 else 0
    return min(100, max(0, round((scroll_y / max_scroll_y) * 100)))


def format_elapsed_compact(seconds: float) -> str:
    """Format elapsed seconds as a compact ticker label.

    Every unit is truncated (floored) rather than rounded so a live
    1 Hz ticker never displays a second that has not fully elapsed.

    Parameters
    ----------
    seconds : float
        Elapsed wall-clock seconds. Negative values clamp to ``0``.

    Returns
    -------
    str
        ``"32s"`` under a minute, ``"7m 32s"`` under an hour, and
        ``"1h 02m"`` from an hour up (seconds dropped to bound width).

    Examples
    --------
    >>> format_elapsed_compact(0)
    '0s'
    >>> format_elapsed_compact(32.9)
    '32s'
    >>> format_elapsed_compact(60)
    '1m 0s'
    >>> format_elapsed_compact(452)
    '7m 32s'
    >>> format_elapsed_compact(3725)
    '1h 02m'
    """
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def render_progress_meter(fraction: float, width: int) -> str:
    """Render a ``▰▱`` progress bar of ``width`` cells.

    Parameters
    ----------
    fraction : float
        Completion in ``[0.0, 1.0]``; values outside the range clamp.
    width : int
        Bar width in cells. Non-positive widths render nothing.

    Returns
    -------
    str
        ``round(fraction * width)`` filled cells (``▰``) followed by
        empty cells (``▱``).

    Examples
    --------
    >>> render_progress_meter(0.52, 17)
    '▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱'
    >>> render_progress_meter(0.0, 5)
    '▱▱▱▱▱'
    >>> render_progress_meter(1.5, 5)
    '▰▰▰▰▰'
    >>> render_progress_meter(0.5, 0)
    ''
    """
    if width <= 0:
        return ""
    clamped = max(0.0, min(1.0, fraction))
    filled = min(width, round(clamped * width))
    return "▰" * filled + "▱" * (width - filled)


def format_progress_percent(fraction: float) -> str:
    """Format a completion fraction as an integer percent.

    Parameters
    ----------
    fraction : float
        Completion in ``[0.0, 1.0]``; values outside the range clamp.

    Returns
    -------
    str
        The rounded integer percent with a ``%`` suffix.

    Examples
    --------
    >>> format_progress_percent(0.524)
    '52%'
    >>> format_progress_percent(1.0)
    '100%'
    >>> format_progress_percent(-0.5)
    '0%'
    """
    clamped = max(0.0, min(1.0, fraction))
    return f"{round(clamped * 100)}%"


def format_scanning_detail(
    phase: str,
    current: int | None,
    total: int | None,
    detail: str | None,
) -> str:
    r"""Compose the verbose scanning line for the toggleable detail row.

    The ``Ctrl-\`` row carries the per-source counts the compact
    statusline omits — phase, scanned/total sources, and in-source
    record/match counts — with the phase word capitalized to open the
    row as a sentence.

    Parameters
    ----------
    phase : str
        Engine phase word (e.g. ``"scanning"``, ``"discovering"``).
    current : int or None
        Index of the source being scanned, when known.
    total : int or None
        Total number of sources, when known.
    detail : str or None
        In-source detail such as ``"2176 records, 354 source matches"``.

    Returns
    -------
    str
        The composed detail line; segments with unknown inputs are
        omitted.

    Examples
    --------
    >>> format_scanning_detail(
    ...     "scanning", 5662, 6748, "2176 records, 354 source matches",
    ... )
    'Scanning 5662/6748 sources | 2176 records, 354 source matches'
    >>> format_scanning_detail("prefiltering", None, None, "~/.codex/sessions/")
    'Prefiltering ~/.codex/sessions/'
    >>> format_scanning_detail("discovering", None, None, None)
    'Discovering'
    """
    heading = phase[:1].upper() + phase[1:]
    if current is not None and total is not None:
        line = f"{heading} {current}/{total} sources"
        if detail:
            line = f"{line} | {detail}"
        return line
    if detail:
        return f"{heading} {detail}"
    return heading


def searching_left_text(elapsed: float, *, narrow: bool) -> str:
    """Compose the left status text shown next to the spinner.

    The query itself is not repeated — the search input directly above
    the statusline already shows it. Narrow mode also drops the elapsed
    ticker (and its ellipsis) so the percent and match count keep their
    cells on small terminals.

    Parameters
    ----------
    elapsed : float
        Wall-clock seconds since the search started.
    narrow : bool
        When ``True``, omit the elapsed suffix for small terminals.

    Returns
    -------
    str
        The left status segment, e.g. ``"Searching… (32s)"``.

    Examples
    --------
    >>> searching_left_text(32.4, narrow=False)
    'Searching… (32s)'
    >>> searching_left_text(32.4, narrow=True)
    'Searching'
    """
    if narrow:
        return "Searching"
    return f"Searching… ({format_elapsed_compact(elapsed)})"


def run_ui(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
    initial_search_text: str | None = None,
) -> None:
    """Launch the streaming Textual explorer for ``query``.

    Thin wrapper that builds the app via :func:`build_streaming_ui_app` and
    calls ``app.run()``. The factory split lets tests construct the app for
    a Textual ``Pilot`` smoke test without entering the blocking run loop.

    Parameters
    ----------
    home : pathlib.Path
        User home directory, passed through to :func:`run_search_query`.
    query : SearchQuery
        Search to run. Empty ``terms`` means "all records" (browse mode).
    control : SearchControl
        Shared cooperative-cancel flag; ``Esc`` / ``Ctrl-C`` call
        ``request_answer_now`` to nudge the worker to wrap up.
    initial_search_text : str | None
        Initial value of the TUI search box. When ``None``, defaults
        to the space-joined ``query.terms``. The CLI passes the raw
        positional string here so a launch like
        ``agentgrep search --ui agent:codex bliss`` opens with the
        full query in the box (not just the text terms).
    """
    app = build_streaming_ui_app(
        home,
        query,
        control=control,
        initial_search_text=initial_search_text,
    )
    t.cast("RunnableAppLike", app).run()


def build_streaming_ui_app(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
    initial_search_text: str | None = None,
) -> object:
    """Construct the streaming Textual app without entering its run loop.

    Returns the constructed ``AgentGrepApp`` instance (typed ``object`` because
    the actual class is defined dynamically inside this factory). Callers can
    invoke ``.run()`` for a real session or ``.run_test()`` for a Pilot smoke
    test. The full app body — message subclasses, ``SpinnerWidget``,
    ``FilterInput``, ``AgentGrepApp`` — lives here so the
    Textual imports stay lazy.

    Parameters
    ----------
    home : pathlib.Path
        User home directory, passed through to :func:`run_search_query`.
    query : SearchQuery
        Search to run. Empty ``terms`` means "all records" (browse mode).
    control : SearchControl
        Shared cooperative-cancel flag; ``Esc`` / ``Ctrl-C`` call
        ``request_answer_now`` to nudge the worker to wrap up.
    """
    try:
        textual_app = t.cast(
            "TextualAppModule",
            t.cast("object", importlib.import_module("textual.app")),
        )
        textual_containers = t.cast(
            "TextualContainersModule",
            t.cast("object", importlib.import_module("textual.containers")),
        )
        textual_widgets = t.cast(
            "TextualWidgetsModule",
            t.cast("object", importlib.import_module("textual.widgets")),
        )
        textual_message = t.cast(
            "TextualMessageModule",
            t.cast("object", importlib.import_module("textual.message")),
        )
        textual_option_list_internals = t.cast(
            "TextualOptionListInternalsModule",
            t.cast("object", importlib.import_module("textual.widgets.option_list")),
        )
        textual_binding = t.cast(
            "TextualBindingModule",
            t.cast("object", importlib.import_module("textual.binding")),
        )
        rich_text_module = t.cast(
            "RichTextModule",
            t.cast("object", importlib.import_module("rich.text")),
        )
    except ImportError as error:
        msg = "Textual is required for --ui. Install with `uv pip install --editable .`."
        raise RuntimeError(msg) from error

    app_type = textual_app.App
    message_type = textual_message.Message
    option_list_type = textual_widgets.OptionList
    option_type = textual_option_list_internals.Option
    binding_type = textual_binding.Binding
    rich_text = rich_text_module
    horizontal = textual_containers.Horizontal
    vertical = textual_containers.Vertical
    vertical_scroll = textual_containers.VerticalScroll
    footer = textual_widgets.Footer
    header = textual_widgets.Header
    input_widget = textual_widgets.Input
    static_type = textual_widgets.Static

    # FilterRequested / FilterCompleted stay on the Textual message bus — they
    # fire at typing speed, not streaming speed, so the FIFO queue is fine for
    # them. Records / progress / search-finished events bypass the message bus
    # entirely (see ``_make_gated_progress`` below) so they never queue behind
    # keystrokes.

    class FilterRequested(message_type):  # ty: ignore[unsupported-base]
        """Debounced filter-text-changed event from :class:`FilterInput`."""

        def __init__(self, payload: FilterRequestedPayload) -> None:
            super().__init__()
            self.payload = payload

    class FilterCompleted(message_type):  # ty: ignore[unsupported-base]
        """Worker-completed filter result posted back to the main thread."""

        def __init__(self, payload: FilterCompletedPayload) -> None:
            super().__init__()
            self.payload = payload

    class SearchRequested(message_type):  # ty: ignore[unsupported-base]
        """Debounced search-text-changed event from :class:`SearchInput`."""

        def __init__(self, payload: SearchRequestedPayload) -> None:
            super().__init__()
            self.payload = payload

    class ResultsScrollChanged(message_type):  # ty: ignore[unsupported-base]
        """Posted by :class:`SearchResultsList` when scroll or cursor moves.

        The app handler renders the right side of the results status line
        from this snapshot — cursor position out of total, plus the scroll
        percent. Pre-shaped here so the widget never reaches into the app
        directly.
        """

        def __init__(self, cursor: int | None, total: int, percent: int) -> None:
            super().__init__()
            self.cursor = cursor
            self.total = total
            self.percent = percent

    class DetailScrollChanged(message_type):  # ty: ignore[unsupported-base]
        """Posted by :class:`DetailScroll` when the detail-pane scrolls."""

        def __init__(self, percent: int) -> None:
            super().__init__()
            self.percent = percent

    class SpinnerWidget(static_type):  # ty: ignore[unsupported-base]
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

    class MeterWidget(static_type):  # ty: ignore[unsupported-base]
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

    class SearchResultsList(
        option_list_type,  # ty: ignore[unsupported-base]
        can_focus=True,
    ):
        """``OptionList`` subclass for streaming agentgrep search records.

        ``OptionList`` is Textual's proven cursor-navigable virtual list. It
        ships with working Tab focus, a visible cursor highlight via the
        ``option-list--option-highlighted`` CSS class, and posts an
        ``OptionHighlighted`` message on cursor movement — all the things our
        previous custom widget had to wire up manually and failed at in the
        real terminal.

        Adding records via ``append_records`` / ``set_records`` runs on the
        event-loop thread because the worker uses ``app.call_from_thread`` to
        invoke these methods. That keeps the streaming transport off the
        Textual message bus so keystroke + timer events never queue behind it.
        """

        BINDINGS: t.ClassVar[list[tuple[str, str, str]]] = [
            ("k", "cursor_up", "Up"),
            ("j", "cursor_down", "Down"),
            ("l", "focus_detail", "Detail"),
            ("right", "focus_detail", ""),
            ("g", "cursor_top", "Top"),
            ("G", "cursor_bottom", "Bottom"),
            ("ctrl+d", "cursor_half_page_down", "½ Down"),
            ("ctrl+u", "cursor_half_page_up", "½ Up"),
        ]

        def __init__(
            self,
            *,
            id: str | None = None,  # noqa: A002 -- forwarded to Textual's ``id`` kwarg
        ) -> None:
            super().__init__(id=id)
            self._records: list[SearchRecord] = []

        def append_records(self, records: cabc.Sequence[SearchRecord]) -> None:
            """Append a batch of records — invoked via ``app.call_from_thread``.

            Eagerly warms :func:`cached_haystack` for each new record so the
            cost is paid during streaming (when the user is already watching
            the spinner) rather than during the next filter keystroke.
            """
            if not records:
                return
            self._records.extend(records)
            for record in records:
                cached_haystack(record)
            self.add_options(
                [option_type(self._render_record(r), id=str(id(r))) for r in records],
            )

        def set_records(self, records: cabc.Sequence[SearchRecord]) -> int:
            """Apply a new filter result by patching the existing options.

            For the common "user typed another character" narrowing case the
            method removes the now-unmatched options without rebuilding the
            list — keeps rendering O(removed) instead of O(total) and never
            touches the haystack cache. Falls back to a full rebuild when
            the new set introduces records not currently shown (widening) or
            when more than half of the current options would be removed
            (where ``remove_option_at_index`` would do worse than a single
            ``clear_options`` + ``add_options`` pair).

            Returns the number of programmatic ``OptionHighlighted`` messages
            Textual queued while applying the record update.
            """
            new_records = list(records)
            new_ids: set[int] = {id(record) for record in new_records}
            current_records = self._records
            if not current_records:
                self._rebuild_options(new_records)
                return 0
            current_index_by_id: dict[int, int] = {
                id(record): idx for idx, record in enumerate(current_records)
            }
            additions = [record for record in new_records if id(record) not in current_index_by_id]
            if additions:
                self._rebuild_options(new_records)
                return 0
            to_remove_indices = sorted(
                (
                    current_index_by_id[id(record)]
                    for record in current_records
                    if id(record) not in new_ids
                ),
                reverse=True,
            )
            if len(to_remove_indices) > len(current_records) // 2:
                # More than half goes — a single clear+rebuild is cheaper
                # than N ``remove_option_at_index`` calls (each shifts the
                # internal options list).
                self._rebuild_options(new_records)
                return 0
            programmatic_highlights = 0
            for idx in to_remove_indices:
                before_highlighted = t.cast("int | None", getattr(self, "highlighted", None))
                self.remove_option_at_index(idx)
                after_highlighted = t.cast("int | None", getattr(self, "highlighted", None))
                if after_highlighted is not None and after_highlighted != before_highlighted:
                    programmatic_highlights += 1
            self._records = new_records
            return programmatic_highlights

        def _rebuild_options(self, records: cabc.Sequence[SearchRecord]) -> None:
            """Full clear + rebuild path. Used when delta-apply isn't safe."""
            self._records = list(records)
            self.clear_options()
            if self._records:
                for record in self._records:
                    cached_haystack(record)
                self.add_options(
                    [option_type(self._render_record(r), id=str(id(r))) for r in self._records],
                )

        def clear(self) -> None:
            """Empty the list."""
            self._records = []
            self.clear_options()

        def _scroll_percent(self) -> int:
            """Compute the current scroll percent, clamped to ``[0, 100]``."""
            return scroll_percent(
                float(getattr(self, "scroll_y", 0) or 0),
                float(getattr(self, "max_scroll_y", 0) or 0),
            )

        def _post_scroll_changed(self, cursor: int | None = None) -> None:
            """Post a :class:`ResultsScrollChanged` snapshot to the app.

            ``cursor`` defaults to the widget's current ``highlighted``
            reactive but accepts an explicit override so watchers can pass
            the freshly-set value through without racing the reactive
            dispatch.
            """
            if cursor is None:
                cursor = t.cast("int | None", getattr(self, "highlighted", None))
            self.post_message(
                ResultsScrollChanged(
                    cursor=cursor,
                    total=len(self._records),
                    percent=self._scroll_percent(),
                ),
            )

        def watch_scroll_y(self, old: float, new: float) -> None:
            """Re-render the status line on scroll. Inherited base does the actual scroll."""
            base = getattr(super(), "watch_scroll_y", None)
            if callable(base):
                base(old, new)
            self._post_scroll_changed()

        def watch_highlighted(self, highlighted: int | None) -> None:
            """Re-render the status line on cursor move."""
            base = getattr(super(), "watch_highlighted", None)
            if callable(base):
                base(highlighted)
            self._post_scroll_changed(cursor=highlighted)

        _AGENT_COLORS: t.ClassVar[dict[str, str]] = {
            "codex": "cyan",
            "claude": "magenta",
            "cursor-cli": "yellow",
            "cursor-ide": "bright_yellow",
        }
        _KIND_COLORS: t.ClassVar[dict[str, str]] = {
            "prompt": "green",
            "history": "blue",
        }

        def _render_record(self, record: SearchRecord) -> object:
            agent_text = (record.agent or "").ljust(8)[:8]
            kind_text = (record.kind or "").ljust(10)[:10]
            timestamp_text = format_timestamp_tig(record.timestamp).ljust(22)[:22]
            title_text = (record.title or "").ljust(40)[:40]
            path_text = format_compact_path(record.path, max_width=60)
            text = rich_text.Text(no_wrap=True, overflow="ellipsis")
            text.append(agent_text, style=self._AGENT_COLORS.get(record.agent or "", ""))
            text.append("  ")
            text.append(kind_text, style=self._KIND_COLORS.get(record.kind or "", ""))
            text.append("  ")
            text.append(timestamp_text, style="italic")
            text.append("  ")
            text.append(title_text, style="bold")
            text.append("  ")
            text.append(path_text, style="grey50")
            return text

        def action_cursor_up(self) -> None:
            """Release focus to the filter input when the cursor is at row 0."""
            if self.highlighted in (None, 0):
                self.app.action_focus_previous()
            else:
                super().action_cursor_up()

        def action_focus_detail(self) -> None:
            """Focus the detail pane (vim-style ``l``), opening it if stacked.

            Routes through the app's ``_focus_detail`` so a collapsed
            stacked pane is revealed before focus lands — focusing it
            directly would move focus into a ``display: none`` pane that
            never appears.
            """
            t.cast("t.Any", self.app)._focus_detail()

        def action_cursor_top(self) -> None:
            """Jump the highlight to the first row (vim-style ``g``)."""
            self.action_first()

        def action_cursor_bottom(self) -> None:
            """Jump the highlight to the last row (vim-style ``G``)."""
            self.action_last()

        def _cursor_jump(self, delta: int) -> None:
            """Move the highlight by ``delta`` rows, clamped to list bounds."""
            row_count = len(self._records)
            if row_count == 0:
                return
            current = self.highlighted if self.highlighted is not None else 0
            target = max(0, min(row_count - 1, current + delta))
            self.highlighted = target

        def action_cursor_half_page_down(self) -> None:
            """Advance the highlight by half the visible viewport height (vim ``Ctrl-D``)."""
            half = max(1, self.size.height // 2)
            self._cursor_jump(half)

        def action_cursor_half_page_up(self) -> None:
            """Move the highlight up by half the visible viewport height (vim ``Ctrl-U``)."""
            half = max(1, self.size.height // 2)
            self._cursor_jump(-half)

    vertical_scroll_base = t.cast("type[object]", vertical_scroll)

    class DetailScroll(
        vertical_scroll_base,  # ty: ignore[unsupported-base]
        can_focus=True,
    ):
        """``VerticalScroll`` subclass for the right-side detail pane.

        Adds vim-style bindings: ``h`` / left-arrow releases focus back to the
        results list, and ``j`` / ``k`` mirror the stock ``down`` / ``up``
        scroll bindings so navigation stays consistent with
        :class:`SearchResultsList`. ``can_focus=True`` is set via the
        class-keyword form — Textual reads it during ``__init_subclass__``,
        so the plain class-attribute form silently fails to enroll the widget
        in the focus chain.
        """

        BINDINGS: t.ClassVar[list[tuple[str, str, str]]] = [
            ("k", "scroll_up", "Up"),
            ("j", "scroll_down", "Down"),
            ("h", "focus_results", "Results"),
            ("left", "focus_results", ""),
            ("g", "scroll_home", "Top"),
            ("G", "scroll_end", "Bottom"),
            ("ctrl+d", "scroll_half_down", "½ Down"),
            ("ctrl+u", "scroll_half_up", "½ Up"),
            ("ctrl+f", "page_down", "Pg Down"),
            ("ctrl+b", "page_up", "Pg Up"),
        ]

        def action_focus_results(self) -> None:
            """Move focus leftward back to the results list (vim-style ``h``)."""
            results = self.app.query_one("#results")
            t.cast("t.Any", results).focus()

        def action_scroll_up(self) -> None:
            """Release focus to the filter input when already scrolled to the top.

            Mirrors :meth:`SearchResultsList.action_cursor_up` — when the
            widget has nothing left to give in that direction, hand focus off
            to the neighbor instead of swallowing the keystroke. Catches both
            ``k`` (our binding) and ``up`` (inherited from
            ``ScrollableContainer``).
            """
            scroll_y = t.cast("float", getattr(self, "scroll_y", 0))
            if scroll_y <= 0:
                self.app.query_one("#filter").focus()
            else:
                super().action_scroll_up()

        def action_scroll_half_down(self) -> None:
            """Scroll down by half the visible viewport (vim ``Ctrl-D``)."""
            half = max(1, self.size.height // 2)
            self.scroll_relative(y=half, animate=True)

        def action_scroll_half_up(self) -> None:
            """Scroll up by half the visible viewport (vim ``Ctrl-U``)."""
            half = max(1, self.size.height // 2)
            self.scroll_relative(y=-half, animate=True)

        def watch_scroll_y(self, old: float, new: float) -> None:
            """Re-render the detail status line on scroll."""
            base = getattr(super(), "watch_scroll_y", None)
            if callable(base):
                base(old, new)
            self.post_message(
                DetailScrollChanged(
                    percent=scroll_percent(
                        float(new or 0),
                        float(getattr(self, "max_scroll_y", 0) or 0),
                    ),
                ),
            )

    class FilterInput(input_widget):  # ty: ignore[unsupported-base]
        """``Input`` subclass with debounced filter + cursor-or-focus arrows.

        The base ``Input.Changed`` event still fires immediately on each
        keystroke so the cursor, selection, and validation feedback stay
        instant. The expensive filter operation is deferred onto a
        :class:`FilterRequested` message which is only posted after 150 ms of
        typing inactivity, letting a worker run the actual filter without
        blocking the input itself.

        Up / down arrows are dual-purpose: when there's text in the input
        they jump the cursor to the start / end; when the input is empty (or
        the cursor is already at the relevant edge) they release focus to
        the previous / next widget so the user can navigate into the results
        table without reaching for Tab.
        """

        _DEBOUNCE_SECONDS: t.ClassVar[float] = 0.15

        BINDINGS: t.ClassVar[list[tuple[str, str, str]]] = [
            ("down", "release_down", "Results"),
        ]

        def __init__(
            self,
            *,
            placeholder: str = "",
            id: str | None = None,  # noqa: A002 -- forwarded to Textual's ``id`` kwarg
            suggester: object | None = None,
        ) -> None:
            super().__init__(placeholder=placeholder, id=id, suggester=suggester)
            self._debounce_timer: object | None = None

        def _watch_value(self, value: str) -> None:
            """Post normal ``Input.Changed`` and arm a debounced ``FilterRequested``."""
            super()._watch_value(value)
            if self._debounce_timer is not None:
                self._debounce_timer.stop()
            self._debounce_timer = self.set_timer(
                self._DEBOUNCE_SECONDS,
                lambda: self.post_message(
                    FilterRequested(payload=FilterRequestedPayload(text=value)),
                ),
            )

        async def _on_key(self, event: object) -> None:
            """Down/up route between cursor-jump and focus-release per spec."""
            key = str(getattr(event, "key", ""))
            cursor = int(getattr(self, "cursor_position", 0))
            value = str(getattr(self, "value", ""))
            stop = getattr(event, "stop", None)
            if key == "down":
                if value and cursor < len(value):
                    self.cursor_position = len(value)
                    if callable(stop):
                        stop()
                    return
                # Empty or at end — release focus to next widget (DataTable)
                if callable(stop):
                    stop()
                self.app.action_focus_next()
                return
            if key == "up":
                if value and cursor > 0:
                    self.cursor_position = 0
                    if callable(stop):
                        stop()
                    return
                # Empty or at start — release focus up to the top search bar
                # so plain ``up`` navigates filter → search without reaching
                # for Ctrl-K. Mirrors the symmetric ``down`` → results path.
                if callable(stop):
                    stop()
                with contextlib.suppress(Exception):
                    self.app.query_one("#search").focus()
                return
            if key == "right" and not value:
                # Empty filter → release focus rightward to the detail pane.
                # When the filter has text, fall through so the cursor can
                # walk through it character-by-character. Route through the
                # app's ``_focus_detail`` so a collapsed stacked pane is
                # revealed before focus lands.
                if callable(stop):
                    stop()
                with contextlib.suppress(Exception):
                    t.cast("t.Any", self.app)._focus_detail()
                return
            await super()._on_key(event)

        def action_release_down(self) -> None:
            """Footer-binding fallback (``_on_key`` handles the real release)."""
            self.app.action_focus_next()

    class SearchInput(input_widget):  # ty: ignore[unsupported-base]
        """``Input`` subclass that fires :class:`SearchRequested` on Enter.

        Keystrokes update the input text immediately so the cursor stays
        instant, but no backend search runs until the user presses
        Enter. This makes the search explicit (no surprise dispatches
        while typing) and gives the cancel-existing-search logic a
        clean trigger to hang off of — every Enter cancels the prior
        worker before spawning a fresh one.
        """

        BINDINGS: t.ClassVar[list[tuple[str, str, str]]] = [
            ("down", "release_down", "Filter"),
        ]

        def __init__(
            self,
            *,
            value: str = "",
            placeholder: str = "",
            id: str | None = None,  # noqa: A002 -- forwarded to Textual's ``id`` kwarg
            suggester: object | None = None,
        ) -> None:
            super().__init__(
                value=value,
                placeholder=placeholder,
                id=id,
                suggester=suggester,
            )

        def on_input_submitted(self, event: object) -> None:
            """Enter pressed — dispatch a :class:`SearchRequested` for the current value."""
            stop = getattr(event, "stop", None)
            if callable(stop):
                stop()
            value = str(getattr(self, "value", ""))
            self.post_message(
                SearchRequested(payload=SearchRequestedPayload(text=value)),
            )

        async def _on_key(self, event: object) -> None:
            """``down`` releases focus to the filter; ``up`` is a no-op (top widget)."""
            key = str(getattr(event, "key", ""))
            cursor = int(getattr(self, "cursor_position", 0))
            value = str(getattr(self, "value", ""))
            stop = getattr(event, "stop", None)
            if key == "down":
                if value and cursor < len(value):
                    self.cursor_position = len(value)
                    if callable(stop):
                        stop()
                    return
                if callable(stop):
                    stop()
                self.app.action_focus_next()
                return
            if key == "up":
                if value and cursor > 0:
                    self.cursor_position = 0
                    if callable(stop):
                        stop()
                    return
                if callable(stop):
                    stop()
                return
            await super()._on_key(event)

        def action_release_down(self) -> None:
            """Footer-binding fallback (``_on_key`` handles the real release)."""
            self.app.action_focus_next()

    class AgentGrepApp(app_type):  # ty: ignore[unsupported-base]
        """Streaming read-only explorer for normalized search records."""

        CSS: t.ClassVar[str] = """
        Screen {
            layout: vertical;
        }
        #search {
            height: 3;
        }
        #body {
            height: 1fr;
        }
        #results-column {
            width: 1fr;
            layout: vertical;
        }
        #detail-column {
            width: 1fr;
            layout: vertical;
        }
        /* Narrow terminals stack the panes: results on top, detail below
           (tig moves its diff view to the bottom on narrow screens). The
           ``1fr``/``2fr`` units are per-axis, so the side-by-side width
           rules above still hold; only the body's layout axis flips. */
        #body.-stacked {
            layout: vertical;
        }
        #body.-stacked > #results-column {
            height: 2fr;
        }
        #body.-stacked > #detail-column {
            height: 1fr;
        }
        /* Stacked detail stays closed until the user selects a row. */
        #detail-column.-collapsed {
            display: none;
        }
        #filter {
            height: 3;
        }
        #detail-scroll {
            height: 1fr;
            overflow-y: auto;
            overflow-x: hidden;
            /* Reserve the border cell up-front (transparent) so toggling
               focus only repaints the perimeter — no layout shift, no
               extra padding when the border appears. Mirrors the
               OptionList default CSS pattern. */
            border: tall transparent;
        }
        #detail-scroll:focus {
            border: tall $border;
        }
        #detail {
            padding: 0 1 0 0;
        }
        #results {
            height: 1fr;
            overflow-x: hidden;
        }
        #results-statusline {
            height: 1;
            /* One cell from each edge: the spinner aligns with the
               input border, and the right slot never touches the
               detail column. */
            padding: 0 1;
            layout: horizontal;
        }
        #status-spinner {
            width: 2;
            color: $accent;
        }
        #status-text {
            width: auto;
            color: ansi_bright_cyan;
            text-style: bold;
        }
        #status-meter {
            width: 1fr;
            color: mediumpurple;
            margin: 0 1;
        }
        /* Post-search outcome colors: green mirrors the results list's
           "prompt" kind; gray mirrors the detail header's Path value
           (grey50). */
        #status-spinner.-done, #status-text.-done, #status-meter.-done {
            color: ansi_green;
        }
        #status-spinner.-stopped, #status-text.-stopped, #status-meter.-stopped {
            color: #808080;
        }
        #status-right {
            width: auto;
            color: $warning;
            text-style: bold;
        }
        #status-detail {
            height: 1;
            /* Statusline left padding (1) + spinner cell (2) so the
               detail text sits under "Searching". */
            padding: 0 1 0 3;
            color: #808080;
            display: none;
        }
        #status-detail.visible {
            display: block;
        }
        #detail-statusline {
            height: 1;
            padding: 0;
            color: #d8d8d8;
        }
        /* Keep Textual's OptionList default of "border appears only on focus"
           (textual/widgets/_option_list.py:154 — ``border: tall $border``).
           We only cancel the two parts of that focus rule that fight our
           per-span semantic colors: the ``$foreground 5%`` background-tint
           and the bright ``$block-cursor-*`` cursor-row recolor. */
        #results:focus {
            background-tint: $foreground 0%;
        }
        #results:focus > .option-list--option-highlighted {
            color: $block-cursor-blurred-foreground;
            background: $block-cursor-blurred-background;
            text-style: $block-cursor-blurred-text-style;
        }
        """
        # ``priority=True`` on the directional ``ctrl+hjkl`` bindings pushes
        # them into Textual's priority dispatch lane so they win over any
        # widget binding for the same key (e.g. ``Input``'s readline
        # ``ctrl+k`` = kill-to-end-of-line). Trade-off accepted per user
        # request: filter loses ``ctrl+k``; ``ctrl+u`` and ``ctrl+w`` are
        # untouched and remain readline-compatible.
        BINDINGS: t.ClassVar[list[t.Any]] = [
            ("tab", "focus_next", "Switch focus"),
            ("q", "quit", "Quit"),
            ("escape", "stop_search", "Stop search"),
            ("ctrl+backslash", "toggle_detail_progress", "Detail"),
            ("ctrl+c", "smart_quit", "Stop / Quit"),
            binding_type("ctrl+h", "focus_pane_left", "← Pane", priority=True),
            binding_type("ctrl+j", "focus_pane_down", "↓ Pane", priority=True),
            binding_type("ctrl+k", "focus_pane_up", "↑ Pane", priority=True),
            binding_type("ctrl+l", "focus_pane_right", "→ Pane", priority=True),
            # Terminal-alias fallback: many terminals (and tmux without
            # ``xterm-keys on``) send 0x08 for both Backspace and Ctrl-H, so
            # Textual sees ``key="backspace"``, never ``ctrl+h``. NO priority
            # here — the filter input's own backspace handler (delete prev
            # char) must keep winning inside the input. In panes nothing
            # else binds backspace, so this fires.
            binding_type("backspace", "focus_pane_left", "", show=False),
        ]
        all_records: list[SearchRecord]
        filtered_records: list[SearchRecord]

        _DETAIL_CACHE_MAX: t.ClassVar[int] = 1024

        # Statusline width (cells) below which the meter bar and the
        # elapsed "(32s)" suffix are dropped — percent and match count
        # keep their cells on small terminals.
        _NARROW_BREAKPOINT: t.ClassVar[int] = 50

        # Body width (cells) below which the detail pane moves from the
        # right (side-by-side) to the bottom (stacked) — each side wants
        # ~50 cells to stay readable. Distinct from the statusline
        # breakpoint above, which measures the results column alone.
        _SPLIT_BREAKPOINT: t.ClassVar[int] = 100

        def __init__(
            self,
            *,
            home: pathlib.Path,
            query: SearchQuery,
            control: SearchControl,
            initial_search_text: str | None = None,
        ) -> None:
            super().__init__()
            self.home = home
            self.query = query
            self.control = control
            self._runtime = SearchRuntime.with_source_scan_cache()
            self.initial_search_text: str | None = initial_search_text
            self.all_records = []
            self.filtered_records = []
            self._filter_text = ""
            self._progress: StreamingSearchProgress | None = None
            self._search_done = False
            self._started_at: float | None = None
            self._last_snapshot: ProgressSnapshot | None = None
            self._results: SearchResultsList | None = None
            self._detail: StaticLike | None = None
            self._status_widget: StaticLike | None = None
            self._matches_widget: StaticLike | None = None
            self._spinner_widget: SpinnerWidget | None = None
            self._meter_widget: MeterWidget | None = None
            self._detail_row: StaticLike | None = None
            self._statusline_container: t.Any = None
            self._elapsed_timer: object | None = None
            self._chrome_generation: int = 0
            self._last_left_text: str = ""
            self._last_detail_text: str = ""
            self._last_right_text: str = ""
            self._finished_status: tuple[str, str] | None = None
            self._detail_visible: bool = False
            self._detail_statusline: StaticLike | None = None
            self._filter_input: FilterInput | None = None
            self._search_input: SearchInput | None = None
            # Inline-completion suggesters: the query suggester is static
            # (registry-backed); the filter suggester's vocabulary refreshes
            # from loaded records as the search finishes.
            self._query_suggester = QuerySuggester(default_registry())
            self._filter_suggester = FilterSuggester([])
            self._filter_vocabulary: set[str] = set()
            self._resize_debounce_timer: object | None = None
            self._current_detail_record: SearchRecord | None = None
            self._detail_scroll: t.Any = None
            self._body: t.Any = None
            self._detail_column: t.Any = None
            # Responsive split: True when the detail pane is stacked
            # below the results rather than beside them. ``_detail_opened``
            # is the tig-style "user selected a row" gate that reveals the
            # stacked detail; programmatic highlights caused by filter-list
            # patching (``_pending_autohighlights``) must not trip it.
            self._stacked: bool = False
            self._detail_opened: bool = False
            self._pending_autohighlights: int = 0
            # LRU caches for detail-pane work. Keyed by
            # ``(id(record), query.terms, case_sensitive, regex)`` — the
            # tuple of attributes that determines the rendered body and
            # the highlighted match line. Bounded so a long browsing
            # session can't grow them without limit.
            self._detail_body_cache: collections.OrderedDict[
                tuple[int, tuple[str, ...], bool, bool],
                tuple[object, str],
            ] = collections.OrderedDict()
            self._first_match_cache: collections.OrderedDict[
                tuple[int, tuple[str, ...], bool, bool],
                int | None,
            ] = collections.OrderedDict()

        def _get_start_time(self) -> float | None:
            return self._started_at

        def compose(self) -> cabc.Iterator[object]:
            """Build the widget tree (header → search → body[results-col, detail-col] → footer).

            The results column carries its live chrome (spinner + status
            + match count + scroll %) as a header above the filter and
            list, so the running search state sits next to the search
            input that drives it. The detail column keeps its status
            line at the bottom — record path + scroll % is contextual to
            whatever's currently being read, so the natural place to
            glance is the foot of the pane.
            """
            yield header()
            if self.initial_search_text is not None:
                initial_search = self.initial_search_text
            else:
                initial_search = " ".join(self.query.terms) if self.query.terms else ""
            yield SearchInput(
                value=initial_search,
                placeholder="Search prompts",
                id="search",
                suggester=self._query_suggester,
            )
            # Decide the responsive split up-front (terminal width is known
            # at compose time) so narrow terminals are born stacked with the
            # detail collapsed — applying the class in on_mount instead would
            # paint the detail once and then hide it, a visible flicker.
            stacked = 0 < self.size.width < self._SPLIT_BREAKPOINT
            body_classes = "-stacked" if stacked else ""
            detail_classes = "-collapsed" if stacked else ""
            with horizontal(id="body", classes=body_classes):
                with vertical(id="results-column"):
                    with horizontal(id="results-statusline"):
                        yield SpinnerWidget(id="status-spinner")
                        yield static_type("", id="status-text")
                        yield MeterWidget(id="status-meter")
                        yield static_type("", id="status-right")
                    yield static_type("", id="status-detail")
                    yield FilterInput(
                        placeholder="Filter loaded results",
                        id="filter",
                        suggester=self._filter_suggester,
                    )
                    yield SearchResultsList(id="results")
                with vertical(id="detail-column", classes=detail_classes):
                    with DetailScroll(id="detail-scroll"):
                        yield static_type("", id="detail")
                    yield static_type("", id="detail-statusline")
            yield footer()

        def on_mount(self) -> None:
            """Cache widget references, start the worker, and seed the chrome."""
            streaming = t.cast("StreamingAppLike", t.cast("object", self))
            self._results = t.cast(
                "SearchResultsList",
                streaming.query_one("#results"),
            )
            self._detail = t.cast(
                "StaticLike",
                streaming.query_one("#detail", static_type),
            )
            self._detail_scroll = streaming.query_one("#detail-scroll")
            self._body = streaming.query_one("#body")
            self._detail_column = streaming.query_one("#detail-column")
            self._status_widget = t.cast(
                "StaticLike",
                streaming.query_one("#status-text", static_type),
            )
            self._matches_widget = t.cast(
                "StaticLike",
                streaming.query_one("#status-right", static_type),
            )
            self._spinner_widget = t.cast(
                "SpinnerWidget",
                streaming.query_one("#status-spinner"),
            )
            self._meter_widget = t.cast(
                "MeterWidget",
                streaming.query_one("#status-meter"),
            )
            self._detail_row = t.cast(
                "StaticLike",
                streaming.query_one("#status-detail", static_type),
            )
            self._statusline_container = streaming.query_one("#results-statusline")
            self._detail_statusline = t.cast(
                "StaticLike",
                streaming.query_one("#detail-statusline", static_type),
            )
            self._filter_input = t.cast(
                "FilterInput",
                streaming.query_one("#filter"),
            )
            self._search_input = t.cast(
                "SearchInput",
                streaming.query_one("#search"),
            )
            # Steady (non-blinking) input cursors. A blinking cursor keeps
            # toggling its inverted-block glyph even when the terminal loses
            # focus — Textual can't tell the tmux pane went inactive without
            # focus-events — so the cursor flickers in the background pane.
            for _input in (self._filter_input, self._search_input):
                t.cast("t.Any", _input).cursor_blink = False
            self._progress = self._make_gated_progress()
            self._apply_responsive_layout()
            if self.query.terms:
                self._start_search_worker(self.query)
                self._filter_input.focus()
            else:
                # No initial query — leave the chrome idle and land focus on
                # the search bar so the user can start typing immediately.
                self._search_done = True
                if self._status_widget is not None:
                    self._status_widget.update("Press Enter to search")
                if self._spinner_widget is not None:
                    self._spinner_widget.freeze(" ")
                self._search_input.focus()

        def _start_search_worker(self, query: SearchQuery) -> None:
            """Reset chrome and spawn a new search worker for ``query``.

            ``exclusive=True`` with ``group="search"`` makes Textual cancel
            any prior in-flight search worker before this one runs, which
            is the canonical Textual pattern for "fire a backend search on
            every debounced keystroke without piling up cancellations."
            """
            self.query = query
            self._reset_search_chrome()
            streaming = t.cast("StreamingAppLike", t.cast("object", self))
            streaming.run_worker(
                self._run_search,
                name="search",
                group="search",
                thread=True,
                exclusive=True,
            )

        def _reset_search_chrome(self) -> None:
            """Wipe per-search state and chrome before a fresh search starts.

            Swap ``self.control`` for a fresh :class:`SearchControl`
            instead of resetting the existing one — any worker thread
            still holding the previous reference will continue to see
            its cancel flag set (signaled by ``on_search_requested``
            before this call) and bail out cooperatively, while the
            new worker starts with a clean slate.
            """
            self.control = SearchControl()
            clear_haystack_cache()
            self._detail_body_cache.clear()
            self._first_match_cache.clear()
            self.all_records = []
            self.filtered_records = []
            self._search_done = False
            self._started_at = None
            self._last_snapshot = None
            self._current_detail_record = None
            # A fresh search re-collapses the stacked detail pane until
            # the user selects a row again.
            self._detail_opened = False
            self._pending_autohighlights = 0
            if self._results is not None:
                self._results.set_records([])
            self._apply_responsive_layout()
            if self._detail is not None:
                self._detail.update("")
            if self._matches_widget is not None:
                self._matches_widget.update("")
            if self._detail_statusline is not None:
                self._detail_statusline.update("")
            self._stop_elapsed_timer()
            self._last_left_text = ""
            self._last_detail_text = ""
            self._last_right_text = ""
            self._finished_status = None
            if self._status_widget is not None:
                self._status_widget.update(
                    searching_left_text(0.0, narrow=self._statusline_narrow()),
                )
            if self._spinner_widget is not None:
                self._spinner_widget.unfreeze()
                self._set_outcome_classes(self._spinner_widget, "")
            if self._status_widget is not None:
                self._set_outcome_classes(self._status_widget, "")
            if self._meter_widget is not None:
                self._meter_widget.reset()
            # ``_detail_visible`` is deliberately NOT reset — the Ctrl-\
            # toggle is sticky for the session; only the row's stale
            # content is wiped.
            if self._detail_row is not None:
                self._detail_row.update("")
            self._progress = self._make_gated_progress()

        def _make_gated_progress(self) -> StreamingSearchProgress:
            """Build a progress reporter whose events die with its generation.

            ``call_from_thread`` schedules the callback directly on the
            event loop rather than enqueuing a ``Message`` — so
            high-frequency record batches don't compete with keystroke /
            timer events for FIFO message dispatch. This is the canonical
            Textual pattern for "many small updates from a worker thread."

            Each reporter captures the chrome generation current at its
            creation. A cancelled worker keeps emitting through its old
            reporter while it drains; :meth:`_apply_streaming_event`
            re-checks the generation on the main thread, so those events
            can never repaint the new search's chrome (stale "Stopped"
            states, old bar fills) no matter when they were queued.
            """
            self._chrome_generation += 1
            generation = self._chrome_generation
            streaming = t.cast("t.Any", self)

            def emit(event: object) -> None:
                # Runs on the worker thread; the generation check happens
                # on the main thread inside _apply_streaming_event.
                streaming.call_from_thread(
                    self._apply_streaming_event,
                    generation,
                    event,
                )

            return StreamingSearchProgress(emit=emit)

        async def _apply_streaming_event(self, generation: int, event: object) -> None:
            """Route one worker event to the chrome, dropping stale generations.

            Async because the records handler chunk-yields to the event
            loop; ``call_from_thread`` awaits coroutine results.
            """
            if generation != self._chrome_generation:
                return
            if isinstance(event, StreamingRecordsBatch):
                await self._apply_records_batch(event.records, event.total)
            elif isinstance(event, ProgressSnapshot):
                self._apply_progress(event)
            elif isinstance(event, StreamingSearchFinished):
                self._apply_finished(
                    event.outcome,
                    event.total,
                    event.elapsed,
                    str(event.error) if event.error else None,
                )

        def _run_search(self) -> None:
            progress = self._progress
            if progress is None:
                return
            try:
                run_search_query(
                    self.home,
                    self.query,
                    progress=progress,
                    control=self.control,
                    runtime=self._runtime,
                )
            except BaseException as exc:
                streaming = t.cast("StreamingAppLike", t.cast("object", self))
                streaming.call_from_thread(
                    self._apply_finished,
                    "error",
                    len(self.all_records),
                    0.0,
                    str(exc),
                )

        def on_search_requested(self, message: SearchRequested) -> None:
            """User changed the top search input; relaunch the backend search.

            Treats whitespace-only / empty input as "no search" and just
            resets the UI to an idle state without spawning a worker.
            """
            text = message.payload.text.strip()
            new_query = self._build_search_query(text)
            self.control.request_answer_now()
            if not text:
                self._reset_search_chrome()
                self._search_done = True
                if self._status_widget is not None:
                    self._status_widget.update("Press Enter to search")
                if self._spinner_widget is not None:
                    self._spinner_widget.freeze(" ")
                self.query = new_query
                return
            self._start_search_worker(new_query)

        def _build_search_query(self, text: str) -> SearchQuery:
            """Build a fresh :class:`SearchQuery` from the search-bar text.

            Routes through :func:`agentgrep.query.build_query_from_input`
            so the search bar accepts the same Lucene-style field
            predicates (`agent:codex`, `(agent:codex OR agent:cursor)`)
            as the one-shot CLI. On parse / compile failure the helper
            returns an error and we fall back to the legacy bare-term
            split so the user can keep typing — a future commit can
            surface the error in a status line.
            """
            from agentgrep.query import build_query_from_input, default_registry

            result = build_query_from_input(text, self.query, default_registry())
            if result.query is not None:
                return result.query
            # Parse / compile error: degrade to legacy split so the
            # search box stays editable. The error message stays
            # accessible on the result for future UI surfacing.
            terms = tuple(text.split()) if text else ()
            return SearchQuery(
                terms=terms,
                scope=self.query.scope,
                any_term=self.query.any_term,
                regex=self.query.regex,
                case_sensitive=self.query.case_sensitive,
                agents=self.query.agents,
                limit=self.query.limit,
                dedupe=self.query.dedupe,
            )

        _APPLY_CHUNK_SIZE: t.ClassVar[int] = 200
        _FILTER_VOCAB_CAP: t.ClassVar[int] = 4000

        def _extend_filter_vocabulary(
            self,
            records: cabc.Sequence[SearchRecord],
        ) -> None:
            """Grow the filter-box completion vocabulary from record text.

            Bounded by :attr:`_FILTER_VOCAB_CAP` so a long streaming search
            can't grow it without limit; once full, later batches are
            ignored. Surrounding punctuation is stripped and very short or
            non-word tokens are skipped to keep completions useful.
            """
            if len(self._filter_vocabulary) >= self._FILTER_VOCAB_CAP:
                return
            changed = False
            for record in records:
                for token in record.text.split():
                    word = token.strip("\"'`.,;:!?()[]{}<>*|=#")
                    if len(word) < 3 or not word[:1].isalnum():
                        continue
                    if word not in self._filter_vocabulary:
                        self._filter_vocabulary.add(word)
                        changed = True
                        if len(self._filter_vocabulary) >= self._FILTER_VOCAB_CAP:
                            break
                if len(self._filter_vocabulary) >= self._FILTER_VOCAB_CAP:
                    break
            if changed:
                self._filter_suggester.set_vocabulary(self._filter_vocabulary)

        async def _apply_records_batch(
            self,
            records: cabc.Sequence[SearchRecord],
            total: int,
        ) -> None:
            """Append a streaming records batch — invoked via ``call_from_thread``.

            Runs as a coroutine so the chunked loop can yield to the event
            loop between each ``_APPLY_CHUNK_SIZE`` slice. ``call_from_thread``
            blocks the worker for the full duration of this coroutine, which
            gives natural backpressure (the worker can't queue up batches
            faster than the UI can apply them) while ``await asyncio.sleep(0)``
            gives the event loop a chance to process keystrokes, timers, and
            renders between chunks — so a 5000-record batch can't freeze the
            UI for the duration of a single apply.
            """
            self.all_records.extend(records)
            self._extend_filter_vocabulary(records)
            matching = [record for record in records if self._matches_filter(record)]
            if matching and self._results is not None:
                results = self._results
                chunk_size = self._APPLY_CHUNK_SIZE
                for start in range(0, len(matching), chunk_size):
                    chunk = matching[start : start + chunk_size]
                    results.append_records(chunk)
                    self.filtered_records.extend(chunk)
                    if start + chunk_size < len(matching):
                        await asyncio.sleep(0)
            self._refresh_results_status_right()

        def _apply_progress(self, snapshot: ProgressSnapshot) -> None:
            """Feed the meter and detail row — invoked via ``call_from_thread``.

            The left status text is owned by the 1 Hz elapsed ticker, not
            this handler: per-source progress events arrive thousands of
            times per search and would otherwise repaint identical text.
            Both the meter (internally) and the detail row (here) gate on
            content change for the same reason. Stale-generation events
            never reach this handler — :meth:`_apply_streaming_event`
            drops them.
            """
            self._last_snapshot = snapshot
            if self._started_at is None:
                self._started_at = time.monotonic()
            if self._elapsed_timer is None:
                self._elapsed_timer = self.set_interval(1.0, self._tick_elapsed)
                # Paint "(0s)" immediately rather than after the first tick.
                self._tick_elapsed()
            if (
                snapshot.phase == "scanning"
                and snapshot.current is not None
                and snapshot.total is not None
                and snapshot.total > 0
            ):
                # Only the scanning phase counts sources; planning emits
                # plan-group counts whose fraction would make the bar
                # jump to a small value and snap back to zero.
                fraction: float | None = snapshot.current / snapshot.total
            else:
                fraction = None
            if self._meter_widget is not None:
                self._meter_widget.set_narrow(self._statusline_narrow())
                self._meter_widget.set_progress(fraction, snapshot.phase)
            if self._detail_visible and self._detail_row is not None:
                detail = format_scanning_detail(
                    snapshot.phase,
                    snapshot.current,
                    snapshot.total,
                    snapshot.detail,
                )
                if detail != self._last_detail_text:
                    self._last_detail_text = detail
                    self._detail_row.update(detail)
            if self._statusline_narrow():
                # Narrow right slots carry the search percent; record
                # batches alone would let it go stale on sparse matches.
                # The refresh is change-gated, so this stays cheap.
                self._refresh_results_status_right()

        def _statusline_narrow(self) -> bool:
            """Report whether the statusline is too narrow for bar + elapsed."""
            container = self._statusline_container
            if container is None:
                return False
            width = int(getattr(container.size, "width", 0) or 0)
            return 0 < width < self._NARROW_BREAKPOINT

        def _apply_responsive_layout(self) -> None:
            """Flip the detail pane between right (wide) and bottom (narrow).

            Below :data:`_SPLIT_BREAKPOINT` cells the body stacks the panes
            (results on top, detail below) and the detail stays collapsed
            until the user selects a row — matching tig, which moves its
            diff view to the bottom on narrow screens and opens it on
            selection. Wide statuslines keep the detail on the right and
            always visible. Idempotent and cheap: only touches a class
            when the target state changes.
            """
            if self._body is None or self._detail_column is None:
                return
            # Use the app (terminal) width, not ``_body.size`` — the body
            # hasn't been laid out yet at on_mount, so its width reads 0
            # and the detail would flash visible before the first resize
            # collapsed it. ``self.size`` is known from the driver at mount.
            width = int(getattr(self.size, "width", 0) or 0)
            stacked = 0 < width < self._SPLIT_BREAKPOINT
            self._stacked = stacked
            body = t.cast("t.Any", self._body)
            body.set_class(stacked, "-stacked")
            # ``_detail_opened`` is the single source of truth for "the user
            # wants the detail visible": stacked collapses it until the user
            # selects a row or focuses the pane (the auto row-0 highlight
            # never counts). Wide always shows it. Coupling this to
            # ``filtered_records`` left an explicit focus on an empty result
            # set stranded in a hidden pane.
            collapsed = stacked and not self._detail_opened
            t.cast("t.Any", self._detail_column).set_class(collapsed, "-collapsed")

        def _tick_elapsed(self) -> None:
            """Repaint the left status text from wall-clock elapsed (1 Hz).

            Uses ``time.monotonic() - self._started_at`` rather than
            ``ProgressSnapshot.elapsed`` — the snapshot field only advances
            when the engine emits an event, so one slow source would
            freeze the displayed time.
            """
            if self._search_done or self._status_widget is None or self._started_at is None:
                return
            elapsed = time.monotonic() - self._started_at
            left = searching_left_text(elapsed, narrow=self._statusline_narrow())
            if left != self._last_left_text:
                self._last_left_text = left
                self._status_widget.update(left)

        def _stop_elapsed_timer(self) -> None:
            """Stop and drop the elapsed ticker (idempotent)."""
            if self._elapsed_timer is not None:
                t.cast("t.Any", self._elapsed_timer).stop()
                self._elapsed_timer = None

        def action_toggle_detail_progress(self) -> None:
            r"""``Ctrl-\``: show/hide the verbose scanning detail row (sticky)."""
            self._detail_visible = not self._detail_visible
            if self._detail_row is None:
                return
            row = t.cast("t.Any", self._detail_row)
            if self._detail_visible:
                row.add_class("visible")
                # Populate immediately: a finished search shows its data
                # summary, a running one the latest scanning snapshot.
                detail: str | None = None
                if self._finished_status is not None:
                    detail = self._finished_status[1]
                elif self._last_snapshot is not None:
                    snap = self._last_snapshot
                    detail = format_scanning_detail(
                        snap.phase,
                        snap.current,
                        snap.total,
                        snap.detail,
                    )
                if detail is not None:
                    self._last_detail_text = detail
                    self._detail_row.update(detail)
            else:
                row.remove_class("visible")

        def _apply_finished(
            self,
            outcome: str,
            total: int,
            elapsed: float,
            error_message: str | None,
        ) -> None:
            """Freeze chrome widgets — invoked via ``call_from_thread``.

            Elapsed time is folded into the final status string rather than
            shown as a live-ticking sibling widget. The status line no
            longer claims animation budget once a search is done.
            """
            self._search_done = True
            self._stop_elapsed_timer()
            glyphs = {"complete": "✓", "interrupted": "■", "error": "✗"}
            if self._spinner_widget is not None:
                self._spinner_widget.freeze(glyphs.get(outcome, "·"))
                self._set_outcome_classes(self._spinner_widget, outcome)
            if self._meter_widget is not None:
                self._meter_widget.freeze(outcome)
            if self._status_widget is not None:
                self._set_outcome_classes(self._status_widget, outcome)
            if outcome == "error":
                summary = f"Search failed: {error_message}"
            elif outcome == "interrupted":
                summary = (
                    f"Stopped at {format_match_count(total)} "
                    f"across {self._sources_label()} sources in {elapsed:.1f}s"
                )
            else:
                summary = f"Search complete: {format_match_count(total)} in {elapsed:.1f}s"
            self._finished_status = (outcome, summary)
            # The data summary lives in the ctrl+\ row, not the statusline.
            self._last_detail_text = summary
            if self._detail_visible and self._detail_row is not None:
                self._detail_row.update(summary)
            self._render_finished_status()
            # Recompute the right slot: narrow mode swaps the in-flight
            # search percent for the plain match count once the search ends.
            self._refresh_results_status_right()

        @staticmethod
        def _set_outcome_classes(widget: object, outcome: str) -> None:
            """Apply the post-search ``-done`` / ``-stopped`` color class."""
            classes = {"complete": "-done", "interrupted": "-stopped"}
            target = t.cast("t.Any", widget)
            target.remove_class("-done", "-stopped")
            outcome_class = classes.get(outcome)
            if outcome_class is not None:
                target.add_class(outcome_class)

        def _render_finished_status(self) -> None:
            """Paint the post-search left text — the frozen bar is the summary.

            Wide statuslines show no text at all (the colored bar and the
            right slot carry the outcome); narrow ones, with no room for
            a bar, say ``Done`` or ``Stopped``. The word also stands in
            when the meter has no bar to carry the outcome — an interrupt
            before the first scanning snapshot freezes with no fraction —
            so a stopped search never collapses to a bare glyph.
            Failures keep their message at every width — that information
            has no other home. The full data summary renders in the
            toggleable detail row.
            """
            if self._status_widget is None or self._finished_status is None:
                return
            outcome, summary = self._finished_status
            meter_has_bar = self._meter_widget is not None and self._meter_widget.shows_bar()
            if outcome == "error":
                text = summary
            elif self._statusline_narrow() or not meter_has_bar:
                text = "Done" if outcome == "complete" else "Stopped"
            else:
                text = ""
            self._status_widget.update(text)

        def _sources_label(self) -> str:
            snap = self._last_snapshot
            if snap is None or snap.current is None or snap.total is None:
                return "?"
            return f"{snap.current}/{snap.total}"

        def on_filter_requested(self, message: FilterRequested) -> None:
            """Spawn a worker to recompute the filter; exclusive cancels any in-flight one."""
            text = message.payload.text
            self._filter_text = text.strip().casefold()
            streaming = t.cast("StreamingAppLike", t.cast("object", self))
            streaming.run_worker(
                lambda captured_text=text: self._run_filter_worker(captured_text),
                name="filter",
                group="filter",
                thread=True,
                exclusive=True,
            )

        def _run_filter_worker(self, text: str) -> None:
            """Compute the filtered list on a background thread; post a ``FilterCompleted``.

            Runs in a worker thread; safe to scan ``self.all_records`` since
            list reads under CPython are GIL-protected. The main thread guards
            against stale results by comparing the captured text against the
            current input value in :meth:`on_filter_completed`.
            """
            matching = compute_filter_matches(self.all_records, text)
            streaming = t.cast("StreamingAppLike", t.cast("object", self))
            streaming.post_message(
                FilterCompleted(
                    payload=FilterCompletedPayload(text=text, matching=matching),
                ),
            )

        def on_filter_completed(self, message: FilterCompleted) -> None:
            """Apply the worker's filter result if it matches the current input.

            Skips :meth:`show_detail` when the top filtered record is already
            the one being displayed — detail rendering (Rich Text header,
            JSON/Markdown body, scroll-to-match) is one of the heavier
            main-thread units per filter pass.
            """
            payload = message.payload
            if self._filter_input is not None and payload.text != self._filter_input.value:
                return
            self.filtered_records = list(payload.matching)
            if self._results is not None:
                # Only suppress the programmatic highlights Textual actually
                # queued while patching the list. Non-empty filter results do
                # not guarantee a highlight message will be emitted.
                self._pending_autohighlights = self._results.set_records(payload.matching)
            if self._detail is not None:
                if self.filtered_records:
                    top = self.filtered_records[0]
                    if top is not self._current_detail_record:
                        self.show_detail(top)
                else:
                    self._detail.update(
                        "No results." if self._search_done else "No matches yet.",
                    )
            # Empty results collapse the stacked detail; a populated list
            # keeps whatever open state the user already chose.
            self._apply_responsive_layout()

        def on_option_list_option_highlighted(self, event: object) -> None:
            """Update the detail pane and footer on OptionList cursor move.

            Guards against the redundant re-render that fires when
            ``set_records`` rebuilds the list and Textual re-emits the
            highlight for the same row that's already in the detail pane.
            """
            option_index = getattr(event, "option_index", None)
            if option_index is None:
                self._refresh_results_status_right()
                return
            row_index = int(option_index)
            if self._pending_autohighlights > 0:
                # A programmatic highlight after a filter pass — update
                # content (so the wide pane stays populated) but don't treat
                # it as the user opening the stacked detail.
                self._pending_autohighlights -= 1
            else:
                # A genuine cursor move: open the stacked detail pane and
                # keep it open for the rest of this result set (tig-style).
                self._detail_opened = True
                self._apply_responsive_layout()
            if 0 <= row_index < len(self.filtered_records):
                record = self.filtered_records[row_index]
                if record is not self._current_detail_record:
                    self.show_detail(record)
            self._refresh_results_status_right(
                cursor=row_index,
                visible=len(self.filtered_records),
            )

        def on_results_scroll_changed(self, message: ResultsScrollChanged) -> None:
            """Re-render the right side of the results status line.

            ``message.percent`` is deliberately unused — the results
            list's scrollbar already shows the scroll position, so the
            right slot doesn't restate it.
            """
            self._refresh_results_status_right(
                cursor=message.cursor,
                visible=message.total,
            )

        def on_detail_scroll_changed(self, message: DetailScrollChanged) -> None:
            """Re-render the detail status line on detail-pane scroll."""
            self._refresh_detail_statusline(message.percent)

        def _refresh_results_status_right(
            self,
            *,
            cursor: int | None = None,
            visible: int | None = None,
        ) -> None:
            """Compose the results-status right slot from the most recent state.

            Pulls the cursor position from the results list when no
            explicit values arrive; the change gate keeps repeated
            identical renders from repainting.
            """
            if self._matches_widget is None:
                return
            if cursor is None and visible is None and self._results is not None:
                cursor = t.cast("int | None", getattr(self._results, "highlighted", None))
                visible = len(self._results._records)
            text = self._format_results_right(cursor, visible)
            if text != self._last_right_text:
                self._last_right_text = text
                self._matches_widget.update(text)

        def _format_results_right(
            self,
            cursor: int | None,
            visible: int | None,
        ) -> str:
            """Render the right slot, one count at a time (tig style, thrifty).

            Wide statuslines show ``{cursor+1}/{visible}`` once a cursor
            exists — the denominator already carries the count — and the
            bare match count before that. Narrow statuslines show the
            match count plus the search-completion percent while a search
            runs (the meter bar doesn't fit there), then just the count.
            """
            total_matches = len(self.all_records)
            parts: list[str] = []
            if not self._statusline_narrow():
                if visible and visible > 0 and cursor is not None:
                    parts.append(f"{cursor + 1}/{visible}")
                elif total_matches > 0:
                    parts.append(format_match_count(total_matches))
            else:
                if total_matches > 0:
                    parts.append(format_match_count(total_matches))
                if not self._search_done:
                    search_percent = self._search_progress_percent()
                    if search_percent is not None:
                        parts.append(search_percent)
            return "  ".join(parts)

        def _search_progress_percent(self) -> str | None:
            """Return the search-completion percent from the latest snapshot.

            Scanning-phase only — planning emits plan-group counts whose
            fraction doesn't describe source progress.
            """
            snap = self._last_snapshot
            if (
                snap is None
                or snap.phase != "scanning"
                or snap.current is None
                or snap.total is None
                or snap.total <= 0
            ):
                return None
            return format_progress_percent(snap.current / snap.total)

        def _refresh_detail_statusline(self, percent: int | None = None) -> None:
            """Update the detail status line with the current record path and scroll %."""
            if self._detail_statusline is None:
                return
            record = self._current_detail_record
            if record is None:
                self._detail_statusline.update("")
                return
            pct = percent if percent is not None else self._current_detail_scroll_percent()
            width = max(20, int(getattr(self._detail_statusline.size, "width", 80)))
            path_text = format_compact_path(record.path, max_width=max(10, width - 6))
            pad = max(1, width - len(path_text) - len(f"{pct}%"))
            self._detail_statusline.update(f"{path_text}{' ' * pad}{pct}%")

        def _current_detail_scroll_percent(self) -> int:
            """Compute the detail pane's scroll percent on demand."""
            if self._detail_scroll is None:
                return 100
            scroll = self._detail_scroll
            return scroll_percent(
                float(getattr(scroll, "scroll_y", 0) or 0),
                float(getattr(scroll, "max_scroll_y", 0) or 0),
            )

        # Constant — keep in sync with the label list in ``show_detail`` below.
        # 7 label rows (Agent / Kind / Store / Adapter / Timestamp / Model / Path)
        # plus 1 blank separator = 8 lines of header before the body starts.
        _DETAIL_HEADER_LINES: t.ClassVar[int] = 8

        def show_detail(self, record: SearchRecord) -> None:
            """Render ``record`` with colored labels + format-aware body + scroll-to-match.

            The body is truncated to :data:`DETAIL_BODY_MAX_LINES` lines (the
            ``VerticalScroll`` wrapper handles letting the user scroll within
            the visible window). The body renderable is chosen by
            :func:`detect_content_format`:

            * JSON bodies are pretty-printed and rendered via
              :class:`rich.syntax.Syntax` with ``ansi_dark`` theming.
            * Markdown bodies render via :class:`rich.markdown.Markdown`.
            * Everything else keeps the existing ``Text`` + ``highlight_regex``
              flow so search-term matches stay bold-yellow.

            If any current query term occurs in the body the pane is scrolled
            so that line lands vertically centered in the viewport (line index
            is recomputed against the formatted body for JSON so the jump is
            still accurate).
            """
            if self._detail is None:
                return
            self._current_detail_record = record
            width = max(20, self._detail.size.width or 80)
            agent_color = SearchResultsList._AGENT_COLORS.get(record.agent or "", "")
            kind_color = SearchResultsList._KIND_COLORS.get(record.kind or "", "")
            header = rich_text.Text(no_wrap=False)
            for label, value, value_style in (
                ("Agent:", record.agent or "", agent_color),
                ("Kind:", record.kind or "", kind_color),
                ("Store:", record.store or "", "dim"),
                ("Adapter:", record.adapter_id or "", "dim"),
                ("Timestamp:", record.timestamp or "unknown", "dim"),
                ("Model:", record.model or "unknown", "magenta"),
                (
                    "Path:",
                    format_compact_path(record.path, max_width=width - 8),
                    "grey50",
                ),
            ):
                header.append(f"{label} ", style="bold")
                header.append(f"{value}\n", style=value_style)
            header.append("\n")
            body_truncated = truncate_lines(record.text, DETAIL_BODY_MAX_LINES)
            query_terms = list(self.query.terms)
            body_renderable, body_for_scroll = self._build_detail_body(
                body_truncated,
                query_terms,
            )
            self._detail.update(
                _RichGroup(header, t.cast("t.Any", body_renderable)),
            )
            self._scroll_detail_to_first_match(body_for_scroll, query_terms)
            self._refresh_detail_statusline()

        def _detail_cache_key(
            self,
            query_terms: cabc.Sequence[str],
        ) -> tuple[int, tuple[str, ...], bool, bool] | None:
            """Compose the LRU key for the current record + query.

            Returns ``None`` when there is no current record (e.g. detail
            pane invoked before a record is highlighted) so callers know
            to skip the cache entirely.
            """
            record = self._current_detail_record
            if record is None:
                return None
            return (
                id(record),
                tuple(query_terms),
                self.query.case_sensitive,
                self.query.regex,
            )

        def _build_detail_body(
            self,
            body_text: str,
            query_terms: cabc.Sequence[str],
        ) -> tuple[object, str]:
            """Return ``(renderable, body_text_for_match_search)`` for ``body_text``.

            The second tuple element is whatever text the caller's
            ``find_first_match_line`` should scan. For JSON we pretty-print
            and return the formatted text so the line index lines up with
            what the user actually sees rendered. Result is memoized per
            ``(record, query)`` so scrolling back to a previously-viewed
            record never re-parses the JSON body.
            """
            cache_key = self._detail_cache_key(query_terms)
            if cache_key is not None:
                cached = self._detail_body_cache.get(cache_key)
                if cached is not None:
                    self._detail_body_cache.move_to_end(cache_key)
                    return cached
            fmt = detect_content_format(body_text)
            result: tuple[object, str]
            if fmt == "json":
                try:
                    formatted = json.dumps(
                        json.loads(body_text),
                        indent=2,
                        ensure_ascii=False,
                    )
                except json.JSONDecodeError, ValueError:
                    formatted = body_text
                match_line = find_first_match_line(
                    formatted,
                    query_terms,
                    case_sensitive=self.query.case_sensitive,
                    regex=self.query.regex,
                )
                highlight_lines = {match_line + 1} if match_line is not None else None
                syntax = _RichSyntax(
                    formatted,
                    "json",
                    theme="ansi_dark",
                    word_wrap=True,
                    highlight_lines=highlight_lines,
                )
                result = (syntax, formatted)
            elif fmt == "markdown":
                result = (
                    _RichMarkdown(body_text, code_theme="ansi_dark"),
                    body_text,
                )
            else:
                result = (
                    highlight_matches(
                        body_text,
                        query_terms,
                        case_sensitive=self.query.case_sensitive,
                        regex=self.query.regex,
                    ),
                    body_text,
                )
            if cache_key is not None:
                self._detail_body_cache[cache_key] = result
                self._detail_body_cache.move_to_end(cache_key)
                if len(self._detail_body_cache) > self._DETAIL_CACHE_MAX:
                    self._detail_body_cache.popitem(last=False)
            return result

        def _scroll_detail_to_first_match(
            self,
            body_text: str,
            query_terms: cabc.Sequence[str],
        ) -> None:
            """Jump ``_detail_scroll`` so the first match lands at the viewport center.

            Memoizes ``find_first_match_line`` per ``(record, query)`` so a
            cursor parked on the same record across viewport refreshes does
            not rescan the body each time.
            """
            if self._detail_scroll is None:
                return
            scroll: t.Any = self._detail_scroll
            cache_key = self._detail_cache_key(query_terms)
            if cache_key is not None and cache_key in self._first_match_cache:
                match_line = self._first_match_cache[cache_key]
                self._first_match_cache.move_to_end(cache_key)
            else:
                match_line = find_first_match_line(
                    body_text,
                    query_terms,
                    case_sensitive=self.query.case_sensitive,
                    regex=self.query.regex,
                )
                if cache_key is not None:
                    self._first_match_cache[cache_key] = match_line
                    self._first_match_cache.move_to_end(cache_key)
                    if len(self._first_match_cache) > self._DETAIL_CACHE_MAX:
                        self._first_match_cache.popitem(last=False)
            if match_line is None:
                scroll.scroll_to(y=0, animate=False)
                return
            target_line = self._DETAIL_HEADER_LINES + match_line
            viewport_h = int(getattr(scroll.size, "height", 0) or 0)
            center_offset = max(0, target_line - viewport_h // 2)
            scroll.scroll_to(y=center_offset, animate=False)

        def on_resize(self, event: object) -> None:
            """Debounce rapid resize bursts (e.g. tiling-WM live drag)."""
            del event
            if self._resize_debounce_timer is not None:
                timer = t.cast("t.Any", self._resize_debounce_timer)
                timer.stop()
            self._resize_debounce_timer = self.set_timer(0.05, self._after_resize)

        def _after_resize(self) -> None:
            """Refresh chrome; the detail pane scroll wrapper handles its own reflow."""
            # Recompute (not just repaint) the right slot — crossing the
            # narrow breakpoint adds/removes the cursor/visible segment.
            self._refresh_results_status_right()
            if self._meter_widget is not None:
                self._meter_widget.set_narrow(self._statusline_narrow())
                # The change-gate caches the last composed string; a width
                # change with constant fraction must still repaint the bar.
                self._meter_widget.invalidate()
            # Crossing the narrow breakpoint adds/removes the elapsed suffix.
            self._tick_elapsed()
            # ... and swaps the post-search summary between its wide and
            # minimized forms.
            self._render_finished_status()
            # Crossing the split breakpoint moves the detail pane between
            # the right side and the bottom.
            self._apply_responsive_layout()

        def action_stop_search(self) -> None:
            """``Esc``: cooperative early-exit of the worker (no-op when finished)."""
            self._cancel_active_action()

        def action_smart_quit(self) -> None:
            """``Ctrl-C``: cancel the topmost in-flight action; quit if there are none."""
            if self._has_active_actions():
                self._cancel_active_action()
            else:
                self.exit()

        # Directional pane focus (tmux-style ``ctrl+hjkl``). Routing is
        # layout-aware: side-by-side the detail pane sits to the right of
        # the results, stacked it sits below them, so ``up``/``down`` reach
        # the detail in the stacked layout while ``left``/``right`` reach
        # it side-by-side. Focusable regions: #search (top), then in the
        # body #filter and #results, and #detail-scroll (right or bottom).

        def _focus_widget_by_id(self, widget_id: str) -> None:
            try:
                target = self.query_one(f"#{widget_id}")
            except Exception:
                return
            t.cast("t.Any", target).focus()

        def _record_for_detail_focus(self) -> SearchRecord | None:
            """Return the record explicit detail focus should render."""
            highlighted = None
            if self._results is not None:
                highlighted = t.cast("int | None", getattr(self._results, "highlighted", None))
            if highlighted is not None and 0 <= highlighted < len(self.filtered_records):
                return self.filtered_records[highlighted]
            current = self._current_detail_record
            if current is not None and any(record is current for record in self.filtered_records):
                return current
            return self.filtered_records[0] if self.filtered_records else None

        def _focus_detail(self) -> None:
            """Focus the detail pane, opening it first when stacked-collapsed.

            A ``display: none`` pane cannot take focus, so on a narrow
            statusline the detail is revealed before the focus call. Explicit
            focus also records the user's reader intent in wide mode so the
            pane stays visible if a later resize stacks the layout. It renders
            the best available record so streaming results opened before a
            cursor move don't reveal a blank reader.
            """
            if not self._detail_opened:
                self._detail_opened = True
                self._apply_responsive_layout()
            record = self._record_for_detail_focus()
            if record is not None:
                self.show_detail(record)
            self._focus_widget_by_id("detail-scroll")

        def action_focus_pane_left(self) -> None:
            """``Ctrl-H``: leave the detail pane back to the results."""
            if self.focused is not None and self.focused.id == "detail-scroll":
                self._focus_widget_by_id("results")

        def action_focus_pane_right(self) -> None:
            """``Ctrl-L``: focus the detail pane (to the right / opened below)."""
            if self.focused is not None and self.focused.id in (
                "results",
                "filter",
                "search",
            ):
                self._focus_detail()

        def action_focus_pane_up(self) -> None:
            """``Ctrl-K``: focus the pane above the current one.

            Inside the body, ``up`` lands on the body's top row (``#filter``).
            From the body's top row, ``up`` leaves the body and lands on the
            top-level search bar. When stacked, the detail sits below the
            results, so ``up`` from the detail lands on the results.
            """
            focused_id = self.focused.id if self.focused is not None else None
            if focused_id == "detail-scroll":
                self._focus_widget_by_id("results" if self._stacked else "filter")
            elif focused_id == "results":
                self._focus_widget_by_id("filter")
            elif focused_id == "filter":
                self._focus_widget_by_id("search")

        def action_focus_pane_down(self) -> None:
            """``Ctrl-J``: focus the pane below the current one.

            When stacked, ``down`` from the results reaches the detail pane
            below them (opening it if needed).
            """
            focused_id = self.focused.id if self.focused is not None else None
            if focused_id == "search":
                self._focus_widget_by_id("filter")
            elif focused_id == "filter":
                self._focus_widget_by_id("results")
            elif focused_id == "results" and self._stacked:
                self._focus_detail()

        def _has_active_actions(self) -> bool:
            """Return True if any cancellable in-flight action exists.

            Extension point: when a second cancellable action lands (async
            detail-fetch, debounced refilter, etc.), add its state here.
            """
            return not self._search_done

        def _cancel_active_action(self) -> None:
            """Cancel the topmost in-flight cancellable action.

            Extension point: extend with future cancellable actions in
            most-recently-started order so ``Ctrl-C`` peels them off one at a
            time before exiting.
            """
            if not self._search_done:
                self.control.request_answer_now()

        def _matches_filter(self, record: SearchRecord) -> bool:
            if not self._filter_text:
                return True
            return self._filter_text in build_search_haystack(record).casefold()

    return AgentGrepApp(
        home=home,
        query=query,
        control=control,
        initial_search_text=initial_search_text,
    )
