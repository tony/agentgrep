"""Streaming Textual app — ``run_ui`` and the app factory.

This module defines the ``AgentGrepApp`` Textual app and the per-record LRU
caches that drive the interactive explorer. The widgets it composes
(``ResultsHeader``, ``FilterInput``, ``DetailFindInput``, ...) and their message
types live in ``agentgrep.ui.widgets``.

Textual is imported lazily inside :func:`build_streaming_ui_app` (via
``importlib.import_module``) so importing this module by itself does
not require Textual at import time — the import error is deferred to
the moment a UI is actually built.
"""

from __future__ import annotations

import collections
import dataclasses
import functools
import importlib
import json
import pathlib
import re
import time
import typing as t
from collections import abc as cabc

from rich.console import Group as _RichGroup
from rich.markdown import Markdown as _RichMarkdown
from rich.syntax import Syntax as _RichSyntax

from agentgrep._engine.orchestration import clear_haystack_cache, run_search_query
from agentgrep._engine.runtime import SearchRuntime
from agentgrep._text import (
    DETAIL_BODY_MAX_LINES,
    detect_content_format,
    find_first_match_line,
    format_compact_path,
    highlight_matches,
    truncate_lines,
)
from agentgrep._types import (
    RichTextModule,
    RunnableAppLike,
    StaticLike,
    StreamingAppLike,
    TextualAppModule,
    TextualBindingModule,
    TextualContainersModule,
    TextualWidgetsModule,
)
from agentgrep.progress import (
    FilterCompletedPayload,
    ProgressSnapshot,
    SearchControl,
    StreamingRecordsBatch,
    StreamingSearchFinished,
    StreamingSearchProgress,
    format_match_count,
)
from agentgrep.query import default_registry
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui.completion import (
    QuerySuggester,
    apply_enum_choice,
    apply_word_choice,
    keyword_completion_candidates,
)
from agentgrep.ui.format import (
    format_progress_percent,
    format_scanning_detail,
    scroll_percent,
)
from agentgrep.ui.highlighter import QueryHighlighter


class _DetailMatchStyles(t.NamedTuple):
    """Rich styles resolved on the pump before optional detail offload."""

    search: str
    filter: str


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
    test. ``AgentGrepApp`` is defined inside this factory (rather than at module
    scope) so the Textual imports stay lazy; the widgets and message types it
    composes are imported from ``agentgrep.ui.widgets``.

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
        textual_binding = t.cast(
            "TextualBindingModule",
            t.cast("object", importlib.import_module("textual.binding")),
        )
        rich_text_module = t.cast(
            "RichTextModule",
            t.cast("object", importlib.import_module("rich.text")),
        )
        from agentgrep.ui import _runtime, theme as ui_theme
        from agentgrep.ui.widgets import (
            CompletionDropdown,
            DetailFindInput,
            DetailFindRequested,
            DetailScroll,
            DetailScrollChanged,
            FilterCompleted,
            FilterInput,
            FilterRequested,
            PaneHeader,
            ResultsHeader,
            ResultsScrollChanged,
            SearchInput,
            SearchRequested,
            SearchResultsList,
        )
    except ImportError as error:
        msg = "Textual is required for --ui. Install with `uv pip install --editable .`."
        raise RuntimeError(msg) from error

    app_type = textual_app.App
    binding_type = textual_binding.Binding
    rich_text = rich_text_module
    horizontal = textual_containers.Horizontal
    vertical = textual_containers.Vertical
    footer = textual_widgets.Footer
    static_type = textual_widgets.Static

    # FilterRequested / FilterCompleted stay on the Textual message bus — they
    # fire at typing speed, not streaming speed, so the FIFO queue is fine for
    # them. Records / progress / search-finished events bypass the message bus
    # entirely (see ``_make_gated_progress`` below) so they never queue behind
    # keystrokes. The message classes and widgets live in ``agentgrep.ui.widgets``
    # (imported above) so their bodies sit outside this closure while staying
    # off the eager ``import agentgrep`` path (ADR 0010/0011).

    class AgentGrepApp(app_type):  # ty: ignore[unsupported-base]
        """Streaming read-only explorer for normalized search records."""

        # The pi-lite global stylesheet (semantic tokens + all-widget rules)
        # lives beside this module; ``CSS_PATH`` is resolved relative to
        # ``app.py`` even for this closure-defined class. The ``$ag-*`` tokens
        # it references are guaranteed to resolve via
        # ``get_theme_variable_defaults`` regardless of the active theme.
        CSS_PATH: t.ClassVar[str] = "styles.tcss"
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
        _DETAIL_ASYNC_BODY_THRESHOLD: t.ClassVar[int] = 20_000
        """Body length (chars) above which an uncached detail builds off-thread.

        Cache hits and small bodies build inline so cursor navigation stays
        synchronous; only a large, uncached body — parse, pretty-print, and
        syntax-highlight — is heavy enough to stall the event loop.
        """

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
            # Register and activate the pi-lite themes before the stylesheet
            # loads (CSS is parsed during startup, before ``on_mount``) so the
            # ``$ag-*`` tokens it references resolve from the active theme.
            # ``get_theme_variable_defaults`` is the belt-and-suspenders that
            # keeps those tokens resolvable even under a built-in theme.
            self.register_theme(ui_theme.agentgrep_dark())
            self.register_theme(ui_theme.agentgrep_light())
            self.theme = ui_theme.DARK_THEME_NAME
            # Run with native ANSI background handling so the structural panes
            # can use ``ansi_default`` (emitted as the terminal's own default
            # background, SGR 49) instead of a painted color — the only way a
            # Textual compositor can let the terminal background show through
            # like pi/claude-code. Truecolor ``#hex`` foregrounds and item
            # backgrounds are unaffected by this flag, so the pi palette stays.
            self.ansi_color = True
            self.home = home
            self.query = query
            # The user's launch discovery scope. A ``scope:`` predicate
            # widens the per-search scope to "all"; this stable base is what
            # a search without a ``scope:`` predicate reverts to, so the
            # widening never persists across searches.
            self._user_scope = query.scope
            self.control = control
            self._runtime = SearchRuntime.with_source_scan_cache()
            self.initial_search_text: str | None = initial_search_text
            self.all_records = []
            self.filtered_records = []
            self._progress: StreamingSearchProgress | None = None
            self._search_done = False
            self._started_at: float | None = None
            self._last_snapshot: ProgressSnapshot | None = None
            self._results: SearchResultsList | None = None
            self._detail: StaticLike | None = None
            self._detail_row: StaticLike | None = None
            self._chrome_generation: int = 0
            self._last_detail_text: str = ""
            self._last_right_text: str = ""
            self._finished_status: tuple[str, str] | None = None
            self._detail_visible: bool = False
            self._detail_statusline: StaticLike | None = None
            self._filter_input: FilterInput | None = None
            self._search_input: SearchInput | None = None
            # One registry-backed suggester drives the inline ghost text on
            # both inputs; completion offers query-language keywords only.
            self._completion_suggester = QuerySuggester(default_registry())
            # One highlighter syntax-colors the typed query on both inputs.
            self._query_highlighter = QueryHighlighter()
            self._enum_dropdown: t.Any = None
            self._enum_values: tuple[str, ...] = ()
            self._filter_dropdown: t.Any = None
            self._filter_dropdown_values: tuple[str, ...] = ()
            # Compiled record matcher for the current (query-aware) filter
            # text; ``None`` means no active filter (all records pass).
            self._filter_matcher: t.Any = None
            self._resize_debounce_timer: object | None = None
            self._current_detail_record: SearchRecord | None = None
            self._detail_scroll: t.Any = None
            self._body: t.Any = None
            self._detail_column: t.Any = None
            self._results_header: t.Any = None
            self._detail_header: t.Any = None
            # Responsive split: True when the detail pane is stacked
            # below the results rather than beside them. ``_detail_opened``
            # is the tig-style "user selected a row" gate that reveals the
            # stacked detail; programmatic highlights caused by filter-list
            # patching (``_pending_autohighlights``) must not trip it.
            self._stacked: bool = False
            self._detail_opened: bool = False
            self._pending_autohighlights: int = 0
            # Literal terms of the active filter, highlighted in the detail
            # pane in a distinct color from the search-query terms.
            self._filter_terms: tuple[str, ...] = ()
            # LRU caches for detail-pane work. Keyed by
            # ``(id(record), query.terms, case_sensitive, regex, filter.terms)``
            # — the attributes that determine the rendered body and the
            # highlighted match line. Bounded so a long browsing session
            # can't grow them without limit.
            self._detail_body_cache: collections.OrderedDict[
                tuple[int, tuple[str, ...], bool, bool, tuple[str, ...]],
                tuple[object, str],
            ] = collections.OrderedDict()
            # Per-record detail scroll memory: id(record) -> scroll_y. A
            # revisited record restores its position; a record opened for the
            # first time opens at the top. Bounded like the body cache.
            self._detail_scroll_positions: collections.OrderedDict[int, float] = (
                collections.OrderedDict()
            )
            # Find-in-detail state. The find bar is a third input (separate from
            # #search and #filter), shown only when a detail record is loaded.
            self._detail_find_input: t.Any = None
            self._detail_find_active: bool = False
            self._detail_find_query: str = ""
            self._detail_find_matches: list[tuple[int, int]] = []
            self._detail_find_current: int = 0
            # The current record's truncated body text + built header (Rich Text),
            # kept so find can re-highlight the body without rebuilding the header.
            self._detail_body_text: str = ""
            self._detail_header_text: t.Any = None
            # Per-record find memory, mirroring _detail_scroll_positions:
            # id(record) -> (query, match_index, input_cursor_pos). Bounded LRU.
            self._detail_find_state: collections.OrderedDict[
                int,
                tuple[str, int, int],
            ] = collections.OrderedDict()
            # Staged ctrl-c in inputs: clear the text first, then (on an empty
            # box) a first ctrl-c arms "press ctrl-c again to exit" in the gutter
            # and a second within the window quits. The gutter is a flash-layer
            # Static docked at the bottom.
            self._confirm_exit_pending: bool = False
            self._confirm_exit_timer: object | None = None
            self._ctrlc_gutter: t.Any = None

        def _get_start_time(self) -> float | None:
            return self._started_at

        def get_theme_variable_defaults(self) -> dict[str, str]:
            """Add the ``$ag-*`` token defaults so the stylesheet always resolves.

            Returns
            -------
            dict[str, str]
                Textual's defaults merged with :func:`theme.ag_variable_defaults`
                so a switch to any built-in theme can't leave an ``$ag-*``
                reference unresolved.
            """
            base = t.cast("dict[str, str]", super().get_theme_variable_defaults())
            return {**base, **ui_theme.ag_variable_defaults()}

        def _on_theme_changed(self, _theme: object) -> None:
            """Rebuild Rich-baked surfaces when the palette switches.

            The chrome recolors automatically through TCSS, but the results
            rows and the detail body bake concrete hex into Rich renderables at
            build time, so they are rebuilt against the new theme's tokens. The
            detail caches are dropped so the rebuild reads fresh colors.
            """
            if self._results is not None:
                self._results.rerender_records()
            if self._results_header is not None:
                self._results_header.refresh_theme()
            self._detail_body_cache.clear()
            if self._current_detail_record is not None:
                self.show_detail(self._current_detail_record)

        def compose(self) -> cabc.Iterator[object]:
            """Build the widget tree (search → body[results-col, detail-col] → footer).

            The results column carries its live chrome (spinner + status
            + match count + scroll %) as a header above the filter and
            list, so the running search state sits next to the search
            input that drives it. The detail column keeps its status
            line at the bottom — record path + scroll % is contextual to
            whatever's currently being read, so the natural place to
            glance is the foot of the pane.
            """
            if self.initial_search_text is not None:
                initial_search = self.initial_search_text
            else:
                initial_search = " ".join(self.query.terms) if self.query.terms else ""
            yield SearchInput(
                value=initial_search,
                placeholder="Search prompts",
                id="search",
                suggester=self._completion_suggester,
                highlighter=self._query_highlighter,
            )
            # Enum-value picker for field predicates; floats over the body
            # just below the search bar and stays hidden until an enum
            # field token (agent:/scope:) is typed.
            yield CompletionDropdown(id="enum-dropdown", target_input_id="search")
            # Decide the responsive split up-front (terminal width is known
            # at compose time) so narrow terminals are born stacked with the
            # detail collapsed — applying the class in on_mount instead would
            # paint the detail once and then hide it, a visible flicker.
            stacked = 0 < self.size.width < self._SPLIT_BREAKPOINT
            body_classes = "-stacked" if stacked else ""
            detail_classes = "-collapsed" if stacked else ""
            with horizontal(id="body", classes=body_classes):
                with vertical(id="results-column"):
                    # pi-style section header: a bold label + width-filling rule,
                    # in place of a box border. Recolors to accent when the
                    # results pane (filter or list) holds focus.
                    # The results header folds the live search status (spinner +
                    # bar + percent + match count) into its rule, so the column
                    # spends one row instead of a separate statusline.
                    yield ResultsHeader("results", id="results-header")
                    yield static_type("", id="status-detail")
                    yield FilterInput(
                        placeholder="Filter loaded results",
                        id="filter",
                        suggester=self._completion_suggester,
                        highlighter=self._query_highlighter,
                        label="filter",
                    )
                    # Keyword/term picker for the query-aware filter; floats
                    # over the results just below the filter input.
                    yield CompletionDropdown(
                        id="filter-dropdown",
                        target_input_id="filter",
                    )
                    yield SearchResultsList(id="results")
                    # Shown only in the pre-search bare-canvas state (CSS hides
                    # it otherwise); a dim, centered hint teaching the query
                    # language at the moment of highest intent.
                    yield static_type(
                        "try a search to begin\n\n"
                        "agent:claude   model:gpt*   role:user\n"
                        'timestamp:>2026-01-01   "exact phrase"',
                        id="empty-hint",
                    )
                with vertical(id="detail-column", classes=detail_classes):
                    yield PaneHeader("detail", id="detail-header")
                    with DetailScroll(id="detail-scroll"):
                        yield static_type("", id="detail")
                    # Find-in-detail bar: hidden until `/` or ctrl+f opens it
                    # (only with a record loaded); separate from #search/#filter.
                    yield DetailFindInput(placeholder="Find in detail", id="detail-find")
                    yield static_type("", id="detail-statusline")
            yield footer()
            # Transient gutter for the "press ctrl-c again to exit" confirm; a
            # flash-layer Static that overlays the footer only while shown.
            yield static_type("", id="ctrlc-gutter")

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
            self._results_header = t.cast(
                "ResultsHeader",
                streaming.query_one("#results-header"),
            )
            self._detail_header = streaming.query_one("#detail-header")
            self._detail_row = t.cast(
                "StaticLike",
                streaming.query_one("#status-detail", static_type),
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
            self._detail_find_input = t.cast(
                "DetailFindInput",
                streaming.query_one("#detail-find"),
            )
            t.cast("t.Any", self._detail_find_input).display = False
            t.cast("t.Any", self._detail_find_input).cursor_blink = False
            self._ctrlc_gutter = t.cast("t.Any", streaming.query_one("#ctrlc-gutter"))
            self._enum_dropdown = t.cast("t.Any", streaming.query_one("#enum-dropdown"))
            self._enum_dropdown.display = False
            self._filter_dropdown = t.cast("t.Any", streaming.query_one("#filter-dropdown"))
            self._filter_dropdown.display = False
            # Steady (non-blinking) input cursors. A blinking cursor keeps
            # toggling its inverted-block glyph even when the terminal loses
            # focus — Textual can't tell the tmux pane went inactive without
            # focus-events — so the cursor flickers in the background pane.
            # ``select_on_focus=False`` keeps the cursor where it is when focus
            # returns (e.g. after accepting a dropdown choice) instead of
            # selecting the whole query.
            for _input in (self._filter_input, self._search_input):
                typed_input = t.cast("t.Any", _input)
                typed_input.cursor_blink = False
                typed_input.select_on_focus = False
            self._progress = self._make_gated_progress()
            # Rebuild Rich-baked rows/detail when the user switches palette
            # (e.g. dark <-> light via the command palette).
            self.theme_changed_signal.subscribe(self, self._on_theme_changed)
            # Bind the pump thread for the non-blocking guards (NB-1/NB-2); when
            # opted in via AGENTGREP_TUI_WATCHDOG, arm the heartbeat + watchdog.
            _runtime.bind_pump_thread()
            if _runtime.watchdog_enabled():
                self.set_interval(_runtime.HEARTBEAT_INTERVAL, _runtime.record_heartbeat)
                _runtime.start_pump_watchdog()
            self._apply_responsive_layout()
            if self.query.terms:
                self._start_search_worker(self.query)
                self._filter_input.focus()
            else:
                # No initial query — pi "bare canvas": hide the body chrome
                # behind the centered hint (no status text) and land focus on
                # the search bar so the user can start typing immediately.
                self._search_done = True
                if self._results_header is not None:
                    self._results_header.go_idle()
                self._set_empty_state(empty=True)
                self._search_input.focus()
            self._update_pane_focus()

        def _set_empty_state(self, *, empty: bool) -> None:
            """Toggle the pre-search bare-canvas state on ``#body``.

            When ``empty`` the body chrome (headers, statusline, filter, detail
            column) is hidden by CSS, leaving the centered ``#empty-hint``; a
            launched search reveals it.
            """
            if self._body is not None:
                t.cast("t.Any", self._body).set_class(empty, "-empty")

        def on_descendant_focus(self, event: object) -> None:
            """Recolor the active pane's section header when focus moves."""
            # A focus change cancels a pending "press ctrl-c again to exit".
            self._disarm_confirm_exit()
            self._update_pane_focus()

        def on_descendant_blur(self, event: object) -> None:
            """Recolor the active pane's section header when focus leaves."""
            self._update_pane_focus()

        def _update_pane_focus(self) -> None:
            """Mark the focused pane's header ``-active`` (paint-only recolor).

            Bound to the focused *widget*, not the column: the results header
            lights for the filter or the list, the detail header for the detail
            scroll, and the top search bar lights neither. This avoids the
            results header glowing while the user types in the filter only if
            the cue tracked the column — here it intentionally treats the filter
            as part of the results pane, but never the search bar.
            """
            focused_id = getattr(self.focused, "id", None)
            results_active = focused_id in {"results", "filter"}
            detail_active = focused_id in {"detail-scroll", "detail-find"}
            if self._results_header is not None:
                t.cast("t.Any", self._results_header).set_class(results_active, "-active")
            if self._detail_header is not None:
                t.cast("t.Any", self._detail_header).set_class(detail_active, "-active")

        def on_unmount(self) -> None:
            """Release pump-thread binding and stop the watchdog on teardown."""
            _runtime.unbind_pump_thread()
            _runtime.stop_pump_watchdog()

        def _start_search_worker(self, query: SearchQuery) -> None:
            """Reset chrome and spawn a new search worker for ``query``.

            ``exclusive=True`` with ``group="search"`` makes Textual cancel
            any prior in-flight search worker before this one runs, which
            is the canonical Textual pattern for "fire a backend search on
            every debounced keystroke without piling up cancellations."
            """
            self.query = query
            self._reset_search_chrome()
            # A search is starting — reveal the body chrome (leave the bare
            # canvas), and show the filter now that results will load.
            self._set_empty_state(empty=False)
            self._set_search_rule_state("searching")
            if self._results_header is not None:
                self._results_header.begin()
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
            self._detail_scroll_positions.clear()
            self._detail_find_state.clear()
            # A fresh search wipes the detail; close any open find bar.
            self._reset_detail_find_state()
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
            if self._detail_statusline is not None:
                self._detail_statusline.update("")
            self._last_detail_text = ""
            self._last_right_text = ""
            self._finished_status = None
            # The merged results header carries the search status; clear it back
            # to the plain rule (``_start_search_worker`` re-activates it).
            if self._results_header is not None:
                self._results_header.go_idle()
            self._set_search_rule_state("")
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
            # The emitter runs on the worker thread; the generation check
            # happens on the pump inside _apply_streaming_event. Centralizing it
            # in make_gated_emitter keeps results off the message bus (NB-3) and
            # carrying the generation token (NB-10).
            emit = _runtime.make_gated_emitter(
                streaming.call_from_thread,
                self._apply_streaming_event,
                generation,
            )
            return StreamingSearchProgress(emit=emit)

        @_runtime.pump_only
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

        def on_input_changed(self, event: object) -> None:
            """Refresh the relevant completion dropdown as an input value changes."""
            source = getattr(event, "input", None)
            input_id = getattr(source, "id", None)
            value = str(getattr(event, "value", ""))
            if input_id == "search":
                self._update_search_dropdown(value)
            elif input_id == "filter":
                self._update_filter_dropdown(value)

        def _update_search_dropdown(self, value: str) -> None:
            """Populate and show/hide the search bar's keyword dropdown."""
            values = keyword_completion_candidates(value, default_registry()) or ()
            self._enum_values = values
            self._populate_dropdown(self._enum_dropdown, self._search_input, values)

        def _update_filter_dropdown(self, value: str) -> None:
            """Populate and show/hide the filter box's keyword dropdown."""
            values = keyword_completion_candidates(value, default_registry()) or ()
            self._filter_dropdown_values = values
            self._populate_dropdown(self._filter_dropdown, self._filter_input, values)

        def _populate_dropdown(
            self,
            dropdown: t.Any,
            target_input: t.Any,
            values: tuple[str, ...],
        ) -> None:
            """Fill ``dropdown`` with ``values`` anchored to ``target_input``'s cursor."""
            if dropdown is None:
                return
            if not values:
                dropdown.display = False
                return
            dropdown.clear_options()
            dropdown.add_options(list(values))
            self._align_dropdown_to_cursor(dropdown, target_input)
            dropdown.display = True
            dropdown.highlighted = 0

        def _align_dropdown_to_cursor(self, dropdown: t.Any, target_input: t.Any) -> None:
            """Offset ``dropdown`` so its content sits under ``target_input``'s cursor.

            The overlay's natural slot is at the left edge just below its
            input; shifting its x offset by the cursor's screen column (less
            the 1-cell border) anchors the list to where the user is typing.
            ``constrain: inside inside`` keeps it on-screen.
            """
            if target_input is None or dropdown is None:
                return
            cursor_x = int(t.cast("t.Any", target_input).cursor_screen_offset.x)
            dropdown.styles.offset = (max(cursor_x - 1, 0), 0)

        def on_option_list_option_selected(self, event: object) -> None:
            """Accept a completion-dropdown choice into the originating input."""
            option_list = getattr(event, "option_list", None)
            index = int(getattr(event, "option_index", 0) or 0)
            if option_list is self._enum_dropdown:
                self._accept_dropdown_choice(
                    self._search_input,
                    self._enum_dropdown,
                    self._enum_values,
                    index,
                )
            elif option_list is self._filter_dropdown:
                self._accept_dropdown_choice(
                    self._filter_input,
                    self._filter_dropdown,
                    self._filter_dropdown_values,
                    index,
                )

        def _accept_dropdown_choice(
            self,
            target_input: t.Any,
            dropdown: t.Any,
            values: tuple[str, ...],
            index: int,
        ) -> None:
            """Insert the chosen completion into ``target_input`` and close ``dropdown``."""
            if target_input is None or not (0 <= index < len(values)):
                return
            text = str(target_input.value)
            trailing_token = text.rpartition(" ")[2]
            # field:partial token -> replace the value after the colon; a bare
            # token -> replace the whole token with the chosen keyword/term.
            if ":" in trailing_token:
                new_value = apply_enum_choice(text, values[index])
            else:
                new_value = apply_word_choice(text, values[index])
            target_input.value = new_value
            target_input.cursor_position = len(new_value)
            dropdown.display = False
            target_input.focus()

        @_runtime.offload
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
                # Cleared the box — return to the pi bare canvas + hint.
                # ``_reset_search_chrome`` already idles the header rule.
                self._reset_search_chrome()
                self._search_done = True
                self._set_empty_state(empty=True)
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

            # Reset the base scope to the user's launch scope so a previous
            # search's ``scope:``-widened "all" never feeds back as the base —
            # otherwise a follow-up query with no ``scope:`` predicate would
            # keep scanning conversations invisibly.
            base = dataclasses.replace(self.query, scope=self._user_scope)
            result = build_query_from_input(text, base, default_registry())
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

        @_runtime.pump_only
        async def _apply_records_batch(
            self,
            records: cabc.Sequence[SearchRecord],
            total: int,
        ) -> None:
            """Append a streaming records batch — invoked via ``call_from_thread``.

            Runs as a coroutine so the apply can yield to the event loop between
            each ``_APPLY_CHUNK_SIZE`` slice. ``call_from_thread`` blocks the
            worker for the full duration of this coroutine, which gives natural
            backpressure (the worker can't queue up batches faster than the UI
            can apply them) while :func:`_runtime.stream_apply` yields between
            chunks — so a 5000-record batch can't freeze the UI for the duration
            of a single apply (NB-4).
            """
            self.all_records.extend(records)
            # Results are arriving — make sure the panes are revealed (a search
            # launched via _start_search_worker already did this; a batch driven
            # directly, e.g. in tests, reveals here). Idempotent.
            self._set_empty_state(empty=False)
            matching = [record for record in records if self._matches_filter(record)]
            if matching and self._results is not None:
                results = self._results

                def _append_chunk(chunk: cabc.Sequence[SearchRecord]) -> None:
                    results.append_records(chunk)
                    self.filtered_records.extend(chunk)

                await _runtime.stream_apply(
                    matching,
                    _append_chunk,
                    chunk_size=self._APPLY_CHUNK_SIZE,
                )
            self._refresh_results_status_right()

        @_runtime.pump_only
        def _apply_progress(self, snapshot: ProgressSnapshot) -> None:
            """Feed the header bar and detail row — invoked via ``call_from_thread``.

            Per-source progress events arrive thousands of times per search; the
            header stores the new fraction without repainting (its 2 Hz spinner
            timer picks it up on the next frame) and the detail row gates on
            content change, so neither repaints per event. Stale-generation
            events never reach this handler — :meth:`_apply_streaming_event`
            drops them.
            """
            # A search is in progress — reveal the chrome (idempotent).
            self._set_empty_state(empty=False)
            self._last_snapshot = snapshot
            if self._started_at is None:
                self._started_at = time.monotonic()
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
            if self._results_header is not None:
                self._results_header.set_narrow(self._statusline_narrow())
                self._results_header.set_progress(fraction, snapshot.phase)
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
            """Report whether the header rule is too narrow to also carry the count."""
            header = self._results_header
            if header is None:
                return False
            width = int(getattr(header.size, "width", 0) or 0)
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

        @_runtime.pump_only
        def _apply_finished(
            self,
            outcome: str,
            total: int,
            elapsed: float,
            error_message: str | None,
        ) -> None:
            r"""Freeze the header chrome — invoked via ``call_from_thread``.

            The header's spinner timer stops and the outcome glyph + bar color
            hold; the elapsed total is folded into the summary string the ctrl+\
            detail row shows, not a live-ticking widget.
            """
            # A search ran — its outcome belongs on the (now revealed) chrome.
            self._set_empty_state(empty=False)
            self._search_done = True
            if outcome == "error":
                summary = f"Search failed: {error_message}"
            elif outcome == "interrupted":
                summary = (
                    f"Stopped at {format_match_count(total)} "
                    f"across {self._sources_label()} sources in {elapsed:.1f}s"
                )
            else:
                summary = f"Search complete: {format_match_count(total)} in {elapsed:.1f}s"
            # Freeze the header: the outcome glyph (✓/■/✗) + bar color carry the
            # result; errors show their message in the rule, the full summary
            # lives in the ctrl+\ detail row.
            if self._results_header is not None:
                self._results_header.freeze(outcome, message=error_message or "")
            self._set_search_rule_state(outcome)
            self._finished_status = (outcome, summary)
            self._last_detail_text = summary
            if self._detail_visible and self._detail_row is not None:
                self._detail_row.update(summary)
            # Recompute the right slot: narrow mode swaps the in-flight
            # search percent for the plain match count once the search ends.
            self._refresh_results_status_right()

        def _set_search_rule_state(self, state: str) -> None:
            """Tint the search input's top/bottom rule by search state.

            Mirrors pi's ``updateEditorBorderColor``: the input border is a
            live state indicator, not a static focus pair. ``state`` is one of
            ``""`` (idle), ``"searching"``, ``"complete"``, ``"interrupted"``,
            or ``"error"``; each maps to a ``-`` class on ``#search`` whose
            color lives in ``styles.tcss`` (so this is a paint-only swap that
            wins over ``Input:focus`` by id+class specificity).
            """
            if self._search_input is None:
                return
            target = t.cast("t.Any", self._search_input)
            target.remove_class("-searching", "-done", "-stopped", "-error")
            rule_class = {
                "searching": "-searching",
                "complete": "-done",
                "interrupted": "-stopped",
                "error": "-error",
            }.get(state)
            if rule_class is not None:
                target.add_class(rule_class)

        def _sources_label(self) -> str:
            snap = self._last_snapshot
            if snap is None or snap.current is None or snap.total is None:
                return "?"
            return f"{snap.current}/{snap.total}"

        def on_filter_requested(self, message: FilterRequested) -> None:
            """Spawn a worker to recompute the filter; exclusive cancels any in-flight one."""
            text = message.payload.text
            matcher = self._build_filter_matcher(text)
            # Streaming records use the same matcher so a live search keeps the
            # filtered list query-aware as records arrive.
            self._filter_matcher = matcher
            # The filter's literal terms get highlighted in the detail pane in
            # a distinct color from the search-query terms.
            self._filter_terms = tuple(matcher.query.terms) if matcher is not None else ()
            streaming = t.cast("StreamingAppLike", t.cast("object", self))
            streaming.run_worker(
                lambda captured_text=text, captured_matcher=matcher: self._run_filter_worker(
                    captured_text,
                    captured_matcher,
                ),
                name="filter",
                group="filter",
                thread=True,
                exclusive=True,
            )

        def _build_filter_matcher(self, text: str) -> t.Any:
            """Compile a record matcher for the filter text, or ``None`` if empty.

            The filter accepts the same query language as search, applied
            in-memory to the loaded results: field predicates, booleans, and
            phrases all work. A partial or malformed query (e.g. ``agent:``
            mid-type) falls back to a literal substring match so the filter
            stays usable while typing.
            """
            from agentgrep._engine.matching import compile_record_matcher
            from agentgrep.query import build_query_from_input, default_registry

            stripped = text.strip()
            if not stripped:
                return None
            base = SearchQuery(
                terms=(),
                scope="all",
                any_term=False,
                regex=False,
                case_sensitive=False,
                agents=self.query.agents,
                limit=None,
            )
            result = build_query_from_input(stripped, base, default_registry())
            query = result.query
            if query is None:
                query = SearchQuery(
                    terms=tuple(stripped.split()),
                    scope="all",
                    any_term=False,
                    regex=False,
                    case_sensitive=False,
                    agents=self.query.agents,
                    limit=None,
                )
            return compile_record_matcher(query)

        @_runtime.offload
        def _run_filter_worker(self, text: str, matcher: t.Any) -> None:
            """Compute the filtered list on a background thread; post a ``FilterCompleted``.

            Runs in a worker thread; safe to scan ``self.all_records`` since
            list reads under CPython are GIL-protected. The main thread guards
            against stale results by comparing the captured text against the
            current input value in :meth:`on_filter_completed`.
            """
            if matcher is None:
                matching: tuple[SearchRecord, ...] = tuple(self.all_records)
            else:
                matching = tuple(record for record in self.all_records if matcher.matches(record))
            streaming = t.cast("StreamingAppLike", t.cast("object", self))
            streaming.post_message(
                FilterCompleted(
                    payload=FilterCompletedPayload(text=text, matching=matching),
                ),
            )

        @_runtime.pump_only
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
            option_list = getattr(event, "option_list", None)
            if option_list is self._enum_dropdown or option_list is self._filter_dropdown:
                # The completion dropdowns are separate OptionLists; their
                # highlights must not drive the results detail pane.
                return
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
            """Re-render the detail status line and remember the scroll position."""
            self._refresh_detail_statusline(message.percent)
            self._remember_detail_scroll()

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
            if self._results_header is None:
                return
            if cursor is None and visible is None and self._results is not None:
                cursor = t.cast("int | None", getattr(self._results, "highlighted", None))
                visible = len(self._results._records)
            text = self._format_results_right(cursor, visible)
            if text != self._last_right_text:
                self._last_right_text = text
                self._results_header.set_matches(text)

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
            # When find is active, lead with the match indicator (N/M or "no
            # matches"); the path then truncates into the remaining room.
            find_text = ""
            if self._detail_find_active and self._detail_find_query:
                total = len(self._detail_find_matches)
                find_text = (
                    f"{self._detail_find_current + 1}/{total}  " if total else "no matches  "
                )
            right = f"{pct}%"
            path_text = format_compact_path(
                record.path,
                max_width=max(10, width - 6 - len(find_text)),
            )
            pad = max(1, width - len(find_text) - len(path_text) - len(right))
            self._detail_statusline.update(f"{find_text}{path_text}{' ' * pad}{right}")

        def _current_detail_scroll_percent(self) -> int:
            """Compute the detail pane's scroll percent on demand."""
            if self._detail_scroll is None:
                return 100
            scroll = self._detail_scroll
            return scroll_percent(
                float(getattr(scroll, "scroll_y", 0) or 0),
                float(getattr(scroll, "max_scroll_y", 0) or 0),
            )

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

            A record opened for the first time lands at the top; a record
            viewed before restores the scroll position the user left it at (see
            :meth:`_restore_detail_scroll`).
            """
            if self._detail is None:
                return
            # A record switch while the find bar is open would leave a stale
            # match list + N/M count and apply the outgoing body's offsets to
            # the new body. Save the outgoing record's find state (a revisit +
            # reopen restores it from _detail_find_state) and reset the bar
            # before the new body replaces _detail_body_text. No re-render or
            # refocus here — a switch comes from the results list, which keeps
            # focus; this is state only (see _close_detail_find for the esc path).
            if (
                self._detail_find_active
                and self._current_detail_record is not None
                and self._current_detail_record is not record
            ):
                self._remember_detail_find()
                self._reset_detail_find_state()
            # Showing a record means results exist — leave the bare-canvas state.
            self._set_empty_state(empty=False)
            self._current_detail_record = record
            width = max(20, self._detail.size.width or 80)
            theme_vars = self.theme_variables
            agent_color = ui_theme.resolve(
                theme_vars,
                ui_theme.AGENT_TOKEN_BY_NAME.get(record.agent or ""),
            )
            kind_color = ui_theme.resolve(
                theme_vars,
                ui_theme.KIND_TOKEN_BY_NAME.get(record.kind or ""),
            )
            dim_color = ui_theme.resolve(theme_vars, "ag-dim")
            model_color = ui_theme.resolve(theme_vars, "ag-model")
            path_color = ui_theme.resolve(theme_vars, "ag-muted")
            header = rich_text.Text(no_wrap=False)
            for label, value, value_style in (
                ("Agent:", record.agent or "", agent_color),
                ("Kind:", record.kind or "", kind_color),
                ("Store:", record.store or "", dim_color),
                ("Adapter:", record.adapter_id or "", dim_color),
                ("Timestamp:", record.timestamp or "unknown", dim_color),
                ("Model:", record.model or "unknown", model_color),
                (
                    "Path:",
                    format_compact_path(record.path, max_width=width - 8),
                    path_color,
                ),
            ):
                header.append(f"{label} ", style="bold")
                header.append(f"{value}\n", style=value_style)
            header.append("\n")
            body_truncated = truncate_lines(record.text, DETAIL_BODY_MAX_LINES)
            query_terms = list(self.query.terms)
            # Keep the header + body text so find-in-detail can re-highlight the
            # body (without rebuilding the header) and scroll to matches.
            self._detail_header_text = header
            self._detail_body_text = body_truncated
            match_styles = _DetailMatchStyles(
                search=self._match_style("search"),
                filter=self._match_style("filter"),
            )
            if (
                self._detail_body_is_cached(query_terms)
                or len(body_truncated) <= self._DETAIL_ASYNC_BODY_THRESHOLD
            ):
                self._present_detail(
                    record,
                    header,
                    self._build_detail_body(body_truncated, query_terms, match_styles),
                    query_terms,
                )
                return
            # Large, uncached body: show the header now and build the heavy
            # renderable off the UI thread. ``exclusive=True`` cancels a prior
            # detail build, and ``_present_detail`` discards any result whose
            # record is no longer the one on screen.
            self._detail.update(_RichGroup(header))
            streaming = t.cast("StreamingAppLike", t.cast("object", self))
            streaming.run_worker(
                functools.partial(
                    self._build_detail_in_thread,
                    record,
                    header,
                    body_truncated,
                    query_terms,
                    match_styles,
                ),
                name="detail",
                group="detail",
                thread=True,
                exclusive=True,
            )

        def _detail_body_is_cached(self, query_terms: cabc.Sequence[str]) -> bool:
            """Return whether the detail body for the current record is memoized."""
            cache_key = self._detail_cache_key(query_terms)
            return cache_key is not None and cache_key in self._detail_body_cache

        @_runtime.offload
        def _build_detail_in_thread(
            self,
            record: SearchRecord,
            header: object,
            body_truncated: str,
            query_terms: cabc.Sequence[str],
            match_styles: _DetailMatchStyles,
        ) -> None:
            """Build the detail body off the UI thread, then apply it on the loop."""
            body = self._build_detail_body(body_truncated, query_terms, match_styles)
            streaming = t.cast("StreamingAppLike", t.cast("object", self))
            streaming.call_from_thread(
                self._present_detail,
                record,
                header,
                body,
                query_terms,
            )

        @_runtime.pump_only
        def _present_detail(
            self,
            record: SearchRecord,
            header: object,
            body: tuple[object, str],
            query_terms: cabc.Sequence[str],
        ) -> None:
            """Render ``body`` into the detail pane unless ``record`` is superseded.

            Runs on the event-loop thread (directly for inline builds, via
            ``call_from_thread`` for off-thread builds); the identity check
            drops a stale build whose record the cursor has already left.
            """
            if self._detail is None or self._current_detail_record is not record:
                return
            body_renderable, _body_for_scroll = body
            self._detail.update(
                _RichGroup(t.cast("t.Any", header), t.cast("t.Any", body_renderable))
            )
            self._restore_detail_scroll(record)
            self._refresh_detail_statusline()
            if self._detail_find_active:
                # A same-record re-render (e.g. a theme switch re-renders the
                # current record) with find open just painted the plain body;
                # re-overlay the find highlights so they survive the re-render.
                self._present_detail_find()

        def _detail_cache_key(
            self,
            query_terms: cabc.Sequence[str],
        ) -> tuple[int, tuple[str, ...], bool, bool, tuple[str, ...]] | None:
            """Compose the LRU key for the current record + query + filter.

            Returns ``None`` when there is no current record (e.g. detail
            pane invoked before a record is highlighted) so callers know
            to skip the cache entirely. The filter terms are part of the key
            so changing the filter re-renders the filter-term highlights.
            """
            record = self._current_detail_record
            if record is None:
                return None
            return (
                id(record),
                tuple(query_terms),
                self.query.case_sensitive,
                self.query.regex,
                self._filter_terms,
            )

        def _match_style(self, kind: str) -> str:
            """Build a match-highlight Rich style from ``$ag-match-*`` tokens.

            Search matches (``kind="search"``) render as a calm gold foreground
            — they recur throughout a body, so a background fill would be
            noisy. Filter matches (``kind="filter"``) render as a prominent
            accent background with a contrast-computed foreground. Both adapt to
            the active palette; either falls back to its former literal style if
            a token is missing.

            Parameters
            ----------
            kind : str
                ``"search"`` or ``"filter"``.

            Returns
            -------
            str
                A Rich style string.
            """
            theme_vars = self.theme_variables
            if kind == "search":
                foreground = ui_theme.resolve(theme_vars, "ag-match-search")
                return f"bold {foreground}".rstrip() if foreground else "bold yellow"
            if kind == "find":
                # All find matches: a purple fill, distinct from search-gold and
                # filter-accent.
                color = ui_theme.resolve(theme_vars, "ag-model")
                return f"bold black on {color}" if color else "bold black on magenta"
            if kind == "find-current":
                # The match the find cursor is on: a brighter gold fill so it
                # stands out from the other (purple) find matches.
                color = ui_theme.resolve(theme_vars, "ag-match-search")
                return f"bold black on {color}" if color else "bold black on yellow"
            background = ui_theme.resolve(theme_vars, "ag-match-filter-bg")
            foreground = ui_theme.resolve(theme_vars, "ag-match-filter-fg")
            if background and foreground:
                return f"bold {foreground} on {background}"
            return "bold black on cyan"

        def _apply_filter_highlight(self, text: t.Any, style: str | None = None) -> None:
            """Overlay the filter's literal terms onto ``text`` in a distinct color.

            Applied after the search-term highlight so filter matches stand out
            separately. Filter matching is case-insensitive, so the highlight is
            too; field predicates contribute no literal terms.
            """
            style = style if style is not None else self._match_style("filter")
            for term in self._filter_terms:
                if not term:
                    continue
                try:
                    compiled = re.compile(re.escape(term), re.IGNORECASE)
                except re.error:
                    continue
                text.highlight_regex(compiled, style=style)

        def _build_detail_body(
            self,
            body_text: str,
            query_terms: cabc.Sequence[str],
            match_styles: _DetailMatchStyles | None = None,
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
                highlighted = highlight_matches(
                    body_text,
                    query_terms,
                    case_sensitive=self.query.case_sensitive,
                    regex=self.query.regex,
                    style=match_styles.search if match_styles else self._match_style("search"),
                )
                self._apply_filter_highlight(
                    highlighted,
                    match_styles.filter if match_styles else None,
                )
                result = (highlighted, body_text)
            if cache_key is not None:
                self._detail_body_cache[cache_key] = result
                self._detail_body_cache.move_to_end(cache_key)
                if len(self._detail_body_cache) > self._DETAIL_CACHE_MAX:
                    self._detail_body_cache.popitem(last=False)
            return result

        def _restore_detail_scroll(self, record: SearchRecord) -> None:
            """Open ``record`` at its remembered scroll, or at the top if new.

            A record viewed before restores the position the user left it at; a
            record opened for the first time opens at the top (and is recorded
            at 0 so the next visit is a no-op until the user scrolls).
            """
            if self._detail_scroll is None:
                return
            scroll: t.Any = self._detail_scroll
            key = id(record)
            remembered = self._detail_scroll_positions.get(key)
            scroll.scroll_to(y=remembered if remembered is not None else 0, animate=False)
            if remembered is None:
                self._detail_scroll_positions[key] = 0.0
                self._detail_scroll_positions.move_to_end(key)
                if len(self._detail_scroll_positions) > self._DETAIL_CACHE_MAX:
                    self._detail_scroll_positions.popitem(last=False)

        def _remember_detail_scroll(self) -> None:
            """Save the current detail scroll position for the on-screen record."""
            if self._detail_scroll is None or self._current_detail_record is None:
                return
            key = id(self._current_detail_record)
            self._detail_scroll_positions[key] = float(
                getattr(self._detail_scroll, "scroll_y", 0.0) or 0.0,
            )
            self._detail_scroll_positions.move_to_end(key)

        # --- find-in-detail (the `/` or ctrl+f bar) -----------------------
        def action_open_detail_find(self) -> None:
            """Open the find bar at the bottom of the detail pane.

            Gated: a no-op unless a detail record is loaded (so the bar only
            shows with a detail on screen). Restores the record's remembered
            find query + match cursor, runs the find, and focuses the input.
            """
            record = self._current_detail_record
            if record is None or self._detail_find_input is None:
                return
            self._detail_find_active = True
            find_input = t.cast("t.Any", self._detail_find_input)
            find_input.display = True
            query, match_index, cursor = self._detail_find_state.get(
                id(record),
                ("", 0, 0),
            )
            find_input.load_query(query)
            find_input.cursor_position = min(cursor, len(query))
            self._detail_find_current = match_index
            self._run_detail_find(query, reset_cursor=False)
            find_input.focus()
            self._update_pane_focus()

        def on_detail_find_requested(self, message: DetailFindRequested) -> None:
            """Re-run the find from the first match when the (debounced) query changes."""
            if not self._detail_find_active or self._detail_find_input is None:
                return
            live_text = str(getattr(self._detail_find_input, "value", "") or "")
            if message.text != live_text or message.text == self._detail_find_query:
                return
            self._run_detail_find(message.text, reset_cursor=True)

        def _run_detail_find(self, query: str, *, reset_cursor: bool) -> None:
            """Recompute matches for ``query`` and re-render the highlighted body.

            ``reset_cursor`` jumps to the first match (typing a new query); the
            restore path keeps the remembered match index.
            """
            if self._current_detail_record is None:
                return
            self._detail_find_query = query
            self._detail_find_matches = self._compute_find_matches(
                self._detail_body_text,
                query,
            )
            total = len(self._detail_find_matches)
            if reset_cursor or self._detail_find_current >= total:
                self._detail_find_current = 0
            self._present_detail_find()
            self._scroll_to_current_match()
            self._refresh_detail_statusline()

        def _detail_find_step(self, delta: int) -> None:
            """Move the find cursor to the next (+1) / previous (-1) match, wrapping."""
            total = len(self._detail_find_matches)
            if total == 0:
                return
            self._detail_find_current = (self._detail_find_current + delta) % total
            self._present_detail_find()
            self._scroll_to_current_match()
            self._refresh_detail_statusline()

        @staticmethod
        def _compute_find_matches(body_text: str, query: str) -> list[tuple[int, int]]:
            """Return up to 1000 ``(start, end)`` spans of ``query`` in ``body_text``.

            Case-insensitive literal search (the find bar is a plain substring
            find, not the query language). Capped so a one-character query on a
            huge body can't produce an unbounded match list.
            """
            if not query:
                return []
            try:
                pattern = re.compile(re.escape(query), re.IGNORECASE)
            except re.error:
                return []
            matches: list[tuple[int, int]] = []
            for match in pattern.finditer(body_text):
                matches.append((match.start(), match.end()))
                if len(matches) >= 1000:
                    break
            return matches

        def _present_detail_find(self) -> None:
            """Render the body as text with search/filter/find highlights overlaid.

            While find is active the body renders as highlighted text (search
            gold, filter accent, all find matches purple, the current match
            gold) rather than the format-aware renderable, so matches show
            consistently. Built fresh each time so the body cache stays clean.
            """
            if self._detail is None or self._current_detail_record is None:
                return
            query_terms = list(self.query.terms)
            text = highlight_matches(
                self._detail_body_text,
                query_terms,
                case_sensitive=self.query.case_sensitive,
                regex=self.query.regex,
                style=self._match_style("search"),
            )
            self._apply_filter_highlight(text)
            find_style = self._match_style("find")
            current_style = self._match_style("find-current")
            for index, (start, end) in enumerate(self._detail_find_matches):
                style = current_style if index == self._detail_find_current else find_style
                text.stylize(style, start, end)
            self._detail.update(
                _RichGroup(t.cast("t.Any", self._detail_header_text), t.cast("t.Any", text)),
            )

        def _scroll_to_current_match(self) -> None:
            """Scroll the detail pane so the current find match is near the top.

            The line index is the body match's logical line plus the header's
            line count; with word wrap this is approximate, but the highlight
            marks the exact match once it's on screen.
            """
            if self._detail_scroll is None or not self._detail_find_matches:
                return
            start = self._detail_find_matches[self._detail_find_current][0]
            body_line = self._detail_body_text.count("\n", 0, start)
            header = self._detail_header_text
            header_lines = (
                str(getattr(header, "plain", "")).count("\n") if header is not None else 0
            )
            target = max(0, header_lines + body_line - 2)
            t.cast("t.Any", self._detail_scroll).scroll_to(y=target, animate=False)

        def _reset_detail_find_state(self) -> None:
            """Clear the find state and hide the bar (no re-render, no refocus).

            The pure state half of closing the find — used both by
            :meth:`_close_detail_find` (which adds the re-render + refocus) and by
            :meth:`show_detail` when a record switch happens with the bar open
            (which must not steal focus from the results list driving the switch).
            """
            self._detail_find_active = False
            self._detail_find_query = ""
            self._detail_find_matches = []
            self._detail_find_current = 0
            if self._detail_find_input is not None:
                find_input = t.cast("t.Any", self._detail_find_input)
                find_input.cancel_pending_request()
                find_input.display = False

        def _close_detail_find(self) -> None:
            """Close + cancel the find: save state, drop highlights, restore focus.

            esc / ctrl+c land here. The find query + match cursor are saved to
            per-record memory (so reopening restores them), the body re-renders
            without find highlights at the current scroll, and focus returns to
            the detail scroll.
            """
            self._remember_detail_find()
            # Keep the find's scroll position as the record's remembered scroll
            # so the non-find re-render below doesn't jump away from the match.
            self._remember_detail_scroll()
            self._reset_detail_find_state()
            record = self._current_detail_record
            if record is not None:
                # Re-render via show_detail so a large uncached body offloads to a
                # worker instead of building inline on the pump (ADR 0011 NB-9),
                # and the match-style snapshot contract is honored. The scroll
                # was just remembered, so show_detail's restore won't jump.
                self.show_detail(record)
            self._focus_widget_by_id("detail-scroll")
            self._update_pane_focus()

        def _remember_detail_find(self) -> None:
            """Save the find query + match cursor for the on-screen record (LRU)."""
            record = self._current_detail_record
            if record is None or self._detail_find_input is None:
                return
            key = id(record)
            # Save the input's live value (the debounced _detail_find_query may
            # lag a pending keystroke); restore clamps the cursor to its matches.
            query = str(getattr(self._detail_find_input, "value", "") or "")
            cursor = int(getattr(self._detail_find_input, "cursor_position", 0) or 0)
            self._detail_find_state[key] = (query, self._detail_find_current, cursor)
            self._detail_find_state.move_to_end(key)
            if len(self._detail_find_state) > self._DETAIL_CACHE_MAX:
                self._detail_find_state.popitem(last=False)

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
            if self._results_header is not None:
                # A width change with constant fraction must still repaint the
                # folded bar (its gap/cap math depends on the new width).
                self._results_header.set_narrow(self._statusline_narrow())
                self._results_header.invalidate()
            # Crossing the split breakpoint moves the detail pane between
            # the right side and the bottom.
            self._apply_responsive_layout()

        def action_stop_search(self) -> None:
            """``Esc``: cooperative early-exit of the worker (no-op when finished)."""
            self._cancel_active_action()

        def action_smart_quit(self) -> None:
            """``Ctrl-C`` outside an input: cancel an in-flight action; else quit.

            Inputs intercept ctrl+c first for the staged clear/confirm-exit flow
            (:meth:`_handle_input_ctrl_c`), so this only fires when focus is on a
            non-input widget (results list, detail scroll).
            """
            if self._has_active_actions():
                self._cancel_active_action()
            else:
                self.exit()

        # --- staged ctrl-c in the inputs --------------------------------
        def _handle_input_ctrl_c(self, widget: object) -> None:
            """Staged ctrl-c from a focused input.

            With text, clear the box. On an empty box: the find input closes (its
            "exit" is closing the bar); the search/filter inputs arm a "press
            ctrl-c again to exit" gutter on the first press and quit on a second
            press within the window.
            """
            target = t.cast("t.Any", widget)
            if str(getattr(target, "value", "")):
                target.value = ""
                self._disarm_confirm_exit()
                return
            if widget is self._detail_find_input:
                self._close_detail_find()
                return
            if self._confirm_exit_pending:
                self.exit()
                return
            self._confirm_exit_pending = True
            self._set_ctrlc_gutter("press ctrl-c again to exit")
            if self._confirm_exit_timer is not None:
                t.cast("t.Any", self._confirm_exit_timer).stop()
            self._confirm_exit_timer = self.set_timer(2.0, self._disarm_confirm_exit)

        def _disarm_confirm_exit(self) -> None:
            """Cancel a pending confirm-exit and hide the gutter (idempotent)."""
            if not self._confirm_exit_pending:
                return
            self._confirm_exit_pending = False
            if self._confirm_exit_timer is not None:
                t.cast("t.Any", self._confirm_exit_timer).stop()
                self._confirm_exit_timer = None
            self._set_ctrlc_gutter("")

        def _set_ctrlc_gutter(self, message: str) -> None:
            """Show ``message`` in the bottom gutter, or hide it when empty."""
            if self._ctrlc_gutter is None:
                return
            gutter = t.cast("t.Any", self._ctrlc_gutter)
            gutter.update(message)
            gutter.set_class(bool(message), "-shown")

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
            matcher = self._filter_matcher
            if matcher is None:
                return True
            return bool(matcher.matches(record))

    return AgentGrepApp(
        home=home,
        query=query,
        control=control,
        initial_search_text=initial_search_text,
    )
