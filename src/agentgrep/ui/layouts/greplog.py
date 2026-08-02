"""``GrepLogLayout`` — an append-only streaming grep-log layout (ADR 0013).

The second layout: a query input over a :class:`~textual.widgets.Log`
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
import functools
import typing as t
from collections import abc as cabc

from textual import events
from textual._cells import cell_width_to_column_index
from textual.binding import BindingType
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Footer, Log, Static

from agentgrep._text import format_compact_path
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
from agentgrep.ui._result_status import (
    format_next_action_hint,
    format_run_status,
)
from agentgrep.ui._source_diagnostics import UiProgressSnapshot
from agentgrep.ui.highlighter import QueryHighlighter
from agentgrep.ui.layouts._base import COPY_SELECTION_BINDING, LayoutScreen
from agentgrep.ui.widgets import CompletionDropdown, SearchInput, SearchRequested

if t.TYPE_CHECKING:
    from agentgrep._engine.matching import CompiledRecordMatcher
    from agentgrep.results import RunSummary
    from agentgrep.ui.workflows import Workflow

#: Bounded slice size for streaming log writes (NB-4), matching the HUD applier.
_APPLY_CHUNK_SIZE = 200


class _SelectableLog(Log):
    """A ``Log`` with correct multi-line extraction at the Textual 3.2 floor."""

    _mouse_start_scroll: Offset | None = None

    @_runtime.pump_only
    def on_mouse_down(self, _event: events.MouseDown) -> None:
        """Retain the content scroll origin for this selection drag."""
        self._mouse_start_scroll = self.scroll_offset

    def _mouse_cell_selection(self) -> Selection | None:
        """Return the active mouse drag in content-cell coordinates."""
        if not self.is_mounted:
            return None
        screen = t.cast("t.Any", self.screen)
        if (state := getattr(screen, "_select_state", None)) is not None:
            end = getattr(state, "end", None)
            if (
                end is None
                or state.start.content_widget is not self
                or end.content_widget is not self
            ):
                return None
            start_screen = (
                state.start.container_initial_offset + state.start.container_pointer_delta
            )
            end_screen = state.screen_offset
        else:
            start = getattr(screen, "_select_start", None)
            end = getattr(screen, "_select_end", None)
            if start is None or end is None or start[0] is not self or end[0] is not self:
                return None
            start_screen, end_screen = start[1], end[1]
        origin = self.content_region.offset
        start_scroll = self.scroll_offset
        if self._mouse_start_scroll is not None:
            start_scroll = self._mouse_start_scroll
        offsets = sorted(
            (
                start_screen - origin + start_scroll,
                end_screen - origin + self.scroll_offset,
            ),
            key=lambda offset: offset.transpose,
        )
        return Selection(offsets[0], offsets[1] + Offset(1, 0))

    @_runtime.pump_only
    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Return the retained plain text covered by ``selection``."""
        if not self.lines:
            return "", "\n"
        cell_selection = self._mouse_cell_selection()
        active_selection = selection if cell_selection is None else cell_selection
        start_y = 0 if active_selection.start is None else max(0, active_selection.start.y)
        end_y = (
            len(self.lines) - 1
            if active_selection.end is None
            else min(active_selection.end.y, len(self.lines) - 1)
        )
        if start_y > end_y:
            return "", "\n"
        selected_lines: list[str] = []
        for y in range(start_y, end_y + 1):
            if (span := active_selection.get_span(y)) is None:
                continue
            start_x, end_x = span
            line = self._process_line(self.lines[y])
            if cell_selection is not None:
                start_x = cell_width_to_column_index(line, start_x, 8)
                if end_x != -1:
                    end_x = cell_width_to_column_index(line, end_x, 8)
            selected_lines.append(line[start_x:] if end_x == -1 else line[start_x:end_x])
        return "\n".join(selected_lines), "\n"


class GrepLogLayout(LayoutScreen):
    """A query input over an append-only :class:`Log` of streamed records."""

    ZOOM_ARGUMENT_HINT: t.ClassVar[str] = "[log]"

    DEFAULT_CSS = """
    GrepLogLayout { layout: vertical; }
    GrepLogLayout #search { height: 3; }
    GrepLogLayout #greplog { height: 1fr; }
    GrepLogLayout #greplog-status { height: 1; padding: 0 1; color: $text-muted; }
    GrepLogLayout.-zoom-log #greplog-status { display: none; }
    """

    BINDINGS: t.ClassVar[list[BindingType]] = [
        ("tab", "app.focus_next", "Switch focus"),
        ("q", "app.quit", "Quit"),
        ("escape", "stop_search", "Stop search"),
        COPY_SELECTION_BINDING,
        ("ctrl+c", "app.quit", "Quit"),
    ]

    def __init__(self, ctx: UiContext, workflow: Workflow) -> None:
        super().__init__(ctx, workflow)
        self.search_query = ctx.query
        self._user_scope = ctx.base_scope
        self._user_effort = ctx.base_effort
        self._user_scope_provenance = ctx.base_scope_provenance
        self._user_conversation_limit = ctx.base_conversation_limit
        self.control = ctx.control
        self._records: list[SearchRecord] = []
        self._search_emit: cabc.Callable[[object], None] | None = None
        self._generation = 0
        self._filter_generation = 0
        self._filter_matcher: CompiledRecordMatcher | None = None
        self._filter_scanned_count = 0
        self._filter_scan_generation: int | None = None
        self._search_done = False
        self._log: t.Any = None
        self._status: t.Any = None
        self._search_input: t.Any = None
        self._query_highlighter = QueryHighlighter()
        self._theme_refresh_pending = False
        self._rendered_theme_name: str | None = None

    def compose(self) -> cabc.Iterator[object]:
        """Build the tree: a search input over a log scrollback and a status line."""
        initial = (
            self.context.initial_search_text
            if self.context.initial_search_text is not None
            else " ".join(self.context.query.terms)
        )
        yield SearchInput(
            value=initial,
            placeholder="grep prompts",
            id="search",
            highlighter=self._query_highlighter,
        )
        yield CompletionDropdown(id="enum-dropdown", target_input_id="search")
        yield _SelectableLog(id="greplog", highlight=False, max_lines=5000)
        yield Static("", id="greplog-status", markup=False)
        yield Footer()

    @_runtime.pump_only
    def on_mount(self) -> None:
        """Cache widgets, then attach the workflow (its initial dispatch streams)."""
        self._search_input = self.query_one("#search")
        self._enum_dropdown = self.query_one("#enum-dropdown")
        self._enum_dropdown.display = False
        self._log = self.query_one("#greplog")
        self._status = self.query_one("#greplog-status")
        self._search_input.cursor_blink = False
        self._query_highlighter.set_theme(
            dark=bool(self.app.current_theme.dark),
            theme_variables=self._owned_theme_variables(),
        )
        self._rendered_theme_name = self.app.theme
        self.app.theme_changed_signal.subscribe(self, self._on_theme_changed)
        self._search_emit = self._make_gated_emit()
        super().on_mount()
        self._search_input.focus()

    @_runtime.pump_only
    def _on_theme_changed(self, selected_theme: object) -> None:
        """Repaint the search query with the selected theme's syntax palette."""
        if not self.is_mounted:
            return
        if self.app.theme == self._rendered_theme_name:
            self._theme_refresh_pending = False
            return
        if self.app.screen is not self:
            self._theme_refresh_pending = True
            return
        self._apply_theme_refresh(selected_theme)

    def _apply_theme_refresh(self, selected_theme: object) -> None:
        """Apply the latest owned or polarity-based query palette."""
        self._query_highlighter.set_theme(
            dark=bool(getattr(selected_theme, "dark", True)),
            theme_variables=self._owned_theme_variables(),
        )
        self._search_input.refresh()
        self._rendered_theme_name = self.app.theme

    @_runtime.pump_only
    def on_screen_resume(self) -> None:
        """Coalesce hidden picker previews into one visible repaint."""
        if not self._theme_refresh_pending:
            return
        self._theme_refresh_pending = False
        self._apply_theme_refresh(self.app.current_theme)

    def _owned_theme_variables(self) -> cabc.Mapping[str, str] | None:
        """Return profile variables, or polarity fallback for unowned themes."""
        if self.app.theme not in ui_theme.THEME_PROFILE_BY_NAME:
            return None
        return self.app.theme_variables

    @_runtime.pump_only
    def on_input_changed(self, event: object) -> None:
        """Update the shared slash menu as grep-log input changes."""
        source = getattr(event, "input", None)
        if getattr(source, "id", None) != "search":
            return
        if self._search_input is not None and t.cast(
            "t.Any",
            self._search_input,
        ).has_class("-error"):
            t.cast("t.Any", self._search_input).remove_class("-error")
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
        self._remember_active_search_text(text)
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
    def build_query(self, text: str, *, notify_warning: bool = False) -> SearchQuery:
        """Parse ``text`` into a query at the launch scope (host surface).

        Unlike the HUD layout, ``on_input_changed`` here never calls
        ``build_query`` — this method only ever runs at submit time via
        :meth:`~agentgrep.ui.workflows.search.SearchWorkflow.on_query` — but
        the ``notify_warning`` parameter stays on the shared
        :class:`~agentgrep.ui.workflows._protocol.WorkflowHost` surface so
        both layouts present the same signature.
        """
        import dataclasses

        from agentgrep.query import build_query_from_input, default_registry

        base = dataclasses.replace(
            self.search_query,
            scope=self._user_scope,
            effort=self._user_effort,
            scope_provenance=self._user_scope_provenance,
            conversation_limit=self._user_conversation_limit,
        )
        result = build_query_from_input(text, base, default_registry())
        if result.query is not None:
            if notify_warning and result.warning is not None:
                self.show_query_warning(result.warning)
            return result.query
        return dataclasses.replace(
            base,
            terms=tuple(text.split()) if text else (),
            compiled=None,
        )

    @_runtime.pump_only
    def show_query_error(self, message: str) -> None:
        """Present a query error without replacing or dispatching the input."""
        if self._search_input is not None:
            target = t.cast("t.Any", self._search_input)
            target.add_class("-error")
            target.focus()
        self.notify(message, title="Invalid query", severity="error")

    @_runtime.pump_only
    def show_query_warning(self, message: str) -> None:
        """Present one non-fatal query diagnostic without disturbing the input."""
        self.notify(message, title="Unrecognized field", severity="warning")

    def run_search(self, query: SearchQuery) -> None:
        """Clear the log and stream ``query`` into it (host surface)."""
        self.search_query = query
        self._run_summary = None
        self.control = SearchControl()
        self._records = []
        self._filter_matcher = None
        self._filter_generation += 1
        self._filter_scanned_count = 0
        self._filter_scan_generation = None
        self._search_done = False
        if self._log is not None:
            self._clear_log()
        if self._status is not None:
            self._status.update("searching…")
        # Bumps the generation that discards a replaced run's late events.
        emit = self._make_gated_emit()
        # Bind the request on the pump. Reading these off ``self`` inside the
        # worker would resolve them whenever the thread first runs, so a
        # replacement landing in that window would hand the outgoing worker the
        # incoming run's control — and the signal meant for it would reach
        # nobody.
        self.run_worker(
            functools.partial(self._run_search, query, self.control, emit),
            name="search",
            group="search",
            description="run search",
            thread=True,
            exclusive=True,
        )

    def filter_loaded(self, text: str) -> None:
        """Re-render the loaded log filtered in-memory by ``text`` (host surface).

        A new filter scans the loaded buffer off the pump (NB-1) and rewrites
        matches in bounded chunks (NB-4). Records that stream in afterward are
        projected once as ordered tail segments instead of rescanning the prefix.
        """
        self._filter_matcher = self._build_matcher(text)
        self._refresh_filter_log(self._filter_matcher)

    def _refresh_filter_log(self, matcher: CompiledRecordMatcher | None) -> None:
        """Start a fresh off-pump projection of the loaded log through ``matcher``."""
        self._filter_generation += 1
        self._filter_scanned_count = 0
        self._filter_scan_generation = None
        self._continue_filter_projection(repaint=True, matcher=matcher)

    def _continue_filter_projection(
        self,
        *,
        repaint: bool = False,
        matcher: CompiledRecordMatcher | None = None,
    ) -> None:
        """Scan only the records not yet projected by the active filter."""
        generation = self._filter_generation
        if self._filter_scan_generation == generation:
            return
        start = 0 if repaint else self._filter_scanned_count
        end = len(self._records)
        if start >= end:
            if repaint and self._log is not None:
                self._clear_log()
            self._filter_scanned_count = end
            return
        active_matcher = self._filter_matcher if matcher is None else matcher
        records = tuple(self._records[start:end])
        self._filter_scan_generation = generation
        self.run_worker(
            functools.partial(
                self._run_log_filter,
                generation,
                start,
                end,
                records,
                active_matcher,
                repaint,
            ),
            name="filter",
            group="filter",
            description="filter grep log",
            thread=True,
            exclusive=True,
        )

    def reset_view(self) -> None:
        """Clear the log to the idle state without a search (host surface)."""
        self._records = []
        self._filter_matcher = None
        self._filter_generation += 1
        self._filter_scanned_count = 0
        self._filter_scan_generation = None
        self._search_done = True
        self._run_summary = None
        if self._log is not None:
            self._clear_log()
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
    def _run_search(
        self,
        query: SearchQuery,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Run the grep off the pump against the request bound when it started.

        Parameters
        ----------
        query : SearchQuery
            Request this worker owns, bound at spawn.
        control : SearchControl
            Cooperative-cancel flag this worker owns. Passed rather than read
            from ``self`` so a replacement cannot swap it mid-flight.
        emit : cabc.Callable[[object], None]
            Generation-gated event sink for this run.
        """
        try:
            self.context.invoker.run(query, control=control, emit=emit)
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
            if (
                self._filter_matcher is not None
                or self._filter_scan_generation == self._filter_generation
            ):
                self._continue_filter_projection()
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
                event.summary,
            )

    @_runtime.pump_only
    def _apply_finished(
        self,
        outcome: str,
        total: int,
        elapsed: float,
        error_message: str | None,
        run_summary: RunSummary | None = None,
    ) -> None:
        """Freeze the status line with the grep outcome."""
        self._search_done = True
        self._run_summary = run_summary
        if self._status is None:
            return
        if outcome == "error":
            self._status.update(f"grep failed: {error_message}")
        elif run_summary is not None:
            status = format_run_status(run_summary)
            action_hint = format_next_action_hint(run_summary) if total == 0 else ""
            suffix = f" · {action_hint}" if action_hint else ""
            self._status.update(f"{status}{suffix} · {elapsed:.1f}s")
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
            if (
                self._filter_matcher is not None
                or self._filter_scan_generation == self._filter_generation
            ):
                self._continue_filter_projection()
                return
            self._write_chunk(records[start : start + _APPLY_CHUNK_SIZE])
            if start + _APPLY_CHUNK_SIZE < len(records):
                await asyncio.sleep(0)

    def _write_chunk(self, chunk: cabc.Sequence[SearchRecord]) -> None:
        """Append one bounded slice of records to the log (pump-side)."""
        if chunk:
            max_lines = self._log.max_lines
            if max_lines is not None and self._log.line_count + len(chunk) > max_lines:
                self._clear_log_selection()
            text = "\n".join(_format_log_line(record) for record in chunk)
            separator = "\n" if self._log.line_count else ""
            self._log.write(f"{separator}{text}")

    def _clear_log_selection(self) -> None:
        """Drop native offsets before retained log rows move or disappear."""
        if self._log.is_mounted and self._log in self.screen.selections:
            self.screen.clear_selection()

    def _clear_log(self) -> None:
        """Clear the log and any native selection anchored to its rows."""
        self._clear_log_selection()
        self._log.clear()

    @_runtime.offload
    def _run_log_filter(
        self,
        generation: int,
        start: int,
        end: int,
        records: tuple[SearchRecord, ...],
        matcher: CompiledRecordMatcher | None,
        repaint: bool,
    ) -> None:
        """Filter one captured record segment, then project its matches."""
        matching = records if matcher is None else tuple(r for r in records if matcher.matches(r))
        self.app.call_from_thread(
            self._apply_filter_segment,
            generation,
            start,
            end,
            matching,
            repaint,
        )

    @_runtime.pump_only
    async def _apply_filter_segment(
        self,
        generation: int,
        start: int,
        end: int,
        matching: cabc.Sequence[SearchRecord],
        repaint: bool,
    ) -> None:
        """Apply one ordered filter segment and schedule any newly arrived tail."""
        if generation != self._filter_generation or start != self._filter_scanned_count:
            return
        await self._write_filter_projection(generation, matching, repaint=repaint)
        if generation != self._filter_generation:
            return
        self._filter_scanned_count = end
        self._filter_scan_generation = None
        self._continue_filter_projection()

    @_runtime.pump_only
    async def _apply_log_filter(
        self,
        generation: int,
        matching: cabc.Sequence[SearchRecord],
    ) -> None:
        """Re-render the log from ``matching`` in bounded chunks (NB-4)."""
        await self._write_filter_projection(generation, matching, repaint=True)

    async def _write_filter_projection(
        self,
        generation: int,
        matching: cabc.Sequence[SearchRecord],
        *,
        repaint: bool,
    ) -> None:
        """Write one bounded filter projection while its generation stays live."""
        if generation != self._filter_generation or self._log is None:
            return
        if repaint:
            self._clear_log()

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
    title = (record.title or record.text or "")[:81].splitlines()
    summary = title[0][:80] if title else ""
    path = format_compact_path(record.path, max_width=50)
    return f"{agent}  {kind}  {summary}  {path}"
