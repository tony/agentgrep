"""Detail interaction for the default HUD layout."""

from __future__ import annotations

import contextlib
import re
import typing as t

from rich.syntax import Syntax as _RichSyntax
from rich.text import Text
from textual import events
from textual.content import Content
from textual.geometry import Offset
from textual.selection import Selection

from agentgrep.ui import _runtime, _streaming, theme as ui_theme
from agentgrep.ui._detail_render import apply_filter_highlight
from agentgrep.ui.layouts._hud_detail import (
    _DetailCacheKey as _DetailCacheKey,
    _HudDetailBase,
)
from agentgrep.ui.widgets import DetailFindRequested

_DetailFindBaseKey = tuple[str, tuple[str, ...], bool, bool, tuple[str, ...]]

#: Keys that yank and leave visual mode. ``y`` / ``enter`` are the tmux
#: copy-mode-vi verbs; the rest mirror ``COPY_SELECTION_BINDING`` so the chord
#: that copies a mouse selection also copies a visual one. In visual mode the
#: detail pane consumes these before the layout's ctrl+c reaches stop/quit.
_VISUAL_YANK_KEYS = frozenset({"y", "enter", "ctrl+c", "super+c", "ctrl+shift+c", "shift+super+c"})

#: Background-only tint for the ambient reading-position line. Background
#: alone -- no foreground -- so it never competes with the search highlight
#: (gold fg only); filter and find both also tint the background
#: (``bg+fg``), so :meth:`_paint_body_with_ambient_cursor` applies this via
#: ``stylize_before`` rather than ``stylize`` -- an earlier-applied span
#: loses the background channel to a later one, and filter/find are already
#: baked into ``base`` by the time this runs, so this band must apply
#: *before* them in span order to lose the channel rather than win it. Only
#: the least assertive tier of the muted/dim/faint text triad
#: (``$ag-faint``, see ``theme.py``'s ``_TEXT_HUES``) is calibrated for a
#: marker that is always on rather than actively selected. Left as the
#: unresolved ``$token`` (not a concrete hex via ``ui_theme.resolve``) so it
#: is applied to a :class:`~textual.content.Content` span after conversion,
#: matching :func:`agentgrep.ui.widgets.welcome.depth_offer_content`'s cursor
#: pattern -- Textual's own renderer resolves ``$ag-faint`` at paint time,
#: and the literal string stays inspectable in ``.visual.spans`` for tests.
_DETAIL_CURSOR_LINE_STYLE = "on $ag-faint"


class _HudDetailInteractionBase(_HudDetailBase):
    """Detail interaction base for the HUD layout."""

    @_runtime.pump_only
    def action_toggle_detail_raw(self) -> None:
        """Flip the session-global raw/rendered mode and repaint (no re-render).

        Delegated to from :meth:`DetailScroll.action_toggle_raw`
        (``alt+r`` / ``ctrl+e``). Both representations are resident, so this is
        a pump-safe repaint.
        """
        if self._current_detail_record is None:
            return
        self._detail_raw_mode = not self._detail_raw_mode
        if self._detail_find_active:
            # Find is open: it already overlays the raw source, so a toggle
            # here just re-paints the find view. Exact-source selection stays
            # available by closing find first, then toggling.
            self._present_detail_find()
        else:
            self._paint_detail_body()
        self.notify("raw source" if self._detail_raw_mode else "rendered")

    @_runtime.pump_only
    def action_copy_detail_source(self) -> None:
        """Copy the bounded raw source (``y``), independent of the active view.

        Encodes the already-truncated ``_detail_body_text`` (<=64 KiB), never
        the uncapped ``record.text`` — ``App.copy_to_clipboard`` base64-encodes
        on the calling (pump) thread, so an unbounded encode would block the
        pump (ADR 0011 NB-2). Delivery is a bounded OSC-52 write.
        """
        if self._current_detail_record is None:
            return
        truncated = len(self._current_detail_record.text) > len(self._detail_body_text)
        self.send_to_clipboard(self._detail_body_text, label="source", truncated=truncated)

    @_runtime.pump_only
    def action_copy_detail_rendered(self) -> None:
        """Copy the flattened rendered text (``Y``): markdown flattened, JSON pretty."""
        if self._current_detail_record is None:
            return
        self.send_to_clipboard(self._detail_rendered_plain, label="rendered text")

    # -- tmux copy-mode-vi visual select (native Textual selection) ----------

    @_runtime.pump_only
    def handle_detail_visual_key(self, event: events.Key) -> bool:
        """Route a detail-pane key for tmux-style visual select.

        Delegated to from :meth:`DetailScroll.on_key`. Returns ``True`` when the
        key was consumed so the caller stops the event and the normal
        scroll/copy bindings do not also fire; ``False`` lets them run.

        Outside visual mode only ``v`` / ``space`` are claimed (they begin a
        selection); every other key falls through to the stock bindings. Inside
        visual mode the vi motions (``hjkl`` / ``0`` / ``$`` / ``g`` / ``G``)
        move the selection cursor, ``y`` / ``enter`` and the copy chords yank,
        and ``escape`` / ``q`` cancel. Each branch is O(1) plus a bounded
        re-render (NB-9).
        """
        key = event.key
        char = event.character
        if not self._detail_visual_active:
            if key in {"v", "space"}:
                self._begin_detail_visual()
                return True
            return False
        if key in {"escape", "q"}:
            self._cancel_detail_visual()
        elif key in _VISUAL_YANK_KEYS:
            self._yank_detail_visual()
        elif key in {"v", "space"}:
            self._detail_visual_anchor = self._detail_visual_cursor
            self._render_detail_visual()
        elif key in {"h", "left"}:
            self._move_detail_visual(0, -1)
        elif key in {"l", "right"}:
            self._move_detail_visual(0, 1)
        elif key in {"j", "down"}:
            self._move_detail_visual(1, 0)
        elif key in {"k", "up"}:
            self._move_detail_visual(-1, 0)
        elif key in {"0", "home"} or char == "0":
            self._visual_line_edge(start=True)
        elif key in {"dollar_sign", "end"} or char == "$":
            self._visual_line_edge(start=False)
        elif key == "g":
            self._visual_document_edge(top=True)
        elif key == "G":
            self._visual_document_edge(top=False)
        else:
            return False
        return True

    def _visual_clamp_col(self, row: int, col: int) -> int:
        """Clamp ``col`` onto a character of line ``row`` (vi never sits past EOL)."""
        line = self._detail_visual_lines[row] if 0 <= row < len(self._detail_visual_lines) else ""
        return max(0, min(col, max(0, len(line) - 1)))

    def _visual_top_visible_row(self) -> int:
        """Logical source line at the top of the viewport (wrap-aware).

        Walks the source lines accumulating each one's wrapped display-row count
        until it passes the scrolled-past rows -- the first still-visible logical
        line. Bounded by ``scroll_y``, so it never touches the message pump per
        motion. Accurate for a raw/plain body; a close estimate for a
        markdown/code body whose rendered scroll maps only approximately onto
        source lines.

        Used both to seed ``v`` (visual select) and, continuously, to paint the
        ambient current-line indicator -- so outside visual mode (before
        ``_detail_visual_lines`` has been seeded) it falls back to splitting the
        resident bounded body text on demand rather than tracking a second copy
        of the same lines.
        """
        scroll_y = int(getattr(self._detail_scroll, "scroll_y", 0) or 0)
        if scroll_y <= 0 or self._detail_body is None:
            return 0
        width = max(1, int(getattr(self._detail_body.size, "width", 80) or 80))
        lines = self._detail_visual_lines or tuple(self._detail_body_text.splitlines() or [""])
        consumed = 0
        for index, line in enumerate(lines):
            rows = max(1, -(-len(line) // width))
            if consumed + rows > scroll_y:
                return index
            consumed += rows
        return max(0, len(lines) - 1)

    def _detail_cursor_line_span(self) -> tuple[int, int] | None:
        """Return the raw-body ``(start, end)`` char offsets of the top visible row."""
        lines = self._detail_body_text.splitlines(keepends=True)
        row = self._visual_top_visible_row()
        if not 0 <= row < len(lines):
            return None
        start = sum(len(line) for line in lines[:row])
        return start, start + len(lines[row].rstrip("\n"))

    def _paint_body_with_ambient_cursor(
        self, base: Text | _RichSyntax
    ) -> Text | _RichSyntax | Content:
        """Overlay the ambient reading-position line onto ``base`` while focused.

        The highlighted row is a byproduct of scroll position
        (:meth:`_visual_top_visible_row`, the same helper that seeds ``v``) --
        there is no independent cursor state here, so only a scroll or a focus
        change ever moves it. Mirrors :meth:`DepthOffer.watch_has_focus
        <agentgrep.ui.widgets.welcome.DepthOffer.watch_has_focus>`: painted
        only while the pane holds focus, hidden the instant it doesn't, and
        suppressed while visual select owns the body's plain-source ``Text``
        (:meth:`_begin_detail_visual`).

        Skipped for a flattened markdown/code render or a small-JSON
        :class:`~rich.syntax.Syntax` object, whose rendered offsets do not
        line up 1:1 with the raw body -- the same invariant
        :meth:`_present_detail` uses to gate the find base. Toggling to raw
        mode (``alt+r``) still shows the line there.
        """
        scroll = self._detail_scroll
        if (
            scroll is None
            or not scroll.has_focus
            or self._detail_visual_active
            or not isinstance(base, Text)
            or base.plain != self._detail_body_text
        ):
            return base
        span = self._detail_cursor_line_span()
        if span is None:
            return base
        content = Content.from_rich_text(base, console=self.app.console)
        return content.stylize_before(_DETAIL_CURSOR_LINE_STYLE, *span)

    @_runtime.pump_only
    def _begin_detail_visual(self) -> None:
        """Enter visual mode: anchor at the cursor and paint the selectable source.

        Presents the bounded raw source as a plain ``Text`` so native selection
        renders and extracts identically for text, markdown, and JSON bodies —
        ``y`` then yanks an exact source substring, mirroring the whole-source
        ``y`` command over a range.
        """
        if self._current_detail_record is None or self._detail_body is None:
            return
        source = self._detail_body_text
        if not source:
            return
        self._detail_visual_lines = tuple(source.splitlines() or [""])
        # Seed the anchor at the top of the current viewport so ``v`` begins
        # where the eye is on a long body instead of at line 0.
        row = self._visual_top_visible_row()
        col = 0
        self._detail_visual_cursor = (row, col)
        self._detail_visual_anchor = (row, col)
        self._detail_visual_active = True
        self._detail_body.update(Text(source, no_wrap=False))
        self._render_detail_visual()
        self.notify("visual select — hjkl move, y yank, esc cancel")

    @_runtime.pump_only
    def _move_detail_visual(self, drow: int, dcol: int) -> None:
        """Move the selection cursor by ``drow`` lines / ``dcol`` columns."""
        row, col = self._detail_visual_cursor
        if drow:
            row = max(0, min(row + drow, len(self._detail_visual_lines) - 1))
            col = self._visual_clamp_col(row, col)
        if dcol:
            col = self._visual_clamp_col(row, col + dcol)
        self._detail_visual_cursor = (row, col)
        self._render_detail_visual()
        self._follow_detail_visual_cursor()

    @_runtime.pump_only
    def _visual_line_edge(self, *, start: bool) -> None:
        """Move the cursor to column 0 (``0``) or the last char (``$``)."""
        row = self._detail_visual_cursor[0]
        line = self._detail_visual_lines[row] if self._detail_visual_lines else ""
        col = 0 if start else max(0, len(line) - 1)
        self._detail_visual_cursor = (row, col)
        self._render_detail_visual()
        self._follow_detail_visual_cursor()

    @_runtime.pump_only
    def _visual_document_edge(self, *, top: bool) -> None:
        """Move the cursor to the first (``g``) or last (``G``) line."""
        row = 0 if top else max(0, len(self._detail_visual_lines) - 1)
        col = self._visual_clamp_col(row, 0)
        self._detail_visual_cursor = (row, col)
        self._render_detail_visual()
        self._follow_detail_visual_cursor()

    def _detail_visual_selection(self) -> Selection:
        """Build the inclusive :class:`Selection` for the anchor..cursor range.

        tmux copy-mode selection includes the cell under the cursor, so the
        higher offset's column is bumped by one (Textual selection ends are
        exclusive). ``extract`` and the render both slice-clamp, so the bump is
        safe at end-of-line.
        """
        (loy, lox), (hiy, hix) = sorted(
            (self._detail_visual_anchor, self._detail_visual_cursor),
        )
        return Selection(Offset(lox, loy), Offset(hix + 1, hiy))

    @_runtime.pump_only
    def _render_detail_visual(self) -> None:
        """Publish the current selection to the screen (Textual paints the highlight).

        Reassigns ``screen.selections`` only; the body's resident ``Text`` is
        re-rendered with a per-logical-line span (no re-tokenize, NB-9).
        """
        if self._detail_body is None:
            return
        self.screen.selections = {
            t.cast("t.Any", self._detail_body): self._detail_visual_selection(),
        }

    @_runtime.pump_only
    def _yank_detail_visual(self) -> None:
        """Copy the selected source substring (``y`` / Enter) and exit visual mode.

        Reads the widget's native ``get_selection`` over the active
        :class:`Selection` (an exact ``(text, ending)`` extract), then copies
        via OSC-52 ``copy_to_clipboard``. Bounded: the source is already
        <=64 KiB and the read is O(selected).
        """
        if self._detail_body is None:
            self._cancel_detail_visual()
            return
        selection = self._detail_visual_selection()
        extracted = t.cast("t.Any", self._detail_body).get_selection(selection)
        text = (
            extracted[0]
            if extracted is not None
            else selection.extract(
                self._detail_body_text,
            )
        )
        self._cancel_detail_visual()
        self.send_to_clipboard(text, label="selection")

    @_runtime.pump_only
    def _cancel_detail_visual(self) -> None:
        """Exit visual mode (``escape`` / ``q``): clear the selection and repaint."""
        if not self._detail_visual_active:
            return
        self._detail_visual_active = False
        self.screen.clear_selection()
        if self._detail_find_active:
            self._present_detail_find()
        else:
            self._paint_detail_body()

    def _reset_detail_visual(self, *, record_changed: bool = True) -> None:
        """Drop visual state on a record switch or fresh search (clears highlight).

        Parameters
        ----------
        record_changed : bool
            Whether a different record is being presented. A repaint of the
            same record -- a resize or a theme change -- passes ``False`` so it
            keeps a selection the user is still holding.
        """
        if self._detail_visual_active:
            self._detail_visual_active = False
            with contextlib.suppress(Exception):
                self.screen.clear_selection()
        elif record_changed:
            self._clear_stale_body_selection()
        self._detail_visual_anchor = (0, 0)
        self._detail_visual_cursor = (0, 0)
        self._detail_visual_lines = ()

    def _clear_stale_body_selection(self) -> None:
        """Drop a native (mouse) selection anchored on the outgoing record.

        ``#detail-body`` is one reused widget, so Textual's offsets outlive the
        switch and re-target the *incoming* body's characters at the outgoing
        body's coordinates. The highlight migrates with them, so a copy returns
        text from a record the user never selected.

        Reading ``_detail_body`` before the pane is composed is not an error
        worth raising here, hence the guard: this runs on every record switch,
        including the first.
        """
        if self._detail_body is None:
            return
        with contextlib.suppress(Exception):
            if self._detail_body in self.screen.selections:
                self.screen.clear_selection()

    @_runtime.pump_only
    def _follow_detail_visual_cursor(self) -> None:
        """Best-effort scroll so the selection cursor's line stays in view.

        Logical-line based; exact for unwrapped source and approximate under
        wrapping. Guarded so a scroll hiccup never breaks a motion.
        """
        scroll = self._detail_scroll
        if scroll is None:
            return
        row = self._detail_visual_cursor[0]
        with contextlib.suppress(Exception):
            height = int(getattr(scroll.size, "height", 0) or 0)
            top = float(getattr(scroll, "scroll_y", 0) or 0)
            if row < top:
                scroll.scroll_to(y=row, animate=False)
            elif height and row >= top + height:
                scroll.scroll_to(y=max(0, row - height + 1), animate=False)

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
        Only the body Static repaints — the metadata header stays put, so a
        find keystroke no longer re-renders the header.
        """
        if self._detail_body is None or self._current_detail_record is None:
            return
        source = self._detail_find_source or self._detail_body_text
        text = self._detail_find_base_for(source).copy()
        find_style = self._match_style("find")
        current_style = self._match_style("find-current")
        for index, (start, end) in enumerate(self._detail_find_matches):
            style = current_style if index == self._detail_find_current else find_style
            text.stylize(style, start, end)
        self._detail_body.update(t.cast("t.Any", text))

    def _detail_find_base_for(self, source: str) -> Text:
        """Return the syntax+search+filter body for ``source`` and highlight state.

        Small JSON bodies are syntax-highlighted via :class:`rich.syntax.Syntax`
        so token colors survive find. Other renderables use bounded literal
        highlighting. The find-match overlay changes per keystroke/step but
        this base does not, so retaining or building it once keeps repeated
        highlighting off the message pump.
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
            syntax_theme = ui_theme.detail_syntax_theme(
                dark=self.app.current_theme.dark,
                theme_name=self.app.theme,
            )
            text = _RichSyntax(source, "json", theme=syntax_theme, word_wrap=True).highlight(source)
            text.no_wrap = False
            self._apply_search_highlight(text)
        else:
            text = Text(source, no_wrap=False)
            _streaming._apply_bounded_literal_highlights(
                text,
                source,
                () if self.search_query.regex else self.search_query.terms,
                case_sensitive=self.search_query.case_sensitive,
                style=self._match_style("search"),
            )
        apply_filter_highlight(
            text,
            terms=self._filter_terms,
            style=self._match_style("filter"),
        )
        self._detail_find_base = text
        self._detail_find_base_key = key
        return text

    def _apply_search_highlight(self, text: t.Any) -> None:
        """Overlay the active search-query terms onto ``text`` (for the JSON path).

        The plain-text path bakes these through the bounded literal helper; on
        the Syntax-highlighted JSON ``Text`` literal terms are layered with
        the same style. Regex terms are omitted because presentation must not
        re-run an untrusted pattern on the message pump.
        """
        if self.search_query.regex:
            return
        _streaming._apply_bounded_literal_highlights(
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
        if self._detail_body is not None:
            width = int(getattr(self._detail_body.content_size, "width", 0) or 0)
        width = max(1, width)
        try:
            return self._wrap_aware_row(offset, width, header_text, body)
        except Exception:
            return header_text.count("\n") + body.count("\n", 0, offset)

    @staticmethod
    def _wrap_aware_row(offset: int, width: int, header_text: str, body: str) -> int:
        """Count no-wrap header rows, then wrapped body rows to ``offset``."""
        from rich._wrap import divide_line

        def rows(line: str) -> int:
            return len(divide_line(line, width)) + 1

        row = header_text.count("\n")
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
        self._reset_detail_find_state()
        record = self._current_detail_record
        if record is not None:
            # Re-render via show_detail so a large uncached body offloads to a
            # worker instead of building inline on the pump (ADR 0011 NB-9),
            # and the match-style snapshot contract is honored. DetailScroll
            # owns the active record offset, so the repaint does not jump.
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
