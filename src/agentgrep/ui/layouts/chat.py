"""``ChatLayout`` — a Claude-Code/pi-style conversation transcript layout (ADR 0014).

The chat layout presents a search as a conversation: each submitted query is a
``you ▸ …`` turn and the matches stream in beneath it as bounded result bubbles,
capped per block with a count note — the deductive narrowing story (``1240`` →
``88`` → ``12``) reads straight down the transcript. It subclasses
:class:`~agentgrep.ui.layouts._base.LayoutScreen`, hosts any workflow (``search``
streams a fresh set, ``browse`` loads once then filters, ``deductive`` narrows a
fixed haystack), and reuses the grep-log non-blocking transport verbatim
(``make_gated_emitter`` / ``@offload`` / ``@pump_only`` / chunked mounts).

A focused result bubble opens :class:`DetailScreen`, whose heavy body renderable
is built off the pump (ADR 0011). Imported only from inside the app factory (and
the tests), never by the eager ``import agentgrep`` path (ADR 0010).
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import typing as t
from collections import abc as cabc

from rich.console import Group as _RichGroup
from rich.markdown import Markdown as _RichMarkdown
from rich.syntax import Syntax as _RichSyntax
from rich.text import Text
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Static

from agentgrep._text import (
    DETAIL_BODY_MAX_LINES,
    detect_content_format,
    format_compact_path,
    highlight_matches,
    truncate_lines,
)
from agentgrep.progress import (
    ProgressSnapshot,
    SearchControl,
    StreamingRecordsBatch,
    StreamingSearchFinished,
    format_match_count,
)
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui import _runtime, theme as ui_theme
from agentgrep.ui._context import UiContext
from agentgrep.ui.layouts._base import LayoutScreen
from agentgrep.ui.widgets import ResultsHeader, SearchInput, SearchRequested
from agentgrep.ui.widgets.breadcrumb import RefinementBreadcrumb
from agentgrep.ui.widgets.transcript import ConversationLog
from agentgrep.ui.widgets.turns import (
    MessageTurn,
    QueryTurn,
    ResultTurn,
    SystemTurn,
    Turn,
    TurnRenderer,
)

if t.TYPE_CHECKING:
    from rich.console import RenderableType

    from agentgrep._engine.matching import CompiledRecordMatcher
    from agentgrep.ui.workflows import Workflow

#: Bounded slice for mounting result turns (NB-4). Mounting a widget is heavier
#: than ``RichLog.write``, so the chunk is smaller than the grep-log's.
_APPLY_CHUNK_SIZE = 50
#: Maximum result bubbles mounted per query/refinement block. A larger set shows
#: this many plus a count note; narrowing reveals the rest. Bounds the per-block
#: widget count regardless of workflow.
_RESULT_TURN_CAP = 50


class ChatLayout(LayoutScreen):
    """A conversation transcript of streamed records over a bottom prompt."""

    DEFAULT_CSS = """
    ChatLayout { layout: vertical; }
    ChatLayout #transcript { height: 1fr; }
    ChatLayout #breadcrumb {
        height: auto; padding: 0 1; color: $text-muted; background: transparent;
    }
    ChatLayout #chat-status { height: 1; padding: 0 1; color: $text-muted; }
    ChatLayout #prompt { dock: bottom; height: 3; }
    ChatLayout .turn { padding: 0 1; background: transparent; }
    ChatLayout MessageTurn:focus { background: $boost; }
    """

    BINDINGS: t.ClassVar[list[t.Any]] = [
        ("escape", "stop_search", "Stop"),
        ("tab", "app.focus_next", "Focus"),
        ("enter", "open_detail", "Open"),
        ("ctrl+c", "app.quit", "Quit"),
    ]

    def __init__(self, ctx: UiContext, workflow: Workflow) -> None:
        super().__init__(ctx, workflow)
        self.search_query = ctx.query
        self._user_scope = ctx.query.scope
        self.control = ctx.control
        self._records: list[SearchRecord] = []
        self._search_emit: cabc.Callable[[object], None] | None = None
        self._generation = 0
        self._filter_generation = 0
        self._filter_matcher: CompiledRecordMatcher | None = None
        self._mounted_results = 0
        #: Raw input text captured before the workflow turns it into a query, so
        #: the user bubble shows the literal text with no host-surface change.
        self._pending_turn_text = ""
        self._renderer: TurnRenderer | None = None
        self._log: ConversationLog | None = None
        self._status: ResultsHeader | None = None
        self._breadcrumb: RefinementBreadcrumb | None = None
        self._search_input: SearchInput | None = None

    def compose(self) -> cabc.Iterator[t.Any]:
        """Build the tree: a transcript over a status line and a docked prompt."""
        initial = (
            self.context.initial_search_text
            if self.context.initial_search_text is not None
            else " ".join(self.context.query.terms)
        )
        yield ConversationLog(id="transcript")
        yield RefinementBreadcrumb(id="breadcrumb")
        yield ResultsHeader("chat", id="chat-status")
        yield SearchInput(
            value=initial,
            placeholder="search your prompt history",
            id="prompt",
            label="chat",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Cache widgets and build the renderer, then attach the workflow."""
        self._log = self.query_one("#transcript", ConversationLog)
        self._status = self.query_one("#chat-status", ResultsHeader)
        self._breadcrumb = self.query_one("#breadcrumb", RefinementBreadcrumb)
        self._search_input = self.query_one("#prompt", SearchInput)
        self._search_input.cursor_blink = False
        self._renderer = TurnRenderer(self.app.theme_variables)
        self._search_emit = self._make_gated_emit()
        super().on_mount()
        self._search_input.focus()

    def on_search_requested(self, message: SearchRequested) -> None:
        """Primary input submitted — capture the literal text, then route it."""
        self._pending_turn_text = message.payload.text.strip()
        self._workflow.on_query(self, self._pending_turn_text)

    def action_stop_search(self) -> None:
        """``Esc``: cooperatively stop the in-flight search."""
        self.request_cancel()

    def action_open_detail(self) -> None:
        """``Enter`` on a focused result bubble opens its record detail."""
        turn = getattr(self.focused, "turn", None)
        if isinstance(turn, ResultTurn):
            self.app.push_screen(
                DetailScreen(
                    turn.record,
                    self.search_query,
                    self.app.theme_variables,
                ),
            )

    # --- WorkflowHost surface -------------------------------------------------
    def build_query(self, text: str) -> SearchQuery:
        """Parse ``text`` into a query at the launch scope (host surface)."""
        from agentgrep.query import build_query_from_input, default_registry

        base = dataclasses.replace(self.search_query, scope=self._user_scope)
        result = build_query_from_input(text, base, default_registry())
        if result.query is not None:
            return result.query
        return dataclasses.replace(base, terms=tuple(text.split()) if text else ())

    def run_search(self, query: SearchQuery) -> None:
        """Clear the transcript, post the query turn, and stream a fresh set."""
        self.search_query = query
        self.control = SearchControl()
        self._records = []
        self._filter_matcher = None
        self._filter_generation += 1
        self._mounted_results = 0
        if self._log is not None:
            self._log.clear_turns()
        self._post_query_turn(self._take_turn_text(query), depth=0)
        if self._status is not None:
            self._status.begin()
        self._search_emit = self._make_gated_emit()
        self.run_worker(
            self._run_search,
            name="search",
            group="search",
            thread=True,
            exclusive=True,
        )

    def filter_loaded(self, text: str) -> None:
        """Refinement: append a query turn and a narrowed result block (no clear).

        The whole-set scan runs off the pump (NB-1) and the matching subset is
        mounted in bounded chunks (NB-4); the prior turns stay frozen above.
        """
        self._mounted_results = 0
        self._post_query_turn(self._pending_turn_text or text, depth=1)
        self._pending_turn_text = ""
        matcher = self._build_matcher(text)
        self._filter_matcher = matcher
        records = tuple(self._records)
        self._filter_generation += 1
        generation = self._filter_generation
        self.run_worker(
            lambda captured=generation, recs=records, m=matcher: self._run_block_filter(
                captured,
                recs,
                m,
            ),
            name="filter",
            group="filter",
            thread=True,
            exclusive=True,
        )

    def reset_view(self) -> None:
        """Clear the transcript to the idle state without a search (host surface)."""
        self._records = []
        self._filter_matcher = None
        self._filter_generation += 1
        self._mounted_results = 0
        if self._log is not None:
            self._log.clear_turns()
        if self._status is not None:
            self._status.go_idle()
        self._search_emit = self._make_gated_emit()

    def record_history(self, text: str) -> None:
        """No-op: the chat layout does not persist its own input history."""
        del text

    def request_cancel(self) -> None:
        """Cooperatively signal the in-flight search to wrap up (host surface)."""
        self.control.request_answer_now()

    def set_input_text(self, text: str) -> None:
        """Set the prompt's value — the deductive widen re-seed (host surface)."""
        if self._search_input is not None:
            self._search_input.value = text

    def update_breadcrumb(self, frames: cabc.Sequence[str]) -> None:
        """Repaint the refinement path above the prompt (host surface)."""
        if self._breadcrumb is not None:
            self._breadcrumb.set_frames(frames)

    # --- streaming transport (shared primitives, turn-specific present) --------
    def _make_gated_emit(self) -> cabc.Callable[[object], None]:
        """Return a worker emit whose events die with the current generation."""
        self._generation += 1
        return _runtime.make_gated_emitter(
            self.app.call_from_thread,
            self._apply_event,
            self._generation,
        )

    @_runtime.offload
    def _run_search(self) -> None:
        """Run the search off the pump, forwarding events to the gated emitter."""
        emit = self._search_emit
        if emit is None:
            return
        try:
            self.context.invoker.run(self.search_query, control=self.control, emit=emit)
        except BaseException as exc:
            self.app.call_from_thread(self._apply_finished, "error", 0, 0.0, str(exc))

    @_runtime.pump_only
    async def _apply_event(self, generation: int, event: object) -> None:
        """Route one worker event to the transcript, dropping stale generations."""
        if generation != self._generation:
            return
        if isinstance(event, StreamingRecordsBatch):
            self._records.extend(event.records)
            await self._mount_results(event.records, generation, self._current_search_gen)
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
    def _apply_progress(self, snapshot: ProgressSnapshot) -> None:
        """Store the scan fraction + phase; the header's timer repaints it."""
        if self._status is None:
            return
        if snapshot.phase == "scanning" and snapshot.current is not None and snapshot.total:
            fraction: float | None = snapshot.current / snapshot.total
        else:
            fraction = None
        self._status.set_progress(fraction, snapshot.phase)

    @_runtime.pump_only
    def _apply_finished(
        self,
        outcome: str,
        total: int,
        elapsed: float,
        error_message: str | None,
    ) -> None:
        """Freeze the status line and post the outcome/count as a system turn."""
        if self._status is not None:
            self._status.freeze(outcome)
        if outcome == "error":
            self._post_system(f"search failed: {error_message}", tone="error")
            return
        self._post_system(self._count_note(total, elapsed, outcome), tone="muted")

    @_runtime.offload
    def _run_block_filter(
        self,
        generation: int,
        records: tuple[SearchRecord, ...],
        matcher: CompiledRecordMatcher | None,
    ) -> None:
        """Filter a captured snapshot off the pump, then mount the matches."""
        matching = records if matcher is None else tuple(r for r in records if matcher.matches(r))
        self.app.call_from_thread(self._apply_block_filter, generation, matching)

    @_runtime.pump_only
    async def _apply_block_filter(
        self,
        generation: int,
        matching: cabc.Sequence[SearchRecord],
    ) -> None:
        """Mount a narrowed block + count note (NB-4), dropping stale filters."""
        if generation != self._filter_generation:
            return
        await self._mount_results(matching, generation, self._current_filter_gen)
        if generation != self._filter_generation:
            return
        self._post_system(self._count_note(len(matching), None, "complete"), tone="muted")

    @_runtime.pump_only
    async def _mount_results(
        self,
        records: cabc.Sequence[SearchRecord],
        generation: int,
        current: cabc.Callable[[], int],
    ) -> None:
        """Mount up to the per-block cap of result bubbles, yielding between chunks.

        ``current`` returns the live generation to compare ``generation`` against
        (the search or filter generation), so a superseded block stops mounting.
        """
        if self._log is None:
            return
        room = _RESULT_TURN_CAP - self._mounted_results
        if room <= 0:
            return
        to_mount = list(records[:room])
        for start in range(0, len(to_mount), _APPLY_CHUNK_SIZE):
            if generation != current():
                return
            chunk = to_mount[start : start + _APPLY_CHUNK_SIZE]
            self._log.mount_turns([self._make_turn(ResultTurn(self._generation, r)) for r in chunk])
            self._mounted_results += len(chunk)
            if start + _APPLY_CHUNK_SIZE < len(to_mount):
                await asyncio.sleep(0)

    def _current_search_gen(self) -> int:
        """Return the live search generation (the ``_mount_results`` gate)."""
        return self._generation

    def _current_filter_gen(self) -> int:
        """Return the live filter generation (the ``_mount_results`` gate)."""
        return self._filter_generation

    # --- turn helpers ---------------------------------------------------------
    def _make_turn(self, turn: Turn) -> MessageTurn:
        """Build a bubble for ``turn`` with its (bounded) renderable."""
        renderer = self._renderer
        renderable: RenderableType = renderer.render(turn) if renderer is not None else Text("")
        return MessageTurn(turn, renderable)

    def _post_query_turn(self, text: str, *, depth: int) -> None:
        """Append a ``you ▸ …`` user turn to the transcript."""
        if self._log is not None:
            self._log.mount_turns([self._make_turn(QueryTurn(self._generation, text, depth=depth))])

    def _post_system(
        self, text: str, *, tone: t.Literal["info", "error", "muted"] = "info"
    ) -> None:
        """Append a system note (count / outcome) to the transcript."""
        if self._log is not None:
            self._log.mount_turns([self._make_turn(SystemTurn(self._generation, text, tone=tone))])

    def _take_turn_text(self, query: SearchQuery) -> str:
        """Return the captured input text (else the query terms), clearing it."""
        text = self._pending_turn_text or " ".join(query.terms)
        self._pending_turn_text = ""
        return text

    def _count_note(self, total: int, elapsed: float | None, outcome: str) -> str:
        """Render the per-block result count note (with a 'showing N' hint)."""
        note = format_match_count(total)
        if outcome == "interrupted":
            note = f"stopped at {note}"
        if total > self._mounted_results:
            note = f"{note} · showing {self._mounted_results}, narrow to see all"
        if elapsed is not None:
            note = f"{note} in {elapsed:.1f}s"
        return note

    def _build_matcher(self, text: str) -> CompiledRecordMatcher | None:
        """Compile a record matcher for ``text``, or ``None`` for an empty filter."""
        from agentgrep._engine.matching import compile_record_matcher
        from agentgrep.query import build_query_from_input, default_registry

        stripped = text.strip()
        if not stripped:
            return None
        base = dataclasses.replace(self.search_query, terms=(), scope="all", limit=None)
        result = build_query_from_input(stripped, base, default_registry())
        query = result.query or dataclasses.replace(base, terms=tuple(stripped.split()))
        return compile_record_matcher(query)


class DetailScreen(ModalScreen[None]):
    """A modal that shows one record's header and format-aware body (ADR 0014).

    Self-contained (it does not reuse the HUD-coupled ``DetailScroll``): a plain
    scroll over a :class:`~textual.widgets.Static`. A small body renders inline;
    a large one shows the header immediately and builds the heavy renderable in
    an ``@offload`` worker (ADR 0011), swapping it in when ready.
    """

    DEFAULT_CSS = """
    DetailScreen { align: center middle; }
    DetailScreen #detail-modal { width: 90%; height: 90%; padding: 1 2; background: $surface; }
    DetailScreen #detail-body { height: auto; }
    """

    BINDINGS: t.ClassVar[list[t.Any]] = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    #: Bodies larger than this build off the pump; smaller ones render inline.
    _ASYNC_BODY_THRESHOLD: t.ClassVar[int] = 20_000

    def __init__(
        self,
        record: SearchRecord,
        query: SearchQuery,
        theme_variables: cabc.Mapping[str, str],
    ) -> None:
        super().__init__()
        self._record = record
        self._query = query
        self._theme_variables = theme_variables

    def compose(self) -> cabc.Iterator[t.Any]:
        """Yield a bordered scroll panel over a single content ``Static``."""
        with VerticalScroll(id="detail-modal"):
            yield Static(id="detail-body")

    def on_mount(self) -> None:
        """Show the header now; render the body inline or off the pump by size."""
        body_widget = self.query_one("#detail-body", Static)
        header = self._build_header()
        body_truncated = truncate_lines(self._record.text, DETAIL_BODY_MAX_LINES)
        if len(body_truncated) <= self._ASYNC_BODY_THRESHOLD:
            body_widget.update(_RichGroup(header, self._build_detail_body(body_truncated)))
            return
        body_widget.update(_RichGroup(header))
        self.run_worker(
            functools.partial(self._build_body_in_thread, header, body_truncated),
            name="detail",
            group="detail",
            thread=True,
            exclusive=True,
        )

    @_runtime.offload
    def _build_body_in_thread(self, header: RenderableType, body_truncated: str) -> None:
        """Build the heavy body renderable off the pump, then swap it in."""
        body = self._build_detail_body(body_truncated)
        self.app.call_from_thread(self._present, header, body)

    @_runtime.pump_only
    def _present(self, header: RenderableType, body: RenderableType) -> None:
        """Replace the header-only content with header + body (pump-side)."""
        body_widget = self.query_one("#detail-body", Static)
        body_widget.update(_RichGroup(header, body))

    def _build_header(self) -> RenderableType:
        """Render the bounded label/value header (pump-safe)."""
        record = self._record
        agent_color = ui_theme.resolve(
            self._theme_variables,
            ui_theme.AGENT_TOKEN_BY_NAME.get(record.agent or ""),
        )
        kind_color = ui_theme.resolve(
            self._theme_variables,
            ui_theme.KIND_TOKEN_BY_NAME.get(record.kind or ""),
        )
        dim = ui_theme.resolve(self._theme_variables, "ag-dim")
        model = ui_theme.resolve(self._theme_variables, "ag-model")
        path = ui_theme.resolve(self._theme_variables, "ag-muted")
        header = Text(no_wrap=False)
        rows = (
            ("Agent:", record.agent or "", agent_color),
            ("Kind:", record.kind or "", kind_color),
            ("Store:", record.store or "", dim),
            ("Timestamp:", record.timestamp or "unknown", dim),
            ("Model:", record.model or "unknown", model),
            ("Path:", format_compact_path(record.path, max_width=72), path),
        )
        for label, value, value_style in rows:
            header.append(f"{label} ", style="bold")
            header.append(f"{value}\n", style=value_style or None)
        header.append("\n")
        return header

    def _build_detail_body(self, body_text: str) -> RenderableType:
        """Render the body format-aware: JSON syntax, markdown, or highlighted text.

        Heavy (unbounded-CPU) work — JSON parse, markdown/syntax build, match
        highlight — so the caller runs it off the pump for large bodies.
        """
        fmt = detect_content_format(body_text)
        if fmt == "json":
            import json

            try:
                formatted = json.dumps(json.loads(body_text), indent=2, ensure_ascii=False)
            except json.JSONDecodeError, ValueError:
                formatted = body_text
            return _RichSyntax(formatted, "json", theme="ansi_dark", word_wrap=True)
        if fmt == "markdown":
            return _RichMarkdown(body_text, code_theme="ansi_dark")
        return highlight_matches(
            body_text,
            self._query.terms,
            case_sensitive=self._query.case_sensitive,
            regex=self._query.regex,
            style="bold yellow",
        )
