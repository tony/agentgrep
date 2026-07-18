"""Terminal formatting for the grep / search / find output.

The match-line, heading, snippet, and relative-time formatters, the ANSI
highlight application, and the fd-shaped find filters — the building blocks the
subcommand dispatchers compose into human-readable terminal output.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import datetime
import fnmatch
import pathlib
import re
import sys

from agentgrep._text import AnsiColors, format_display_path
from agentgrep.cli.parser import CaseMode, FindArgs, GrepArgs, SearchArgs
from agentgrep.cli.serializers import (
    serialize_grep_begin,
    serialize_grep_end,
    serialize_grep_match_line,
)
from agentgrep.records import FindRecord, SearchRecord


def _format_find_text_line(record: FindRecord, args: FindArgs) -> str:
    """Compose one line for ``--list-details`` / ``--print0`` output."""
    path = _format_find_path(record, args)
    if args.list_details:
        return f"{record.agent}\t{record.path_kind}\t{record.store}\t{record.adapter_id}\t{path}"
    return path


def _format_find_path(record: FindRecord, args: FindArgs) -> str:
    """Return the find path according to display vs. shell-consumer mode."""
    if args.print0 or args.absolute_path:
        return str(record.path)
    return format_display_path(record.path)


def _resolve_find_case_sensitive(pattern: str | None, mode: CaseMode) -> bool:
    """Apply fd's smart-case rule to a find pattern."""
    if mode == "respect":
        return True
    if mode == "ignore":
        return False
    return pattern is not None and any(ch.isupper() for ch in pattern)


def _pattern_matches(record: FindRecord, args: FindArgs) -> bool:
    """Decide whether a find record satisfies the requested pattern mode.

    Glob mode (`-g`) matches against the file basename by default, with
    `--full-path` opting into matching against the absolute path —
    mirroring fd's default vs. `-p` flag semantics. Regex, fixed, and
    exact modes keep the joined `agent store adapter_id path path_kind`
    haystack so substring matches against the metadata still work.
    """
    if args.pattern is None:
        return True
    case_sensitive = _resolve_find_case_sensitive(args.pattern, args.case_mode)
    haystack = " ".join(
        (record.agent, record.store, record.adapter_id, str(record.path), record.path_kind),
    )
    if not case_sensitive:
        haystack = haystack.casefold()
        needle = args.pattern.casefold()
    else:
        needle = args.pattern
    if args.pattern_mode == "exact":
        adapter_id = record.adapter_id if case_sensitive else record.adapter_id.casefold()
        return adapter_id == needle
    if args.pattern_mode == "fixed":
        return needle in haystack
    if args.pattern_mode == "glob":
        glob_target = str(record.path) if args.full_path else record.path.name
        if not case_sensitive:
            glob_target = glob_target.casefold()
        return fnmatch.fnmatchcase(glob_target, needle)
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.search(args.pattern, haystack, flags) is not None
    except re.error:
        return False


_FIND_TYPE_PATH_KINDS: dict[str, str] = {
    "sessions": "session_file",
    "history": "history_file",
    "prompts": "history_file",
}


def _type_matches(record: FindRecord, args: FindArgs) -> bool:
    """Apply the ``-t/--type`` filter against the record's path kind.

    ``--type`` selects on the record's ``path_kind`` (the on-disk file
    role), not its parse format: ``sessions`` -> ``session_file`` (full
    transcripts) and ``history``/``prompts`` -> ``history_file`` (the
    prompt-history audit logs, where standalone prompt records live).
    The prompt/history distinction is a record-level concept (``search``
    ``--scope``); at the file granularity ``find`` operates on, both map
    to the same path kind.
    """
    if args.type_filter == "all":
        return True
    return record.path_kind == _FIND_TYPE_PATH_KINDS.get(args.type_filter)


def _extensions_match(record: FindRecord, args: FindArgs) -> bool:
    """Apply the ``-e/--extension`` filter."""
    if not args.extensions:
        return True
    suffix = pathlib.Path(str(record.path)).suffix.lstrip(".")
    return suffix.lower() in {ext.lstrip(".").lower() for ext in args.extensions}


def filter_find_records(records: list[FindRecord], args: FindArgs) -> list[FindRecord]:
    """Apply fd-shaped CLI filters (pattern/type/extension) to find results."""
    filtered = [
        record
        for record in records
        if _pattern_matches(record, args)
        and _type_matches(record, args)
        and _extensions_match(record, args)
    ]
    if args.limit is not None:
        filtered = filtered[: args.limit]
    return filtered


def _find_record_passes(record: FindRecord, args: FindArgs) -> bool:
    """Return ``True`` when ``record`` survives every fd-shaped filter."""
    return (
        _pattern_matches(record, args)
        and _type_matches(record, args)
        and _extensions_match(record, args)
    )


def _compile_search_patterns(args: SearchArgs) -> list[re.Pattern[str]]:
    """Compile search terms to regex for snippet highlighting."""
    flags = 0 if args.case_sensitive else re.IGNORECASE
    compiled: list[re.Pattern[str]] = []
    for term in args.terms:
        if ":" in term:
            continue
        source = re.escape(term)
        try:
            compiled.append(re.compile(source, flags))
        except re.error:
            continue
    return compiled


def _compile_grep_patterns(args: GrepArgs) -> list[re.Pattern[str]]:
    """Compile :class:`GrepArgs` patterns into regex objects honoring mode/case.

    Mirrors the engine's pattern-mode resolution so the line-aware renderer
    finds the same matches the search engine surfaced at the record level.
    Malformed patterns are silently skipped (the engine handles its own
    validation; this layer just refuses to crash on bad input).
    """
    case_sensitive = args.case_mode == "respect" or (
        args.case_mode == "smart" and any(any(ch.isupper() for ch in p) for p in args.patterns)
    )
    flags = 0 if case_sensitive else re.IGNORECASE
    compiled: list[re.Pattern[str]] = []
    for pattern in args.patterns:
        if args.pattern_mode == "fixed":
            source = re.escape(pattern)
        elif args.pattern_mode == "word":
            source = rf"\b{pattern}\b"
        else:
            source = pattern
        try:
            compiled.append(re.compile(source, flags))
        except re.error:
            continue
    return compiled


def _merge_overlapping_spans(
    spans: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Collapse overlapping or adjacent spans so highlight doesn't double-color."""
    if not spans:
        return []
    spans = sorted(spans)
    merged: list[tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def extract_search_snippet(
    text: str,
    patterns: list[re.Pattern[str]],
    *,
    max_lines: int = 5,
) -> tuple[str, int]:
    """Extract a match-centered line window from record text.

    Parameters
    ----------
    text : str
        The full record text body.
    patterns : list[re.Pattern[str]]
        Compiled highlight patterns.  Used to find the match center.
    max_lines : int
        Maximum lines to include in the snippet.

    Returns
    -------
    tuple[str, int]
        ``(snippet_text, remaining_line_count)``.  When ``text`` is
        empty, returns ``("", 0)``.
    """
    if not text:
        return ("", 0)
    lines = text.split("\n")
    total = len(lines)
    if total <= max_lines:
        return (text, 0)
    match_idx: int | None = None
    if patterns:
        for idx, line in enumerate(lines):
            for pattern in patterns:
                if pattern.search(line):
                    match_idx = idx
                    break
            if match_idx is not None:
                break
    if match_idx is None:
        snippet_lines = lines[:max_lines]
    else:
        start = max(0, match_idx - 1)
        end = start + max_lines
        if end > total:
            end = total
            start = max(0, end - max_lines)
        snippet_lines = lines[start:end]
    remaining = total - len(snippet_lines)
    return ("\n".join(snippet_lines), remaining)


def highlight_search_spans(
    text: str,
    patterns: list[re.Pattern[str]],
    *,
    colors: AnsiColors,
) -> str:
    """Apply warm-amber accent highlighting to match spans.

    Uses :func:`_merge_overlapping_spans` to avoid nested ANSI
    escape sequences from multi-pattern overlap.
    """
    if not text or not patterns:
        return text
    result_lines: list[str] = []
    for line in text.split("\n"):
        spans: list[tuple[int, int]] = []
        for pattern in patterns:
            for m in pattern.finditer(line):
                if m.start() == m.end():
                    continue
                spans.append((m.start(), m.end()))
        if not spans:
            result_lines.append(line)
            continue
        merged = _merge_overlapping_spans(spans)
        parts: list[str] = []
        cursor = 0
        for start, end in merged:
            parts.append(line[cursor:start])
            parts.append(colors.accent(line[start:end]))
            cursor = end
        parts.append(line[cursor:])
        result_lines.append("".join(parts))
    return "\n".join(result_lines)


def iter_match_lines(
    record_text: str,
    args: GrepArgs,
) -> cabc.Iterator[tuple[int, str, list[tuple[int, int]]]]:
    """Yield ``(line_number, line_text, match_spans)`` for each matching line.

    Lines are 1-indexed from the start of ``record_text``, matching rg's
    convention. ``match_spans`` are byte (string) offsets within the line,
    sorted and merged so multiple-pattern overlap doesn't produce nested
    ANSI escape sequences.

    Returns nothing when no patterns compile or no lines match.
    """
    patterns = _compile_grep_patterns(args)
    if not patterns:
        return
    for line_number, line in enumerate(record_text.split("\n"), start=1):
        spans: list[tuple[int, int]] = []
        for pattern in patterns:
            for m in pattern.finditer(line):
                if m.start() == m.end():
                    continue  # skip zero-width matches (e.g. `\b` alone)
                spans.append((m.start(), m.end()))
        if args.invert_match:
            if not spans:
                yield line_number, line, []
        elif spans:
            yield line_number, line, _merge_overlapping_spans(spans)


def format_grep_line(
    line_number: int,
    line_text: str,
    match_spans: list[tuple[int, int]],
    *,
    colors: AnsiColors,
    show_line: bool = False,
    show_column: bool = False,
) -> str:
    """Format one matching line for grep text output.

    Returns one of three shapes depending on ``show_line`` / ``show_column``:

    - ``show_line=False, show_column=False`` → just ``text`` (rg's default
      pipe shape; the path prefix is the caller's job).
    - ``show_line=True, show_column=False`` → ``line:text`` (rg's ``-n``).
    - ``show_line=True, show_column=True`` → ``line:col:text`` (rg's
      ``--column`` and ``--vimgrep``).

    Asking for ``show_column=True`` with ``show_line=False`` is treated as
    ``show_line=True`` too — rg's ``--column`` implies ``-n``. The line
    number is wrapped in the green LINE_NUMBER color and the matched
    spans in red+bold MATCH. Column is the 1-indexed byte offset of the
    first match span.
    """
    if show_column:
        show_line = True
    body_parts: list[str] = []
    cursor = 0
    for start, end in match_spans:
        body_parts.append(line_text[cursor:start])
        body_parts.append(colors.match(line_text[start:end]))
        cursor = end
    body_parts.append(line_text[cursor:])
    body = "".join(body_parts)
    if not show_line:
        return body
    line_prefix = colors.line_number(str(line_number))
    if not show_column:
        return f"{line_prefix}:{body}"
    column = (match_spans[0][0] + 1) if match_spans else 1
    return f"{line_prefix}:{column}:{body}"


def format_grep_heading(
    record: SearchRecord,
    *,
    colors: AnsiColors,
) -> str:
    """Format the per-record heading line for heading-mode grep output.

    Shape: ``agent  [timestamp]  path``, all in muted gray except the
    path which gets the rg-shaped magenta. Empty timestamps are
    suppressed so synthetic records without one don't carry a stray
    double-space.
    """
    path = format_display_path(record.path)
    pieces = [colors.muted(record.agent)]
    if record.timestamp:
        pieces.append(colors.muted(record.timestamp))
    pieces.append(colors.path(path))
    return "  ".join(pieces)


def _iter_grep_json_events(
    records: list[SearchRecord],
    args: GrepArgs,
) -> cabc.Iterator[dict[str, object]]:
    """Yield rg-shaped JSON events for each record in ``records``.

    For each record, emits ``begin`` → 0+ ``match`` (one per matching
    line) → ``end``. A trailing ``summary`` event is appended by the
    caller (``json`` mode) or omitted (``ndjson`` mode).
    """
    for record in records:
        matches = list(iter_match_lines(record.text, args))
        yield serialize_grep_begin(record)
        match_span_total = 0
        for line_number, line_text, match_spans in matches:
            yield serialize_grep_match_line(
                record,
                line_number,
                line_text,
                match_spans,
            )
            match_span_total += len(match_spans)
        yield serialize_grep_end(
            record,
            matched_lines=len(matches),
            matches=match_span_total,
        )


def _grep_show_line_col(args: GrepArgs) -> tuple[bool, bool]:
    """Resolve whether to render line/column prefixes from grep flags.

    Mirrors rg's resolution: default is text-only (``False, False``).
    ``-n``/``--line-number`` opts into line numbers. ``--column`` adds
    column numbers (and implies ``-n``). ``--vimgrep`` forces both on.
    """
    if args.vimgrep or args.column:
        return True, True
    if args.line_number is True:
        return True, False
    return False, False


@dataclasses.dataclass(slots=True)
class GrepSummary:
    """Accumulates per-agent match counts for pretty-style grep footer."""

    total: int = 0
    per_agent: dict[str, int] = dataclasses.field(default_factory=dict)
    elapsed: float = 0.0

    def add(self, record: SearchRecord) -> None:
        """Record one emitted search result."""
        self.total += 1
        self.per_agent[record.agent] = self.per_agent.get(record.agent, 0) + 1

    def format(self, *, colors: AnsiColors) -> str:
        """Format the summary footer line."""
        if self.total == 0:
            return ""
        parts = [f"{self.total} records"]
        for agent, count in sorted(self.per_agent.items()):
            parts.append(f"{count} {agent}")
        elapsed_str = f"{self.elapsed:.1f}s"
        parts.append(elapsed_str)
        line = " · ".join(parts)
        return colors.dim(line)


def format_grep_record_pretty(
    record: SearchRecord,
    args: GrepArgs,
    *,
    colors: AnsiColors,
) -> str:
    """Format one record in snippet-first pretty style.

    Content first at full foreground with warm-amber match highlighting,
    dim provenance line underneath.
    """
    lines: list[str] = []
    patterns = _compile_grep_patterns(args)

    if record.text:
        snippet, remaining = extract_search_snippet(record.text, patterns)
        highlighted = highlight_search_spans(snippet, patterns, colors=colors)
        lines.append(highlighted)
        if remaining > 0:
            lines.append(colors.dim(f"  ... {remaining} more lines"))
    provenance_parts: list[str] = [record.agent, record.kind]
    if record.timestamp:
        provenance_parts.append(format_relative_time(record.timestamp))
    if record.model:
        provenance_parts.append(record.model)
    display_path = format_display_path(record.path)
    provenance_parts.append(colors.path(display_path))
    provenance = " · ".join(provenance_parts)
    lines.append(colors.dim(f"  {provenance}"))

    return "\n".join(lines)


def format_grep_record(record: SearchRecord, args: GrepArgs) -> str:
    """Format one matching record for text-mode ``grep`` output.

    Default shape (rg-faithful): ``path:text`` on pipe, ``text`` rows
    grouped under a heading line on TTY. ``-n`` / ``--column`` /
    ``--vimgrep`` add line and column prefixes per rg's resolution.

    ``--vimgrep`` emits one row per match span (one line can produce
    multiple rows). ``-o`` / ``--only-matching`` emits only the matched
    substrings; ``-l`` emits just the path.
    """
    path = format_display_path(record.path)
    if args.files_with_matches:
        return path
    colors = AnsiColors.for_stream(args.color_mode, sys.stdout)
    matches = list(iter_match_lines(record.text, args))
    if args.invert_match and not matches:
        return ""

    if args.only_matching:
        chunks: list[str] = []
        for _, line, spans in matches:
            if args.invert_match:
                chunks.append(line)
                continue
            for start, end in spans:
                chunks.append(line[start:end])
        return "\n".join(chunks)

    if args.vimgrep:
        rows: list[str] = []
        for line_no, line, spans in matches:
            if args.invert_match:
                rows.append(f"{colors.path(path)}:{line_no}:{line}")
                continue
            for start, _end in spans:
                col = start + 1
                rows.append(f"{colors.path(path)}:{line_no}:{col}:{line}")
        return "\n".join(rows)

    if args.style == "pretty":
        return format_grep_record_pretty(record, args, colors=colors)

    if not matches:
        # Record matched at the engine level but no individual line carries
        # the pattern (e.g. multi-line regex). Surface the heading anyway so
        # the user sees there's a hit they can inspect.
        return format_grep_heading(record, colors=colors)

    show_line, show_column = _grep_show_line_col(args)
    heading_on = args.heading if args.heading is not None else sys.stdout.isatty()
    line_rows = [
        format_grep_line(
            line_no,
            line,
            spans,
            colors=colors,
            show_line=show_line,
            show_column=show_column,
        )
        for line_no, line, spans in matches
    ]
    if heading_on:
        return "\n".join([format_grep_heading(record, colors=colors), *line_rows])
    path_prefix = colors.path(path)
    return "\n".join(f"{path_prefix}:{row}" for row in line_rows)


def format_relative_time(
    iso_timestamp: str,
    *,
    now: datetime.datetime | None = None,
) -> str:
    """Convert an ISO 8601 timestamp to a human-scannable relative form.

    Parameters
    ----------
    iso_timestamp : str
        ISO 8601 timestamp string.  Assumed UTC when no timezone info
        is present.
    now : datetime.datetime | None
        Reference time for delta computation.  Defaults to
        ``datetime.datetime.now(datetime.UTC)``.

    Returns
    -------
    str
        Relative time such as ``now``, ``3m ago``, ``2d ago``.
        Returns *iso_timestamp* verbatim when parsing fails.
    """
    try:
        dt = datetime.datetime.fromisoformat(iso_timestamp)
    except ValueError, TypeError:
        return iso_timestamp
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    ref = now if now is not None else datetime.datetime.now(datetime.UTC)
    delta = ref - dt
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return iso_timestamp
    if total_seconds < 60:
        return "now"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = total_seconds // 3600
    if hours < 24:
        return f"{hours}h ago"
    days = total_seconds // 86400
    if days < 7:
        return f"{days}d ago"
    if days < 30:
        weeks = days // 7
        return f"{weeks}w ago"
    if days < 365:
        months = days // 30
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


__all__ = (
    "GrepSummary",
    "extract_search_snippet",
    "filter_find_records",
    "format_grep_heading",
    "format_grep_line",
    "format_grep_record",
    "format_grep_record_pretty",
    "format_relative_time",
    "highlight_search_spans",
    "iter_match_lines",
)
