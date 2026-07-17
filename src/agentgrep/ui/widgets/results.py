"""The streaming results list widget.

``SearchResultsList`` stores the complete ordered result model but renders it
through Textual's fixed-height ``ScrollView`` line API. Textual therefore asks
for only the rows in the viewport rather than materializing one ``Option`` per
record on the message pump.
"""

from __future__ import annotations

import collections
import typing as t
from collections import abc as cabc

import rich.text as rich_text
from rich.segment import Segment
from rich.style import Style
from rich.styled import Styled
from textual import events
from textual.geometry import Region, Size
from textual.reactive import reactive
from textual.scroll_view import ScrollView
from textual.strip import Strip

from agentgrep._text import format_compact_path
from agentgrep.discovery import format_timestamp_tig
from agentgrep.records import SearchRecord
from agentgrep.ui import _runtime, theme as ui_theme
from agentgrep.ui.format import scroll_percent
from agentgrep.ui.widgets.messages import ResultHighlighted, ResultsScrollChanged

__all__ = ["SearchResultsList"]

#: Rich style for a row's title span. Empty (regular weight) by design: bold is a
#: *selection* signal applied to the highlighted row via CSS (pi bolds the
#: selected line, not every line), so an always-on bold here would flatten the
#: agent → title → metadata hierarchy. One knob to dial row title weight.
_TITLE_STYLE = ""


class SearchResultsList(ScrollView, can_focus=True):
    """Fixed-height virtual results list with one globally-highlighted row."""

    ALLOW_SELECT = False
    COMPONENT_CLASSES: t.ClassVar[set[str]] = {
        "option-list--option",
        "option-list--option-highlighted",
    }
    DEFAULT_CSS = """
    SearchResultsList {
        background: ansi_default;
        padding: 0 1;
        scrollbar-size: 0 0;
    }
    """
    BINDINGS: t.ClassVar[list[tuple[str, str, str]]] = [
        ("up", "cursor_up", ""),
        ("down", "cursor_down", ""),
        ("home", "first", ""),
        ("end", "last", ""),
        ("pageup", "page_up", ""),
        ("pagedown", "page_down", ""),
        ("k", "cursor_up", "Up"),
        ("j", "cursor_down", "Down"),
        ("l", "focus_detail", "Detail"),
        ("right", "focus_detail", ""),
        ("g", "cursor_top", "Top"),
        ("G", "cursor_bottom", "Bottom"),
        ("ctrl+d", "cursor_half_page_down", "½ Down"),
        ("ctrl+u", "cursor_half_page_up", "½ Up"),
    ]

    highlighted: reactive[int | None] = reactive(None, repaint=False)

    _RENDER_CACHE_MAX: t.ClassVar[int] = 2_048
    _STRIP_CACHE_MAX: t.ClassVar[int] = 2_048

    def __init__(
        self,
        *,
        id: str | None = None,  # noqa: A002 -- forwarded to Textual's ``id`` kwarg
    ) -> None:
        super().__init__(id=id)
        self._records: list[SearchRecord] = []
        self._record_ids: set[int] = set()
        self._generation = 0
        self._next_highlight_programmatic = False
        # Rows bake theme hex, so the palette name participates in the key.
        # The LRU cap keeps filtering and theme switches from retaining one
        # renderable per result in an arbitrarily large history store.
        self._render_cache: collections.OrderedDict[
            tuple[str, int],
            rich_text.Text,
        ] = collections.OrderedDict()
        self._strip_cache: collections.OrderedDict[
            tuple[str, int, Style, int],
            Strip,
        ] = collections.OrderedDict()

    @property
    def option_count(self) -> int:
        """Return the number of result rows."""
        return len(self._records)

    @property
    def generation(self) -> int:
        """Return the generation of the current complete result model."""
        return self._generation

    def uses_records(self, records: list[SearchRecord]) -> bool:
        """Return whether ``records`` is the widget's current backing list."""
        return self._records is records

    @_runtime.pump_only
    def append_records(self, records: cabc.Sequence[SearchRecord]) -> None:
        """Append records without building row renderables on the pump."""
        if not records:
            return
        self._records.extend(records)
        self._record_ids.update(id(record) for record in records)
        self._sync_virtual_size()
        self.refresh()
        # Records now exist — leave the app's pre-search bare-canvas state so the
        # panes are visible (idempotent; the live search flow also does this at
        # launch).
        reveal = getattr(self.screen, "_set_empty_state", None)
        if callable(reveal):
            reveal(empty=False)

    @_runtime.pump_only
    def set_records(
        self,
        records: list[SearchRecord],
        *,
        record_ids: set[int],
    ) -> None:
        """Adopt a worker-prepared result model without rendering its rows."""
        self._generation += 1
        self._records = records
        self._record_ids = record_ids
        self._sync_virtual_size()

        highlighted = self.highlighted
        if not self._records:
            if highlighted is not None:
                self._next_highlight_programmatic = True
                self.highlighted = None
            self.scroll_home(animate=False, immediate=True)
        elif highlighted is not None:
            target = max(0, min(highlighted, len(self._records) - 1))
            if target == highlighted:
                self._post_result_highlighted(target, programmatic=True)
            else:
                self._next_highlight_programmatic = True
                self.highlighted = target

        self.refresh()

    @_runtime.pump_only
    def validate_highlighted(self, highlighted: int | None) -> int | None:
        """Clamp the global cursor to the current result model."""
        if highlighted is None or not self._records:
            return None
        return max(0, min(len(self._records) - 1, highlighted))

    @_runtime.pump_only
    def refresh_theme(self) -> None:
        """Invalidate visible lines after the application theme changes."""
        self.refresh()

    @_runtime.pump_only
    def clear(self) -> None:
        """Empty the list and release cached renderables."""
        self._generation += 1
        self._records = []
        self._record_ids.clear()
        self._render_cache.clear()
        self._strip_cache.clear()
        self.highlighted = None
        self._sync_virtual_size()
        self.scroll_home(animate=False, immediate=True)
        self.refresh()

    def contains_record(self, record: SearchRecord) -> bool:
        """Return whether ``record`` is in the current filtered result set."""
        return id(record) in self._record_ids

    def _sync_virtual_size(self) -> None:
        """Publish the current fixed-height row extent to ``ScrollView``."""
        self.virtual_size = Size(max(1, self.size.width), len(self._records))

    def _scroll_percent(self) -> int:
        """Compute the current scroll percent, clamped to ``[0, 100]``."""
        return scroll_percent(
            float(getattr(self, "scroll_y", 0) or 0),
            float(getattr(self, "max_scroll_y", 0) or 0),
        )

    def _post_scroll_changed(self, cursor: int | None = None) -> None:
        """Post a :class:`ResultsScrollChanged` snapshot to the app."""
        if cursor is None:
            cursor = self.highlighted
        self.post_message(
            ResultsScrollChanged(
                cursor=cursor,
                total=len(self._records),
                percent=self._scroll_percent(),
            ),
        )

    @_runtime.pump_only
    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Re-render the viewport and status line on scroll."""
        super().watch_scroll_y(old_value, new_value)
        self._post_scroll_changed()

    @_runtime.pump_only
    def watch_highlighted(
        self,
        old_highlighted: int | None,
        highlighted: int | None,
    ) -> None:
        """Refresh cursor rows, keep the cursor visible, and post detail state."""
        programmatic = self._next_highlight_programmatic
        self._next_highlight_programmatic = False
        if old_highlighted is not None:
            self.refresh_line(old_highlighted)
        if highlighted is not None:
            self.refresh_line(highlighted)
            if 0 <= highlighted < len(self._records):
                self.scroll_to_region(
                    Region(0, highlighted, max(1, self.size.width), 1),
                    animate=False,
                    force=True,
                    immediate=True,
                    x_axis=False,
                )
                self._post_result_highlighted(
                    highlighted,
                    programmatic=programmatic,
                )
        self._post_scroll_changed(cursor=highlighted)

    @_runtime.pump_only
    def _post_result_highlighted(self, index: int, *, programmatic: bool) -> None:
        """Post one generation-scoped result highlight message."""
        self.post_message(
            ResultHighlighted(
                record=self._records[index],
                index=index,
                generation=self._generation,
                programmatic=programmatic,
            ),
        )

    @_runtime.pump_only
    def on_resize(self, _event: events.Resize) -> None:
        """Keep virtual width aligned with the current content viewport."""
        self._sync_virtual_size()
        self.refresh()

    @_runtime.pump_only
    def on_click(self, event: events.Click) -> None:
        """Move the global cursor to the clicked visible row."""
        offset = event.get_content_offset(self)
        if offset is None:
            return
        index = self.scroll_offset.y + offset.y
        if 0 <= index < len(self._records):
            self.focus()
            if index == self.highlighted:
                self._post_result_highlighted(index, programmatic=False)
            else:
                self.highlighted = index

    @_runtime.pump_only
    def render_line(self, y: int) -> Strip:
        """Render one visible fixed-height row requested by ``ScrollView``."""
        width = max(0, self.size.width)
        index = self.scroll_offset.y + y
        if width == 0 or not 0 <= index < len(self._records):
            return Strip.blank(width, self.visual_style.rich_style)

        component = (
            "option-list--option-highlighted"
            if index == self.highlighted
            else "option-list--option"
        )
        style = self.get_component_rich_style(component)
        record = self._records[index]
        cache_key = (str(self.app.theme), width, style, id(record))
        cached = self._strip_cache.get(cache_key)
        if cached is not None:
            self._strip_cache.move_to_end(cache_key)
            return cached
        options = self.app.console.options.update(
            width=width,
            min_width=width,
            max_width=width,
            overflow="ellipsis",
            no_wrap=True,
            height=1,
        )
        lines = self.app.console.render_lines(
            Styled(self._render_record(record), style),
            options,
            pad=True,
        )
        if not lines:
            strip = Strip.blank(width, style)
        else:
            # Text with an explicitly empty span style may leave a Rich segment's
            # style as ``None``. Textual filters expect every custom line segment to
            # carry a concrete style, so inherit the row component style here.
            segments = [
                segment
                if segment.style is not None
                else Segment(segment.text, style, segment.control)
                for segment in lines[0]
            ]
            strip = Strip(segments, width)
        self._strip_cache[cache_key] = strip
        self._strip_cache.move_to_end(cache_key)
        while len(self._strip_cache) > self._STRIP_CACHE_MAX:
            self._strip_cache.popitem(last=False)
        return strip

    def _render_record(self, record: SearchRecord) -> rich_text.Text:
        """Return one rendered row from the bounded palette/identity LRU."""
        cache_key = (str(self.app.theme), id(record))
        cached = self._render_cache.get(cache_key)
        if cached is not None:
            self._render_cache.move_to_end(cache_key)
            return cached
        row = self._build_row(record)
        self._render_cache[cache_key] = row
        self._render_cache.move_to_end(cache_key)
        while len(self._render_cache) > self._RENDER_CACHE_MAX:
            self._render_cache.popitem(last=False)
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

    @_runtime.pump_only
    def action_cursor_up(self) -> None:
        """Release focus to the filter input when the cursor is at row 0."""
        if self.highlighted in (None, 0):
            self.app.action_focus_previous()
        else:
            self.highlighted -= 1

    @_runtime.pump_only
    def action_cursor_down(self) -> None:
        """Move the cursor down one row, selecting row zero from no cursor."""
        if not self._records:
            return
        current = self.highlighted
        self.highlighted = 0 if current is None else min(len(self._records) - 1, current + 1)

    @_runtime.pump_only
    def action_focus_detail(self) -> None:
        """Focus the detail pane, opening it first when stacked."""
        t.cast("t.Any", self.screen)._focus_detail()

    @_runtime.pump_only
    def action_first(self) -> None:
        """Move the cursor to the first row."""
        if self._records:
            self.highlighted = 0

    @_runtime.pump_only
    def action_last(self) -> None:
        """Move the cursor to the final row."""
        if self._records:
            self.highlighted = len(self._records) - 1

    @_runtime.pump_only
    def action_cursor_top(self) -> None:
        """Jump the highlight to the first row (vim-style ``g``)."""
        self.action_first()

    @_runtime.pump_only
    def action_cursor_bottom(self) -> None:
        """Jump the highlight to the last row (vim-style ``G``)."""
        self.action_last()

    def _cursor_jump(self, delta: int) -> None:
        """Move the highlight by ``delta`` rows, clamped to list bounds."""
        if not self._records:
            return
        current = self.highlighted if self.highlighted is not None else 0
        self.highlighted = max(0, min(len(self._records) - 1, current + delta))

    @_runtime.pump_only
    def action_cursor_half_page_down(self) -> None:
        """Advance the highlight by half the viewport height (vim ``Ctrl-D``)."""
        self._cursor_jump(max(1, self.size.height // 2))

    @_runtime.pump_only
    def action_cursor_half_page_up(self) -> None:
        """Move the highlight up by half the viewport height (vim ``Ctrl-U``)."""
        self._cursor_jump(-max(1, self.size.height // 2))

    @_runtime.pump_only
    def action_page_down(self) -> None:
        """Advance the cursor by one viewport."""
        self._cursor_jump(max(1, self.size.height))

    @_runtime.pump_only
    def action_page_up(self) -> None:
        """Move the cursor up by one viewport."""
        self._cursor_jump(-max(1, self.size.height))
