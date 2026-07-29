"""Search, history, and result streaming for the default HUD layout."""

from __future__ import annotations

import dataclasses
import functools
import time
import typing as t
from collections import abc as cabc

from textual.worker import Worker, WorkerCancelled

from agentgrep._engine.orchestration import clear_haystack_cache, search_record_sort_key
from agentgrep._types import StreamingAppLike
from agentgrep.progress import (
    ProgressSnapshot,
    SearchControl,
    StreamingRecordsBatch,
    StreamingSearchFinished,
    format_match_count,
)
from agentgrep.query import default_registry
from agentgrep.records import SearchQuery, SearchRecord
from agentgrep.ui import _history, _runtime, _streaming
from agentgrep.ui._result_status import format_run_status
from agentgrep.ui._source_diagnostics import (
    SourceScanFinished,
    SourceScanStarted,
    UiProgressSnapshot,
)
from agentgrep.ui.completion import (
    apply_enum_choice,
    apply_word_choice,
    keyword_completion_candidates,
)
from agentgrep.ui.layouts._hud_detail_interaction import (
    _DetailCacheKey as _DetailCacheKey,
    _DetailFindBaseKey as _DetailFindBaseKey,
    _HudDetailInteractionBase,
)
from agentgrep.ui.widgets import (
    FilterCompleted,
    FilterRequested,
    HistoryRecall,
    ResultHighlighted,
    ResultsScrollChanged,
    SearchRequested,
)

if t.TYPE_CHECKING:
    from agentgrep._engine.matching import CompiledRecordMatcher
    from agentgrep.results import RunSummary


class _HudSearchBase(_HudDetailInteractionBase):
    """Search and result-streaming base for the HUD layout."""

    _query_error_active: bool = False

    def _start_search_worker(self, query: SearchQuery) -> None:
        """Reset chrome and spawn a new search worker for ``query``.

        ``exclusive=True`` with ``group="search"`` makes Textual cancel
        any prior in-flight search worker before this one runs, which
        is the canonical Textual pattern for "fire a backend search on
        every debounced keystroke without piling up cancellations."
        """
        self.search_query = query
        self._run_summary = None
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
        emit = self._search_emit
        if emit is None:
            return
        # Bind the request on the pump. Reading these off ``self`` inside the
        # worker would resolve them whenever the thread first runs, so a
        # replacement landing in that window would hand the outgoing worker the
        # incoming run's control — and the signal meant for it would reach
        # nobody.
        streaming.run_worker(
            functools.partial(self._run_search, query, self.control, emit),
            name="search",
            group="search",
            # Without this Textual titles the worker with repr(partial), which
            # now expands the bound query's terms into the worker log.
            description="run search",
            thread=True,
            exclusive=True,
        )

    def _reset_search_chrome(self) -> None:
        """Wipe per-search state and chrome before a fresh search starts.

        Swap ``self.control`` for a fresh :class:`SearchControl`;
        callers that replace or clear a running search must signal the
        old control first so the new worker starts with a clean slate.
        """
        self._query_error_active = False
        self.control = SearchControl()
        self._filter_generation += 1
        self._records_generation += 1
        self._detail_build_generation += 1
        clear_haystack_cache()
        self._detail_body_cache.clear()
        self._presented_detail_cache_key = None
        if self._detail_scroll is not None:
            self._detail_scroll.clear_record_memory()
        self._detail_find_state.clear()
        # A fresh search wipes the detail; close any open find bar and cancel
        # any in-flight visual select.
        self._reset_detail_find_state()
        self._reset_detail_visual()
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
        self._clear_detail_panes()
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
                event.summary,
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
                self._query_error_active = False
                self._set_search_rule_state("")
            self._update_search_dropdown(value)
            # The offer is derived from the live query, so an inline scope:
            # predicate can retire a rung mid-edit. Repaint or the panel keeps
            # claiming coverage the next Enter would not match.
            self._refresh_depth_offer()
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
    def _run_search(
        self,
        query: SearchQuery,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Run one search off the pump against the request bound when it started.

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
            self._invoker.run(query, control=control, emit=emit)
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
        self._remember_active_search_text(text)
        self._workflow.on_query(self, text)

    # --- WorkflowHost surface: the active workflow drives the layout here -----
    def build_query(self, text: str) -> SearchQuery:
        """Parse ``text`` into a query at the user's launch scope (host surface)."""
        return self._build_search_query(text)

    @_runtime.pump_only
    def show_query_error(self, message: str) -> None:
        """Present a query error without replacing or dispatching the input."""
        self._query_error_active = True
        self._set_search_rule_state("error")
        if self._search_input is not None:
            self._search_input.focus()
        self.notify(message, title="Invalid query", severity="error")

    def run_search(self, query: SearchQuery) -> None:
        """Reset the chrome and stream ``query`` through the engine (host surface)."""
        self._query_error_active = False
        self._start_search_worker(query)

    def reset_view(self) -> None:
        """Return to the idle bare-canvas state without a search (host surface)."""
        self._reset_search_chrome()
        self._search_done = True
        self._run_summary = None
        self._set_empty_state(empty=True)
        self.search_query = self._build_search_query("")
        self._refresh_depth_offer()

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
        base = dataclasses.replace(
            self.search_query,
            scope=self._user_scope,
            effort=self._user_effort,
            scope_provenance=self._user_scope_provenance,
            conversation_limit=self._user_conversation_limit,
            # An uncapped explorer query declares scan order so it runs on the
            # streaming driver; a capped one keeps its order so the cap stays exact.
            order=("scan" if self.search_query.limit is None else self.search_query.order),
        )
        result = build_query_from_input(text, base, default_registry())
        if result.query is not None:
            return result.query
        # Parse / compile error: degrade to legacy split so the
        # search box stays editable. The error message stays
        # accessible on the result for future UI surfacing.
        terms = tuple(text.split()) if text else ()
        return dataclasses.replace(base, terms=terms, compiled=None)

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
            # Scan order arrives file-order within a source, so each batch is sorted
            # newest-first; sources dispatch mtime-descending, making the stream as a
            # whole read newest-first.
            records = sorted(records, key=search_record_sort_key, reverse=True)
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
                for record_chunk in _streaming._stream_filter_chunks(
                    records,
                    max_records=self._APPLY_CHUNK_SIZE,
                    max_chars=_streaming._STREAM_FILTER_MAX_TEXT_CHARS,
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

    @_runtime.pump_only
    def _apply_finished(
        self,
        outcome: str,
        total: int,
        elapsed: float,
        error_message: str | None,
        run_summary: RunSummary | None = None,
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
        self._run_summary = run_summary
        if self.all_records:
            self._set_results_view("results")
        elif self._searching_panel is not None:
            self._set_results_view("searching")
            self._searching_panel.freeze(
                outcome,
                total=total,
                elapsed=elapsed,
                message=error_message or "",
                summary=run_summary,
            )
        else:
            self._set_results_view("results")
        if outcome == "error":
            summary = f"Search failed: {error_message}"
        elif run_summary is not None:
            summary = f"{format_run_status(run_summary)} in {elapsed:.1f}s"
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
        if not self._query_error_active:
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

        Reuses the current detail only when its render key still matches.
        Changed highlight state is rebuilt inline only for bounded small
        bodies; large uncached bodies remain offloaded by :meth:`show_detail`.
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
        if self._detail_body is not None:
            if self.filtered_records:
                highlighted = self._results.highlighted if self._results is not None else None
                row_index = highlighted if highlighted is not None else 0
                record = self.filtered_records[row_index]
                detail_key = self._detail_cache_key(self.search_query.terms, record)
                if (
                    record is not self._current_detail_record
                    or detail_key != self._presented_detail_cache_key
                ):
                    self.show_detail(record)
            else:
                find_had_focus = self.app.focused is self._detail_find_input
                if self._detail_find_active:
                    self._remember_detail_find()
                self._detail_build_generation += 1
                self._reset_detail_find_state()
                self._current_detail_record = None
                self._detail_opened = False
                self._presented_detail_cache_key = None
                self._detail_body_text = ""
                self._detail_header_text = None
                self._detail_find_source = ""
                self._detail_find_json_syntax = False
                self._detail_find_base = None
                self._detail_find_base_key = None
                self._detail_rendered_renderable = None
                self._detail_rendered_plain = ""
                if self._detail_meta is not None:
                    self._detail_meta.update("")
                self._detail_body.update(
                    "No results." if self._search_done else "No matches yet.",
                )
                self._refresh_detail_statusline()
                if find_had_focus and self._filter_input is not None:
                    self._filter_input.focus()
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
