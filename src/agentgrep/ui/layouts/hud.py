"""The default heads-up layout: search -> results | detail -> status.

``HudLayout`` is the explorer's default pluggable layout (ADR 0013): a
:class:`~agentgrep.ui.layouts._base.LayoutScreen` that composes the search bar,
the streaming results list, and the detail pane, driven by the active workflow.
It imports Textual at the top but is only reached from inside the factory (and
the tests), so ``import agentgrep`` stays Textual-free (ADR 0010).
"""

from __future__ import annotations

import collections
import contextlib
import dataclasses
import functools
import json
import pathlib
import re
import time
import typing as t
from collections import abc as cabc

from rich.console import Group as _RichGroup
from rich.markdown import Markdown as _RichMarkdown
from rich.syntax import Syntax as _RichSyntax
from rich.text import Text
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Static
from textual.worker import Worker, WorkerCancelled

from agentgrep._engine.orchestration import clear_haystack_cache
from agentgrep._text import (
    DETAIL_BODY_MAX_CHARS,
    DETAIL_BODY_MAX_LINES,
    detect_content_format,
    find_first_match_line,
    format_compact_path,
    format_display_path,
    truncate_lines,
)
from agentgrep._types import (
    StaticLike,
    StreamingAppLike,
)
from agentgrep.progress import (
    ProgressSnapshot,
    SearchControl,
    StreamingRecordsBatch,
    StreamingSearchFinished,
    format_match_count,
)
from agentgrep.query import default_registry
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui import _history, _runtime, theme as ui_theme
from agentgrep.ui._context import UiContext
from agentgrep.ui._source_diagnostics import (
    SourceScanFinished,
    SourceScanStarted,
    UiProgressSnapshot,
)
from agentgrep.ui.completion import (
    QuerySuggester,
    apply_enum_choice,
    apply_word_choice,
    keyword_completion_candidates,
)
from agentgrep.ui.format import scroll_percent
from agentgrep.ui.highlighter import QueryHighlighter
from agentgrep.ui.layouts._base import LayoutScreen
from agentgrep.ui.widgets import (
    CompletionDropdown,
    DetailFindInput,
    DetailFindRequested,
    DetailFocusRequested,
    DetailScroll,
    DetailScrollChanged,
    FilterCompleted,
    FilterHeader,
    FilterInput,
    FilterRequested,
    HistoryRecall,
    PaneHeader,
    ResultHighlighted,
    ResultsHeader,
    ResultsScrollChanged,
    SearchingPanel,
    SearchInput,
    SearchRequested,
    SearchResultsList,
    SlowSourceDiagnosticsRow,
)

if t.TYPE_CHECKING:
    from agentgrep._engine.matching import CompiledRecordMatcher
    from agentgrep.ui.workflows import Workflow


class _DetailMatchStyles(t.NamedTuple):
    """Rich styles resolved on the pump before optional detail offload."""

    search: str
    filter: str


_DetailFindBaseKey = tuple[str, tuple[str, ...], bool, bool, tuple[str, ...]]
_DetailCacheKey = tuple[int, tuple[str, ...], bool, bool, tuple[str, ...]]
_DetailBody = tuple[object, str]
_DETAIL_RICH_FORMAT_MAX_CHARS = 2048
_DETAIL_HIGHLIGHT_MAX_TERMS = 32
_DETAIL_HIGHLIGHT_MAX_MATCHES = 256
_DETAIL_HIGHLIGHT_MAX_TERM_CHARS = 256
_DETAIL_HIGHLIGHT_MAX_TOTAL_TERM_CHARS = 2048
_STREAM_FILTER_MAX_TEXT_CHARS = 2 << 20
_RichSyntaxType = _RichSyntax


def _bounded_literal_terms(
    terms: cabc.Sequence[str],
    *,
    case_sensitive: bool,
) -> tuple[str, ...]:
    """Deduplicate decorative literal terms within a fixed presentation budget."""
    result: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    for term in terms:
        if not term or len(term) > _DETAIL_HIGHLIGHT_MAX_TERM_CHARS:
            continue
        key = term if case_sensitive else term.casefold()
        if key in seen:
            continue
        if total_chars + len(term) > _DETAIL_HIGHLIGHT_MAX_TOTAL_TERM_CHARS:
            break
        seen.add(key)
        result.append(term)
        total_chars += len(term)
        if len(result) >= _DETAIL_HIGHLIGHT_MAX_TERMS:
            break
    return tuple(result)


def _stream_filter_chunks(
    records: cabc.Sequence[SearchRecord],
    *,
    max_records: int,
    max_chars: int,
) -> cabc.Iterator[tuple[SearchRecord, ...]]:
    """Yield record slices bounded by count and projected body characters."""
    chunk: list[SearchRecord] = []
    chunk_chars = 0
    for record in records:
        record_chars = min(len(record.text), max_chars)
        if chunk and (len(chunk) >= max_records or chunk_chars + record_chars > max_chars):
            yield tuple(chunk)
            chunk = []
            chunk_chars = 0
        chunk.append(record)
        chunk_chars += record_chars
    if chunk:
        yield tuple(chunk)


def _apply_bounded_literal_highlights(
    text: Text,
    source: str,
    terms: cabc.Sequence[str],
    *,
    case_sensitive: bool,
    style: str,
) -> None:
    """Apply a bounded number of literal match spans to ``text``."""
    flags = 0 if case_sensitive else re.IGNORECASE
    remaining = _DETAIL_HIGHLIGHT_MAX_MATCHES
    for term in _bounded_literal_terms(terms, case_sensitive=case_sensitive):
        compiled = re.compile(re.escape(term), flags)
        for match in compiled.finditer(source):
            text.stylize(style, match.start(), match.end())
            remaining -= 1
            if remaining == 0:
                return


def _json_pretty_print_is_bounded(text: str) -> bool:
    """Return whether two-space indentation has a conservative output budget."""
    depth = 0
    max_depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            max_depth = max(max_depth, depth)
        elif char in "]}":
            depth = max(0, depth - 1)
    estimated_max = len(text) * (2 * max_depth + 3)
    return estimated_max <= DETAIL_BODY_MAX_CHARS


class HudLayout(LayoutScreen):
    """Search box, streaming results list, detail pane, and status chrome."""

    ZOOM_ARGUMENT_HINT: t.ClassVar[str] = "[results|detail]"

    # ``priority=True`` on the directional ``ctrl+hjkl`` bindings pushes
    # them into Textual's priority dispatch lane so they win over any
    # widget binding for the same key (e.g. ``Input``'s readline
    # ``ctrl+k`` = kill-to-end-of-line). Trade-off accepted per user
    # request: filter loses ``ctrl+k``; ``ctrl+u`` and ``ctrl+w`` are
    # untouched and remain readline-compatible.
    BINDINGS: t.ClassVar[list[BindingType]] = [
        ("tab", "app.focus_next", "Switch focus"),
        ("q", "app.quit", "Quit"),
        ("escape", "stop_search", "Stop search"),
        ("ctrl+backslash", "toggle_detail_progress", "Detail"),
        ("ctrl+c", "smart_quit", "Stop / Quit"),
        # Priority so the focused search Input cannot intercept recall.
        Binding("ctrl+r", "recall_history", "History", priority=True),
        Binding("ctrl+h", "focus_pane_left", "← Pane", priority=True),
        Binding("ctrl+j", "focus_pane_down", "↓ Pane", priority=True),
        Binding("ctrl+k", "focus_pane_up", "↑ Pane", priority=True),
        Binding("ctrl+l", "focus_pane_right", "→ Pane", priority=True),
        # Terminal-alias fallback: many terminals (and tmux without
        # ``xterm-keys on``) send 0x08 for both Backspace and Ctrl-H, so
        # Textual sees ``key="backspace"``, never ``ctrl+h``. NO priority
        # here — the filter input's own backspace handler (delete prev
        # char) must keep winning inside the input. In panes nothing
        # else binds backspace, so this fires.
        Binding("backspace", "focus_pane_left", "", show=False),
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

    # Body width (cells) below which the detail pane moves from the
    # right (side-by-side) to the bottom (stacked) — each side wants
    # ~50 cells to stay readable. Distinct from the statusline
    # breakpoint above, which measures the results column alone.
    _SPLIT_BREAKPOINT: t.ClassVar[int] = 100

    def __init__(self, ctx: UiContext, workflow: Workflow) -> None:
        super().__init__(ctx, workflow)
        self.home = ctx.home
        self.search_query = ctx.query
        # The user's launch discovery scope. A ``scope:`` predicate
        # widens the per-search scope to "all"; this stable base is what
        # a search without a ``scope:`` predicate reverts to, so the
        # widening never persists across searches.
        self._user_scope = ctx.base_scope
        self.control = ctx.control
        self._invoker = ctx.invoker
        self.initial_search_text: str | None = ctx.initial_search_text
        self.all_records = []
        self.filtered_records = []
        self._search_emit: cabc.Callable[[object], None] | None = None
        self._search_done = False
        self._started_at: float | None = None
        self._last_snapshot: ProgressSnapshot | None = None
        self._active_source_snapshots: dict[int, ProgressSnapshot] = {}
        self._searching_panel: SearchingPanel | None = None
        # Persisted search-input history (agentgrep's only self-written state —
        # under XDG_STATE_HOME, never a searched store). The factory loads the
        # snapshot before Textual starts; the recall modal only reads memory.
        self._history_disabled = ctx.history_disabled
        self._history_path = _history.history_path(self.home)
        self._history = list(ctx.history)
        self._last_recorded_text = self._history[0].text if self._history else ""
        self._results: SearchResultsList | None = None
        self._detail: StaticLike | None = None
        self._detail_row: SlowSourceDiagnosticsRow | None = None
        self._chrome_generation: int = 0
        self._last_detail_text: str = ""
        self._last_right_text: str = ""
        self._detail_visible: bool = False
        self._detail_statusline: StaticLike | None = None
        self._filter_input: FilterInput | None = None
        self._search_input: SearchInput | None = None
        # One registry-backed suggester drives the inline ghost text on
        # both inputs; completion offers query-language keywords only.
        self._completion_suggester = QuerySuggester(default_registry())
        # One highlighter syntax-colors the typed query on both inputs.
        self._query_highlighter = QueryHighlighter()
        self._enum_values: tuple[str, ...] = ()
        self._filter_dropdown: t.Any = None
        self._filter_dropdown_values: tuple[str, ...] = ()
        # Compiled record matcher for the current (query-aware) filter
        # text; ``None`` means no active filter (all records pass).
        self._filter_matcher: CompiledRecordMatcher | None = None
        self._filter_generation = 0
        self._records_generation = 0
        self._resize_debounce_timer: object | None = None
        self._current_detail_record: SearchRecord | None = None
        self._detail_scroll: t.Any = None
        self._body: t.Any = None
        self._detail_column: t.Any = None
        self._filter_header: t.Any = None
        self._results_header: t.Any = None
        self._detail_header: t.Any = None
        # Responsive split: True when the detail pane is stacked
        # below the results rather than beside them. ``_detail_opened``
        # is the tig-style "user selected a row" gate that reveals the
        # stacked detail; programmatic filter highlights must not trip it.
        self._stacked: bool = False
        self._detail_opened: bool = False
        self._zoomed_pane: t.Literal["results", "detail"] | None = None
        self._last_content_pane: t.Literal["results", "detail"] = "results"
        # Literal terms of the active filter, highlighted in the detail
        # pane in a distinct color from the search-query terms.
        self._filter_terms: tuple[str, ...] = ()
        # LRU caches for detail-pane work. Keyed by
        # ``(id(record), query.terms, case_sensitive, regex, filter.terms)``
        # — the attributes that determine the rendered body and the
        # highlighted match line. Bounded so a long browsing session
        # can't grow them without limit.
        self._detail_body_cache: collections.OrderedDict[
            _DetailCacheKey,
            tuple[SearchRecord, object, str],
        ] = collections.OrderedDict()
        self._detail_build_generation = 0
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
        # The text the detail body is actually DISPLAYED as — the pretty-
        # printed JSON for json bodies, the raw body otherwise. Find matches
        # and scroll work against this so offsets line up with what is shown.
        self._detail_find_source: str = ""
        self._detail_find_json_syntax = False
        # Cached syntax+search+filter find body; the find-match overlay changes
        # per keystroke but this base does not, so it is built once per
        # highlight state and copied (invalidated in _present_detail).
        self._detail_find_base: Text | None = None
        self._detail_find_base_key: _DetailFindBaseKey | None = None
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

    @_runtime.pump_only
    def _on_theme_changed(self, _theme: object) -> None:
        """Rebuild Rich-baked surfaces when the palette switches.

        The chrome recolors automatically through TCSS, but the results
        rows and the detail body bake concrete hex into Rich renderables at
        build time, so they are rebuilt against the new theme's tokens. The
        detail caches are dropped so the rebuild reads fresh colors.
        """
        if not self.is_mounted:
            return
        results = self._results
        if results is not None:
            results.refresh_theme()
        if self._filter_header is not None:
            self._filter_header.refresh_theme()
        if self._searching_panel is not None:
            self._searching_panel.refresh_theme()
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
            initial_search = " ".join(self.search_query.terms) if self.search_query.terms else ""
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
        with Horizontal(id="body", classes=body_classes):
            with Vertical(id="results-column"):
                # The two rules name the content directly beneath them. Search
                # lifecycle state stays on the filter rule; result navigation
                # stays on the results rule so the two never compete for space.
                yield FilterHeader("filter", id="filter-header")
                yield SlowSourceDiagnosticsRow(id="status-detail")
                yield FilterInput(
                    placeholder="Filter loaded results",
                    id="filter",
                    suggester=self._completion_suggester,
                    highlighter=self._query_highlighter,
                )
                yield ResultsHeader("results", id="results-header")
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
                yield Static(
                    "try a search to begin\n\n"
                    "agent:claude   model:gpt*   role:user\n"
                    'timestamp:>2026-01-01   "exact phrase"',
                    id="empty-hint",
                )
                # Shown only while a search runs before its first result
                # (CSS hides it otherwise): a centered spinner + phase verb
                # + counts + elapsed, collapsed to the results list the
                # moment records arrive.
                yield SearchingPanel(id="searching-panel")
            with Vertical(id="detail-column", classes=detail_classes):
                yield PaneHeader("detail", id="detail-header")
                with DetailScroll(id="detail-scroll"):
                    yield Static("", id="detail")
                # Find-in-detail bar: hidden until `/` or ctrl+f opens it
                # (only with a record loaded); separate from #search/#filter.
                yield DetailFindInput(placeholder="Find in detail", id="detail-find")
                yield Static("", id="detail-statusline", markup=False)
        yield Footer()
        # Transient gutter for the "press ctrl-c again to exit" confirm; a
        # flash-layer Static that overlays the footer only while shown.
        yield Static("", id="ctrlc-gutter")

    def on_mount(self) -> None:
        """Cache widget references, start the worker, and seed the chrome."""
        streaming = t.cast("StreamingAppLike", t.cast("object", self))
        self._results = t.cast(
            "SearchResultsList",
            streaming.query_one("#results"),
        )
        self._detail = t.cast(
            "StaticLike",
            streaming.query_one("#detail", Static),
        )
        self._detail_scroll = streaming.query_one("#detail-scroll")
        self._body = streaming.query_one("#body")
        self._detail_column = streaming.query_one("#detail-column")
        self._filter_header = t.cast(
            "FilterHeader",
            streaming.query_one("#filter-header"),
        )
        self._results_header = t.cast(
            "ResultsHeader",
            streaming.query_one("#results-header"),
        )
        self._searching_panel = t.cast(
            "SearchingPanel",
            streaming.query_one("#searching-panel"),
        )
        self._detail_header = streaming.query_one("#detail-header")
        self._detail_row = t.cast(
            "SlowSourceDiagnosticsRow",
            streaming.query_one("#status-detail", SlowSourceDiagnosticsRow),
        )
        self._detail_statusline = t.cast(
            "StaticLike",
            streaming.query_one("#detail-statusline", Static),
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
        self._search_emit = self._make_gated_emit()
        # Rebuild Rich-baked rows/detail when the active color palette changes.
        # The pump-thread bind and watchdog are owned by the App shell (it owns
        # the pump).
        self.app.theme_changed_signal.subscribe(self, self._on_theme_changed)
        self._apply_responsive_layout()
        # Attach the workflow (base.on_mount): it seeds the initial dispatch —
        # a launch search or the idle bare canvas — now that the widgets exist.
        super().on_mount()
        # Focus the filter when a search is running, else the search box so the
        # user can start typing immediately.
        if not self._search_done:
            self._filter_input.focus()
        else:
            self._search_input.focus()
        self._update_pane_focus()

    def _set_empty_state(self, *, empty: bool) -> None:
        """Toggle the pre-search bare-canvas state on ``#body``.

        Compatibility shim over :meth:`_set_results_view`: ``empty`` is the
        pre-search bare canvas; not-empty reveals the results chrome. The
        search flow uses ``_set_results_view`` directly for the intermediate
        ``searching`` view.
        """
        self._set_results_view("empty" if empty else "results")

    def _set_results_view(self, view: str) -> None:
        """Switch the results region between empty / searching / results.

        ``empty`` is the pre-search bare canvas (centered ``#empty-hint``);
        ``searching`` is the centered ``#searching-panel`` shown while a
        search runs before any result arrives; ``results`` reveals the
        header rule, filter, and list. Mutually-exclusive ``-empty`` /
        ``-searching`` classes on ``#body`` drive the CSS. The panel's
        spinner timer is stopped whenever the region leaves the searching
        view; its ``begin`` is armed by the search flow on entry.
        """
        if view in {"empty", "searching"} and self._zoomed_pane == "detail":
            self.handle_minimize_command()
        if self._body is not None:
            body = t.cast("t.Any", self._body)
            body.set_class(view == "empty", "-empty")
            body.set_class(view == "searching", "-searching")
        if view != "searching" and self._searching_panel is not None:
            self._searching_panel.go_idle()

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

        Bound to the focused *widget*, not the column: the filter and results
        rules light independently, the detail header tracks detail scroll/find,
        and the top search bar lights none of them.
        """
        if not self.is_mounted:
            # Teardown / between screens: nothing to recolor.
            return
        focused_id = getattr(self.focused, "id", None)
        filter_active = focused_id == "filter"
        results_active = focused_id == "results"
        detail_active = focused_id in {"detail-scroll", "detail-find"}
        if filter_active or results_active:
            self._last_content_pane = "results"
        elif detail_active:
            self._last_content_pane = "detail"
        if self._filter_header is not None:
            t.cast("t.Any", self._filter_header).set_class(filter_active, "-active")
        if self._results_header is not None:
            t.cast("t.Any", self._results_header).set_class(results_active, "-active")
        if self._detail_header is not None:
            t.cast("t.Any", self._detail_header).set_class(detail_active, "-active")

    def _start_search_worker(self, query: SearchQuery) -> None:
        """Reset chrome and spawn a new search worker for ``query``.

        ``exclusive=True`` with ``group="search"`` makes Textual cancel
        any prior in-flight search worker before this one runs, which
        is the canonical Textual pattern for "fire a backend search on
        every debounced keystroke without piling up cancellations."
        """
        self.search_query = query
        self._reset_search_chrome()
        # A search is starting — give the empty canvas its centered
        # "searching" moment; the first record batch collapses it to the
        # results list and the folded header rule carries the phase there.
        self._set_results_view("searching")
        self._set_search_rule_state("searching")
        if self._filter_header is not None:
            self._filter_header.begin()
        if self._searching_panel is not None:
            self._searching_panel.begin()
        if self._detail_row is not None:
            self._detail_row.begin()
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

        Swap ``self.control`` for a fresh :class:`SearchControl`;
        callers that replace or clear a running search must signal the
        old control first so the new worker starts with a clean slate.
        """
        self.control = SearchControl()
        self._filter_generation += 1
        self._records_generation += 1
        self._detail_build_generation += 1
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
        self._active_source_snapshots.clear()
        self._current_detail_record = None
        # A fresh search re-collapses the stacked detail pane until
        # the user selects a row again.
        self._detail_opened = False
        if self._results is not None:
            self._results.clear()
        self._apply_responsive_layout()
        if self._detail is not None:
            self._detail.update("")
        if self._detail_statusline is not None:
            self._detail_statusline.update("")
        self._last_detail_text = ""
        self._last_right_text = ""
        if self._results_header is not None:
            self._results_header.set_right("")
        # The filter header carries the search status; clear it back
        # to the plain rule (``_start_search_worker`` re-activates it).
        if self._filter_header is not None:
            self._filter_header.go_idle()
        if self._searching_panel is not None:
            self._searching_panel.go_idle()
        self._set_search_rule_state("")
        # ``_detail_visible`` is deliberately NOT reset — the Ctrl-\
        # toggle is sticky for the session; only the row's stale
        # content is wiped.
        if self._detail_row is not None:
            self._detail_row.go_idle()
        self._search_emit = self._make_gated_emit()

    def _make_gated_emit(self) -> cabc.Callable[[object], None]:
        """Build a worker-thread emit callback whose events die with its generation.

        ``call_from_thread`` schedules the callback directly on the
        event loop rather than enqueuing a ``Message`` — so
        high-frequency record batches don't compete with keystroke /
        timer events for FIFO message dispatch. This is the canonical
        Textual pattern for "many small updates from a worker thread."

        Each reporter captures the chrome generation current at its
        creation. A cancelled worker keeps emitting through its old
        reporter while it drains; :meth:`_apply_streaming_event`
        re-checks the generation on the main thread, so those events
        can never repaint the new search's chrome (stale "Stopped",
        source, or heartbeat state) no matter when they were queued.
        """
        self._chrome_generation += 1
        generation = self._chrome_generation
        # The emitter runs on the worker thread; the generation check
        # happens on the pump inside _apply_streaming_event. Centralizing it
        # in make_gated_emitter keeps results off the message bus (NB-3) and
        # carrying the generation token (NB-10).
        return _runtime.make_gated_emitter(
            self.app.call_from_thread,
            self._apply_streaming_event,
            generation,
        )

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
        elif isinstance(event, UiProgressSnapshot):
            if self._detail_row is not None:
                self._detail_row.set_lifecycle(event.lifecycle)
            self._apply_source_progress(event)
        elif isinstance(event, ProgressSnapshot):
            self._apply_progress(event)
        elif isinstance(event, StreamingSearchFinished):
            self._apply_finished(
                event.outcome,
                event.total,
                event.elapsed,
                str(event.error) if event.error else None,
            )

    @_runtime.pump_only
    def on_input_changed(self, event: object) -> None:
        """Refresh the relevant completion dropdown as an input value changes."""
        source = getattr(event, "input", None)
        input_id = getattr(source, "id", None)
        value = str(getattr(event, "value", ""))
        if input_id == "search":
            # Typing clears a lingering unknown-command error border.
            if self._search_input is not None and t.cast(
                "t.Any",
                self._search_input,
            ).has_class("-error"):
                self._set_search_rule_state("")
            self._update_search_dropdown(value)
        elif input_id == "filter":
            self._update_filter_dropdown(value)

    def _update_search_dropdown(self, value: str) -> None:
        """Populate the search dropdown — slash commands, else keyword completion."""
        if self._update_command_completion(value):
            return
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

    @_runtime.pump_only
    def on_option_list_option_selected(self, event: object) -> None:
        """Accept a completion choice — or run a slash command — from the dropdown."""
        option_list = getattr(event, "option_list", None)
        index = int(getattr(event, "option_index", 0) or 0)
        if option_list is self._enum_dropdown:
            if self._select_command_option(event):
                return
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
        target_input.cursor_position = len(target_input.value)
        dropdown.display = False
        target_input.focus()

    @_runtime.offload
    def _run_search(self) -> None:
        emit = self._search_emit
        if emit is None:
            return
        try:
            self._invoker.run(self.search_query, control=self.control, emit=emit)
        except BaseException as exc:
            emit(
                StreamingSearchFinished(
                    outcome="error",
                    total=0,
                    elapsed=0.0,
                    error=exc,
                ),
            )

    @_runtime.pump_only
    def on_search_requested(self, message: SearchRequested) -> None:
        """Primary input submitted: run a slash command, else route to the workflow.

        Leading-slash text that resolves to an exact command runs a handler;
        anything else (including ``/path`` text and empty input) is handed to the
        active workflow, which decides whether to search, filter, or reset.
        """
        text = message.payload.text.strip()
        if self._dispatch_slash_text(text) is not None:
            return
        self._workflow.on_query(self, text)

    # --- WorkflowHost surface: the active workflow drives the layout here -----
    def build_query(self, text: str) -> SearchQuery:
        """Parse ``text`` into a query at the user's launch scope (host surface)."""
        return self._build_search_query(text)

    def run_search(self, query: SearchQuery) -> None:
        """Reset the chrome and stream ``query`` through the engine (host surface)."""
        self._start_search_worker(query)

    def reset_view(self) -> None:
        """Return to the idle bare-canvas state without a search (host surface)."""
        self._reset_search_chrome()
        self._search_done = True
        self._set_empty_state(empty=True)
        self.search_query = self._build_search_query("")

    def record_history(self, text: str) -> None:
        """Persist ``text`` to the search-input history (host surface)."""
        self._record_history(text)

    def request_cancel(self) -> None:
        """Cooperatively signal the in-flight search to wrap up (host surface)."""
        self.control.request_answer_now()

    def _record_history(self, text: str) -> None:
        """Append a submitted, non-empty query to the persisted history.

        Skips a consecutive duplicate of the last recorded query and updates
        the in-memory newest-first snapshot the recall modal reads, so a
        fresh Ctrl-R reflects this search without re-reading the file.
        """
        if self._history_disabled:
            return
        stripped = text.strip()
        if not stripped or stripped == self._last_recorded_text:
            return
        now = time.time()
        dedup_last = self._last_recorded_text
        self._last_recorded_text = stripped
        entry = _history.HistoryEntry(text=stripped, ts=now, scope=self._user_scope)
        self._history = [entry, *(e for e in self._history if e.text != stripped)]
        streaming = t.cast("StreamingAppLike", t.cast("object", self))
        streaming.run_worker(
            functools.partial(
                self._write_history_entry,
                stripped,
                self._user_scope,
                now,
                dedup_last,
            ),
            name="history",
            group="history",
            description="write search history",
            thread=True,
            # Not exclusive: unlike search/filter/detail, a later submit
            # must not cancel an earlier append before it reaches disk.
            exclusive=False,
        )

    @_runtime.offload
    def _write_history_entry(
        self,
        text: str,
        scope: str,
        now: float,
        dedup_last: str,
    ) -> None:
        """Persist one search-history row from a worker thread."""
        _history.append_query(
            self._history_path,
            text,
            scope=scope,
            now=now,
            dedup_last=dedup_last,
        )

    def action_recall_history(self) -> None:
        """``Ctrl-R``: open the search-history recall modal (idempotent)."""
        if isinstance(self.screen, HistoryRecall):
            return
        seed = ""
        if self._search_input is not None:
            seed = str(getattr(self._search_input, "value", "") or "")
        self.app.push_screen(
            HistoryRecall(self._history, seed=seed),
            self._apply_recalled_query,
        )

    def _apply_recalled_query(self, query: str | None) -> None:
        """Fill the search box with a recalled query — never auto-submit.

        agentgrep's search is explicit (Enter dispatches), so recall seeds
        the box and focuses it; the user reviews/edits and presses Enter.
        """
        if not query or self._search_input is None:
            return
        target = t.cast("t.Any", self._search_input)
        target.load_query(query)
        target.focus()

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
        base = dataclasses.replace(self.search_query, scope=self._user_scope)
        result = build_query_from_input(text, base, default_registry())
        if result.query is not None:
            return result.query
        # Parse / compile error: degrade to legacy split so the
        # search box stays editable. The error message stays
        # accessible on the result for future UI surfacing.
        terms = tuple(text.split()) if text else ()
        return SearchQuery(
            terms=terms,
            scope=self._user_scope,
            any_term=self.search_query.any_term,
            regex=self.search_query.regex,
            case_sensitive=self.search_query.case_sensitive,
            agents=self.search_query.agents,
            limit=self.search_query.limit,
            dedupe=self.search_query.dedupe,
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
        filter_generation = self._filter_generation
        filter_matcher = self._filter_matcher
        if records:
            self.all_records.extend(records)
            self._records_generation += 1
        # Results are arriving — collapse the centered searching panel to
        # the results list (idempotent; a batch driven directly, e.g. in
        # tests, switches here too).
        self._set_results_view("results")
        if records and self._results is not None:
            results = self._results

            def _append_chunk(chunk: cabc.Sequence[SearchRecord]) -> None:
                if filter_generation != self._filter_generation:
                    return
                results.append_records(chunk)
                if not results.uses_records(self.filtered_records):
                    self.filtered_records.extend(chunk)

            if filter_matcher is None:
                await _runtime.stream_apply(
                    records,
                    _append_chunk,
                    chunk_size=self._APPLY_CHUNK_SIZE,
                )
            else:
                streaming = t.cast("StreamingAppLike", t.cast("object", self))
                for record_chunk in _stream_filter_chunks(
                    records,
                    max_records=self._APPLY_CHUNK_SIZE,
                    max_chars=_STREAM_FILTER_MAX_TEXT_CHARS,
                ):
                    worker = t.cast(
                        "Worker[tuple[SearchRecord, ...]]",
                        streaming.run_worker(
                            functools.partial(
                                self._match_stream_chunk,
                                filter_matcher,
                                record_chunk,
                            ),
                            name="stream filter",
                            group="stream-filter",
                            description="match streamed records",
                            thread=True,
                            exclusive=True,
                        ),
                    )
                    try:
                        matching = await worker.wait()
                    except WorkerCancelled:
                        return
                    if filter_generation != self._filter_generation:
                        return
                    await _runtime.stream_apply(
                        matching,
                        _append_chunk,
                        chunk_size=self._APPLY_CHUNK_SIZE,
                    )
        self._refresh_results_status_right()

    @_runtime.offload
    def _match_stream_chunk(
        self,
        matcher: CompiledRecordMatcher,
        records: tuple[SearchRecord, ...],
    ) -> tuple[SearchRecord, ...]:
        """Project one bounded streaming slice through an active filter."""
        return tuple(record for record in records if matcher.matches(record))

    @_runtime.pump_only
    def _apply_source_progress(self, event: UiProgressSnapshot) -> None:
        """Project one lifecycle snapshot onto a currently active source."""
        lifecycle = event.lifecycle
        if isinstance(lifecycle, SourceScanStarted):
            self._active_source_snapshots[lifecycle.source_id] = event.snapshot
            self._apply_progress(event.snapshot)
            return
        if isinstance(lifecycle, SourceScanFinished):
            self._active_source_snapshots.pop(lifecycle.source_id, None)
        if self._active_source_snapshots:
            source_id = next(reversed(self._active_source_snapshots))
            self._apply_progress(self._active_source_snapshots[source_id])
            return
        self._apply_progress(
            dataclasses.replace(
                event.snapshot,
                current=None,
                total=None,
                detail=None,
                source_records_seen=None,
            ),
        )

    @_runtime.pump_only
    def _apply_progress(self, snapshot: ProgressSnapshot) -> None:
        """Feed active-search chrome via ``call_from_thread``.

        Per-source progress events arrive thousands of times per search; the
        header stores source-local facts without repainting (its 2 Hz spinner
        timer picks them up on the next frame). TUI-private lifecycle markers
        drive the separately sampled detail row. Stale-generation events never
        reach this handler.
        """
        # A search is in progress with no results yet — keep the centered
        # panel up (the batch handler switches to the list on first result).
        if not self.all_records:
            self._set_results_view("searching")
        source_id = snapshot.current
        if snapshot.phase == "scanning" and source_id in self._active_source_snapshots:
            self._active_source_snapshots.pop(source_id)
            self._active_source_snapshots[source_id] = snapshot
        self._last_snapshot = snapshot
        if self._started_at is None:
            self._started_at = time.monotonic()
        if self._searching_panel is not None:
            self._searching_panel.set_snapshot(snapshot)
        if self._filter_header is not None:
            self._filter_header.set_snapshot(snapshot)

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

    @_runtime.pump_only
    def handle_maximize_command(self, argument: str) -> bool:
        """Toggle or select a logical results/detail column zoom."""
        target = argument.strip().lower()
        if not target:
            if self._zoomed_pane is not None:
                return self.handle_minimize_command()
            target = self._last_content_pane
        if target not in {"results", "detail"}:
            self.notify(
                "Maximize target must be results or detail.",
                title="Maximize",
                severity="warning",
            )
            return False
        if target == "detail":
            record = self._record_for_detail_focus()
            if record is None:
                self.notify(
                    "No detail is available to maximize.",
                    title="Maximize",
                    severity="warning",
                )
                return False
            self.show_detail(record)
        zoomed: t.Literal["results", "detail"] = "detail" if target == "detail" else "results"
        self._set_zoomed_pane(zoomed)
        return True

    @_runtime.pump_only
    def handle_minimize_command(self) -> bool:
        """Restore the responsive results/detail split without moving focus."""
        self._zoomed_pane = None
        if self._body is not None:
            body = t.cast("t.Any", self._body)
            body.remove_class("-zoom-results", "-zoom-detail")
        self._apply_responsive_layout()
        return True

    @_runtime.pump_only
    def action_toggle_detail_progress(self) -> None:
        r"""``Ctrl-\``: show/hide actionable search detail (sticky)."""
        self._detail_visible = not self._detail_visible
        if self._detail_row is None:
            return
        self._detail_row.set_expanded(self._detail_visible)

    @_runtime.pump_only
    def _apply_finished(
        self,
        outcome: str,
        total: int,
        elapsed: float,
        error_message: str | None,
    ) -> None:
        r"""Freeze the header chrome — invoked via ``call_from_thread``.

        The header's spinner timer stops and the terminal outcome holds; the
        elapsed total is folded into the summary string the ctrl+\ detail row
        shows, not a live-ticking widget.
        """
        # A search ran — show its outcome. With results, collapse to the
        # list; with none, keep the centered panel and freeze it into its
        # terminal state instead of revealing an empty list.
        self._search_done = True
        if self.all_records:
            self._set_results_view("results")
        elif self._searching_panel is not None:
            self._set_results_view("searching")
            self._searching_panel.freeze(
                outcome,
                total=total,
                elapsed=elapsed,
                message=error_message or "",
            )
        else:
            self._set_results_view("results")
        if outcome == "error":
            summary = f"Search failed: {error_message}"
        elif outcome == "interrupted":
            source_label = self._scanning_source_label()
            source_summary = f" while scanning source {source_label}" if source_label else ""
            summary = f"Stopped at {format_match_count(total)}{source_summary} in {elapsed:.1f}s"
        else:
            summary = f"Search complete: {format_match_count(total)} in {elapsed:.1f}s"
        # Freeze the filter header to bounded text; the full summary lives in
        # the ctrl+\ row while result navigation remains on the results rule.
        if self._filter_header is not None:
            self._filter_header.freeze(outcome, message=error_message or "")
        self._set_search_rule_state(outcome)
        detail = summary
        if self._detail_row is not None:
            detail = self._detail_row.freeze(summary, now=time.monotonic())
        self._last_detail_text = detail
        # Recompute the right slot so the terminal match count is current.
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

    def _scanning_source_label(self) -> str | None:
        """Return a source ordinal only when the last event was a scan."""
        snap = self._last_snapshot
        if snap is None or snap.phase != "scanning" or snap.current is None or snap.total is None:
            return None
        return f"{snap.current} of {snap.total}"

    def on_filter_requested(self, message: FilterRequested) -> None:
        """Narrow the loaded records when the #filter box changes."""
        self.filter_loaded(message.payload.text)

    def filter_loaded(self, text: str) -> None:
        """Recompute the in-memory filter on a worker (host surface).

        ``exclusive`` cancels any in-flight filter; the same matcher is reused
        for streaming records so a live search stays query-aware (NB-6).
        """
        matcher = self._build_filter_matcher(text)
        # Streaming records use the same matcher so a live search keeps the
        # filtered list query-aware as records arrive.
        self._filter_matcher = matcher
        # The filter's literal terms get highlighted in the detail pane in
        # a distinct color from the search-query terms.
        self._filter_terms = tuple(matcher.query.terms) if matcher is not None else ()
        self._filter_generation += 1
        generation = self._filter_generation
        records_generation = self._records_generation
        records = tuple(self.all_records)
        streaming = t.cast("StreamingAppLike", t.cast("object", self))
        streaming.run_worker(
            functools.partial(
                self._run_filter_worker,
                text,
                matcher,
                records,
                generation,
                records_generation,
            ),
            name="filter",
            group="filter",
            description="filter loaded records",
            thread=True,
            exclusive=True,
        )

    def _build_filter_matcher(self, text: str) -> CompiledRecordMatcher | None:
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
            agents=self.search_query.agents,
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
                agents=self.search_query.agents,
                limit=None,
            )
        return compile_record_matcher(query)

    @_runtime.offload
    def _run_filter_worker(
        self,
        text: str,
        matcher: CompiledRecordMatcher | None,
        records: tuple[SearchRecord, ...],
        generation: int,
        records_generation: int,
    ) -> None:
        """Compute the filtered list on a background thread; post a ``FilterCompleted``.

        Match a pump-owned immutable snapshot. The pump advances the records
        generation on every mutation, so a snapshot superseded by a streamed
        batch is discarded and retried in :meth:`on_filter_completed`.
        """
        if matcher is None:
            matching = list(records)
        else:
            matching = [record for record in records if matcher.matches(record)]
        record_ids = {id(record) for record in matching}
        streaming = t.cast("StreamingAppLike", t.cast("object", self))
        streaming.post_message(
            FilterCompleted(
                text=text,
                records=matching,
                record_ids=record_ids,
                generation=generation,
                records_generation=records_generation,
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
        if message.generation != self._filter_generation:
            return
        if self._filter_input is not None and message.text != self._filter_input.value:
            return
        if message.records_generation != self._records_generation:
            self.filter_loaded(message.text)
            return
        self.filtered_records = message.records
        if self._results is not None:
            self._results.set_records(
                message.records,
                record_ids=message.record_ids,
            )
            self._refresh_results_status_right()
        if self._detail is not None:
            if self.filtered_records:
                highlighted = self._results.highlighted if self._results is not None else None
                row_index = highlighted if highlighted is not None else 0
                record = self.filtered_records[row_index]
                if record is not self._current_detail_record:
                    self.show_detail(record)
            else:
                self._detail.update(
                    "No results." if self._search_done else "No matches yet.",
                )
        # Empty results collapse the stacked detail; a populated list
        # keeps whatever open state the user already chose.
        self._apply_responsive_layout()

    @_runtime.pump_only
    def on_result_highlighted(self, message: ResultHighlighted) -> None:
        """Update the detail pane and footer on a result cursor move.

        Guards against the redundant re-render that fires when
        a queued highlight belongs to a superseded filtered result set.
        """
        row_index = message.index
        results = self._results
        if results is None or message.generation != results.generation:
            self._refresh_results_status_right()
            return
        if not (
            0 <= row_index < len(self.filtered_records)
            and self.filtered_records[row_index] is message.record
        ):
            self._refresh_results_status_right()
            return
        if not message.programmatic:
            # A genuine cursor move: open the stacked detail pane and
            # keep it open for the rest of this result set (tig-style).
            self._detail_opened = True
            self._apply_responsive_layout()
        if message.record is not self._current_detail_record:
            self.show_detail(message.record)
        self._refresh_results_status_right(
            cursor=row_index,
            visible=len(self.filtered_records),
        )

    def on_results_scroll_changed(self, message: ResultsScrollChanged) -> None:
        """Re-render the right side of the results status line.

        Treat the message as an invalidation rather than trusting its snapshot:
        a queued pre-reset event must not repaint stale navigation state.
        """
        self._refresh_results_status_right()

    def on_detail_scroll_changed(self, message: DetailScrollChanged) -> None:
        """Re-render the detail status line and remember the scroll position."""
        self._refresh_detail_statusline(message.percent)
        self._remember_detail_scroll()

    def _refresh_results_status_right(
        self,
        *,
        cursor: int | None = None,
        visible: int | None = None,
        percent: int | None = None,
    ) -> None:
        """Compose the results-status right slot from the most recent state.

        Pulls the cursor position from the results list when no
        explicit values arrive; the change gate keeps repeated
        identical renders from repainting.
        """
        if self._results_header is None:
            return
        if self._results is not None:
            if cursor is None and visible is None:
                cursor = t.cast("int | None", getattr(self._results, "highlighted", None))
                visible = len(self._results._records)
            if percent is None:
                percent = self._results._scroll_percent()
        text = (
            ""
            if not self.all_records and not self._search_done
            else self._format_results_right(cursor, visible, percent=percent)
        )
        if text != self._last_right_text:
            self._last_right_text = text
            self._results_header.set_right(text)

    def _format_results_right(
        self,
        cursor: int | None,
        visible: int | None,
        *,
        percent: int | None = None,
    ) -> str:
        """Render fixed-width item position/count plus list scroll percent.

        Once a cursor exists, its numerator is padded to the denominator width.
        The percentage is padded to three digits. Right-anchoring that stable
        footprint prevents the rule label from moving as either value advances.
        """
        total_matches = len(self.all_records)
        if visible and visible > 0 and cursor is not None:
            digits = len(str(visible))
            position = f"{cursor + 1:>{digits}}/{visible}"
        elif visible is not None:
            position = format_match_count(max(0, visible))
        elif total_matches > 0:
            position = format_match_count(total_matches)
        else:
            return ""
        bounded_percent = max(0, min(100, percent if percent is not None else 100))
        return f"{position}  {bounded_percent:>3}%"

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
            find_text = f"{self._detail_find_current + 1}/{total}  " if total else "no matches  "
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

        The body is truncated to :data:`DETAIL_BODY_MAX_CHARS` characters and
        :data:`DETAIL_BODY_MAX_LINES` lines (the ``VerticalScroll`` wrapper
        handles letting the user scroll within the visible window). The body
        renderable is chosen by
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
        self._detail_build_generation += 1
        detail_generation = self._detail_build_generation
        width = max(20, self._detail.size.width or 80)
        theme_vars = self.app.theme_variables
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
        header = Text(no_wrap=False)
        header_rows: list[tuple[str, str, str]] = [
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
        ]
        if record.origin is not None:
            for label, value in (
                ("Cwd:", record.origin.cwd),
                ("Repo:", record.origin.repo),
                ("Worktree:", record.origin.worktree),
            ):
                if value:
                    header_rows.append(
                        (
                            label,
                            format_display_path(pathlib.Path(value), directory=True),
                            path_color,
                        ),
                    )
            if record.origin.branch:
                header_rows.append(("Branch:", record.origin.branch, dim_color))
            if record.origin.cwd_hash:
                header_rows.append(("Cwd hash:", record.origin.cwd_hash, dim_color))
        for label, value, value_style in header_rows:
            header.append(f"{label} ", style="bold")
            header.append(f"{value}\n", style=value_style)
        header.append("\n")
        body_truncated = truncate_lines(
            record.text,
            DETAIL_BODY_MAX_LINES,
            max_chars=DETAIL_BODY_MAX_CHARS,
        )
        query_terms = tuple(self.search_query.terms)
        case_sensitive = self.search_query.case_sensitive
        regex = self.search_query.regex
        filter_terms = self._filter_terms
        cache_key = self._detail_cache_key(query_terms, record)
        # Keep the header + body text so find-in-detail can re-highlight the
        # body (without rebuilding the header) and scroll to matches.
        self._detail_header_text = header
        self._detail_body_text = body_truncated
        self._detail_find_source = ""
        self._detail_find_json_syntax = False
        match_styles = _DetailMatchStyles(
            search=self._match_style("search"),
            filter=self._match_style("filter"),
        )
        cached = self._cached_detail_body(record, cache_key)
        if cached is not None:
            self._present_detail(
                record,
                header,
                cached,
                query_terms,
                generation=detail_generation,
                cache_key=cache_key,
            )
            return
        json_like = body_truncated.lstrip(" \t\r\n").startswith(("{", "["))
        if len(body_truncated) <= self._DETAIL_ASYNC_BODY_THRESHOLD and not json_like:
            self._present_detail(
                record,
                header,
                self._build_detail_body(
                    body_truncated,
                    query_terms,
                    match_styles,
                    case_sensitive=case_sensitive,
                    regex=regex,
                    filter_terms=filter_terms,
                ),
                query_terms,
                generation=detail_generation,
                cache_key=cache_key,
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
                detail_generation,
                cache_key,
                case_sensitive,
                regex,
                filter_terms,
            ),
            name="detail",
            group="detail",
            description="build detail body",
            thread=True,
            exclusive=True,
        )

    def _detail_body_is_cached(self, query_terms: cabc.Sequence[str]) -> bool:
        """Return whether the detail body for the current record is memoized."""
        record = self._current_detail_record
        cache_key = self._detail_cache_key(query_terms, record)
        return record is not None and self._cached_detail_body(record, cache_key) is not None

    def _cached_detail_body(
        self,
        record: SearchRecord,
        cache_key: _DetailCacheKey | None,
    ) -> _DetailBody | None:
        """Return a retained-record cache hit, rejecting a reused object id."""
        if cache_key is None:
            return None
        cached = self._detail_body_cache.get(cache_key)
        if cached is None:
            return None
        cached_record, renderable, source = cached
        if cached_record is not record:
            del self._detail_body_cache[cache_key]
            return None
        self._detail_body_cache.move_to_end(cache_key)
        return renderable, source

    @_runtime.offload
    def _build_detail_in_thread(
        self,
        record: SearchRecord,
        header: object,
        body_truncated: str,
        query_terms: cabc.Sequence[str],
        match_styles: _DetailMatchStyles,
        generation: int,
        cache_key: _DetailCacheKey | None,
        case_sensitive: bool,
        regex: bool,
        filter_terms: tuple[str, ...],
    ) -> None:
        """Build the detail body off the UI thread, then apply it on the loop."""
        body = self._build_detail_body(
            body_truncated,
            query_terms,
            match_styles,
            case_sensitive=case_sensitive,
            regex=regex,
            filter_terms=filter_terms,
        )
        self.app.call_from_thread(
            functools.partial(
                self._present_detail,
                record,
                header,
                body,
                query_terms,
                generation=generation,
                cache_key=cache_key,
            ),
        )

    @_runtime.pump_only
    def _present_detail(
        self,
        record: SearchRecord,
        header: object,
        body: _DetailBody,
        query_terms: cabc.Sequence[str],
        *,
        generation: int | None = None,
        cache_key: _DetailCacheKey | None = None,
    ) -> None:
        """Render ``body`` into the detail pane unless ``record`` is superseded.

        Runs on the event-loop thread (directly for inline builds, via
        ``call_from_thread`` for off-thread builds); the identity check
        drops a stale build whose record the cursor has already left.
        """
        if (
            self._detail is None
            or self._current_detail_record is not record
            or (generation is not None and generation != self._detail_build_generation)
        ):
            return
        body_renderable, body_for_scroll = body
        if cache_key is not None:
            self._detail_body_cache[cache_key] = (record, body_renderable, body_for_scroll)
            self._detail_body_cache.move_to_end(cache_key)
            if len(self._detail_body_cache) > self._DETAIL_CACHE_MAX:
                self._detail_body_cache.popitem(last=False)
        # The displayed text find searches/scrolls against — formatted JSON
        # for json bodies, the raw body otherwise.
        self._detail_find_source = body_for_scroll
        self._detail_find_json_syntax = isinstance(body_renderable, _RichSyntaxType)
        self._detail_find_base = None  # a fresh body invalidates the find base
        self._detail_find_base_key = None
        self._detail.update(_RichGroup(t.cast("t.Any", header), t.cast("t.Any", body_renderable)))
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
        record: SearchRecord | None = None,
    ) -> _DetailCacheKey | None:
        """Compose the LRU key for the current record + query + filter.

        Returns ``None`` when there is no current record (e.g. detail
        pane invoked before a record is highlighted) so callers know
        to skip the cache entirely. The filter terms are part of the key
        so changing the filter re-renders the filter-term highlights.
        """
        record = record if record is not None else self._current_detail_record
        if record is None:
            return None
        return (
            id(record),
            tuple(query_terms),
            self.search_query.case_sensitive,
            self.search_query.regex,
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
        theme_vars = self.app.theme_variables
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

    def _apply_filter_highlight(
        self,
        text: t.Any,
        style: str | None = None,
        *,
        terms: cabc.Sequence[str] | None = None,
    ) -> None:
        """Overlay the filter's literal terms onto ``text`` in a distinct color.

        Applied after the search-term highlight so filter matches stand out
        separately. Filter matching is case-insensitive, so the highlight is
        too; field predicates contribute no literal terms.
        """
        style = style if style is not None else self._match_style("filter")
        source = str(getattr(text, "plain", ""))
        _apply_bounded_literal_highlights(
            text,
            source,
            self._filter_terms if terms is None else terms,
            case_sensitive=False,
            style=style,
        )

    def _build_detail_body(
        self,
        body_text: str,
        query_terms: cabc.Sequence[str],
        match_styles: _DetailMatchStyles | None = None,
        *,
        case_sensitive: bool | None = None,
        regex: bool | None = None,
        filter_terms: cabc.Sequence[str] | None = None,
    ) -> _DetailBody:
        """Return ``(renderable, body_text_for_match_search)`` for ``body_text``.

        The second tuple element is whatever text the caller's
        ``find_first_match_line`` should scan. For JSON we pretty-print
        and return the formatted text so the line index lines up with
        what the user actually sees rendered. This computation is detached:
        the pump validates its generation and owns the shared LRU.
        """
        effective_case_sensitive = (
            self.search_query.case_sensitive if case_sensitive is None else case_sensitive
        )
        effective_regex = self.search_query.regex if regex is None else regex
        safe_query_terms = (
            ()
            if effective_regex
            else _bounded_literal_terms(
                query_terms,
                case_sensitive=effective_case_sensitive,
            )
        )
        fmt = detect_content_format(body_text)
        result: _DetailBody
        if fmt == "json":
            formatted = body_text
            if _json_pretty_print_is_bounded(body_text):
                with contextlib.suppress(RecursionError, ValueError):
                    formatted = json.dumps(
                        json.loads(body_text),
                        indent=2,
                        ensure_ascii=False,
                    )
            formatted = truncate_lines(
                formatted,
                DETAIL_BODY_MAX_LINES,
                max_chars=DETAIL_BODY_MAX_CHARS,
            )
            match_line = find_first_match_line(
                formatted,
                safe_query_terms,
                case_sensitive=effective_case_sensitive,
                regex=False,
            )
            highlight_lines = {match_line + 1} if match_line is not None else None
            if len(formatted) <= _DETAIL_RICH_FORMAT_MAX_CHARS:
                renderable: object = _RichSyntax(
                    formatted,
                    "json",
                    theme="ansi_dark",
                    word_wrap=True,
                    highlight_lines=highlight_lines,
                )
            else:
                plain = Text(formatted, no_wrap=False)
                _apply_bounded_literal_highlights(
                    plain,
                    formatted,
                    safe_query_terms,
                    case_sensitive=effective_case_sensitive,
                    style=match_styles.search if match_styles else self._match_style("search"),
                )
                self._apply_filter_highlight(
                    plain,
                    match_styles.filter if match_styles else None,
                    terms=filter_terms,
                )
                renderable = plain
            result = (renderable, formatted)
        elif fmt == "markdown":
            if len(body_text) <= _DETAIL_RICH_FORMAT_MAX_CHARS:
                renderable = _RichMarkdown(body_text, code_theme="ansi_dark")
            else:
                plain = Text(body_text, no_wrap=False)
                _apply_bounded_literal_highlights(
                    plain,
                    body_text,
                    safe_query_terms,
                    case_sensitive=effective_case_sensitive,
                    style=match_styles.search if match_styles else self._match_style("search"),
                )
                self._apply_filter_highlight(
                    plain,
                    match_styles.filter if match_styles else None,
                    terms=filter_terms,
                )
                renderable = plain
            result = (renderable, body_text)
        else:
            highlighted = Text(body_text, no_wrap=False)
            _apply_bounded_literal_highlights(
                highlighted,
                body_text,
                safe_query_terms,
                case_sensitive=effective_case_sensitive,
                style=match_styles.search if match_styles else self._match_style("search"),
            )
            self._apply_filter_highlight(
                highlighted,
                match_styles.filter if match_styles else None,
                terms=filter_terms,
            )
            result = (highlighted, body_text)
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
            self._detail_find_source or self._detail_body_text,
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
        """Render the body with search/filter/find highlights overlaid.

        The syntax+search+filter base is cached per render
        (:meth:`_detail_find_base_for`); only the find-match spans are layered
        here, on a copy, so stepping matches never re-tokenizes the body (NB-9).
        """
        if self._detail is None or self._current_detail_record is None:
            return
        source = self._detail_find_source or self._detail_body_text
        text = self._detail_find_base_for(source).copy()
        find_style = self._match_style("find")
        current_style = self._match_style("find-current")
        for index, (start, end) in enumerate(self._detail_find_matches):
            style = current_style if index == self._detail_find_current else find_style
            text.stylize(style, start, end)
        self._detail.update(
            _RichGroup(self._detail_header_text, t.cast("t.Any", text)),
        )

    def _detail_find_base_for(self, source: str) -> Text:
        """Return the syntax+search+filter body for ``source`` and highlight state.

        For JSON the body is syntax-highlighted via :class:`rich.syntax.Syntax`
        so token colors survive find; other formats use ``highlight_matches``.
        The find-match overlay changes per keystroke/step but this base does
        not, so building it once and copying keeps the per-keystroke cost off a
        full-body ``Syntax`` re-highlight. Invalidated in :meth:`_present_detail`.
        """
        key = (
            source,
            tuple(self.search_query.terms),
            self.search_query.case_sensitive,
            self.search_query.regex,
            self._filter_terms,
        )
        cached = self._detail_find_base
        if cached is not None and self._detail_find_base_key == key:
            return cached
        if self._detail_find_json_syntax:
            text = _RichSyntax(source, "json", theme="ansi_dark", word_wrap=True).highlight(source)
            text.no_wrap = False
            self._apply_search_highlight(text)
        else:
            text = Text(source, no_wrap=False)
            _apply_bounded_literal_highlights(
                text,
                source,
                () if self.search_query.regex else self.search_query.terms,
                case_sensitive=self.search_query.case_sensitive,
                style=self._match_style("search"),
            )
        self._apply_filter_highlight(text)
        self._detail_find_base = text
        self._detail_find_base_key = key
        return text

    def _apply_search_highlight(self, text: t.Any) -> None:
        """Overlay the active search-query terms onto ``text`` (for the JSON path).

        The plain-text path bakes these via ``highlight_matches``; on the
        Syntax-highlighted JSON ``Text`` literal terms are layered with the
        same style. Regex terms are omitted because presentation must not
        re-run an untrusted pattern on the message pump.
        """
        if self.search_query.regex:
            return
        _apply_bounded_literal_highlights(
            text,
            str(getattr(text, "plain", "")),
            self.search_query.terms,
            case_sensitive=self.search_query.case_sensitive,
            style=self._match_style("search"),
        )

    def _scroll_to_current_match(self) -> None:
        """Scroll the detail pane so the current find match is near the top.

        Maps the match's character offset to its VISUAL (post-wrap) row so
        it lands on screen even when long lines wrap — a logical newline
        count is wrong under word wrap (a match on logical line 8 can sit at
        visual row 48). Falls back to the logical-line estimate if the wrap
        helper is unavailable.
        """
        if self._detail_scroll is None or not self._detail_find_matches:
            return
        start = self._detail_find_matches[self._detail_find_current][0]
        target = self._match_visual_row(start)
        t.cast("t.Any", self._detail_scroll).scroll_to(y=max(0, target - 2), animate=False)

    def _match_visual_row(self, offset: int) -> int:
        """Return the visual (post-wrap) row of body char ``offset``.

        Uses Rich's own line-divider (the same one Textual wraps with) at the
        Static's rendered content width; falls back to a logical-line count
        if that private helper is unavailable.
        """
        header = self._detail_header_text
        header_text = str(getattr(header, "plain", "")) if header is not None else ""
        body = self._detail_find_source or self._detail_body_text
        width = 0
        if self._detail is not None:
            width = int(getattr(self._detail.content_size, "width", 0) or 0)
        width = max(1, width)
        try:
            return self._wrap_aware_row(offset, width, header_text, body)
        except Exception:
            return header_text.count("\n") + body.count("\n", 0, offset)

    @staticmethod
    def _wrap_aware_row(offset: int, width: int, header_text: str, body: str) -> int:
        """Count header wrapped rows, then body wrapped rows up to ``offset``."""
        from rich._wrap import divide_line

        def rows(line: str) -> int:
            return len(divide_line(line, width)) + 1

        row = sum(rows(line) for line in header_text.split("\n"))
        pos = 0
        for line in body.split("\n"):
            if pos + len(line) >= offset:
                col = offset - pos
                return row + sum(1 for brk in divide_line(line, width) if brk <= col)
            row += rows(line)
            pos += len(line) + 1
        return row

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
        # Recompute (not just repaint) because the result viewport's new height
        # can change max_scroll_y and therefore the displayed percentage.
        self._refresh_results_status_right()
        if self._filter_header is not None:
            # Width selects a whole active-status variant, so repaint even when
            # the stored facts are stable.
            self._filter_header.invalidate()
        # Crossing the split breakpoint moves the detail pane between
        # the right side and the bottom.
        self._apply_responsive_layout()

    def action_stop_search(self) -> None:
        """``Esc``: cooperative early-exit of the worker (no-op when finished)."""
        self._cancel_active_action()

    def action_smart_quit(self) -> None:
        """``Ctrl-C`` outside an input: cancel an in-flight action; else stage exit.

        Inputs intercept ctrl+c first for the staged clear/confirm-exit flow
        (:meth:`_handle_input_ctrl_c`); this fires when focus is on a non-input
        widget (results list, detail scroll). With an action in flight the first
        press cancels it; otherwise it arms the same "press ctrl-c again to exit"
        gutter as the inputs, so the warning shows whichever pane holds focus.
        """
        if self._has_active_actions():
            self._cancel_active_action()
            return
        self._arm_or_confirm_exit()

    # --- staged ctrl-c in the inputs --------------------------------
    def _handle_input_ctrl_c(self, widget: object) -> None:
        """Staged ctrl-c from a focused input.

        With text, clear the box. On an empty box: the find input closes (its
        "exit" is closing the bar), active work is cancelled, and only an idle
        search/filter input arms the staged exit gutter.
        """
        target = t.cast("t.Any", widget)
        if str(getattr(target, "value", "")):
            target.value = ""
            self._disarm_confirm_exit()
            return
        if widget is self._detail_find_input:
            self._close_detail_find()
            return
        if self._has_active_actions():
            self._cancel_active_action()
            return
        self._arm_or_confirm_exit()

    def _arm_or_confirm_exit(self) -> None:
        """Arm the confirm-exit gutter, or quit if it is already armed.

        Shared by the focused-input path (:meth:`_handle_input_ctrl_c`) and the
        non-input binding (:meth:`action_smart_quit`) so the "press ctrl-c again
        to exit" gutter behaves identically in every pane. The first call shows
        the gutter and starts a 2 s disarm timer; a second call within that
        window exits.
        """
        if self._confirm_exit_pending:
            self.app.exit()
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

    @_runtime.pump_only
    def _set_zoomed_pane(self, pane: t.Literal["results", "detail"]) -> None:
        """Paint one logical content pane without moving focus."""
        self._zoomed_pane = pane
        if self._body is None:
            return
        body = t.cast("t.Any", self._body)
        body.set_class(pane == "results", "-zoom-results")
        body.set_class(pane == "detail", "-zoom-detail")

    def _focus_widget_by_id(self, widget_id: str) -> None:
        try:
            target = self.query_one(f"#{widget_id}")
        except Exception:
            return
        target_pane: t.Literal["results", "detail"] | None = None
        if widget_id in {"results", "filter"}:
            target_pane = "results"
        elif widget_id in {"detail-scroll", "detail-find"}:
            target_pane = "detail"
        if target_pane is not None and self._zoomed_pane not in {None, target_pane}:
            self._set_zoomed_pane(target_pane)
        target.focus()

    @_runtime.pump_only
    def on_detail_focus_requested(self, message: DetailFocusRequested) -> None:
        """Reveal and focus a neighboring widget requested by the detail pane."""
        self._focus_widget_by_id(message.target)

    def _record_for_detail_focus(self) -> SearchRecord | None:
        """Return the record explicit detail focus should render."""
        highlighted = None
        if self._results is not None:
            highlighted = t.cast("int | None", getattr(self._results, "highlighted", None))
        if highlighted is not None and 0 <= highlighted < len(self.filtered_records):
            return self.filtered_records[highlighted]
        current = self._current_detail_record
        if (
            current is not None
            and self._results is not None
            and self._results.contains_record(current)
        ):
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
