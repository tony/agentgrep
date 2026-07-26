"""Detail rendering for the default HUD layout."""

from __future__ import annotations

import contextlib
import functools
import json
import pathlib
import typing as t
from collections import abc as cabc

from rich.console import Console as _RichConsole
from rich.markdown import Markdown as _RichMarkdown
from rich.syntax import Syntax as _RichSyntax
from rich.text import Text

from agentgrep._text import (
    DETAIL_BODY_MAX_CHARS,
    DETAIL_BODY_MAX_LINES,
    detect_content_format,
    find_first_match_line,
    format_compact_path,
    format_display_path,
    looks_like_code,
    looks_like_markup,
    truncate_lines,
)
from agentgrep._types import StreamingAppLike
from agentgrep.records import SearchRecord
from agentgrep.ui import _runtime, _streaming, theme as ui_theme
from agentgrep.ui.format import scroll_percent
from agentgrep.ui.highlighter import MarkupHighlighter
from agentgrep.ui.layouts._base import LayoutScreen
from agentgrep.ui.widgets import DetailScrollChanged


class _DetailMatchStyles(t.NamedTuple):
    """Rich styles resolved on the pump before optional detail offload."""

    search: str
    filter: str


# The trailing ``int`` is the body render width: markdown is baked to a styled
# ``Text`` at a fixed pane width off-thread, so a resize-then-revisit must miss
# the LRU rather than return a stale-width render.
_DetailCacheKey = tuple[int, tuple[str, ...], bool, bool, tuple[str, ...], int]
# ``(renderable, source_for_find, rendered_plain)``: the paint renderable, the
# text find scans/scrolls against, and the flattened text ``Y`` copies.
_DetailBody = tuple[object, str, str]
_DETAIL_RICH_FORMAT_MAX_CHARS = 2048

#: Bytes of a body sampled for the off-pump Pygments language guess.
_CODE_GUESS_SAMPLE_BYTES = 4096

#: Minimum Pygments ``analyse_text`` confidence to syntax-highlight a body as
#: code. Real code scores near 1.0; prose scores ~0.01, so this rejects prose.
_CODE_GUESS_MIN_CONFIDENCE = 0.3
_RichSyntaxType = _RichSyntax


class _HudDetailBase(LayoutScreen):
    """Detail rendering base for the HUD layout."""

    def on_detail_scroll_changed(self, message: DetailScrollChanged) -> None:
        """Re-render the detail status line and remember the scroll position."""
        self._refresh_detail_statusline(message.percent)
        self._remember_detail_scroll()

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
        viewed before restores the scroll position the user left it at (see
        :meth:`_restore_detail_scroll`).
        """
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
        # anchor index the outgoing body's lines).
        self._reset_detail_visual()
        self._current_detail_record = record
        self._detail_build_generation += 1
        detail_generation = self._detail_build_generation
        width = self._detail_render_width()
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
        # Drop the previous record's resident render so a copy-rendered (``Y``)
        # during the offload window falls back to the raw source rather than the
        # stale prior record's flattened text. ``_present_detail`` sets the real
        # values once the (possibly off-thread) build lands.
        self._detail_rendered_renderable = None
        self._detail_rendered_plain = body_truncated
        self._detail_find_source = ""
        self._detail_find_json_syntax = False
        match_styles = _DetailMatchStyles(
            search=self._match_style("search"),
            filter=self._match_style("filter"),
        )
        syntax_theme = ui_theme.detail_syntax_theme(
            dark=self.app.current_theme.dark,
            theme_name=self.app.theme,
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
        # Route by format: JSON and markdown always offload (their Rich
        # tokenize/flatten is the pump-blocking work), and any large body
        # offloads regardless. Only a small plain-text body builds inline so
        # cursor navigation stays synchronous. ``detect_content_format`` is a
        # bounded scan over <=64 KiB (pump-safe).
        fmt = detect_content_format(body_truncated)
        inline_ok = (
            fmt == "text"
            and len(body_truncated) <= self._DETAIL_ASYNC_BODY_THRESHOLD
            and not looks_like_code(body_truncated)
        )
        if inline_ok:
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
                    syntax_theme=syntax_theme,
                    render_width=width,
                ),
                query_terms,
                generation=detail_generation,
                cache_key=cache_key,
            )
            return
        # Large / formatted uncached body: show the header now and build the
        # heavy renderable off the UI thread. ``exclusive=True`` cancels a prior
        # detail build, and ``_present_detail`` discards any result whose
        # record is no longer the one on screen.
        self._detail_meta.update(header)
        # Raw mode can paint the resident source immediately; rendered mode
        # shows a blank body until the worker's present arrives.
        self._paint_detail_body()
        streaming = t.cast("StreamingAppLike", t.cast("object", self))
        streaming.run_worker(
            functools.partial(
                self._build_detail_in_thread,
                record,
                header,
                body_truncated,
                query_terms,
                match_styles,
                syntax_theme,
                detail_generation,
                cache_key,
                case_sensitive,
                regex,
                filter_terms,
                width,
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
        cached_record, renderable, source, rendered_plain = cached
        if cached_record is not record:
            del self._detail_body_cache[cache_key]
            return None
        self._detail_body_cache.move_to_end(cache_key)
        return renderable, source, rendered_plain

    @_runtime.offload
    def _build_detail_in_thread(
        self,
        record: SearchRecord,
        header: object,
        body_truncated: str,
        query_terms: cabc.Sequence[str],
        match_styles: _DetailMatchStyles,
        syntax_theme: str,
        generation: int,
        cache_key: _DetailCacheKey | None,
        case_sensitive: bool,
        regex: bool,
        filter_terms: tuple[str, ...],
        render_width: int,
    ) -> None:
        """Build the detail body off the UI thread, then apply it on the loop.

        The ``rich.markdown.Markdown`` -> styled ``Text`` flatten and the JSON
        pretty-print both run here (off the pump), so a full-document render
        can never blow the frame budget (ADR 0011 NB-2).
        """
        body = self._build_detail_body(
            body_truncated,
            query_terms,
            match_styles,
            case_sensitive=case_sensitive,
            regex=regex,
            filter_terms=filter_terms,
            syntax_theme=syntax_theme,
            render_width=render_width,
            guess_code=True,
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
            self._detail_body is None
            or self._detail_meta is None
            or self._current_detail_record is not record
            or (generation is not None and generation != self._detail_build_generation)
        ):
            return
        body_renderable, body_for_scroll, rendered_plain = body
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
        self._restore_detail_scroll(record)
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
        """
        if self._detail_body is None:
            return
        if self._detail_raw_mode:
            self._detail_body.update(Text(self._detail_body_text, no_wrap=False))
        else:
            # ``None`` while a large body renders off-thread: paint blank until
            # the worker's present arrives (raw mode paints the source above).
            renderable = self._detail_rendered_renderable
            self._detail_body.update(renderable if renderable is not None else "")

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
        _streaming._apply_bounded_literal_highlights(
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
        syntax_theme: str = "ansi_dark",
        render_width: int = 80,
        guess_code: bool = False,
    ) -> _DetailBody:
        """Return ``(renderable, find_source, rendered_plain)`` for ``body_text``.

        ``find_source`` is whatever text the caller's ``find_first_match_line``
        should scan (pretty JSON for JSON, the raw body otherwise) so line
        indices line up with what the user sees. ``rendered_plain`` is the
        flattened text the ``Y`` copy command emits.

        Markdown is rendered off-thread to a styled ``rich.text.Text`` at
        ``render_width`` — cheap to paint, mouse-selectable, and flattened via
        ``.plain``. The ``rich.markdown.Markdown`` flatten is the pump-blocking
        work the caller offloads (ADR 0011); this method never runs it inline.
        """
        effective_case_sensitive = (
            self.search_query.case_sensitive if case_sensitive is None else case_sensitive
        )
        effective_regex = self.search_query.regex if regex is None else regex
        safe_query_terms = (
            ()
            if effective_regex
            else _streaming._bounded_literal_terms(
                query_terms,
                case_sensitive=effective_case_sensitive,
            )
        )
        fmt = detect_content_format(body_text)
        result: _DetailBody
        # Code takes precedence over the JSON/markdown heuristic: a source file
        # with a ``# comment`` line would otherwise trip the ATX-heading rule.
        # The guess is off the pump (only the offload worker sets guess_code).
        code_body: Text | None = None
        if guess_code and looks_like_code(body_text):
            lexer = self._guess_code_lexer(body_text)
            if lexer is not None:
                code_body = self._flatten_syntax(
                    body_text,
                    render_width=render_width,
                    lexer=lexer,
                    theme=syntax_theme,
                )
        if code_body is not None:
            _streaming._apply_bounded_literal_highlights(
                code_body,
                code_body.plain,
                safe_query_terms,
                case_sensitive=effective_case_sensitive,
                style=match_styles.search if match_styles else self._match_style("search"),
            )
            self._apply_filter_highlight(
                code_body,
                match_styles.filter if match_styles else None,
                terms=filter_terms,
            )
            result = (code_body, body_text, code_body.plain)
        elif fmt == "json":
            formatted = body_text
            if _streaming._json_pretty_print_is_bounded(body_text):
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
                    theme=syntax_theme,
                    word_wrap=True,
                    highlight_lines=highlight_lines,
                )
            else:
                plain = Text(formatted, no_wrap=False)
                _streaming._apply_bounded_literal_highlights(
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
            result = (renderable, formatted, formatted)
        elif fmt == "markdown":
            # Uncapped: the Markdown -> styled Text flatten runs off-thread
            # (this branch is only reached via the offload worker), so
            # ``_DETAIL_RICH_FORMAT_MAX_CHARS`` no longer gates markdown.
            styled = self._flatten_markdown(
                body_text,
                render_width=render_width,
                code_theme=syntax_theme,
            )
            _streaming._apply_bounded_literal_highlights(
                styled,
                styled.plain,
                safe_query_terms,
                case_sensitive=effective_case_sensitive,
                style=match_styles.search if match_styles else self._match_style("search"),
            )
            self._apply_filter_highlight(
                styled,
                match_styles.filter if match_styles else None,
                terms=filter_terms,
            )
            result = (styled, body_text, styled.plain)
        else:
            highlighted = Text(body_text, no_wrap=False)
            _streaming._apply_bounded_literal_highlights(
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
            # Structural-tag overlay for markup-shaped prompt bodies
            # (``<EPHEMERAL_MESSAGE>`` reminders and the like). Gated on a
            # paired-tag structural test so generics/comparisons in prose stay
            # plain, and applied by offset so ``highlighted.plain`` is unchanged.
            # This runs in the offload worker; the lexer is one bounded pass.
            if looks_like_markup(body_text):
                MarkupHighlighter(dark=syntax_theme != "ansi_light").highlight(highlighted)
            result = (highlighted, body_text, body_text)
        return result

    @staticmethod
    def _guess_code_lexer(body_text: str) -> str | None:
        """Return a Pygments lexer alias when ``body_text`` is confidently code.

        Runs only in the offload worker: ``guess_lexer`` scans every lexer's
        ``analyse_text`` (bounded but not pump-cheap). A leading sample drives
        the guess, and the confidence gate rejects prose (~0.01) while keeping
        real code (near 1.0). ``None`` means "render as plain text".
        """
        from pygments.lexers import guess_lexer
        from pygments.lexers.special import TextLexer
        from pygments.util import ClassNotFound

        sample = body_text[:_CODE_GUESS_SAMPLE_BYTES]
        with contextlib.suppress(ClassNotFound):
            lexer = guess_lexer(sample)
            if (
                not isinstance(lexer, TextLexer)
                and lexer.aliases
                and lexer.analyse_text(sample) >= _CODE_GUESS_MIN_CONFIDENCE
            ):
                return lexer.aliases[0]
        return None

    @staticmethod
    def _flatten_syntax(
        body_text: str,
        *,
        render_width: int,
        lexer: str,
        theme: str,
    ) -> Text:
        """Render code to a styled ``Text`` via a headless Rich ``Console``.

        Mirrors :meth:`_flatten_markdown` for a ``rich.syntax.Syntax``: the
        result paints cheaply, stays mouse-selectable, and ``.plain`` yields the
        visible source. Runs off the pump (offload worker only).
        """
        console = _RichConsole(
            width=max(1, render_width),
            color_system="truecolor",
            force_terminal=False,
            highlight=False,
            markup=False,
            emoji=False,
        )
        syntax = _RichSyntax(
            body_text,
            lexer,
            theme=theme,
            word_wrap=True,
            background_color="default",
        )
        styled = Text(no_wrap=False)
        for line in console.render_lines(syntax, pad=False):
            for segment in line:
                if segment.control or not segment.text:
                    continue
                styled.append(segment.text, segment.style)
            styled.append("\n")
        return styled

    @staticmethod
    def _flatten_markdown(
        body_text: str,
        *,
        render_width: int,
        code_theme: str,
    ) -> Text:
        """Render markdown to a styled ``Text`` via a headless Rich ``Console``.

        Rebuilds a ``Text`` from the rendered segments (text + style per
        segment, one newline per line) at ``render_width``. The result paints
        cheaply, is mouse-selectable once mounted, and flattens to ``.plain``
        for copy-rendered. Runs off the pump (called only from the offload
        worker), so no ``active_app`` context is required.
        """
        console = _RichConsole(
            width=max(1, render_width),
            color_system="truecolor",
            force_terminal=False,
            highlight=False,
            markup=False,
            emoji=False,
        )
        markdown = _RichMarkdown(body_text, code_theme=code_theme)
        styled = Text(no_wrap=False)
        for line in console.render_lines(markdown, pad=False):
            for segment in line:
                if segment.control or not segment.text:
                    continue
                styled.append(segment.text, segment.style)
            styled.append("\n")
        return styled

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
