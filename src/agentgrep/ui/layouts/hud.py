"""The default heads-up layout: search -> results | detail -> status.

``HudLayout`` is the explorer's default pluggable layout (ADR 0013): a
:class:`~agentgrep.ui.layouts._base.LayoutScreen` that composes the search bar,
the streaming results list, and the detail pane, driven by the active workflow.
It imports Textual at the top but is only reached from inside the factory (and
the tests), so ``import agentgrep`` stays Textual-free (ADR 0010).
"""

from __future__ import annotations

import collections
import dataclasses
import functools
import typing as t
from collections import abc as cabc

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.containers import Center, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.widgets import Footer, Static

from agentgrep import bookmarks
from agentgrep._query_gate import strip_depth_directive
from agentgrep._types import (
    StaticLike,
    StreamingAppLike,
)
from agentgrep.progress import ProgressSnapshot, SearchControl, StreamingRecordsBatch
from agentgrep.query import default_registry
from agentgrep.records import AGENT_CHOICES, SearchQuery, SearchRecord
from agentgrep.ui import _history, _runtime, commands, theme as ui_theme
from agentgrep.ui._context import UiContext
from agentgrep.ui._result_status import depth_offer_typed_directive
from agentgrep.ui.completion import QuerySuggester
from agentgrep.ui.highlighter import QueryHighlighter
from agentgrep.ui.layouts._base import COPY_SELECTION_BINDING
from agentgrep.ui.layouts._hud_search import (
    _DetailCacheKey,
    _DetailFindBaseKey,
    _HudSearchBase,
)
from agentgrep.ui.widgets import (
    BookmarkChoice,
    BookmarkRecall,
    CompletionDropdown,
    DepthOffer,
    DepthOfferSelected,
    DetailFindInput,
    DetailFocusRequested,
    DetailScroll,
    FilterHeader,
    FilterInput,
    PaneHeader,
    ResultsHeader,
    SearchingPanel,
    SearchInput,
    SearchResultsList,
    SlowSourceDiagnosticsRow,
    WelcomeExamples,
    WelcomeQuerySelected,
)
from agentgrep.ui.widgets.welcome import (
    _WELCOME_BRAND_SHINE,
    _WELCOME_QUERIES,
    _WELCOME_SHINE_INTERVAL,
    _welcome_query_examples,
    _WelcomeWordmark,
)

if t.TYPE_CHECKING:
    from agentgrep._engine.matching import CompiledRecordMatcher
    from agentgrep.identity import RecordIdentity
    from agentgrep.ui._seams import SearchInvoker
    from agentgrep.ui.workflows import Workflow


@dataclasses.dataclass(frozen=True, slots=True)
class _LoadedBookmarks:
    """Worker-loaded bookmark snapshot or a path-free failure."""

    entries: tuple[bookmarks.BookmarkEntry, ...]
    error: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class _BookmarkToggleResult:
    """One accepted toggle result returned from the write worker."""

    record: SearchRecord
    mutation: bookmarks.BookmarkMutation | None
    error: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class _BookmarkResolution:
    """Resolved choices returned as one bounded pump-side update."""

    choices: tuple[BookmarkChoice, ...]
    error: str | None
    cancelled: bool = False


class HudLayout(_HudSearchBase):
    """Search box, streaming results list, detail pane, and status chrome."""

    ZOOM_ARGUMENT_HINT: t.ClassVar[str] = "[results|detail]"
    EXTRA_SLASH_COMMANDS: t.ClassVar[tuple[commands.SlashCommand, ...]] = (
        commands.bookmark_commands()
    )

    # ``priority=True`` on the directional ``ctrl+hjkl`` bindings pushes
    # them into Textual's priority dispatch lane so they win over any
    # widget binding for the same key (e.g. ``Input``'s readline
    # ``ctrl+k`` = kill-to-end-of-line). Trade-off accepted per user
    # request: filter loses ``ctrl+k``; ``ctrl+u`` and ``ctrl+w`` are
    # untouched and remain readline-compatible.
    BINDINGS: t.ClassVar[list[BindingType]] = [
        ("tab", "app.focus_next", "Switch focus"),
        ("q", "confirm_quit", "Quit"),
        ("escape", "stop_search", "Stop search"),
        ("ctrl+backslash", "toggle_detail_progress", "Detail"),
        COPY_SELECTION_BINDING,
        ("ctrl+c", "smart_quit", "Stop / Quit"),
        # Priority so the focused search Input cannot intercept recall.
        Binding("ctrl+r", "recall_history", "History", priority=True),
        Binding("b", "toggle_bookmark", "Bookmark"),
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

    # Detail width below which compact labels keep fixed-width identity
    # handles on one visual row after the Static's horizontal padding.
    _DETAIL_COMPACT_IDENTITY_WIDTH: t.ClassVar[int] = 42

    # Body width (cells) below which the detail pane moves from the
    # right (side-by-side) to the bottom (stacked) — each side wants
    # ~50 cells to stay readable. Distinct from the statusline
    # breakpoint above, which measures the results column alone.
    _SPLIT_BREAKPOINT: t.ClassVar[int] = 100
    _WELCOME_COMPACT_WIDTH: t.ClassVar[int] = 20
    _WELCOME_COMPACT_HEIGHT: t.ClassVar[int] = 18

    def __init__(self, ctx: UiContext, workflow: Workflow) -> None:
        super().__init__(ctx, workflow)
        self.home = ctx.home
        self.search_query = ctx.query
        # The user's launch discovery scope. A ``scope:`` predicate
        # widens the per-search scope to "all"; this stable base is what
        # a search without a ``scope:`` predicate reverts to, so the
        # widening never persists across searches.
        self._user_scope = ctx.base_scope
        self._user_effort = ctx.base_effort
        self._user_scope_provenance = ctx.base_scope_provenance
        self._user_conversation_limit = ctx.base_conversation_limit
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
        self._welcome_widget: _WelcomeWordmark | None = None
        self._welcome_examples: WelcomeExamples | None = None
        self._depth_offer: DepthOffer | None = None
        self._welcome_shine_timer: Timer | None = None
        # Persisted search-input history (agentgrep's only self-written state —
        # under XDG_STATE_HOME, never a searched store). The factory loads the
        # snapshot before Textual starts; the recall modal only reads memory.
        self._history_disabled = ctx.history_disabled
        self._history_path = _history.history_path(self.home)
        self._history = list(ctx.history)
        self._last_recorded_text = self._history[0].text if self._history else ""
        # Bookmarks are agentgrep-owned state, loaded once on a worker. The
        # write gate accepts at most one toggle at a time, so an accepted
        # transaction is never superseded by a later keypress.
        self._bookmark_store = bookmarks.BookmarkStore()
        self._bookmark_entries: list[bookmarks.BookmarkEntry] = []
        self._bookmarked_ids: set[str] = set()
        self._bookmarks_loaded = False
        self._bookmark_load_generation = 0
        self._bookmark_write_generation = 0
        self._bookmark_write_pending = False
        self._bookmark_resolution_generation = 0
        self._bookmark_resolution_control: SearchControl | None = None
        self._results: SearchResultsList | None = None
        # The detail pane is un-Grouped into two stacked, individually
        # selectable ``Static``s: the metadata header and the body. A single
        # ``Group`` renderable forces Textual's non-selectable ``RichVisual``
        # path even when the body itself is a ``Text``; splitting them keeps
        # the body a ``Text``/``Content`` so a mouse drag selects it.
        self._detail_meta: StaticLike | None = None
        self._detail_body: StaticLike | None = None
        self._detail_row: SlowSourceDiagnosticsRow | None = None
        self._chrome_generation: int = 0
        self._detail_generation: int = 0
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
        self._theme_refresh_pending = False
        self._rendered_theme_name: str | None = None
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
        self._detail_scroll: DetailScroll | None = None
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
            tuple[SearchRecord, object, str, str],
        ] = collections.OrderedDict()
        self._detail_identity_cache: collections.OrderedDict[
            int,
            tuple[SearchRecord, RecordIdentity],
        ] = collections.OrderedDict()
        self._presented_detail_cache_key: _DetailCacheKey | None = None
        self._detail_build_generation = 0
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
        # Raw <-> rendered toggle (alt+r / ctrl+e). A single session-global
        # flag applied to every record (default rendered), mirroring codex's
        # one global ``raw_output_mode``. Both representations are resident, so
        # a toggle is a cheap repaint with no re-render, parse, or worker.
        self._detail_raw_mode: bool = False
        # The current record's rendered body renderable (styled ``Text`` for
        # markdown, ``Syntax`` for JSON, highlighted ``Text`` for prose) and
        # its flattened plain projection, both resident for the toggle and for
        # the ``Y`` copy-rendered command.
        self._detail_rendered_renderable: object = None
        self._detail_rendered_plain: str = ""
        # tmux copy-mode-vi visual select over the detail body, driven by
        # Textual's NATIVE selection. When active the body Static shows a plain
        # selectable Text of the bounded source and a logical (row, col) cursor
        # drives a Selection overlay. Per motion only ``screen.selections`` is
        # reassigned (O(1)); Textual re-renders the resident Text with a
        # per-logical-line span, so no body is re-tokenized (ADR 0011).
        self._detail_visual_active: bool = False
        self._detail_visual_anchor: tuple[int, int] = (0, 0)
        self._detail_visual_cursor: tuple[int, int] = (0, 0)
        self._detail_visual_lines: tuple[str, ...] = ()
        # The text the detail body is actually DISPLAYED as — the pretty-
        # printed JSON for json bodies, the raw body otherwise. Find matches
        # and scroll work against this so offsets line up with what is shown.
        self._detail_find_source: str = ""
        self._detail_find_json_syntax = False
        # Cached syntax+search+filter find body; the find-match overlay changes
        # per keystroke but this base does not. A presented Text is retained;
        # other renderables are converted once per highlight state, then copied.
        self._detail_find_base: Text | None = None
        self._detail_find_base_key: _DetailFindBaseKey | None = None
        # Per-record find memory, parallel to DetailScroll's owned memory:
        # id(record) -> (query, match_index, input_cursor_pos). Bounded LRU.
        self._detail_find_state: collections.OrderedDict[
            int,
            tuple[str, int, int],
        ] = collections.OrderedDict()

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
        if self.app.theme == self._rendered_theme_name:
            self._theme_refresh_pending = False
            return
        if self.app.screen is not self:
            self._theme_refresh_pending = True
            return
        self._refresh_query_highlighting(dark=bool(getattr(_theme, "dark", True)))
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
        self._rendered_theme_name = self.app.theme

    @_runtime.pump_only
    def _refresh_query_highlighting(self, *, dark: bool) -> None:
        """Repaint the shared query grammar with the active theme palette."""
        self._query_highlighter.set_theme(
            dark=dark,
            theme_variables=(
                self.app.theme_variables
                if self.app.theme in ui_theme.THEME_PROFILE_BY_NAME
                else None
            ),
        )
        if self._search_input is not None:
            self._search_input.refresh()
        if self._filter_input is not None:
            self._filter_input.refresh()
        if self._welcome_examples is not None:
            self._welcome_examples.update(_welcome_query_examples(self._query_highlighter))

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
                with Vertical(id="empty-hint"):
                    with Center():
                        yield _WelcomeWordmark(id="empty-welcome")
                    with Center():
                        yield Static("try a search to begin", id="empty-lead")
                    with Center():
                        yield WelcomeExamples(
                            _welcome_query_examples(self._query_highlighter),
                            id="empty-examples",
                            markup=False,
                        )
                    # Engine-authored depth choices for the request the search
                    # box would submit. Selecting one is the only way into
                    # ``targeted`` from a cold session, and it names what each
                    # rung reads instead of implying corpus coverage.
                    with Center():
                        yield DepthOffer(id="empty-depth", markup=False)
                # Shown only while a search runs before its first result
                # (CSS hides it otherwise): a centered spinner + phase verb
                # + counts + elapsed, collapsed to the results list the
                # moment records arrive.
                yield SearchingPanel(id="searching-panel")
            with Vertical(id="detail-column", classes=detail_classes):
                yield PaneHeader("detail", id="detail-header")
                with DetailScroll(id="detail-scroll"):
                    # Two stacked, individually selectable Statics (never a
                    # Group): the metadata header and the body renderable.
                    yield Static("", id="detail-meta")
                    yield Static("", id="detail-body")
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
        self._detail_meta = t.cast(
            "StaticLike",
            streaming.query_one("#detail-meta", Static),
        )
        self._detail_body = t.cast(
            "StaticLike",
            streaming.query_one("#detail-body", Static),
        )
        self._detail_scroll = t.cast(
            "DetailScroll",
            streaming.query_one("#detail-scroll", DetailScroll),
        )
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
        self._welcome_widget = t.cast(
            "_WelcomeWordmark",
            streaming.query_one("#empty-welcome", _WelcomeWordmark),
        )
        self._welcome_examples = t.cast(
            "WelcomeExamples",
            streaming.query_one("#empty-examples", WelcomeExamples),
        )
        self._depth_offer = t.cast(
            "DepthOffer",
            streaming.query_one("#empty-depth", DepthOffer),
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
        self._refresh_query_highlighting(dark=bool(self.app.current_theme.dark))
        self._refresh_depth_offer()
        self._detail_find_input = t.cast(
            "DetailFindInput",
            streaming.query_one("#detail-find"),
        )
        t.cast("t.Any", self._detail_find_input).display = False
        t.cast("t.Any", self._detail_find_input).cursor_blink = False
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
        self._start_bookmark_load()
        # Rebuild Rich-baked rows/detail when the active color palette changes.
        # The pump-thread bind and watchdog are owned by the App shell (it owns
        # the pump).
        self._rendered_theme_name = self.app.theme
        self.app.theme_changed_signal.subscribe(self, self._on_theme_changed)
        self._apply_responsive_layout()
        # Attach the workflow (base.on_mount): it seeds the initial dispatch —
        # a launch search or the idle bare canvas — now that the widgets exist.
        super().on_mount()
        self._welcome_shine_timer = self.set_interval(
            _WELCOME_SHINE_INTERVAL,
            self._animate_welcome_wordmark,
            name="welcome-shine",
            pause=True,
        )
        self._sync_welcome_shine_timer()
        # The primary search input stays visible in every launch state. Keep
        # mount focus there even when an initial search hides the filter.
        self._search_input.focus()
        self._update_pane_focus()

    @_runtime.pump_only
    def on_unmount(self) -> None:
        """Cooperatively stop bookmark resolution when this layout tears down."""
        self._bookmark_resolution_generation += 1
        if self._bookmark_resolution_control is not None:
            self._bookmark_resolution_control.request_answer_now()
            self._bookmark_resolution_control = None

    def _set_empty_state(self, *, empty: bool) -> None:
        """Toggle the pre-search bare-canvas state on ``#body``.

        Compatibility shim over :meth:`_set_results_view`: ``empty`` is the
        pre-search bare canvas; not-empty reveals the results chrome. The
        search flow uses ``_set_results_view`` directly for the intermediate
        ``searching`` view.
        """
        self._set_results_view("empty" if empty else "results")

    @_runtime.pump_only
    def on_welcome_query_selected(self, message: WelcomeQuerySelected) -> None:
        """Load and focus one fixed welcome query without submitting it."""
        if self._search_input is None or not (0 <= message.index < len(_WELCOME_QUERIES)):
            return
        self._search_input.load_query(_WELCOME_QUERIES[message.index])
        self._search_input.focus()

    @_runtime.pump_only
    def on_depth_offer_selected(self, message: DepthOfferSelected) -> None:
        """Type the chosen depth action's ``depth:`` term into the query and submit it."""
        message.stop()
        directive = depth_offer_typed_directive(message.action_id)
        if directive is None or self._search_input is None:
            return
        current = strip_depth_directive(self._search_input.value).strip()
        text = f"{current} {directive}" if current else directive
        self._search_input.load_query(text)
        if self._dispatch_slash_text(text) is None:
            self._remember_active_search_text(text)
            self._workflow.on_query(self, text)
        # Starting the search hides the idle canvas, which blurs this panel and
        # leaves the screen with no focused widget. Hand focus back to the input
        # so the next keystroke is not silently discarded.
        if self._search_input is not None:
            self._search_input.focus()

    def _refresh_depth_offer(self) -> None:
        """Repaint the idle canvas with the engine's current depth offer.

        Bounded string work over at most two engine-authored rows. The offer is
        derived from the live query text, so an inline ``scope:`` predicate can
        retire a rung as it is typed; repainting per edit keeps the panel's
        coverage claim true.
        """
        if self._depth_offer is None:
            return
        self._depth_offer.show_offer(self.pending_depth_actions())

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
        self._sync_welcome_shine_timer()

    @_runtime.pump_only
    def on_screen_suspend(self) -> None:
        """Pause the welcome shine while another screen covers this layout."""
        if self._welcome_shine_timer is not None:
            self._welcome_shine_timer.pause()

    @_runtime.pump_only
    def on_screen_resume(self) -> None:
        """Apply a coalesced theme preview, then restore the welcome shine."""
        if self._theme_refresh_pending:
            self._theme_refresh_pending = False
            self._on_theme_changed(self.app.current_theme)
        self._sync_welcome_shine_timer()

    @_runtime.pump_only
    def _sync_welcome_shine_timer(self) -> None:
        """Match the shine timer to active-screen and empty-view state."""
        if self._welcome_shine_timer is None:
            return
        body = self._body
        if (
            self.app.animation_level == "full"
            and self.is_active
            and body is not None
            and body.has_class("-empty")
        ):
            self._welcome_shine_timer.resume()
        else:
            self._welcome_shine_timer.pause()

    @_runtime.pump_only
    def _animate_welcome_wordmark(self) -> None:
        """Advance the bounded welcome shine while its timer is active."""
        body = self._body
        if (
            self._welcome_widget is None
            or self.app.animation_level != "full"
            or not self.is_active
            or body is None
            or not body.has_class("-empty")
        ):
            self._sync_welcome_shine_timer()
            return
        if not self.app.app_focus:
            return
        current_offset = self._welcome_widget.shine_offset
        self._welcome_widget.shine_offset = (current_offset + 1) % len(_WELCOME_BRAND_SHINE)

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

        Also the trigger for the ambient detail cursor line's show/hide: it is
        a byproduct of :attr:`DetailScroll.has_focus <textual.widget.Widget.has_focus>`,
        so a Tab, a click, ``h`` (release to results), or find opening/closing
        (which moves focus to/from ``detail-find``) all need to repaint it,
        and this handler already runs on every focus move in the screen
        (:meth:`on_descendant_focus` / :meth:`on_descendant_blur`).
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
        # Find and visual select own the body's overlay while active; leave
        # their repaint to their own close/cancel paths (see
        # ``on_detail_scroll_changed``).
        if not self._detail_find_active and not self._detail_visual_active:
            self._paint_detail_body()

    # --- durable bookmarks -----------------------------------------------
    @_runtime.pump_only
    def _start_bookmark_load(self) -> None:
        """Start the one session bookmark load without touching storage here."""
        self._bookmark_load_generation += 1
        generation = self._bookmark_load_generation
        emit = _runtime.make_gated_emitter(
            self.app.call_from_thread,
            self._apply_loaded_bookmarks,
            generation,
        )
        self.run_worker(
            functools.partial(
                self._load_bookmarks_in_thread,
                self._bookmark_store,
                emit,
            ),
            name="bookmark-load",
            group="bookmark-load",
            thread=True,
            exclusive=True,
        )

    @_runtime.offload
    def _load_bookmarks_in_thread(
        self,
        store: bookmarks.BookmarkStore,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Read and validate the bookmark snapshot away from the pump."""
        try:
            entries = tuple(store.list())
        except bookmarks.BookmarkError as exc:
            payload = _LoadedBookmarks((), str(exc))
        except Exception:  # noqa: BLE001 - worker must return a terminal payload
            payload = _LoadedBookmarks((), "Bookmark storage operation failed")
        else:
            payload = _LoadedBookmarks(entries, None)
        emit(payload)

    @_runtime.pump_only
    def _apply_loaded_bookmarks(self, generation: int, event: object) -> None:
        """Install the loaded canonical snapshot if this layout is still live."""
        if (
            generation != self._bookmark_load_generation
            or not isinstance(event, _LoadedBookmarks)
            or not self._has_live_screen_stack()
        ):
            return
        self._bookmark_entries = list(event.entries)
        self._bookmarked_ids = {entry.target_id for entry in event.entries}
        self._bookmarks_loaded = True
        if event.error is not None:
            self.notify(event.error, title="Bookmarks", severity="error")
        self._refresh_current_bookmark_marker()

    def _has_live_screen_stack(self) -> bool:
        """Return whether this layout remains in any Textual mode stack."""
        stacks = getattr(self.app, "_screen_stacks", {})
        return any(self in stack for stack in stacks.values())

    def _selected_bookmark_record(self) -> SearchRecord | None:
        """Return the highlighted result, falling back to the visible detail."""
        focused_id = getattr(self.focused, "id", None)
        if focused_id == "detail-scroll" and self._current_detail_record is not None:
            return self._current_detail_record
        highlighted = None
        if self._results is not None:
            highlighted = t.cast("int | None", getattr(self._results, "highlighted", None))
        if (
            focused_id == "results"
            and highlighted is not None
            and 0 <= highlighted < len(self.filtered_records)
        ):
            return self.filtered_records[highlighted]
        if self._current_detail_record is not None:
            return self._current_detail_record
        if highlighted is not None and 0 <= highlighted < len(self.filtered_records):
            return self.filtered_records[highlighted]
        return None

    @_runtime.pump_only
    def action_toggle_bookmark(self) -> None:
        """Toggle the selected exact record from a result/detail-owned ``b``."""
        self.toggle_bookmark("record")

    @_runtime.pump_only
    def toggle_bookmark(self, scope: str) -> None:
        """Accept one selected-record toggle and offload identity plus storage."""
        if scope not in {"record", "thread", "content"}:
            self.notify(
                "Bookmark scope must be record, thread, or content.",
                title="Bookmark",
            )
            return
        if not self._bookmarks_loaded:
            self.notify("Bookmarks are still loading.", title="Bookmark")
            return
        if self._bookmark_write_pending:
            self.notify("A bookmark change is already in progress.", title="Bookmark")
            return
        record = self._selected_bookmark_record()
        if record is None:
            self.notify("Select a record to bookmark.", title="Bookmark")
            return
        if self._bookmark_resolution_control is not None:
            self._bookmark_resolution_control.request_answer_now()
            self._bookmark_resolution_control = None
            self._bookmark_resolution_generation += 1
        self._bookmark_write_pending = True
        self._bookmark_write_generation += 1
        generation = self._bookmark_write_generation
        emit = _runtime.make_gated_emitter(
            self.app.call_from_thread,
            self._apply_bookmark_mutation,
            generation,
        )
        self.run_worker(
            functools.partial(
                self._toggle_bookmark_in_thread,
                self._bookmark_store,
                record,
                scope,
                emit,
            ),
            name="bookmark-write",
            group="bookmark-write",
            thread=True,
            exclusive=True,
        )

    @_runtime.offload
    def _toggle_bookmark_in_thread(
        self,
        store: bookmarks.BookmarkStore,
        record: SearchRecord,
        scope: bookmarks.BookmarkScope,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Prepare one canonical target and complete its transaction off-pump."""
        try:
            entry = bookmarks.bookmark_entry_for_record(record, scope=scope)
            mutation = store.toggle(entry)
        except bookmarks.BookmarkError as exc:
            payload = _BookmarkToggleResult(record, None, str(exc))
        except Exception:  # noqa: BLE001 - worker must release the pending gate
            payload = _BookmarkToggleResult(record, None, "Bookmark change failed")
        else:
            payload = _BookmarkToggleResult(record, mutation, None)
        emit(payload)

    @_runtime.pump_only
    def _apply_bookmark_mutation(self, generation: int, event: object) -> None:
        """Apply exactly one accepted transaction result on the pump."""
        if generation != self._bookmark_write_generation or not isinstance(
            event,
            _BookmarkToggleResult,
        ):
            return
        self._bookmark_write_pending = False
        if not self._has_live_screen_stack():
            return
        if event.error is not None or event.mutation is None:
            self.notify(
                event.error or "Bookmark change failed",
                title="Bookmark",
                severity="error",
            )
            return
        mutation = event.mutation
        entry = mutation.entry
        if mutation.action == "added" and entry is not None:
            if entry.target_id not in self._bookmarked_ids:
                self._bookmark_entries.append(entry)
            self._bookmarked_ids.add(entry.target_id)
        elif mutation.action == "removed" and entry is not None:
            self._bookmark_entries = [
                item for item in self._bookmark_entries if item.target_id != entry.target_id
            ]
            self._bookmarked_ids.discard(entry.target_id)
        self._refresh_current_bookmark_marker(event.record)
        if mutation.action == "added":
            self.notify(f"Bookmarked {entry.scope if entry is not None else 'record'}.")
        elif mutation.action == "removed":
            self.notify(f"Removed {entry.scope if entry is not None else 'record'} bookmark.")

    def _refresh_current_bookmark_marker(
        self,
        record: SearchRecord | None = None,
    ) -> None:
        """Rebuild only the current header from an already-prepared identity."""
        current = self._current_detail_record
        if current is None or (record is not None and current is not record):
            return
        identity = self._cached_detail_identity(current)
        if identity is None or self._detail_meta is None:
            return
        self._replace_detail_header(
            self._build_detail_header(
                current,
                identity,
                width=self._detail_render_width(),
            )
        )

    @_runtime.pump_only
    def open_bookmarks(self) -> None:
        """Resolve the loaded snapshot through a fresh scope-all search worker."""
        if not self._bookmarks_loaded:
            self.notify("Bookmarks are still loading.", title="Bookmarks")
            return
        if self._bookmark_write_pending:
            self.notify("A bookmark change is already in progress.", title="Bookmarks")
            return
        if isinstance(getattr(self.app, "screen", None), BookmarkRecall):
            return
        if self._bookmark_resolution_control is not None:
            self._bookmark_resolution_control.request_answer_now()
        self._bookmark_resolution_generation += 1
        generation = self._bookmark_resolution_generation
        entries = tuple(self._bookmark_entries)
        if not entries:
            self._bookmark_resolution_control = None
            self._apply_bookmark_resolution(
                generation,
                _BookmarkResolution(choices=(), error=None),
            )
            return
        control = SearchControl()
        self._bookmark_resolution_control = control
        query = SearchQuery(
            terms=(),
            scope="all",
            any_term=False,
            regex=False,
            case_sensitive=False,
            agents=AGENT_CHOICES,
            limit=None,
            dedupe=False,
        )
        emit = _runtime.make_gated_emitter(
            self.app.call_from_thread,
            self._apply_bookmark_resolution,
            generation,
        )
        self.run_worker(
            functools.partial(
                self._resolve_bookmarks_in_thread,
                self._invoker,
                entries,
                query,
                control,
                emit,
            ),
            name="bookmark-resolve",
            group="bookmark-resolve",
            thread=True,
            exclusive=True,
        )

    @_runtime.offload
    def _resolve_bookmarks_in_thread(
        self,
        invoker: SearchInvoker,
        entries: tuple[bookmarks.BookmarkEntry, ...],
        query: SearchQuery,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Search and hash candidates off-pump until every target resolves."""
        from agentgrep.identity import record_identity

        unresolved = {entry.target_id: entry for entry in entries}
        resolved: dict[str, SearchRecord] = {}

        def consume(event: object) -> None:
            if not isinstance(event, StreamingRecordsBatch) or not unresolved:
                return
            for record in event.records:
                if control.answer_now_requested():
                    return
                identity = record_identity(record)
                candidates = (
                    identity.record_id,
                    identity.thread_id,
                    identity.content_id,
                )
                for target_id in candidates:
                    if target_id is None or target_id not in unresolved:
                        continue
                    entry = unresolved[target_id]
                    if entry.scope == "record" and entry.content_id != identity.content_id:
                        continue
                    resolved[target_id] = record
                    unresolved.pop(target_id)
                if not unresolved:
                    control.request_answer_now()
                    return

        error: str | None = None
        try:
            invoker.run(query, control=control, emit=consume)
        except BaseException:  # noqa: BLE001 - worker must emit terminal state
            error = "Bookmark resolution failed"
        cancelled = bool(unresolved) and control.answer_now_requested() and error is None
        choices = tuple(BookmarkChoice(entry, resolved.get(entry.target_id)) for entry in entries)
        emit(_BookmarkResolution(choices=choices, error=error, cancelled=cancelled))

    @_runtime.pump_only
    def _apply_bookmark_resolution(self, generation: int, event: object) -> None:
        """Open recall for the live resolver generation when the stack exists."""
        if (
            generation != self._bookmark_resolution_generation
            or not isinstance(event, _BookmarkResolution)
            or not self._has_live_screen_stack()
        ):
            return
        self._bookmark_resolution_control = None
        if event.cancelled:
            return
        if event.error is not None:
            self.notify(event.error, title="Bookmarks", severity="error")
        self.app.push_screen(BookmarkRecall(event.choices), self._apply_bookmark_choice)

    @_runtime.pump_only
    def _apply_bookmark_choice(self, choice: BookmarkChoice | None) -> None:
        """Present a resolved record or report one path-free unavailable target."""
        if choice is None or not self._has_live_screen_stack():
            return
        if choice.record is None:
            self.notify(
                f"{choice.entry.target_id} is unavailable in the current stores.",
                title="Bookmarks",
            )
            return
        self._detail_opened = True
        self._apply_responsive_layout()
        self.show_detail(choice.record)

    def _apply_responsive_layout(self) -> None:
        """Apply welcome compaction and wide/stacked detail geometry.

        The welcome canvas sheds spacing at its width and height boundaries.
        Below :data:`_SPLIT_BREAKPOINT` cells the body stacks the panes
        (results on top, detail below) and the detail stays collapsed
        until the user selects a row — matching tig, which moves its
        diff view to the bottom on narrow screens and opens it on
        selection. Wide statuslines keep the detail on the right and always
        visible. Idempotent and cheap: only touches classes when their target
        state changes.
        """
        if self._body is None or self._detail_column is None:
            return
        # Use the app (terminal) width, not ``_body.size`` — the body
        # hasn't been laid out yet at on_mount, so its width reads 0
        # and the detail would flash visible before the first resize
        # collapsed it. ``self.size`` is known from the driver at mount.
        width = int(getattr(self.size, "width", 0) or 0)
        height = int(getattr(self.size, "height", 0) or 0)
        self.set_class(0 < width <= self._WELCOME_COMPACT_WIDTH, "-compact-width")
        self.set_class(0 < height <= self._WELCOME_COMPACT_HEIGHT, "-compact-height")
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

    def on_resize(self, event: object) -> None:
        """Debounce rapid resize bursts (e.g. tiling-WM live drag)."""
        del event
        if self._resize_debounce_timer is not None:
            timer = t.cast("t.Any", self._resize_debounce_timer)
            timer.stop()
        self._resize_debounce_timer = self.set_timer(0.05, self._after_resize)

    @_runtime.pump_only
    def _after_resize(self) -> None:
        """Refresh chrome and re-flow the width-baked detail body on a resize."""
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
        # The rendered markdown/code body is flattened to a Text baked at the
        # pane width, so re-run the (off-pump) build when a resize actually
        # changed the width. The cache-key guard skips no-op resizes (width
        # quantizes), mirroring the filter path. Raw source and the visual
        # overlay are plain Text that Textual re-wraps for free -- skip them.
        record = self._current_detail_record
        if record is not None:
            identity = self._cached_detail_identity(record)
            self._replace_detail_header(
                self._build_detail_header(
                    record,
                    identity,
                    width=self._detail_render_width(),
                ),
            )
        if record is not None and not self._detail_visual_active and not self._detail_raw_mode:
            detail_key = self._detail_cache_key(self.search_query.terms, record)
            if detail_key != self._presented_detail_cache_key:
                # Native selections store line/column offsets into the old
                # width-baked Text. Reflow changes that coordinate space even
                # when the record itself is unchanged.
                self._clear_stale_body_selection()
                self.show_detail(record)

    def action_stop_search(self) -> None:
        """``Esc``: cooperative early-exit of the worker (no-op when finished)."""
        self._cancel_active_action()

    @_runtime.pump_only
    def action_smart_quit(self) -> None:
        """``Ctrl-C`` outside an input: cancel an in-flight action; else stage exit.

        Inputs intercept ctrl+c first for the staged clear/confirm-exit flow
        (:meth:`_handle_input_ctrl_c`); this fires when focus is on a non-input
        widget (results list, detail scroll). With an action in flight the first
        press cancels it; otherwise it arms the same "press ctrl-c again to exit"
        gutter as the inputs, so the warning shows whichever pane holds focus.
        """
        if self._has_active_actions():
            self._disarm_confirm_exit()
            self._cancel_active_action()
            return
        self._arm_or_confirm_exit("ctrl-c")

    # --- staged ctrl-c in the inputs --------------------------------
    @_runtime.pump_only
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
        self._arm_or_confirm_exit("ctrl-c")

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
        except NoMatches:
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
        resolving = (
            self._bookmark_resolution_control is not None
            and not self._bookmark_resolution_control.answer_now_requested()
        )
        return resolving or not self._search_done

    def _cancel_active_action(self) -> None:
        """Cancel the topmost in-flight cancellable action.

        Extension point: extend with future cancellable actions in
        most-recently-started order so ``Ctrl-C`` peels them off one at a
        time before exiting.
        """
        if self._bookmark_resolution_control is not None:
            self._bookmark_resolution_control.request_answer_now()
            self._bookmark_resolution_control = None
            self._bookmark_resolution_generation += 1
        elif not self._search_done:
            self.control.request_answer_now()
