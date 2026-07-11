"""Detail rendering for the default HUD layout."""

from __future__ import annotations

import dataclasses
import functools
import pathlib
import typing as t
from collections import abc as cabc

from rich.syntax import Syntax as _RichSyntax
from rich.text import Text

from agentgrep._text import (
    DETAIL_BODY_MAX_CHARS,
    DETAIL_BODY_MAX_LINES,
    detect_content_format,
    format_compact_path,
    format_display_path,
    looks_like_code,
    truncate_lines,
)
from agentgrep._types import StreamingAppLike
from agentgrep.records import SearchRecord
from agentgrep.ui import _runtime, theme as ui_theme
from agentgrep.ui._detail_render import (
    DetailRenderRequest,
    DetailRenderResult,
    build_detail_body,
)
from agentgrep.ui.format import scroll_percent
from agentgrep.ui.layouts._base import LayoutScreen
from agentgrep.ui.widgets import DetailScrollChanged

if t.TYPE_CHECKING:
    from agentgrep.identity import RecordIdentity

# The trailing ``int`` is the body render width: markdown is baked to a styled
# ``Text`` at a fixed pane width off-thread, so a resize-then-revisit must miss
# the LRU rather than return a stale-width render.
_DetailCacheKey = tuple[int, tuple[str, ...], bool, bool, tuple[str, ...], int]
_RichSyntaxType = _RichSyntax


@dataclasses.dataclass(frozen=True, slots=True)
class _PreparedDetail:
    """Worker-prepared identity and optional body for one detail generation."""

    record: SearchRecord
    identity: RecordIdentity
    body: DetailRenderResult | None
    query_terms: tuple[str, ...]
    cache_key: _DetailCacheKey | None
    present_body: bool


@dataclasses.dataclass(frozen=True, slots=True)
class _DetailSnapshot:
    """Immutable inputs captured on the pump for one detail worker."""

    record: SearchRecord
    identity: RecordIdentity | None
    render_request: DetailRenderRequest | None
    query_terms: tuple[str, ...]
    cache_key: _DetailCacheKey | None


class _HudDetailBase(LayoutScreen):
    """Detail rendering base for the HUD layout."""

    @_runtime.pump_only
    def on_detail_scroll_changed(self, message: DetailScrollChanged) -> None:
        """Re-render status and the ambient cursor line for the new scroll offset."""
        record = self._current_detail_record
        if record is None or message.record_token != id(record):
            return
        self._refresh_detail_statusline(message.percent)
        # Find and visual select repaint the body through their own overlay
        # paths; repainting here too would strip their highlights out from
        # under a scroll their own motions triggered.
        if not self._detail_find_active and not self._detail_visual_active:
            self._paint_detail_body()

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

        * Small JSON bodies are pretty-printed and rendered via
          :class:`rich.syntax.Syntax` with active light/dark theming.
        * Small Markdown bodies render via :class:`rich.markdown.Markdown`.
        * Larger formatted bodies and plain text use bounded ``Text``
          highlighting so search-term matches stay responsive.

        A record opened for the first time lands at the top; a record
        viewed before restores the scroll position the user left through
        ``DetailScroll.activate_record``.
        """
        self.workers.cancel_group(self, "detail")
        self._detail_generation += 1
        generation = self._detail_generation
        if self._detail_body is None:
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
        # A record switch cancels any in-flight visual select (its cursor +
        # anchor index the outgoing body's lines) and any native selection
        # anchored on the outgoing body. A repaint of the same record -- a
        # resize or theme change re-enters here -- leaves a selection the user
        # is still holding alone.
        self._reset_detail_visual(record_changed=record is not self._current_detail_record)
        self._current_detail_record = record
        self._detail_build_generation += 1
        detail_generation = self._detail_build_generation
        width = self._detail_render_width()
        identity = self._cached_detail_identity(record)
        header = self._build_detail_header(record, identity, width=width)
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
        # Drop the previous record's resident render so a copy-rendered (``Y``)
        # during the offload window falls back to the raw source rather than the
        # stale prior record's flattened text. ``_present_detail`` sets the real
        # values once the (possibly off-thread) build lands.
        self._detail_rendered_renderable = None
        self._detail_rendered_plain = body_truncated
        self._detail_find_source = ""
        self._detail_find_json_syntax = False
        search_style = self._match_style("search")
        filter_style = self._match_style("filter")
        syntax_theme = ui_theme.detail_syntax_theme(
            dark=self.app.current_theme.dark,
            theme_name=self.app.theme,
        )
        body = self._cached_detail_body(record, cache_key)
        render_request: DetailRenderRequest | None = None
        if body is None:
            # Route by format: JSON and markdown always offload (their Rich
            # tokenize/flatten is the pump-blocking work), and any large body
            # offloads regardless. Only a small plain-text body builds inline
            # so cursor navigation stays synchronous.
            fmt = detect_content_format(body_truncated)
            inline_ok = (
                fmt == "text"
                and len(body_truncated) <= self._DETAIL_ASYNC_BODY_THRESHOLD
                and not looks_like_code(body_truncated)
            )
            request = DetailRenderRequest(
                body_text=body_truncated,
                query_terms=query_terms,
                case_sensitive=case_sensitive,
                regex=regex,
                filter_terms=filter_terms,
                search_style=search_style,
                filter_style=filter_style,
                syntax_theme=syntax_theme,
                render_width=width,
                guess_code=not inline_ok,
            )
            if inline_ok:
                body = build_detail_body(request)
            else:
                render_request = request

        if body is None:
            # Raw mode can paint the resident source immediately; rendered
            # mode shows a blank body until the worker result lands.
            if self._detail_meta is not None:
                self._detail_meta.update(header)
            self._paint_detail_body()
        else:
            self._present_detail(
                record,
                header,
                body,
                query_terms,
                generation=detail_generation,
                cache_key=cache_key,
            )

        if identity is not None and render_request is None:
            return
        emit = _runtime.make_gated_emitter(
            self.app.call_from_thread,
            self._apply_prepared_detail,
            generation,
        )
        streaming = t.cast("StreamingAppLike", t.cast("object", self))
        streaming.run_worker(
            functools.partial(
                self._prepare_detail_in_thread,
                _DetailSnapshot(
                    record=record,
                    identity=identity,
                    render_request=render_request,
                    query_terms=query_terms,
                    cache_key=cache_key,
                ),
                emit,
            ),
            name="detail",
            group="detail",
            description="prepare record detail",
            thread=True,
            exclusive=True,
        )

    def _build_detail_header(
        self,
        record: SearchRecord,
        identity: RecordIdentity | None,
        *,
        width: int,
    ) -> Text:
        """Build the bounded metadata header for one selected record."""
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
        leading_rows: tuple[tuple[str, str, str], ...] = (
            ("Agent:", record.agent or "", agent_color),
            ("Kind:", record.kind or "", kind_color),
            ("Store:", record.store or "", dim_color),
            ("Adapter:", record.adapter_id or "", dim_color),
        )
        trailing_rows: list[tuple[str, str, str]] = [
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
                    trailing_rows.append(
                        (
                            label,
                            format_display_path(pathlib.Path(value), directory=True),
                            path_color,
                        ),
                    )
            if record.origin.branch:
                trailing_rows.append(("Branch:", record.origin.branch, dim_color))
            if record.origin.cwd_hash:
                trailing_rows.append(("Cwd hash:", record.origin.cwd_hash, dim_color))
        for label, value, value_style in leading_rows:
            header.append(f"{label} ", style="bold")
            header.append(f"{value}\n", style=value_style)
        for label, value in (
            ("Record:", None if identity is None else identity.record_id),
            ("Content:", None if identity is None else identity.content_id),
            ("Thread:", None if identity is None else identity.thread_id),
        ):
            header.append(f"{label} ", style="dim")
            if identity is None:
                header.append("…\n", style="dim")
            else:
                header.append(f"{value or '—'}\n")
        for label, value, value_style in trailing_rows:
            header.append(f"{label} ", style="bold")
            header.append(f"{value}\n", style=value_style)
        header.append("\n")
        return header

    def _cached_detail_identity(self, record: SearchRecord) -> RecordIdentity | None:
        """Return a retained-record identity cache hit, rejecting reused IDs."""
        cached = self._detail_identity_cache.get(id(record))
        if cached is None or cached[0] is not record:
            return None
        self._detail_identity_cache.move_to_end(id(record))
        return cached[1]

    def _detail_body_is_cached(self, query_terms: cabc.Sequence[str]) -> bool:
        """Return whether the detail body for the current record is memoized."""
        record = self._current_detail_record
        cache_key = self._detail_cache_key(query_terms, record)
        return record is not None and self._cached_detail_body(record, cache_key) is not None

    def _cached_detail_body(
        self,
        record: SearchRecord,
        cache_key: _DetailCacheKey | None,
    ) -> DetailRenderResult | None:
        """Return a retained-record cache hit, rejecting a reused object id."""
        if cache_key is None:
            return None
        cached = self._detail_body_cache.get(cache_key)
        if cached is None:
            return None
        cached_record, renderable, source, rendered_plain = cached
        if cached_record is not record:
            del self._detail_body_cache[cache_key]
            return None
        self._detail_body_cache.move_to_end(cache_key)
        return DetailRenderResult(renderable, source, rendered_plain)

    @_runtime.offload
    def _prepare_detail_in_thread(
        self,
        snapshot: _DetailSnapshot,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """Prepare missing identity/body data without reading pump-owned state."""
        identity = snapshot.identity
        if identity is None:
            from agentgrep.identity import record_identity

            identity = record_identity(snapshot.record)
        body = (
            None
            if snapshot.render_request is None
            else build_detail_body(snapshot.render_request)
        )
        emit(
            _PreparedDetail(
                record=snapshot.record,
                identity=identity,
                body=body,
                query_terms=snapshot.query_terms,
                cache_key=snapshot.cache_key,
                present_body=snapshot.render_request is not None,
            ),
        )

    @_runtime.pump_only
    def _apply_prepared_detail(self, generation: int, event: object) -> None:
        """Cache and paint one worker result when its exact selection is live."""
        if (
            generation != self._detail_generation
            or not isinstance(event, _PreparedDetail)
            or self._current_detail_record is not event.record
        ):
            return
        self._remember_detail_identity(event.record, event.identity)
        header = self._build_detail_header(
            event.record,
            event.identity,
            width=self._detail_render_width(),
        )
        if event.present_body and event.body is not None:
            self._present_detail(
                event.record,
                header,
                event.body,
                event.query_terms,
                generation=self._detail_build_generation,
                cache_key=event.cache_key,
            )
        else:
            self._replace_detail_header(header)

    def _remember_detail_identity(
        self,
        record: SearchRecord,
        identity: RecordIdentity,
    ) -> None:
        """Store one object-safe prepared identity in the bounded LRU."""
        key = id(record)
        self._detail_identity_cache[key] = (record, identity)
        self._detail_identity_cache.move_to_end(key)
        if len(self._detail_identity_cache) > self._DETAIL_CACHE_MAX:
            self._detail_identity_cache.popitem(last=False)

    def _replace_detail_header(self, header: Text) -> None:
        """Replace only the detail header, preserving the exact live body."""
        if self._detail_meta is None:
            return
        self._detail_header_text = header
        self._detail_meta.update(header)

    @_runtime.pump_only
    def _present_detail(
        self,
        record: SearchRecord,
        header: object,
        body: DetailRenderResult,
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
            self._detail_body is None
            or self._detail_meta is None
            or self._current_detail_record is not record
            or (generation is not None and generation != self._detail_build_generation)
        ):
            return
        body_renderable = body.renderable
        body_for_scroll = body.find_source
        rendered_plain = body.rendered_plain
        if cache_key is not None:
            self._detail_body_cache[cache_key] = (
                record,
                body_renderable,
                body_for_scroll,
                rendered_plain,
            )
            self._detail_body_cache.move_to_end(cache_key)
            if len(self._detail_body_cache) > self._DETAIL_CACHE_MAX:
                self._detail_body_cache.popitem(last=False)
        self._presented_detail_cache_key = cache_key
        # Resident representations for the raw/rendered toggle and copy: the
        # rendered renderable (styled Text / Syntax / highlighted Text) and its
        # flattened plain text (what ``Y`` copies).
        self._detail_rendered_renderable = body_renderable
        self._detail_rendered_plain = rendered_plain
        # The displayed text find searches/scrolls against — formatted JSON
        # for json bodies, the raw body otherwise.
        self._detail_find_source = body_for_scroll
        self._detail_find_json_syntax = isinstance(body_renderable, _RichSyntaxType)
        # A styled markdown render is a ``Text`` whose ``.plain`` is the
        # flattened document (headings/fences stripped) — its offsets do not
        # line up with ``body_for_scroll`` (the raw source find scans), so it
        # must NOT seed the find base. Only the plain/JSON-fallback ``Text``
        # (``.plain == body_for_scroll``) is a valid find base.
        if isinstance(body_renderable, Text) and body_renderable.plain == body_for_scroll:
            if cache_key is None:
                highlight_state = (
                    tuple(query_terms),
                    self.search_query.case_sensitive,
                    self.search_query.regex,
                    self._filter_terms,
                )
            else:
                _, terms, case_sensitive, regex, filter_terms, _width = cache_key
                highlight_state = (terms, case_sensitive, regex, filter_terms)
            self._detail_find_base = body_renderable
            self._detail_find_base_key = (
                body_for_scroll,
                *highlight_state,
            )
        else:
            self._detail_find_base = None
            self._detail_find_base_key = None
        self._detail_meta.update(t.cast("t.Any", header))
        self._paint_detail_body()
        if self._detail_scroll is not None:
            self._detail_scroll.activate_record(id(record))
        self._refresh_detail_statusline()
        if self._detail_find_active:
            # A same-record re-render (e.g. a theme switch re-renders the
            # current record) with find open just painted the plain body;
            # re-overlay the find highlights so they survive the re-render.
            self._present_detail_find()

    def _detail_render_width(self) -> int:
        """Return the pane width markdown is baked to (and keyed on).

        Falls back to 80 before first layout; the value feeds both the
        markdown render and :meth:`_detail_cache_key`, so a resize changes the
        key and the stale-width render can never be served from the LRU.
        """
        if self._detail_body is None:
            return 80
        return max(20, int(getattr(self._detail_body.size, "width", 0) or 80))

    @_runtime.pump_only
    def _clear_detail_panes(self) -> None:
        """Blank both detail Statics and drop the resident rendered body."""
        self._detail_rendered_renderable = None
        self._detail_rendered_plain = ""
        if self._detail_meta is not None:
            self._detail_meta.update("")
        if self._detail_body is not None:
            self._detail_body.update("")

    @_runtime.pump_only
    def _paint_detail_body(self) -> None:
        """Paint the body Static for the active raw/rendered mode.

        A cheap read of already-resident data: raw mode shows a plain ``Text``
        of the bounded source, rendered mode shows the resident rendered
        renderable. Never re-renders, parses, or spawns a worker (ADR 0011).
        The resident renderable is then overlaid with the ambient
        current-line indicator (:meth:`_paint_body_with_ambient_cursor`) --
        also a cheap, bounded pass, so this stays a paint-only method.
        """
        if self._detail_body is None:
            return
        if self._detail_raw_mode:
            base: Text | _RichSyntaxType = Text(self._detail_body_text, no_wrap=False)
        else:
            # ``None`` while a large body renders off-thread: paint blank until
            # the worker's present arrives (raw mode paints the source above).
            renderable = self._detail_rendered_renderable
            if renderable is None:
                self._detail_body.update("")
                return
            base = renderable
        self._detail_body.update(self._paint_body_with_ambient_cursor(base))

    def _detail_cache_key(
        self,
        query_terms: cabc.Sequence[str],
        record: SearchRecord | None = None,
    ) -> _DetailCacheKey | None:
        """Compose the LRU key for the current record + query + filter.

        Returns ``None`` when there is no current record (e.g. detail
        pane invoked before a record is highlighted) so callers know
        to skip the cache entirely. The filter terms are part of the key
        so changing the filter re-renders the filter-term highlights. The
        render width is part of the key because markdown is baked to a styled
        ``Text`` at a fixed width — without it a resize-then-revisit would
        serve a stale-width render from the LRU.
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
            self._detail_render_width(),
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
            background = ui_theme.resolve(theme_vars, "ag-match-find-bg")
            foreground = ui_theme.resolve(theme_vars, "ag-match-find-fg")
            if background and foreground:
                return f"bold {foreground} on {background}"
            return "bold black on magenta"
        if kind == "find-current":
            background = ui_theme.resolve(theme_vars, "ag-match-find-current-bg")
            foreground = ui_theme.resolve(theme_vars, "ag-match-find-current-fg")
            if background and foreground:
                return f"bold {foreground} on {background}"
            return "bold black on yellow"
        background = ui_theme.resolve(theme_vars, "ag-match-filter-bg")
        foreground = ui_theme.resolve(theme_vars, "ag-match-filter-fg")
        if background and foreground:
            return f"bold {foreground} on {background}"
        return "bold black on cyan"
