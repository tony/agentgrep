"""The Ctrl-R search-history recall modal.

``HistoryRecall`` is the app's first :class:`~textual.screen.ModalScreen`: a
two-pane recall surface — a left list of past search queries (each row a
relative-time prefix + the query, fuzzy-highlighted) and a right preview of the
selected entry — with a bottom incremental filter and a footer hint. It owns all
keys while open, keeps the screen below visible, and ``dismiss(...)`` flows the
chosen query (or ``None`` on cancel) back to the app, which fills the search box.

The list is a capped, in-memory snapshot. Filtering is debounced and scored in
an exclusive thread worker so rapid typing never runs fuzzy ranking on Textual's
message pump. The query text is rendered as a plain
:class:`~textual.content.Content` (never ``from_markup``) so a query containing
``[...]`` is shown verbatim, and the fuzzy match offsets are stylized by hand —
mirroring the completion dropdown's ``markup=False`` guard.
"""

from __future__ import annotations

import functools
import time
import typing as t

import rapidfuzz.distance
import rapidfuzz.fuzz
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from agentgrep.ui import _runtime, theme as ui_theme
from agentgrep.ui._history import DISPLAY_LIMIT, HistoryEntry
from agentgrep.ui.format import format_relative_time
from agentgrep.ui.widgets.inputs import INPUT_MAX_LENGTH

if t.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["HistoryRecall"]

_AGE_WIDTH = 9
"""Fixed width of the relative-time prefix column so query text aligns."""

_ROW_TEXT_MAX_CHARS = 160
"""Maximum query characters projected into one list row."""

_FILTER_WORKER_GROUP = "history-recall-filter"
"""Stable worker group for supersedable history scoring."""


class _FilterSnapshot(t.NamedTuple):
    """Immutable inputs captured on the pump for one fuzzy score pass."""

    generation: int
    query: str
    entries: tuple[HistoryEntry, ...]


class _FilterRow(t.NamedTuple):
    """One worker-produced row ready for bounded pump-side rendering."""

    entry: HistoryEntry
    projected: str
    offsets: tuple[int, ...]


class _FilterResult(t.NamedTuple):
    """One immutable history-filter result returned to the pump."""

    query: str
    rows: tuple[_FilterRow, ...]


def _row_text(entry: HistoryEntry) -> str:
    """Project one history entry to the bounded single-line list surface."""
    first_line, separator, _rest = entry.text.partition("\n")
    truncated = bool(separator) or len(first_line) > _ROW_TEXT_MAX_CHARS
    result = first_line[: _ROW_TEXT_MAX_CHARS - int(truncated)]
    return f"{result}…" if truncated else result


def _subsequence_offsets(query: str, text: str) -> tuple[int, ...]:
    """Return linear case-insensitive subsequence offsets for decoration."""
    if not query:
        return ()
    needle = query.casefold()
    needle_index = 0
    offsets: list[int] = []
    for source_offset, char in enumerate(text):
        for folded_char in char.casefold():
            if folded_char != needle[needle_index]:
                continue
            if not offsets or offsets[-1] != source_offset:
                offsets.append(source_offset)
            needle_index += 1
            if needle_index == len(needle):
                return tuple(offsets)
    return ()


def _score_snapshot(snapshot: _FilterSnapshot) -> tuple[_FilterRow, ...]:
    """Score one immutable history snapshot, preserving newest-first ties."""
    if not snapshot.query:
        return tuple(_FilterRow(entry, _row_text(entry), ()) for entry in snapshot.entries)
    folded_query = snapshot.query.casefold()
    scored: list[tuple[float, _FilterRow]] = []
    for entry in snapshot.entries:
        folded_entry = entry.text.casefold()
        if rapidfuzz.distance.LCSseq.similarity(
            folded_query,
            folded_entry,
            score_cutoff=len(folded_query),
        ) != len(folded_query):
            continue
        projected = _row_text(entry)
        score = rapidfuzz.fuzz.ratio(
            folded_query,
            folded_entry,
            processor=None,
        )
        scored.append(
            (
                score,
                _FilterRow(
                    entry,
                    projected,
                    _subsequence_offsets(snapshot.query, projected),
                ),
            )
        )
    # Python's sort is stable, so equal scores retain the newest-first snapshot order.
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return tuple(row for _score, row in scored)


@_runtime.offload
def _score_and_deliver(
    snapshot: _FilterSnapshot,
    emit: cabc.Callable[[object], None],
) -> None:
    """Score off-pump and carry the snapshot generation back to the modal."""
    try:
        emit(_FilterResult(snapshot.query, _score_snapshot(snapshot)))
    except RuntimeError:
        # The app may finish while a cancelled thread drains its pure-CPU pass.
        return


class HistoryRecall(ModalScreen[t.Optional[str]]):  # noqa: UP045 -- Textual generic base needs a runtime subscript
    """Two-pane Ctrl-R recall over the persisted search-input history."""

    AUTO_FOCUS = "#history-filter"
    # The base declares this ``ClassVar[...] | None``; override with the same
    # shape (no bare ``ClassVar``, which the checker rejects in a union).
    HORIZONTAL_BREAKPOINTS: list[tuple[int, str]] | None = [(0, "-narrow"), (80, "-wide")]  # noqa: RUF012
    PREVIEW_ROWS: t.ClassVar[int] = 12
    _DEBOUNCE_SECONDS: t.ClassVar[float] = 0.15

    BINDINGS: t.ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
        # priority so ctrl+c stages here (clear/close) instead of the app's
        # ctrl+c -> smart_quit firing and quitting out from under the modal.
        Binding("ctrl+c", "filter_clear_or_cancel", "Clear / Cancel", priority=True, show=False),
        Binding("up", "nav_up", show=False),
        Binding("down", "nav_down", show=False),
        Binding("pageup", "nav_page_up", show=False),
        Binding("pagedown", "nav_page_down", show=False),
        Binding("home", "nav_home", show=False),
        Binding("end", "nav_end", show=False),
    ]

    DEFAULT_CSS = """
    HistoryRecall {
        align: center middle;
    }
    #history-dialog {
        width: 90%;
        max-width: 120;
        height: 80%;
        max-height: 24;
        padding: 0 1;
    }
    #history-title { height: 1; }
    #history-body { height: 1fr; }
    #history-list { width: 2fr; height: 1fr; }
    #history-preview-scroll { width: 3fr; height: 1fr; padding: 0 1; }
    #history-filter { height: 3; }
    #history-footer { height: 1; color: $text-muted; }
    HistoryRecall.-narrow #history-preview-scroll { display: none; }
    HistoryRecall.-narrow #history-list { width: 1fr; }
    """

    def __init__(
        self,
        entries: cabc.Sequence[HistoryEntry],
        *,
        seed: str = "",
        id: str | None = None,  # noqa: A002 -- Textual ``id`` kwarg
    ) -> None:
        super().__init__(id=id)
        self._entries = tuple(
            entry._replace(text=entry.text[:INPUT_MAX_LENGTH]) for entry in entries[:DISPLAY_LIMIT]
        )
        self._seed = seed[:_ROW_TEXT_MAX_CHARS]
        self._matches: list[HistoryEntry] = []
        self._now = int(time.time())
        self._filter_generation = 0
        self._applied_filter_generation = -1
        self._accept_generation: int | None = None
        self._filter_timer: Timer | None = None
        self._pending_filter: _FilterSnapshot | None = None
        # Content.stylize takes a style string; reverse-video by default,
        # recolored to the accent hex once the theme resolves on mount.
        self._match_style = "reverse"

    def compose(self) -> cabc.Iterator[t.Any]:
        """Lay out the title, the list+preview body, the filter, and the footer."""
        with Vertical(id="history-dialog"):
            yield Static("Search prompts", id="history-title")
            with Horizontal(id="history-body"):
                yield OptionList(id="history-list", markup=False)
                with VerticalScroll(id="history-preview-scroll"):
                    yield Static("", id="history-preview")
            yield Input(
                placeholder="Search history",
                id="history-filter",
                max_length=_ROW_TEXT_MAX_CHARS,
            )
            yield Static(
                "↑/↓ to navigate · Enter to use · Esc to cancel",
                id="history-footer",
            )

    @_runtime.pump_only
    def on_mount(self) -> None:
        """Resolve the highlight color, seed the filter, and render the first list."""
        accent = self._resolve_accent()
        if accent:
            self._match_style = f"{accent} bold"
        filter_input = self.query_one("#history-filter", Input)
        if self._seed:
            with filter_input.prevent(Input.Changed):
                filter_input.value = self._seed
        self._launch_refilter(self._new_snapshot(self._seed))

    @_runtime.pump_only
    def on_unmount(self) -> None:
        """Invalidate pending callbacks while Textual tears down the modal."""
        self._invalidate_refilter()

    def _resolve_accent(self) -> str:
        """Return the theme's accent hex, or ``""`` when it cannot be resolved."""
        try:
            return ui_theme.resolve(t.cast("t.Any", self.app).theme_variables, "accent")
        except AttributeError, KeyError, TypeError:
            return ""

    # --- filtering + rendering -------------------------------------------
    @_runtime.pump_only
    def _refilter(self, query: str) -> None:
        """Debounce one immutable query snapshot before fuzzy scoring."""
        snapshot = self._new_snapshot(query)
        self._pending_filter = snapshot
        if self._filter_timer is not None:
            self._filter_timer.stop()
        self._filter_timer = self.set_timer(
            self._DEBOUNCE_SECONDS,
            self._start_pending_refilter,
        )

    def _new_snapshot(self, query: str) -> _FilterSnapshot:
        """Invalidate older work and return the next immutable score request."""
        self._filter_generation += 1
        return _FilterSnapshot(
            self._filter_generation,
            query[:_ROW_TEXT_MAX_CHARS],
            self._entries,
        )

    @_runtime.pump_only
    def _start_pending_refilter(self) -> None:
        """Launch only the newest request after typing goes quiet."""
        self._filter_timer = None
        snapshot = self._pending_filter
        self._pending_filter = None
        if snapshot is not None and self.is_mounted:
            self._launch_refilter(snapshot)

    @_runtime.pump_only
    def _launch_refilter(self, snapshot: _FilterSnapshot) -> None:
        """Start one exclusive scorer over pump-captured immutable values."""
        emit = _runtime.make_gated_emitter(
            self.app.call_from_thread,
            self._apply_refilter,
            snapshot.generation,
        )
        self.run_worker(
            functools.partial(
                _score_and_deliver,
                snapshot,
                emit,
            ),
            name="history recall filter",
            group=_FILTER_WORKER_GROUP,
            description="Score one immutable history recall snapshot",
            thread=True,
            exclusive=True,
        )

    @_runtime.pump_only
    def _apply_refilter(
        self,
        generation: int,
        event: object,
    ) -> None:
        """Apply the newest worker result and discard stale generations."""
        if generation != self._filter_generation or not self.is_mounted:
            return
        result = t.cast("_FilterResult", event)
        option_list = self.query_one("#history-list", OptionList)
        option_list.clear_options()
        self._applied_filter_generation = generation
        self._matches = [row.entry for row in result.rows]
        if not self._matches:
            option_list.add_option(Option(self._empty_text(result.query), disabled=True))
            self.query_one("#history-preview", Static).update("")
        else:
            option_list.add_options(
                Option(self._row(row.entry, projected=row.projected, offsets=row.offsets))
                for row in result.rows
            )
            option_list.highlighted = 0
            self._update_preview(0)
        if self._accept_generation == generation:
            self._accept_generation = None
            self._accept(option_list.highlighted)

    @_runtime.pump_only
    def _invalidate_refilter(self) -> None:
        """Cancel the debounce and gate any draining worker result."""
        self._filter_generation += 1
        self._accept_generation = None
        self._pending_filter = None
        if self._filter_timer is not None:
            self._filter_timer.stop()
            self._filter_timer = None
        self.workers.cancel_group(self, _FILTER_WORKER_GROUP)

    def _row(
        self,
        entry: HistoryEntry,
        query: str = "",
        *,
        projected: str | None = None,
        offsets: cabc.Sequence[int] | None = None,
    ) -> Content:
        """Compose a list row: a dim relative-time prefix + the (highlighted) query."""
        prefix = f"{format_relative_time(entry.ts, self._now):<{_AGE_WIDTH}}"
        projected = _row_text(entry) if projected is None else projected
        content = Content(projected)
        match_offsets = _subsequence_offsets(query, projected) if offsets is None else offsets
        for offset in match_offsets:
            if not projected[offset].isspace():
                content = content.stylize(self._match_style, offset, offset + 1)
        return Content.assemble((prefix, "dim"), content)

    def _empty_text(self, query: str) -> str:
        """Return the disabled-row text: a no-match hint, or a no-history hint."""
        return "No matching prompts" if query else "No history yet"

    def _preview_content(self, entry: HistoryEntry) -> Content:
        """Render the right preview, truncating to a row budget with '+N lines'."""
        lines = entry.text.split("\n")
        if len(lines) > self.PREVIEW_ROWS:
            shown = lines[: self.PREVIEW_ROWS - 1]
            more = len(lines) - len(shown)
            body = "\n".join(shown)
            return Content.assemble(body + "\n", (f"+{more} lines", "dim"))
        return Content(entry.text)

    def _update_preview(self, index: int | None) -> None:
        """Repaint the preview pane for the highlighted row index."""
        preview = self.query_one("#history-preview", Static)
        if index is None or not (0 <= index < len(self._matches)):
            preview.update("")
            return
        preview.update(self._preview_content(self._matches[index]))

    # --- navigation (filter keeps focus; these drive the list) -----------
    def _set_highlight(self, index: int) -> None:
        if not self._matches:
            return
        option_list = self.query_one("#history-list", OptionList)
        option_list.highlighted = max(0, min(len(self._matches) - 1, index))

    def _move(self, delta: int) -> None:
        if not self._matches:
            return
        option_list = self.query_one("#history-list", OptionList)
        current = option_list.highlighted if option_list.highlighted is not None else 0
        self._set_highlight(current + delta)

    def action_nav_up(self) -> None:
        """Move the list selection up one row."""
        self._move(-1)

    def action_nav_down(self) -> None:
        """Move the list selection down one row."""
        self._move(1)

    def action_nav_page_up(self) -> None:
        """Move the list selection up a page."""
        self._move(-10)

    def action_nav_page_down(self) -> None:
        """Move the list selection down a page."""
        self._move(10)

    def action_nav_home(self) -> None:
        """Jump the selection to the newest row."""
        self._set_highlight(0)

    def action_nav_end(self) -> None:
        """Jump the selection to the oldest row."""
        self._set_highlight(len(self._matches) - 1)

    # --- messages --------------------------------------------------------
    def on_input_changed(self, event: Input.Changed) -> None:
        """Re-filter the list as the user types in the bottom filter."""
        if event.input.id == "history-filter":
            self._refilter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the filter accepts the highlighted row."""
        if event.input.id == "history-filter":
            option_list = self.query_one("#history-list", OptionList)
            self._accept(option_list.highlighted)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Drive the preview pane from the newly highlighted row."""
        self._update_preview(event.option_index)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Accept a clicked or selected row."""
        self._accept(event.option_index)

    def _accept(self, index: int | None) -> None:
        """Dismiss with the chosen query text, or ``None`` when there is none."""
        if self._applied_filter_generation != self._filter_generation:
            self._accept_generation = self._filter_generation
            if self._pending_filter is not None:
                self._start_pending_refilter()
            return
        self._invalidate_refilter()
        if index is None or not (0 <= index < len(self._matches)):
            self.dismiss(None)
            return
        self.dismiss(self._matches[index].text)

    def action_cancel(self) -> None:
        """Escape cancels: dismiss with ``None`` so the search box is left as-is."""
        self._invalidate_refilter()
        self.dismiss(None)

    def action_filter_clear_or_cancel(self) -> None:
        """Staged ctrl-c: clear the filter if it has text, else close the modal.

        Mirrors the app's input ctrl-c (text → clear; empty → close), but the
        modal's "exit" is closing itself. Setting ``value = ""`` re-fires
        ``Input.Changed`` → :meth:`on_input_changed` → ``_refilter("")``, so the
        full list repaints with no manual re-trigger.
        """
        filter_input = self.query_one("#history-filter", Input)
        if filter_input.value:
            filter_input.value = ""
            return
        self._invalidate_refilter()
        self.dismiss(None)
