"""``GrepLogLayout`` — an append-only streaming grep-log layout (ADR 0013).

The second layout: a query input over a :class:`~textual.widgets.RichLog`
scrollback, like ``grep`` piping matches as they arrive. It consumes the *same*
engine seam and the *same* normalized records as the HUD, but composes a single
log (no results-list / detail split) and presents records as appended lines —
the structure axis made concrete. It hosts the same workflows as the HUD:
``search`` re-greps on each submission, ``browse`` filters the loaded log
in-memory.

The streaming transport reuses the shared non-blocking primitives
(``_runtime.make_gated_emitter`` / ``@offload`` / ``@pump_only`` /
``stream_apply``); only the *presentation* — appending log lines — differs from
the HUD. Imported only from inside the app factory (and tests), never eagerly
(ADR 0010).
"""

from __future__ import annotations

import asyncio
import typing as t
from collections import abc as cabc

from textual.widgets import Footer, RichLog, Static

from agentgrep._text import format_compact_path
from agentgrep.progress import (
    ProgressSnapshot,
    SearchControl,
    StreamingRecordsBatch,
    StreamingSearchFinished,
    format_match_count,
)
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui import _runtime
from agentgrep.ui._context import UiContext
from agentgrep.ui._source_diagnostics import UiProgressSnapshot
from agentgrep.ui.layouts._base import LayoutScreen
from agentgrep.ui.widgets import CompletionDropdown, SearchInput, SearchRequested

if t.TYPE_CHECKING:
    from agentgrep._engine.matching import CompiledRecordMatcher
    from agentgrep.ui.workflows import Workflow

#: Bounded slice size for streaming log writes (NB-4), matching the HUD applier.
_APPLY_CHUNK_SIZE = 200


class GrepLogLayout(LayoutScreen):
    """A query input over an append-only :class:`RichLog` of streamed records."""

    ZOOM_ARGUMENT_HINT: t.ClassVar[str] = "[log]"

    DEFAULT_CSS = """
    GrepLogLayout { layout: vertical; }
    GrepLogLayout #search { height: 3; }
    GrepLogLayout #greplog { height: 1fr; }
    GrepLogLayout #greplog-status { height: 1; padding: 0 1; color: $text-muted; }
    GrepLogLayout.-zoom-log #greplog-status { display: none; }
    """

    BINDINGS: t.ClassVar[list[t.Any]] = [
        ("tab", "app.focus_next", "Switch focus"),
        ("q", "app.quit", "Quit"),
        ("escape", "stop_search", "Stop search"),
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
        self._search_done = False
        self._log: t.Any = None
        self._status: t.Any = None
        self._search_input: t.Any = None

    def compose(self) -> cabc.Iterator[object]:
        """Build the tree: a search input over a log scrollback and a status line."""
        initial = (
            self.context.initial_search_text
            if self.context.initial_search_text is not None
            else " ".join(self.context.query.terms)
        )
        yield SearchInput(value=initial, placeholder="grep prompts", id="search")
        yield CompletionDropdown(id="enum-dropdown", target_input_id="search")
        yield RichLog(id="greplog", highlight=False, markup=False, wrap=False, max_lines=5000)
        yield Static("", id="greplog-status")
        yield Footer()

    def on_mount(self) -> None:
        """Cache widgets, then attach the workflow (its initial dispatch streams)."""
        self._search_input = self.query_one("#search")
        self._enum_dropdown = self.query_one("#enum-dropdown")
        self._enum_dropdown.display = False
        self._log = self.query_one("#greplog")
        self._status = self.query_one("#greplog-status")
        self._search_input.cursor_blink = False
        self._search_emit = self._make_gated_emit()
        super().on_mount()
        self._search_input.focus()

    @_runtime.pump_only
    def on_input_changed(self, event: object) -> None:
        """Update the shared slash menu as grep-log input changes."""
        source = getattr(event, "input", None)
        if getattr(source, "id", None) != "search":
            return
        value = str(getattr(event, "value", ""))
        if not self._update_command_completion(value):
            self._hide_command_completion()

    @_runtime.pump_only
    def on_option_list_option_selected(self, event: object) -> None:
        """Run a selected row from the shared slash-command menu."""
        self._select_command_option(event)

    @_runtime.pump_only
    def on_search_requested(self, message: SearchRequested) -> None:
        """Primary input submitted — run a command or route to the workflow."""
        text = message.payload.text.strip()
        if self._dispatch_slash_text(text) is not None:
            return
        self._workflow.on_query(self, text)

    def action_stop_search(self) -> None:
        """``Esc``: cooperatively stop the in-flight grep."""
        self.request_cancel()

    @_runtime.pump_only
    def handle_maximize_command(self, argument: str) -> bool:
        """Give the log all available content rows without hiding the shell."""
        target = argument.strip().lower()
        if target not in {"", "log"}:
            self.notify(
                "Maximize target must be log.",
                title="Maximize",
                severity="warning",
            )
            return False
        self.add_class("-zoom-log")
        return True

    @_runtime.pump_only
    def handle_minimize_command(self) -> bool:
        """Restore the grep-log status chrome."""
        self.remove_class("-zoom-log")
        return True

    # --- WorkflowHost surface -------------------------------------------------
    def build_query(self, text: str) -> SearchQuery:
        """Parse ``text`` into a query at the launch scope (host surface)."""
        import dataclasses

        from agentgrep.query import build_query_from_input, default_registry

        base = dataclasses.replace(self.search_query, scope=self._user_scope)
        result = build_query_from_input(text, base, default_registry())
        if result.query is not None:
            return result.query
        return dataclasses.replace(base, terms=tuple(text.split()) if text else ())

    def run_search(self, query: SearchQuery) -> None:
        """Clear the log and stream ``query`` into it (host surface)."""
        self.search_query = query
        self.control = SearchControl()
        self._records = []
        self._filter_matcher = None
        self._filter_generation += 1
        self._search_done = False
        if self._log is not None:
            self._log.clear()
        if self._status is not None:
            self._status.update("searching…")
        self._search_emit = self._make_gated_emit()
        self.run_worker(
            self._run_search,
            name="search",
            group="search",
            thread=True,
            exclusive=True,
        )

    def filter_loaded(self, text: str) -> None:
        """Re-render the loaded log filtered in-memory by ``text`` (host surface).

        The whole-buffer scan runs off the pump (NB-1) and the matching subset is
        re-written in bounded chunks (NB-4).
        """
        self._filter_matcher = self._build_matcher(text)
        self._refresh_filter_log(self._filter_matcher)

    def _refresh_filter_log(self, matcher: CompiledRecordMatcher | None) -> None:
        """Schedule an off-pump repaint of the loaded log through ``matcher``."""
        records = tuple(self._records)
        self._filter_generation += 1
        generation = self._filter_generation
        self.run_worker(
            lambda captured_generation=generation, captured_records=records, captured=matcher: (
                self._run_log_filter(
                    captured_generation,
                    captured_records,
                    captured,
                )
            ),
            name="filter",
            group="filter",
            thread=True,
            exclusive=True,
        )

    def reset_view(self) -> None:
        """Clear the log to the idle state without a search (host surface)."""
        self._records = []
        self._filter_matcher = None
        self._filter_generation += 1
        self._search_done = True
        if self._log is not None:
            self._log.clear()
        if self._status is not None:
            self._status.update("")
        self._search_emit = self._make_gated_emit()

    def record_history(self, text: str) -> None:
        """No-op: the grep log does not persist its own input history."""
        del text

    def request_cancel(self) -> None:
        """Cooperatively signal the in-flight grep to wrap up (host surface)."""
        self.control.request_answer_now()

    # --- streaming transport (shared primitives, log-specific present) --------
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
        """Run the grep off the pump, forwarding events to the gated emitter."""
        emit = self._search_emit
        if emit is None:
            return
        try:
            self.context.invoker.run(self.search_query, control=self.control, emit=emit)
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
    async def _apply_event(self, generation: int, event: object) -> None:
        """Route one worker event to the log, dropping stale generations (NB-10)."""
        if generation != self._generation:
            return
        if isinstance(event, StreamingRecordsBatch):
            self._records.extend(event.records)
            if self._filter_matcher is not None:
                self._refresh_filter_log(self._filter_matcher)
                return
            await self._write_unfiltered_records(
                generation,
                self._filter_generation,
                event.records,
            )
        elif isinstance(event, UiProgressSnapshot):
            if not self._search_done and self._status is not None:
                self._status.update(self._scanning_text(event.snapshot))
        elif isinstance(event, ProgressSnapshot):
            if not self._search_done and self._status is not None:
                self._status.update(self._scanning_text(event))
        elif isinstance(event, StreamingSearchFinished):
            self._apply_finished(
                event.outcome,
                event.total,
                event.elapsed,
                str(event.error) if event.error else None,
            )

    @_runtime.pump_only
    def _apply_finished(
        self,
        outcome: str,
        total: int,
        elapsed: float,
        error_message: str | None,
    ) -> None:
        """Freeze the status line with the grep outcome."""
        self._search_done = True
        if self._status is None:
            return
        if outcome == "error":
            self._status.update(f"grep failed: {error_message}")
        elif outcome == "interrupted":
            self._status.update(f"stopped at {format_match_count(total)} in {elapsed:.1f}s")
        else:
            self._status.update(f"{format_match_count(total)} in {elapsed:.1f}s")

    @_runtime.pump_only
    async def _write_unfiltered_records(
        self,
        generation: int,
        filter_generation: int,
        records: cabc.Sequence[SearchRecord],
    ) -> None:
        """Append unfiltered records until search or filter state changes."""
        for start in range(0, len(records), _APPLY_CHUNK_SIZE):
            if generation != self._generation or filter_generation != self._filter_generation:
                return
            if self._filter_matcher is not None:
                self._refresh_filter_log(self._filter_matcher)
                return
            self._write_chunk(records[start : start + _APPLY_CHUNK_SIZE])
            if start + _APPLY_CHUNK_SIZE < len(records):
                await asyncio.sleep(0)

    def _write_chunk(self, chunk: cabc.Sequence[SearchRecord]) -> None:
        """Append one bounded slice of records to the log (pump-side)."""
        for record in chunk:
            self._log.write(_format_log_line(record))

    @_runtime.offload
    def _run_log_filter(
        self,
        generation: int,
        records: tuple[SearchRecord, ...],
        matcher: CompiledRecordMatcher | None,
    ) -> None:
        """Filter a captured record snapshot, then re-render the matches."""
        matching = records if matcher is None else tuple(r for r in records if matcher.matches(r))
        self.app.call_from_thread(self._apply_log_filter, generation, matching)

    @_runtime.pump_only
    async def _apply_log_filter(
        self,
        generation: int,
        matching: cabc.Sequence[SearchRecord],
    ) -> None:
        """Re-render the log from ``matching`` in bounded chunks (NB-4)."""
        if generation != self._filter_generation:
            return
        if self._log is None:
            return
        self._log.clear()

        def write_chunk_if_live(chunk: cabc.Sequence[SearchRecord]) -> None:
            if generation == self._filter_generation:
                self._write_chunk(chunk)

        await _runtime.stream_apply(
            matching,
            write_chunk_if_live,
            chunk_size=_APPLY_CHUNK_SIZE,
        )

    def _build_matcher(self, text: str) -> CompiledRecordMatcher | None:
        """Compile a record matcher for ``text``, or ``None`` for an empty filter."""
        import dataclasses

        from agentgrep._engine.matching import compile_record_matcher
        from agentgrep.query import build_query_from_input, default_registry

        stripped = text.strip()
        if not stripped:
            return None
        base = dataclasses.replace(self.search_query, terms=(), scope="all", limit=None)
        result = build_query_from_input(stripped, base, default_registry())
        query = result.query or dataclasses.replace(base, terms=tuple(stripped.split()))
        return compile_record_matcher(query)

    @staticmethod
    def _scanning_text(snapshot: ProgressSnapshot) -> str:
        """Render the in-flight scanning status from ``snapshot``."""
        if snapshot.current is not None and snapshot.total:
            text = f"{snapshot.phase} {snapshot.current}/{snapshot.total}"
        else:
            text = snapshot.phase
        records = snapshot.source_records_seen
        if records is not None and records > 0:
            suffix = "record" if records == 1 else "records"
            text = f"{text} · {records} {suffix}"
        return f"{text}…"


def _format_log_line(record: SearchRecord) -> str:
    """Render one record as a compact single grep-log line."""
    agent = (record.agent or "").ljust(8)[:8]
    kind = (record.kind or "").ljust(8)[:8]
    title = (record.title or record.text or "").splitlines()
    summary = title[0][:80] if title else ""
    path = format_compact_path(record.path, max_width=50)
    return f"{agent}  {kind}  {summary}  {path}"
