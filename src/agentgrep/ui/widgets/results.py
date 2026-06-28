"""The streaming results list widget.

``SearchResultsList`` is an ``OptionList`` subclass that renders normalized
records as colored rows. It imports Textual at the top but is imported only from
inside ``build_streaming_ui_app`` (and the tests), so ``import agentgrep`` stays
free of Textual (ADR 0010); the widget is unit-testable in isolation.
"""

from __future__ import annotations

import typing as t
from collections import abc as cabc

import rich.text as rich_text
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from agentgrep._engine.orchestration import cached_haystack
from agentgrep._text import format_compact_path
from agentgrep.discovery import format_timestamp_tig
from agentgrep.records import SearchRecord
from agentgrep.ui import theme as ui_theme
from agentgrep.ui.format import scroll_percent
from agentgrep.ui.widgets.messages import ResultsScrollChanged

__all__ = ["SearchResultsList"]

#: Rich style for a row's title span. Empty (regular weight) by design: bold is a
#: *selection* signal applied to the highlighted row via CSS (pi bolds the
#: selected line, not every line), so an always-on bold here would flatten the
#: agent → title → metadata hierarchy. One knob to dial row title weight.
_TITLE_STYLE = ""


class SearchResultsList(OptionList, can_focus=True):
    """``OptionList`` subclass for streaming agentgrep search records.

    ``OptionList`` is Textual's proven cursor-navigable virtual list. It
    ships with working Tab focus, a visible cursor highlight via the
    ``option-list--option-highlighted`` CSS class, and posts an
    ``OptionHighlighted`` message on cursor movement — all the things our
    previous custom widget had to wire up manually and failed at in the
    real terminal.

    Adding records via ``append_records`` / ``set_records`` runs on the
    event-loop thread because the worker uses ``app.call_from_thread`` to
    invoke these methods. That keeps the streaming transport off the
    Textual message bus so keystroke + timer events never queue behind it.
    """

    BINDINGS: t.ClassVar[list[tuple[str, str, str]]] = [
        ("k", "cursor_up", "Up"),
        ("j", "cursor_down", "Down"),
        ("l", "focus_detail", "Detail"),
        ("right", "focus_detail", ""),
        ("g", "cursor_top", "Top"),
        ("G", "cursor_bottom", "Bottom"),
        ("ctrl+d", "cursor_half_page_down", "½ Down"),
        ("ctrl+u", "cursor_half_page_up", "½ Up"),
    ]

    def __init__(
        self,
        *,
        id: str | None = None,  # noqa: A002 -- forwarded to Textual's ``id`` kwarg
    ) -> None:
        super().__init__(id=id)
        self._records: list[SearchRecord] = []
        # Rendered rows memoized by record id (rows bake theme hex but are stable
        # within a palette): a filter rebuild reuses them instead of paying the
        # dominant _render_record cost again. Cleared on clear() / rerender.
        self._render_cache: dict[int, rich_text.Text] = {}

    def append_records(self, records: cabc.Sequence[SearchRecord]) -> None:
        """Append a batch of records — invoked via ``app.call_from_thread``.

        Eagerly warms :func:`cached_haystack` for each new record so the
        cost is paid during streaming (when the user is already watching
        the spinner) rather than during the next filter keystroke.
        """
        if not records:
            return
        self._records.extend(records)
        for record in records:
            cached_haystack(record)
        self.add_options(
            [Option(self._render_record(r), id=str(id(r))) for r in records],
        )
        # Records now exist — leave the app's pre-search bare-canvas state so the
        # panes are visible (idempotent; the live search flow also does this at
        # launch).
        reveal = getattr(self.app, "_set_empty_state", None)
        if callable(reveal):
            reveal(empty=False)

    def set_records(self, records: cabc.Sequence[SearchRecord]) -> int:
        """Apply a new filter result by patching the existing options.

        For the common "user typed another character" narrowing case the
        method removes the now-unmatched options without rebuilding the
        list — keeps rendering O(removed) instead of O(total) and never
        touches the haystack cache. Falls back to a full rebuild when
        the new set introduces records not currently shown (widening) or
        when more than half of the current options would be removed
        (where ``remove_option_at_index`` would do worse than a single
        ``clear_options`` + ``add_options`` pair).

        Returns the number of programmatic ``OptionHighlighted`` messages
        Textual queued while applying the record update.
        """
        new_records = list(records)
        new_ids: set[int] = {id(record) for record in new_records}
        current_records = self._records
        if not current_records:
            self._rebuild_options(new_records)
            return 0
        current_index_by_id: dict[int, int] = {
            id(record): idx for idx, record in enumerate(current_records)
        }
        additions = [record for record in new_records if id(record) not in current_index_by_id]
        if additions:
            self._rebuild_options(new_records)
            return 0
        to_remove_indices = sorted(
            (
                current_index_by_id[id(record)]
                for record in current_records
                if id(record) not in new_ids
            ),
            reverse=True,
        )
        if len(to_remove_indices) > len(current_records) // 2:
            # More than half goes — a single clear+rebuild is cheaper
            # than N ``remove_option_at_index`` calls (each shifts the
            # internal options list).
            self._rebuild_options(new_records)
            return 0
        programmatic_highlights = 0
        for idx in to_remove_indices:
            before_highlighted = t.cast("int | None", getattr(self, "highlighted", None))
            self.remove_option_at_index(idx)
            after_highlighted = t.cast("int | None", getattr(self, "highlighted", None))
            if after_highlighted is not None and after_highlighted != before_highlighted:
                programmatic_highlights += 1
        self._records = new_records
        return programmatic_highlights

    def _rebuild_options(self, records: cabc.Sequence[SearchRecord]) -> None:
        """Full clear + rebuild path. Used when delta-apply isn't safe."""
        self._records = list(records)
        self.clear_options()
        if self._records:
            for record in self._records:
                cached_haystack(record)
            self.add_options(
                [Option(self._render_record(r), id=str(id(r))) for r in self._records],
            )

    def rerender_records(self) -> None:
        """Re-render the existing rows against the current theme tokens.

        The rows bake concrete hex into Rich renderables at build time, so
        a palette switch needs a full rebuild — drop the row cache first so the
        new palette is rendered.
        """
        self._render_cache.clear()
        self._rebuild_options(self._records)

    def clear(self) -> None:
        """Empty the list."""
        self._records = []
        self._render_cache.clear()
        self.clear_options()

    def _scroll_percent(self) -> int:
        """Compute the current scroll percent, clamped to ``[0, 100]``."""
        return scroll_percent(
            float(getattr(self, "scroll_y", 0) or 0),
            float(getattr(self, "max_scroll_y", 0) or 0),
        )

    def _post_scroll_changed(self, cursor: int | None = None) -> None:
        """Post a :class:`ResultsScrollChanged` snapshot to the app.

        ``cursor`` defaults to the widget's current ``highlighted``
        reactive but accepts an explicit override so watchers can pass
        the freshly-set value through without racing the reactive
        dispatch.
        """
        if cursor is None:
            cursor = t.cast("int | None", getattr(self, "highlighted", None))
        self.post_message(
            ResultsScrollChanged(
                cursor=cursor,
                total=len(self._records),
                percent=self._scroll_percent(),
            ),
        )

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Re-render the status line on scroll. Inherited base does the actual scroll."""
        base = getattr(super(), "watch_scroll_y", None)
        if callable(base):
            base(old_value, new_value)
        self._post_scroll_changed()

    def watch_highlighted(self, highlighted: int | None) -> None:
        """Re-render the status line on cursor move."""
        base = getattr(super(), "watch_highlighted", None)
        if callable(base):
            base(highlighted)
        self._post_scroll_changed(cursor=highlighted)

    def _render_record(self, record: SearchRecord) -> rich_text.Text:
        """Return the rendered row for ``record``, memoized by id."""
        cached = self._render_cache.get(id(record))
        if cached is not None:
            return cached
        row = self._build_row(record)
        self._render_cache[id(record)] = row
        return row

    def _build_row(self, record: SearchRecord) -> rich_text.Text:
        """Build the colored row renderable for ``record`` (the uncached path)."""
        theme_vars = t.cast("t.Any", self.app).theme_variables
        agent_style = ui_theme.resolve(
            theme_vars,
            ui_theme.AGENT_TOKEN_BY_NAME.get(record.agent or ""),
        )
        kind_style = ui_theme.resolve(
            theme_vars,
            ui_theme.KIND_TOKEN_BY_NAME.get(record.kind or ""),
        )
        dim_style = ui_theme.resolve(theme_vars, "ag-dim")
        muted_style = ui_theme.resolve(theme_vars, "ag-muted")
        agent_text = (record.agent or "").ljust(8)[:8]
        kind_text = (record.kind or "").ljust(10)[:10]
        timestamp_text = format_timestamp_tig(record.timestamp).ljust(22)[:22]
        title_text = (record.title or "").ljust(40)[:40]
        path_text = format_compact_path(record.path, max_width=60)
        text = rich_text.Text(no_wrap=True, overflow="ellipsis")
        text.append(agent_text, style=agent_style)
        text.append("  ")
        text.append(kind_text, style=kind_style)
        text.append("  ")
        text.append(timestamp_text, style=f"italic {dim_style}".rstrip())
        text.append("  ")
        text.append(title_text, style=_TITLE_STYLE)
        text.append("  ")
        text.append(path_text, style=muted_style)
        return text

    def action_cursor_up(self) -> None:
        """Release focus to the filter input when the cursor is at row 0."""
        if self.highlighted in (None, 0):
            self.app.action_focus_previous()
        else:
            super().action_cursor_up()

    def action_focus_detail(self) -> None:
        """Focus the detail pane (vim-style ``l``), opening it if stacked.

        Routes through the app's ``_focus_detail`` so a collapsed
        stacked pane is revealed before focus lands — focusing it
        directly would move focus into a ``display: none`` pane that
        never appears.
        """
        t.cast("t.Any", self.app)._focus_detail()

    def action_cursor_top(self) -> None:
        """Jump the highlight to the first row (vim-style ``g``)."""
        self.action_first()

    def action_cursor_bottom(self) -> None:
        """Jump the highlight to the last row (vim-style ``G``)."""
        self.action_last()

    def _cursor_jump(self, delta: int) -> None:
        """Move the highlight by ``delta`` rows, clamped to list bounds."""
        row_count = len(self._records)
        if row_count == 0:
            return
        current = self.highlighted if self.highlighted is not None else 0
        target = max(0, min(row_count - 1, current + delta))
        self.highlighted = target

    def action_cursor_half_page_down(self) -> None:
        """Advance the highlight by half the visible viewport height (vim ``Ctrl-D``)."""
        half = max(1, self.size.height // 2)
        self._cursor_jump(half)

    def action_cursor_half_page_up(self) -> None:
        """Move the highlight up by half the visible viewport height (vim ``Ctrl-U``)."""
        half = max(1, self.size.height // 2)
        self._cursor_jump(-half)
