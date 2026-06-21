"""Search progress reporting: console chrome and streaming TUI events.

Thread-safe search controls, the SearchProgress protocol with its no-op and
ANSI-console implementations, the progress-line formatters, and the
pydantic-backed streaming event payloads the TUI consumes. Depends on the text
presentation layer (AnsiColors, truncation) and the record types; it sits below
the engine, which drives it, and below the frontends. See ADR 0010.
"""

from __future__ import annotations

import dataclasses
import itertools
import os
import pathlib
import select
import shutil
import sys
import threading
import time
import typing as t

import pydantic

from agentgrep._text import (
    AnsiColors,
    _hard_truncate_ansi,
    _visible_width,
    format_display_path,
)
from agentgrep.records import SearchRecord  # runtime: pydantic payload field type

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from agentgrep._types import SearchColors
    from agentgrep.records import ColorMode, SearchQuery, SourceHandle

__all__ = [
    "AnswerNowInputListener",
    "ConsoleSearchProgress",
    "FilterCompletedPayload",
    "FilterRequestedPayload",
    "NoopSearchProgress",
    "ProgressSnapshot",
    "ProgressUpdatedPayload",
    "RecordsAppendedPayload",
    "SearchControl",
    "SearchFinishedPayload",
    "SearchProgress",
    "SearchRequestedPayload",
    "SourceProgressCallback",
    "StreamingRecordsBatch",
    "StreamingSearchFinished",
    "StreamingSearchProgress",
    "format_match_count",
    "format_search_progress_line",
    "format_source_progress_detail",
    "noop_search_progress",
]


type SourceProgressCallback = cabc.Callable[[int, int, SourceHandle, int, int], None]

_SOURCE_PROGRESS_RECORD_INTERVAL = 128
"""Parsed-record cadence for in-source progress updates and GIL yields."""


class SearchControl:
    """Thread-safe cooperative controls for an active search."""

    def __init__(self) -> None:
        self._answer_now = threading.Event()

    def request_answer_now(self) -> None:
        """Request that search return the results collected so far."""
        self._answer_now.set()

    def answer_now_requested(self) -> bool:
        """Return whether search should stop and answer with partial results."""
        return self._answer_now.is_set()


class AnswerNowInputListener:
    """Listen for a blank Enter keypress and request a partial answer."""

    def __init__(
        self,
        control: SearchControl,
        *,
        stream: t.TextIO | None = None,
        poll_interval: float = 0.1,
    ) -> None:
        self._control = control
        self._stream = stream if stream is not None else sys.stdin
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start listening for a blank line on stdin."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="agentgrep-answer-now-input",
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop listening when possible."""
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=0.2)

    def _run(self) -> None:
        selectable = self._stream_is_selectable()
        while not self._stop_event.is_set() and not self._control.answer_now_requested():
            line = self._read_line(selectable)
            if line is None:
                continue
            if line == "":
                return
            if line.strip() == "":
                self._control.request_answer_now()
                return
            if not selectable:
                return

    def _read_line(self, selectable: bool) -> str | None:
        if selectable:
            try:
                readable, _, _ = select.select([self._stream], [], [], self._poll_interval)
            except OSError, TypeError, ValueError:
                return None
            if not readable:
                return None
        try:
            return self._stream.readline()
        except OSError, ValueError:
            return ""

    def _stream_is_selectable(self) -> bool:
        try:
            _ = self._stream.fileno()
            readable, _, _ = select.select([self._stream], [], [], 0)
        except AttributeError, OSError, TypeError, ValueError:
            return False
        return isinstance(readable, list)


class SearchProgress(t.Protocol):
    """Progress reporter used by search internals."""

    def start(self, query: SearchQuery) -> None:
        """Mark search start."""
        ...

    def sources_discovered(self, count: int) -> None:
        """Report discovered source count."""
        ...

    def prefilter_started(self, root: pathlib.Path) -> None:
        """Report root prefilter start."""
        ...

    def sources_planned(self, planned: int, total: int) -> None:
        """Report selected source count."""
        ...

    def source_started(self, index: int, total: int, source: SourceHandle) -> None:
        """Report source scan start."""
        ...

    def source_finished(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        records: int,
        matches: int,
    ) -> None:
        """Report source scan completion."""
        ...

    def result_added(self, count: int) -> None:
        """Report deduped result count."""
        ...

    def record_added(self, record: SearchRecord) -> None:
        """Report a newly deduped record (streaming consumers only)."""
        ...

    def finish(self, result_count: int) -> None:
        """Report search completion."""
        ...

    def answer_now(self, result_count: int) -> None:
        """Report early search completion with partial results."""
        ...

    def interrupt(self) -> None:
        """Report interrupted search."""
        ...

    def close(self) -> None:
        """Release any progress resources."""
        ...


class NoopSearchProgress:
    """Silent search progress reporter."""

    def start(self, query: SearchQuery) -> None:
        """Ignore search start."""

    def sources_discovered(self, count: int) -> None:
        """Ignore discovered source count."""

    def prefilter_started(self, root: pathlib.Path) -> None:
        """Ignore root prefilter start."""

    def sources_planned(self, planned: int, total: int) -> None:
        """Ignore selected source count."""

    def source_started(self, index: int, total: int, source: SourceHandle) -> None:
        """Ignore source scan start."""

    def source_finished(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        records: int,
        matches: int,
    ) -> None:
        """Ignore source scan completion."""

    def result_added(self, count: int) -> None:
        """Ignore deduped result count."""

    def record_added(self, record: SearchRecord) -> None:
        """Ignore newly deduped record."""

    def finish(self, result_count: int) -> None:
        """Ignore search completion."""

    def answer_now(self, result_count: int) -> None:
        """Ignore early search completion."""

    def interrupt(self) -> None:
        """Ignore interrupted search."""

    def close(self) -> None:
        """Nothing to release."""


class ConsoleSearchProgress:
    """Human progress reporter for potentially long searches."""

    _SPINNER_FRAMES: t.ClassVar[str] = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(
        self,
        *,
        enabled: bool,
        stream: t.TextIO | None = None,
        tty: bool | None = None,
        color_mode: ColorMode = "auto",
        refresh_interval: float = 0.1,
        heartbeat_interval: float = 10.0,
        answer_now_hint: bool = False,
    ) -> None:
        self._enabled = enabled
        self._stream = stream if stream is not None else sys.stderr
        self._tty = (
            tty
            if tty is not None
            else bool(
                getattr(self._stream, "isatty", lambda: False)(),
            )
        )
        self._colors = AnsiColors.for_stream(color_mode, self._stream)
        self._refresh_interval = refresh_interval
        self._heartbeat_interval = heartbeat_interval
        self._answer_now_hint = answer_now_hint
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._last_heartbeat_at: float | None = None
        self._last_line_len = 0
        self._query_label = "search"
        self._phase = "starting"
        self._detail: str | None = None
        self._current: int | None = None
        self._total: int | None = None
        self._matches = 0
        self._finished = False

    def start(self, query: SearchQuery) -> None:
        """Begin progress reporting for ``query``."""
        if not self._enabled:
            return
        label = " ".join(query.terms) if query.terms else "all records"
        now = time.monotonic()
        with self._lock:
            self._query_label = label
            self._phase = "discovering"
            self._detail = None
            self._current = None
            self._total = None
            self._matches = 0
            self._started_at = now
            self._last_heartbeat_at = now
            self._finished = False
        if self._tty:
            self._ensure_tty_thread()
        else:
            self._emit_line(self._start_line(label))

    def sources_discovered(self, count: int) -> None:
        """Report discovered source count."""
        self.set_status("discovered", total=count, detail=f"{count} sources")

    def prefilter_started(self, root: pathlib.Path) -> None:
        """Report root prefilter start."""
        self.set_status("prefiltering", detail=format_display_path(root, directory=True))

    def sources_planned(self, planned: int, total: int) -> None:
        """Report selected source count."""
        self.set_status("planning", current=planned, total=total, detail="candidate sources")

    def source_started(self, index: int, total: int, source: SourceHandle) -> None:
        """Report source scan start."""
        self.set_status("scanning", current=index, total=total, detail=source.path.name)

    def source_finished(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        records: int,
        matches: int,
    ) -> None:
        """Report source scan completion."""
        self.set_status(
            "scanning",
            current=index,
            total=total,
            detail=f"{records} records, {format_match_count(matches)} in {source.path.name}",
        )

    def source_progress(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        records: int,
        matches: int,
    ) -> None:
        """Report in-source scan progress."""
        self.set_status(
            "scanning",
            current=index,
            total=total,
            detail=format_source_progress_detail(records, matches),
        )

    def result_added(self, count: int) -> None:
        """Report deduped result count."""
        if not self._enabled:
            return
        with self._lock:
            self._matches = count
        self._emit_heartbeat_if_due()

    def record_added(self, record: SearchRecord) -> None:
        """Ignore the per-record broadcast; counter is tracked via ``result_added``."""

    def set_status(
        self,
        phase: str,
        *,
        current: int | None = None,
        total: int | None = None,
        detail: str | None = None,
    ) -> None:
        """Update the current progress status."""
        if not self._enabled:
            return
        with self._lock:
            self._phase = phase
            self._current = current
            self._total = total
            self._detail = detail
        self._emit_heartbeat_if_due()

    def finish(self, result_count: int) -> None:
        """Finish progress reporting."""
        if not self._enabled:
            return
        with self._lock:
            self._matches = result_count
            self._phase = "complete"
            self._finished = True
        if self._tty:
            self._stop_tty_thread()
            self._clear_tty_line()
            return
        elapsed = self._elapsed_seconds()
        self._emit_line(
            self._finish_line(result_count, elapsed),
        )

    def answer_now(self, result_count: int) -> None:
        """Finish progress reporting with a partial-answer status."""
        if not self._enabled:
            return
        with self._lock:
            self._matches = result_count
            self._phase = "answering now"
            self._finished = True
        line = self._answer_now_line(result_count)
        if self._tty:
            self._stop_tty_thread()
            self._write_tty_line(line)
            return
        self._emit_line(line)

    def close(self) -> None:
        """Stop any active progress renderer."""
        if not self._enabled:
            return
        if self._tty:
            self._stop_tty_thread()
            self._clear_tty_line()

    def interrupt(self) -> None:
        """Stop progress rendering while preserving the current status."""
        if not self._enabled:
            return
        if self._tty:
            self._stop_tty_thread()
            self._write_tty_summary_line()
            return
        self._emit_line(self._summary())

    def _ensure_tty_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._tty_loop,
            daemon=True,
            name="agentgrep-search-progress",
        )
        self._thread.start()

    def _stop_tty_thread(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)

    def _tty_loop(self) -> None:
        frames = itertools.cycle(self._SPINNER_FRAMES)
        while not self._stop_event.is_set():
            self._render_tty(next(frames))
            self._stop_event.wait(self._refresh_interval)

    def _render_tty(self, frame: str) -> None:
        frame_text = self._colors.info(frame)
        summary_width = max(1, self._terminal_width() - _visible_width(frame_text) - 1)
        summary = self._summary(max_width=summary_width)
        line = f"{frame_text} {summary}"
        with self._lock:
            try:
                self._stream.write("\r\033[2K" + line)
                self._stream.flush()
                self._last_line_len = len(line)
            except OSError, ValueError:
                pass

    def _clear_tty_line(self) -> None:
        with self._lock:
            if self._last_line_len == 0:
                return
            try:
                self._stream.write("\r\033[2K")
                self._stream.flush()
            except OSError, ValueError:
                pass
            self._last_line_len = 0

    def _write_tty_summary_line(self) -> None:
        line = self._summary(max_width=self._terminal_width())
        self._write_tty_line(line)

    def _write_tty_line(self, line: str) -> None:
        with self._lock:
            try:
                self._stream.write("\r\033[2K" + line + "\n")
                self._stream.flush()
            except OSError, ValueError:
                pass
            self._last_line_len = 0

    def _emit_heartbeat_if_due(self) -> None:
        if not self._enabled or self._tty:
            return
        with self._lock:
            last = self._last_heartbeat_at
            label = self._query_label
        if last is None:
            return
        now = time.monotonic()
        if now - last < self._heartbeat_interval:
            return
        elapsed = self._elapsed_seconds()
        self._emit_line(
            self._heartbeat_line(label, elapsed),
        )
        with self._lock:
            self._last_heartbeat_at = now

    def _emit_line(self, line: str) -> None:
        try:
            self._stream.write(line + "\n")
            self._stream.flush()
        except OSError, ValueError:
            pass

    def _summary(self, *, max_width: int | None = None) -> str:
        return format_search_progress_line(
            self._snapshot(),
            colors=self._colors,
            answer_now_hint=self._answer_now_hint,
            max_width=max_width,
        )

    def _terminal_width(self) -> int:
        try:
            return max(1, os.get_terminal_size(self._stream.fileno()).columns)
        except AttributeError, OSError, TypeError, ValueError:
            return max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)

    def _snapshot(self) -> ProgressSnapshot:
        elapsed = self._elapsed_seconds()
        with self._lock:
            return ProgressSnapshot(
                query_label=self._query_label,
                phase=self._phase,
                current=self._current,
                total=self._total,
                detail=self._detail,
                matches=self._matches,
                elapsed=elapsed,
            )

    def _start_line(self, label: str) -> str:
        return f"{self._colors.heading('Searching')} {self._colors.highlight(label)}"

    def _heartbeat_line(self, label: str, elapsed: float) -> str:
        prefix = f"{self._colors.muted('...')} {self._colors.heading('still searching')}"
        elapsed_text = self._colors.muted(f"{elapsed:.0f}s elapsed")
        return f"{prefix} {self._colors.highlight(label)}: {self._status_text()} ({elapsed_text})"

    def _finish_line(self, result_count: int, elapsed: float) -> str:
        return (
            f"{self._colors.success('Search complete:')} "
            f"{self._colors.warning(format_match_count(result_count))} "
            f"({self._colors.muted(f'{elapsed:.1f}s elapsed')})"
        )

    def _answer_now_line(self, result_count: int) -> str:
        return (
            f"{self._colors.success('Answering now:')} "
            f"{self._colors.warning(format_match_count(result_count))}"
        )

    def _status_text(self) -> str:
        with self._lock:
            phase = self._phase
            current = self._current
            total = self._total
            detail = self._detail
        if current is not None and total is not None:
            count = self._colors.warning(f"{current}/{total}")
            text = f"{self._colors.heading(phase)} {count} {self._colors.muted('sources')}"
            if detail:
                return f"{text} | {self._colors.muted(detail)}"
            return text
        if detail:
            return f"{self._colors.heading(phase)} {self._colors.muted(detail)}"
        return self._colors.heading(phase)

    def _elapsed_seconds(self) -> float:
        with self._lock:
            started = self._started_at
        if started is None:
            return 0.0
        return time.monotonic() - started


def format_match_count(count: int) -> str:
    """Return a human-readable match count."""
    suffix = "match" if count == 1 else "matches"
    return f"{count} {suffix}"


def format_source_progress_detail(records: int, matches: int) -> str:
    """Return a concise in-source progress detail."""
    match_suffix = "source match" if matches == 1 else "source matches"
    return f"{records} records, {matches} {match_suffix}"


@dataclasses.dataclass(frozen=True)
class ProgressSnapshot:
    """Immutable view of search-progress state for one render pass."""

    query_label: str
    phase: str
    current: int | None
    total: int | None
    detail: str | None
    matches: int
    elapsed: float


def format_search_progress_line(
    snapshot: ProgressSnapshot,
    *,
    colors: SearchColors,
    answer_now_hint: bool = False,
    max_width: int | None = None,
) -> str:
    """Format the single-line progress summary used by both the CLI and the TUI.

    Parameters
    ----------
    snapshot : ProgressSnapshot
        Frozen view of progress counters.
    colors : SearchColors
        An :class:`AnsiColors` instance (used by the CLI chrome).
    answer_now_hint : bool, default False
        When ``True``, append the ``[Press enter, answer now]`` reminder.
    max_width : int or None, default None
        Maximum visible terminal cells for the returned line. When set, the
        formatter drops optional detail and hint segments before truncating.

    Returns
    -------
    str
        ``"Searching <q> | <phase> N/M sources | K matches | T.Ts"`` with
        each segment styled through ``colors``.
    """
    variants = (
        (True, answer_now_hint),
        (False, answer_now_hint),
        (False, False),
    )
    for include_detail, include_hint in variants:
        line = _format_search_progress_line(
            snapshot,
            colors=colors,
            answer_now_hint=include_hint,
            include_detail=include_detail,
        )
        if max_width is None or _visible_width(line) <= max_width:
            return line
    if max_width is None:
        return line
    return _hard_truncate_ansi(line, max_width)


def _format_search_progress_line(
    snapshot: ProgressSnapshot,
    *,
    colors: SearchColors,
    answer_now_hint: bool,
    include_detail: bool,
) -> str:
    """Build one progress-line variant."""
    label_part = f"{colors.heading('Searching')} {colors.highlight(snapshot.query_label)}"
    detail_part = colors.muted(snapshot.detail) if include_detail and snapshot.detail else None
    if snapshot.current is not None and snapshot.total is not None:
        count = colors.warning(f"{snapshot.current}/{snapshot.total}")
        status_part = f"{colors.heading(snapshot.phase)} {count} {colors.muted('sources')}"
    elif include_detail and snapshot.detail:
        status_part = f"{colors.heading(snapshot.phase)} {colors.muted(snapshot.detail)}"
        detail_part = None
    else:
        status_part = colors.heading(snapshot.phase)
    parts = [
        label_part,
        status_part,
    ]
    if detail_part:
        parts.append(detail_part)
    parts.extend(
        [
            colors.warning(format_match_count(snapshot.matches)),
            colors.muted(f"{snapshot.elapsed:.1f}s"),
        ],
    )
    if answer_now_hint:
        parts.append(colors.white("[Press enter, answer now]"))
    return " | ".join(parts)


def noop_search_progress() -> SearchProgress:
    """Return a silent search progress reporter."""
    return NoopSearchProgress()


def _report_source_progress(
    progress: SearchProgress,
    index: int,
    total: int,
    source: SourceHandle,
    records: int,
    matches: int,
) -> None:
    """Call the optional in-source progress hook when a reporter exposes it."""
    callback = getattr(progress, "source_progress", None)
    if callable(callback):
        t.cast("SourceProgressCallback", callback)(index, total, source, records, matches)


@dataclasses.dataclass(frozen=True)
class StreamingRecordsBatch:
    """Batch of newly deduped records emitted by :meth:`StreamingSearchProgress.flush`."""

    records: tuple[SearchRecord, ...]
    total: int


@dataclasses.dataclass(frozen=True)
class StreamingSearchFinished:
    """Terminal event emitted by :class:`StreamingSearchProgress` when the search ends."""

    outcome: t.Literal["complete", "interrupted", "error"]
    total: int
    elapsed: float
    error: BaseException | None = None


class RecordsAppendedPayload(pydantic.BaseModel):
    """Pydantic payload for the ``RecordsAppended`` Textual message."""

    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True, frozen=True)

    records: tuple[SearchRecord, ...]
    total: int


class ProgressUpdatedPayload(pydantic.BaseModel):
    """Pydantic payload for the ``ProgressUpdated`` Textual message."""

    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True, frozen=True)

    snapshot: ProgressSnapshot


class SearchFinishedPayload(pydantic.BaseModel):
    """Pydantic payload for the ``SearchFinished`` Textual message."""

    model_config = pydantic.ConfigDict(frozen=True)

    outcome: t.Literal["complete", "interrupted", "error"]
    total: int
    elapsed: float
    error_message: str | None = None


class FilterRequestedPayload(pydantic.BaseModel):
    """Pydantic payload for a debounced filter-text-changed Textual message."""

    model_config = pydantic.ConfigDict(frozen=True)

    text: str


class SearchRequestedPayload(pydantic.BaseModel):
    """Pydantic payload for a debounced search-bar-changed Textual message."""

    model_config = pydantic.ConfigDict(frozen=True)

    text: str


class FilterCompletedPayload(pydantic.BaseModel):
    """Pydantic payload for a worker-completed filter result Textual message."""

    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True, frozen=True)

    text: str
    matching: tuple[SearchRecord, ...]


class StreamingSearchProgress:
    """Search-progress reporter that emits structured events through an ``emit`` callback.

    Records are buffered under a lock and released as a single
    :class:`StreamingRecordsBatch` per :meth:`flush` (or on terminal events).
    Progress callbacks emit :class:`ProgressSnapshot` instances directly.
    The callback is invoked from whichever thread drives the search and is
    expected to be safe to call cross-thread (e.g. Textual's ``post_message``).
    """

    _FLUSH_INTERVAL_SECONDS: t.ClassVar[float] = 0.05

    def __init__(self, emit: cabc.Callable[[object], None]) -> None:
        self._emit = emit
        self._lock = threading.Lock()
        self._buffer: list[SearchRecord] = []
        self._query_label = "search"
        self._phase = "starting"
        self._detail: str | None = None
        self._current: int | None = None
        self._total: int | None = None
        self._matches = 0
        self._started_at: float | None = None
        self._last_flush_at: float = time.monotonic()

    def start(self, query: SearchQuery) -> None:
        """Record search start and emit the initial progress snapshot."""
        label = " ".join(query.terms) if query.terms else "all records"
        now = time.monotonic()
        with self._lock:
            self._query_label = label
            self._phase = "discovering"
            self._started_at = now
        self._emit_progress()

    def sources_discovered(self, count: int) -> None:
        """Report discovered-source count."""
        with self._lock:
            self._phase = "discovered"
            self._detail = f"{count} sources"
        self._emit_progress()

    def prefilter_started(self, root: pathlib.Path) -> None:
        """Report root prefilter start."""
        with self._lock:
            self._phase = "prefiltering"
            self._detail = format_display_path(root, directory=True)
        self._emit_progress()

    def sources_planned(self, planned: int, total: int) -> None:
        """Report planned-source count."""
        with self._lock:
            self._phase = "planning"
            self._current = planned
            self._total = total
            self._detail = "candidate sources"
        self._emit_progress()

    def source_started(self, index: int, total: int, source: SourceHandle) -> None:
        """Report source-scan start."""
        with self._lock:
            self._phase = "scanning"
            self._current = index
            self._total = total
            self._detail = source.path.name
        self._emit_progress()

    def source_finished(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        records: int,
        matches: int,
    ) -> None:
        """Report source-scan completion."""
        with self._lock:
            self._phase = "scanning"
            self._current = index
            self._total = total
            self._detail = f"{records} records, {format_match_count(matches)} in {source.path.name}"
        self._emit_progress()

    def source_progress(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        records: int,
        matches: int,
    ) -> None:
        """Report in-source scan progress."""
        with self._lock:
            self._phase = "scanning"
            self._current = index
            self._total = total
            self._detail = format_source_progress_detail(records, matches)
        self._emit_progress()

    def result_added(self, count: int) -> None:
        """Update the cumulative match counter."""
        with self._lock:
            self._matches = count

    def record_added(self, record: SearchRecord) -> None:
        """Buffer ``record``; auto-flush when the batching window elapses.

        The window is checked under the buffer lock, so the worker thread paces
        its own emit cadence without needing a main-thread timer to pull from
        the buffer. Explicit :meth:`flush` calls (e.g. on terminal events) still
        drain the remainder.
        """
        with self._lock:
            self._buffer.append(record)
            should_flush = time.monotonic() - self._last_flush_at >= self._FLUSH_INTERVAL_SECONDS
        if should_flush:
            self.flush()

    def finish(self, result_count: int) -> None:
        """Flush pending records and emit a successful terminal event."""
        self.flush()
        self._emit(
            StreamingSearchFinished(
                "complete",
                total=result_count,
                elapsed=self._elapsed(),
            ),
        )

    def answer_now(self, result_count: int) -> None:
        """Flush pending records and emit an interrupted terminal event."""
        self.flush()
        self._emit(
            StreamingSearchFinished(
                "interrupted",
                total=result_count,
                elapsed=self._elapsed(),
            ),
        )

    def interrupt(self) -> None:
        """Flush pending records and emit an interrupted terminal event."""
        self.flush()
        with self._lock:
            matches = self._matches
        self._emit(
            StreamingSearchFinished(
                "interrupted",
                total=matches,
                elapsed=self._elapsed(),
            ),
        )

    def close(self) -> None:
        """No-op: no resources to release."""

    def flush(self) -> None:
        """Drain the record buffer into a single :class:`StreamingRecordsBatch`."""
        with self._lock:
            if not self._buffer:
                return
            batch = tuple(self._buffer)
            self._buffer.clear()
            total = self._matches
            self._last_flush_at = time.monotonic()
        self._emit(StreamingRecordsBatch(records=batch, total=total))

    def _emit_progress(self) -> None:
        self._emit(self._snapshot())

    def _snapshot(self) -> ProgressSnapshot:
        with self._lock:
            current = self._current
            total = self._total
            detail = self._detail
            phase = self._phase
            label = self._query_label
            matches = self._matches
            started = self._started_at
        elapsed = (time.monotonic() - started) if started is not None else 0.0
        return ProgressSnapshot(
            query_label=label,
            phase=phase,
            current=current,
            total=total,
            detail=detail,
            matches=matches,
            elapsed=elapsed,
        )

    def _elapsed(self) -> float:
        with self._lock:
            started = self._started_at
        return (time.monotonic() - started) if started is not None else 0.0
