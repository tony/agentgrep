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


def run_ui(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
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
    """
    app = build_streaming_ui_app(home, query, control=control)
    t.cast("RunnableAppLike", app).run()


def build_streaming_ui_app(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
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
    # entirely (see ``make_emit`` below) so they never queue behind keystrokes.

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

    def make_emit(app: StreamingAppLike) -> cabc.Callable[[object], None]:
        """Build an ``emit`` callback that dispatches streaming events via ``call_from_thread``.

        ``call_from_thread`` schedules the callback directly on the event loop
        rather than enqueuing a ``Message`` — so high-frequency record batches
        don't compete with keystroke / timer events for FIFO message dispatch.
        Vibe-tmux uses the same pattern (``call_from_thread(_rebuild_tree, snap)``)
        and Textual's own ``Log`` widget mutates state directly without a per-
        write message. This is the canonical Textual pattern for "many small
        updates from a worker thread."
        """
        typed_app = t.cast("t.Any", app)

        def emit(event: object) -> None:
            if isinstance(event, StreamingRecordsBatch):
                typed_app.call_from_thread(
                    typed_app._apply_records_batch,
                    event.records,
                    event.total,
                )
            elif isinstance(event, ProgressSnapshot):
                typed_app.call_from_thread(typed_app._apply_progress, event)
            elif isinstance(event, StreamingSearchFinished):
                typed_app.call_from_thread(
                    typed_app._apply_finished,
                    event.outcome,
                    event.total,
                    event.elapsed,
                    str(event.error) if event.error else None,
                )

        return emit

    class SpinnerWidget(static_type):  # ty: ignore[unsupported-base]
        """Self-driving Braille spinner that animates regardless of event-loop load.

        The widget pulls its frame index from ``time.monotonic()`` on every
        ``render`` and lets Textual's per-widget ``auto_refresh`` reactor drive
        the redraw. This decouples the spinner from any main-thread timer or
        message handler — even if record-batch dispatch backs up, the spinner
        keeps ticking.
        """

        _FRAMES: t.ClassVar[str] = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        _FPS: t.ClassVar[float] = 10.0

        def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 -- forwarded to Textual's ``id`` kwarg
            super().__init__("", id=id)
            self._final_glyph: str | None = None
            self._started_at: float = time.monotonic()

        def on_mount(self) -> None:
            """Arm the per-widget refresh timer (Textual reads this after mount)."""
            self.auto_refresh = 1.0 / self._FPS

        def render(self) -> str:
            """Return the current Braille frame from elapsed wall-clock time."""
            if self._final_glyph is not None:
                return self._final_glyph
            elapsed = time.monotonic() - self._started_at
            frame_index = int(elapsed * self._FPS) % len(self._FRAMES)
            return self._FRAMES[frame_index]

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

        def set_records(self, records: cabc.Sequence[SearchRecord]) -> None:
            """Apply a new filter result by patching the existing options.

            For the common "user typed another character" narrowing case the
            method removes the now-unmatched options without rebuilding the
            list — keeps rendering O(removed) instead of O(total) and never
            touches the haystack cache. Falls back to a full rebuild when
            the new set introduces records not currently shown (widening) or
            when more than half of the current options would be removed
            (where ``remove_option_at_index`` would do worse than a single
            ``clear_options`` + ``add_options`` pair).
            """
            new_records = list(records)
            new_ids: set[int] = {id(record) for record in new_records}
            current_records = self._records
            if not current_records:
                self._rebuild_options(new_records)
                return
            current_index_by_id: dict[int, int] = {
                id(record): idx for idx, record in enumerate(current_records)
            }
            additions = [record for record in new_records if id(record) not in current_index_by_id]
            if additions:
                self._rebuild_options(new_records)
                return
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
                return
            for idx in to_remove_indices:
                self.remove_option_at_index(idx)
            self._records = new_records

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

        _AGENT_COLORS: t.ClassVar[dict[str, str]] = {
            "codex": "cyan",
            "claude": "magenta",
            "cursor": "yellow",
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
            """Move focus rightward to the detail-scroll pane (vim-style ``l``)."""
            detail = self.app.query_one("#detail-scroll")
            t.cast("t.Any", detail).focus()

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
        ) -> None:
            super().__init__(placeholder=placeholder, id=id)
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
            await super()._on_key(event)

        def action_release_down(self) -> None:
            """Footer-binding fallback (``_on_key`` handles the real release)."""
            self.app.action_focus_next()

    class SearchInput(input_widget):  # ty: ignore[unsupported-base]
        """``Input`` subclass that fires a debounced :class:`SearchRequested`.

        Keystrokes update the input text immediately so the cursor stays
        instant; the expensive backend search runs only after 150 ms of
        typing inactivity, mirroring :class:`FilterInput`. The Textual
        ``@work(thread=True, exclusive=True, group="search")`` worker on
        the app then auto-cancels any prior in-flight search, so fast
        typing never piles up worker threads.
        """

        _DEBOUNCE_SECONDS: t.ClassVar[float] = 0.15

        BINDINGS: t.ClassVar[list[tuple[str, str, str]]] = [
            ("down", "release_down", "Filter"),
        ]

        def __init__(
            self,
            *,
            value: str = "",
            placeholder: str = "",
            id: str | None = None,  # noqa: A002 -- forwarded to Textual's ``id`` kwarg
        ) -> None:
            # Set attribute BEFORE ``super().__init__`` because Textual's
            # ``Input.__init__`` triggers ``_watch_value`` synchronously when
            # ``value`` is non-empty.
            self._debounce_timer: object | None = None
            self._suppress_initial_dispatch: bool = bool(value)
            super().__init__(value=value, placeholder=placeholder, id=id)

        def _watch_value(self, value: str) -> None:
            """Post normal ``Input.Changed`` and arm a debounced ``SearchRequested``."""
            super()._watch_value(value)
            # Initial value injected via ``value=`` should not auto-dispatch
            # a SearchRequested — the caller (app on_mount) decides whether
            # to seed the initial search.
            if self._suppress_initial_dispatch:
                self._suppress_initial_dispatch = False
                return
            if self._debounce_timer is not None:
                self._debounce_timer.stop()
            self._debounce_timer = self.set_timer(
                self._DEBOUNCE_SECONDS,
                lambda: self.post_message(
                    SearchRequested(payload=SearchRequestedPayload(text=value)),
                ),
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
            padding: 0;
            layout: horizontal;
        }
        #status-spinner {
            width: 2;
            color: $accent;
        }
        #status-text {
            width: 1fr;
            color: ansi_bright_cyan;
            text-style: bold;
        }
        #status-right {
            width: auto;
            color: $warning;
            text-style: bold;
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

        def __init__(
            self,
            *,
            home: pathlib.Path,
            query: SearchQuery,
            control: SearchControl,
        ) -> None:
            super().__init__()
            self.home = home
            self.query = query
            self.control = control
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
            self._detail_statusline: StaticLike | None = None
            self._filter_input: FilterInput | None = None
            self._search_input: SearchInput | None = None
            self._resize_debounce_timer: object | None = None
            self._current_detail_record: SearchRecord | None = None
            self._detail_scroll: t.Any = None
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

            Each body column has its own footer status line — the results
            column carries the live chrome (spinner + status + match count
            + scroll %) and the detail column carries the record path +
            scroll %. There is intentionally no top-level chrome row; the
            reactive state belongs to the pane it describes.
            """
            yield header()
            initial_search = " ".join(self.query.terms) if self.query.terms else ""
            yield SearchInput(
                value=initial_search,
                placeholder="Search prompts and history",
                id="search",
            )
            with horizontal(id="body"):
                with vertical(id="results-column"):
                    yield FilterInput(placeholder="Filter loaded results", id="filter")
                    yield SearchResultsList(id="results")
                    with horizontal(id="results-statusline"):
                        yield SpinnerWidget(id="status-spinner")
                        yield static_type("", id="status-text")
                        yield static_type("", id="status-right")
                with vertical(id="detail-column"):
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
            self._progress = StreamingSearchProgress(emit=make_emit(streaming))
            if self.query.terms:
                self._start_search_worker(self.query)
                self._filter_input.focus()
            else:
                # No initial query — leave the chrome idle and land focus on
                # the search bar so the user can start typing immediately.
                self._search_done = True
                if self._status_widget is not None:
                    self._status_widget.update("Type to search")
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
            """Wipe per-search state and chrome before a fresh search starts."""
            self.control.reset()
            clear_haystack_cache()
            self._detail_body_cache.clear()
            self._first_match_cache.clear()
            self.all_records = []
            self.filtered_records = []
            self._search_done = False
            self._started_at = None
            self._last_snapshot = None
            self._current_detail_record = None
            if self._results is not None:
                self._results.set_records([])
            if self._detail is not None:
                self._detail.update("")
            if self._matches_widget is not None:
                self._matches_widget.update("")
            if self._detail_statusline is not None:
                self._detail_statusline.update("")
            if self._status_widget is not None:
                terms = " ".join(self.query.terms) if self.query.terms else "all records"
                self._status_widget.update(f"Searching {terms}")
            if self._spinner_widget is not None:
                self._spinner_widget.unfreeze()
            self._progress = StreamingSearchProgress(
                emit=make_emit(
                    t.cast("StreamingAppLike", t.cast("object", self)),
                ),
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
                    self._status_widget.update("Type to search")
                if self._spinner_widget is not None:
                    self._spinner_widget.freeze(" ")
                self.query = new_query
                return
            self._start_search_worker(new_query)

        def _build_search_query(self, text: str) -> SearchQuery:
            """Build a fresh :class:`SearchQuery` from the search-bar text.

            Preserves the agent and search-type filters from the current
            query so the search bar lives on top of the existing filter
            scope rather than resetting it.
            """
            terms = tuple(text.split()) if text else ()
            return SearchQuery(
                terms=terms,
                search_type=self.query.search_type,
                any_term=self.query.any_term,
                regex=self.query.regex,
                case_sensitive=self.query.case_sensitive,
                agents=self.query.agents,
                limit=self.query.limit,
            )

        _APPLY_CHUNK_SIZE: t.ClassVar[int] = 200

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
            if self._matches_widget is not None:
                self._matches_widget.update(format_match_count(total))

        def _apply_progress(self, snapshot: ProgressSnapshot) -> None:
            """Update the status widget — invoked via ``call_from_thread``."""
            self._last_snapshot = snapshot
            if self._started_at is None:
                self._started_at = time.monotonic()
            label = snapshot.query_label
            if snapshot.current is not None and snapshot.total is not None:
                status = (
                    f"Searching {label} | "
                    f"{snapshot.phase} {snapshot.current}/{snapshot.total} sources"
                )
            elif snapshot.detail:
                status = f"Searching {label} | {snapshot.phase} {snapshot.detail}"
            else:
                status = f"Searching {label} | {snapshot.phase}"
            if self._status_widget is not None:
                self._status_widget.update(status)

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
            glyphs = {"complete": "✓", "interrupted": "■", "error": "✗"}
            if self._spinner_widget is not None:
                self._spinner_widget.freeze(glyphs.get(outcome, "·"))
            if self._status_widget is not None:
                if outcome == "error":
                    self._status_widget.update(f"Search failed: {error_message}")
                elif outcome == "interrupted":
                    self._status_widget.update(
                        f"Stopped at {format_match_count(total)} "
                        f"across {self._sources_label()} sources in {elapsed:.1f}s",
                    )
                else:
                    self._status_widget.update(
                        f"Search complete: {format_match_count(total)} in {elapsed:.1f}s",
                    )

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
            """Apply the worker's filter result if it matches the current input."""
            payload = message.payload
            if self._filter_input is not None and payload.text != self._filter_input.value:
                return
            self.filtered_records = list(payload.matching)
            if self._results is not None:
                self._results.set_records(payload.matching)
            if self._detail is not None:
                if self.filtered_records:
                    self.show_detail(self.filtered_records[0])
                else:
                    self._detail.update(
                        "No results." if self._search_done else "No matches yet.",
                    )

        def on_option_list_option_highlighted(self, event: object) -> None:
            """Update the detail pane when the OptionList cursor moves."""
            option_index = getattr(event, "option_index", None)
            if option_index is None:
                return
            row_index = int(option_index)
            if 0 <= row_index < len(self.filtered_records):
                self.show_detail(self.filtered_records[row_index])

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
            if self._matches_widget is not None:
                self._matches_widget.refresh()

        def action_stop_search(self) -> None:
            """``Esc``: cooperative early-exit of the worker (no-op when finished)."""
            self._cancel_active_action()

        def action_smart_quit(self) -> None:
            """``Ctrl-C``: cancel the topmost in-flight action; quit if there are none."""
            if self._has_active_actions():
                self._cancel_active_action()
            else:
                self.exit()

        # Directional pane focus (tmux-style ``ctrl+hjkl``). Edge moves (e.g.
        # ``ctrl+j`` from the detail pane — nothing below it) are no-ops.
        # The focusable regions, top-to-bottom: #search (top), then in the
        # body: #filter and #results (left column, sticky filter above the
        # list) and #detail-scroll (right column).

        def _focus_widget_by_id(self, widget_id: str) -> None:
            try:
                target = self.query_one(f"#{widget_id}")
            except Exception:
                return
            t.cast("t.Any", target).focus()

        def action_focus_pane_left(self) -> None:
            """``Ctrl-H``: focus the pane to the left of the current one."""
            if self.focused is not None and self.focused.id == "detail-scroll":
                self._focus_widget_by_id("results")

        def action_focus_pane_right(self) -> None:
            """``Ctrl-L``: focus the pane to the right of the current one."""
            if self.focused is not None and self.focused.id in (
                "results",
                "filter",
                "search",
            ):
                self._focus_widget_by_id("detail-scroll")

        def action_focus_pane_up(self) -> None:
            """``Ctrl-K``: focus the pane above the current one.

            Inside the body, ``up`` lands on the body's top row (``#filter``).
            From the body's top row, ``up`` leaves the body and lands on the
            top-level search bar.
            """
            focused_id = self.focused.id if self.focused is not None else None
            if focused_id in ("results", "detail-scroll"):
                self._focus_widget_by_id("filter")
            elif focused_id == "filter":
                self._focus_widget_by_id("search")

        def action_focus_pane_down(self) -> None:
            """``Ctrl-J``: focus the pane below the current one."""
            focused_id = self.focused.id if self.focused is not None else None
            if focused_id == "search":
                self._focus_widget_by_id("filter")
            elif focused_id == "filter":
                self._focus_widget_by_id("results")

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

    return AgentGrepApp(home=home, query=query, control=control)
