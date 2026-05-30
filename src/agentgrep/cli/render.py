"""CLI output rendering and subcommand dispatch for agentgrep.

This module owns the rendering paths for the ``grep``, ``find``, and
``fuzzy`` subcommands, plus the dispatcher functions that glue parsed
arguments to the engine and the chosen output format.

Runtime callables (engines, helpers, classes) are accessed through the
``agentgrep`` namespace at call time rather than imported by name, so
tests that monkeypatch attributes such as ``agentgrep.run_search_query``
continue to see their patches honored when the dispatchers run.

Symbols defined here are re-exported from :mod:`agentgrep` for backward
compatibility.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import datetime
import fnmatch
import json
import pathlib
import re
import sys
import typing as t

import agentgrep
from agentgrep import (
    EnvelopeFactory,
    EnvelopePayload,
    FindRecord,
    FindRecordPayload,
    SearchRecord,
    SearchRecordPayload,
    SourceHandle,
    SourceHandlePayload,
    SourceVersionDetection,
    SourceVersionDetectionPayload,
)
from agentgrep.cli.parser import FindArgs, FuzzyArgs, GrepArgs, SearchArgs, UIArgs

__all__ = [
    "GrepSummary",
    "build_envelope",
    "build_grep_query",
    "extract_search_snippet",
    "filter_find_records",
    "format_grep_heading",
    "format_grep_line",
    "format_grep_record",
    "format_grep_record_pretty",
    "format_relative_time",
    "fuzzy_filter_lines",
    "highlight_search_spans",
    "iter_match_lines",
    "maybe_build_pydantic",
    "print_find_results",
    "print_grep_results",
    "run_find_command",
    "run_fuzzy_command",
    "run_grep_command",
    "run_search_command",
    "run_ui_command",
    "serialize_find_record",
    "serialize_grep_record",
    "serialize_search_record",
    "serialize_source_handle",
    "stream_find_results",
    "stream_grep_results",
]


def maybe_build_pydantic() -> tuple[
    t.Callable[[SearchRecord], dict[str, object]],
    t.Callable[[FindRecord], dict[str, object]],
    EnvelopeFactory,
]:
    """Return Pydantic serializers or plain fallbacks."""
    try:
        return agentgrep.maybe_use_pydantic()
    except ImportError:
        return (
            lambda record: t.cast("dict[str, object]", serialize_search_record(record)),
            lambda record: t.cast("dict[str, object]", serialize_find_record(record)),
            lambda command, query_data, results: t.cast(
                "dict[str, object]",
                build_envelope(command, query_data, results),
            ),
        )


def serialize_search_record(record: SearchRecord) -> SearchRecordPayload:
    """Serialize a search record to a JSON-compatible mapping."""
    return {
        "schema_version": agentgrep.SCHEMA_VERSION,
        "kind": record.kind,
        "agent": record.agent,
        "store": record.store,
        "adapter_id": record.adapter_id,
        "path": agentgrep.format_display_path(record.path),
        "text": record.text,
        "title": record.title,
        "role": record.role,
        "timestamp": record.timestamp,
        "model": record.model,
        "session_id": record.session_id,
        "conversation_id": record.conversation_id,
        "metadata": record.metadata,
    }


def serialize_find_record(record: FindRecord) -> FindRecordPayload:
    """Serialize a find record to a JSON-compatible mapping."""
    return {
        "schema_version": agentgrep.SCHEMA_VERSION,
        "kind": record.kind,
        "agent": record.agent,
        "store": record.store,
        "adapter_id": record.adapter_id,
        "path": agentgrep.format_display_path(record.path),
        "path_kind": record.path_kind,
        "metadata": record.metadata,
    }


def serialize_source_handle(source: SourceHandle) -> SourceHandlePayload:
    """Serialize a source handle to a JSON-compatible mapping."""
    return {
        "schema_version": agentgrep.SCHEMA_VERSION,
        "agent": source.agent,
        "store": source.store,
        "adapter_id": source.adapter_id,
        "path": agentgrep.format_display_path(source.path),
        "path_kind": source.path_kind,
        "source_kind": source.source_kind,
        "coverage": source.coverage,
        "version_detection": serialize_source_version_detection(source.version_detection),
        "search_root": (
            None
            if source.search_root is None
            else agentgrep.format_display_path(source.search_root, directory=True)
        ),
        "mtime_ns": source.mtime_ns,
    }


def serialize_source_version_detection(
    detection: SourceVersionDetection | None,
) -> SourceVersionDetectionPayload | None:
    """Serialize source version metadata for JSON/MCP discovery payloads."""
    if detection is None:
        return None
    return {
        "app_version": detection.app_version,
        "data_version": detection.data_version,
        "strategy": detection.strategy,
        "confidence": detection.confidence,
        "evidence": detection.evidence,
    }


def build_envelope(
    command: str,
    query_data: dict[str, object],
    results: list[dict[str, object]],
) -> EnvelopePayload:
    """Build a JSON envelope."""
    return {
        "schema_version": agentgrep.SCHEMA_VERSION,
        "command": command,
        "query": query_data,
        "results": results,
    }


def print_find_results(records: list[FindRecord], args: FindArgs) -> None:
    """Emit find results in the requested format.

    ``--list-details`` switches to a one-line-per-record long format with
    agent / kind / store / adapter_id / path columns. ``--print0``
    separates records with NUL instead of newline (for ``xargs -0``).
    ``--json`` / ``--ndjson`` are unaffected by these flags.
    """
    _, serialize_find, serialize_envelope = maybe_build_pydantic()
    query_data: dict[str, object] = {
        "pattern": args.pattern,
        "agents": list(args.agents),
        "limit": args.limit,
        "pattern_mode": args.pattern_mode,
        "type_filter": args.type_filter,
        "extensions": list(args.extensions),
    }
    if args.output_mode == "json":
        payload = serialize_envelope(
            "find",
            query_data,
            [serialize_find(record) for record in records],
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.output_mode == "ndjson":
        for record in records:
            print(json.dumps(serialize_find(record), ensure_ascii=False))
        return
    if args.print0:
        for record in records:
            line = _format_find_text_line(record, args)
            sys.stdout.write(line)
            sys.stdout.write("\0")
        sys.stdout.flush()
        return
    if args.list_details:
        for record in records:
            print(_format_find_text_line(record, args))
        return
    for record in records:
        print(agentgrep.format_display_path(record.path))


def _format_find_text_line(record: FindRecord, args: FindArgs) -> str:
    """Compose one line for ``--list-details`` / ``--print0`` output."""
    path = agentgrep.format_display_path(record.path)
    if args.list_details:
        return f"{record.agent}\t{record.path_kind}\t{record.store}\t{record.adapter_id}\t{path}"
    return path


def _resolve_find_case_sensitive(pattern: str | None, mode: agentgrep.CaseMode) -> bool:
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
    ``--type``); at the file granularity ``find`` operates on, both map
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


def _find_path_is_eager(args: FindArgs) -> bool:
    """Return ``True`` when find's output mode needs the full record list."""
    return args.output_mode == "json" or args.list_details


def stream_find_results(args: FindArgs) -> int:
    """Stream find records to stdout as the engine emits them.

    Consumes :func:`agentgrep.iter_find_events` and filters for
    :class:`agentgrep.events.FindRecordEmitted`. Applies the fd-shaped
    pattern / type / extension / case filters at the consumer level via
    :func:`_find_record_passes` so the engine doesn't need to know about
    those args. Honors ``args.limit`` by breaking the loop once the
    surviving-record count reaches it.

    Returns ``0`` when at least one record was emitted, ``1`` otherwise.
    Eager output modes (``--json`` and ``-l``) route through
    :func:`print_find_results` via :func:`run_find_command` instead.
    """
    from agentgrep import events

    is_tty = sys.stdout.isatty()
    match_count = 0
    serialize_find: t.Callable[[FindRecord], dict[str, object]] | None = None
    if args.output_mode == "ndjson":
        _, serialize_find, _ = maybe_build_pydantic()
    for event in agentgrep.iter_find_events(
        pathlib.Path.home(),
        args.agents,
        pattern=None,
        limit=None,
        compiled=args.compiled,
    ):
        if not isinstance(event, events.FindRecordEmitted):
            continue
        if not _find_record_passes(event.record, args):
            continue
        if args.output_mode == "ndjson" and serialize_find is not None:
            print(json.dumps(serialize_find(event.record), ensure_ascii=False))
        elif args.print0:
            sys.stdout.write(_format_find_text_line(event.record, args))
            sys.stdout.write("\0")
        else:
            print(agentgrep.format_display_path(event.record.path))
        if is_tty:
            sys.stdout.flush()
        match_count += 1
        if args.limit is not None and match_count >= args.limit:
            break
    if match_count == 0 and args.output_mode == "text" and not args.print0:
        print("No matching sources found.", file=sys.stderr)
    return 0 if match_count > 0 else 1


def run_find_command(args: FindArgs) -> int:
    """Execute ``agentgrep find``.

    Routes through either the live streaming path
    (:func:`stream_find_results`, used for text / NDJSON / ``--print0``)
    or the eager list path (:func:`print_find_results`, used for
    ``--json`` and ``--list-details``). See :func:`_find_path_is_eager`
    for the routing decision.

    The ``--ui`` overlay translates the find filters into a
    :class:`SearchQuery` seeded with the same agent / type narrowing,
    then opens the Textual explorer. This mirrors the ``tig`` model:
    same query semantics, different presentation.
    """
    if args.output_mode == "ui":
        query = agentgrep.SearchQuery(
            terms=(args.pattern,) if args.pattern else (),
            search_type="all",
            any_term=False,
            regex=args.pattern_mode == "regex",
            case_sensitive=args.case_mode == "respect",
            agents=args.agents,
            limit=args.limit,
            compiled=args.compiled,
        )
        agentgrep.run_ui(
            pathlib.Path.home(),
            query,
            control=agentgrep.SearchControl(),
            initial_search_text=args.raw_query or None,
        )
        return 0
    from agentgrep import events

    if not _find_path_is_eager(args):
        return stream_find_results(args)
    # Eager output modes (--json, --list-details) need the full
    # record list up front. Drain :func:`agentgrep.iter_find_events`
    # with ``compiled`` so source-level field predicates
    # (``agent:``, ``path:``, ``store:``, ``mtime:``) prune sources;
    # without it, every agent's sources are returned unfiltered.
    raw_records: list[FindRecord] = [
        event.record
        for event in agentgrep.iter_find_events(
            pathlib.Path.home(),
            args.agents,
            pattern=None,
            limit=None,
            compiled=args.compiled,
        )
        if isinstance(event, events.FindRecordEmitted)
    ]
    records = filter_find_records(raw_records, args)
    print_find_results(records, args)
    if records:
        return 0
    if args.output_mode == "text":
        print("No matching sources found.", file=sys.stderr)
    return 1


def run_ui_command(args: UIArgs) -> int:
    """Execute ``agentgrep ui``."""
    initial_terms = tuple(args.initial_query.split()) if args.initial_query else ()
    query = agentgrep.SearchQuery(
        terms=initial_terms,
        search_type="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agentgrep.AGENT_CHOICES,
        limit=None,
    )
    agentgrep.run_ui(pathlib.Path.home(), query, control=agentgrep.SearchControl())
    return 0


def run_search_command(args: SearchArgs) -> int:
    """Execute ``agentgrep search`` with ranked, pretty output.

    Collects all matching records eagerly with a progress spinner,
    scores them by rapidfuzz partial_ratio (skipped with ``--no-rank``
    or on answer-now), groups by session (skipped with ``--no-group``),
    and renders with snippet-first pretty output.  Returns ``0`` when
    at least one result survives, ``1`` otherwise.
    """
    if not args.terms and args.compiled is None and args.output_mode != "ui":
        msg = "search requires at least one term unless --ui is used"
        raise SystemExit(msg)
    query = agentgrep.SearchQuery(
        terms=args.terms,
        search_type=args.search_type,
        any_term=args.any_term,
        regex=args.regex,
        case_sensitive=args.case_sensitive,
        agents=args.agents,
        limit=args.limit,
        compiled=args.compiled,
    )
    if args.output_mode == "ui":
        agentgrep.run_ui(
            pathlib.Path.home(),
            query,
            control=agentgrep.SearchControl(),
            initial_search_text=args.raw_query or None,
        )
        return 0
    if args.output_mode in ("json", "ndjson"):
        return _run_search_eager(args, query)
    control = agentgrep.SearchControl()
    human_output = args.output_mode == "text"
    progress_enabled = args.progress_mode == "always" or (
        args.progress_mode == "auto" and human_output
    )
    answer_now_enabled = (
        progress_enabled
        and human_output
        and bool(getattr(sys.stdin, "isatty", lambda: False)())
        and bool(getattr(sys.stderr, "isatty", lambda: False)())
    )
    listener = agentgrep.AnswerNowInputListener(control) if answer_now_enabled else None
    progress: agentgrep.SearchProgress
    if not progress_enabled:
        progress = agentgrep.noop_search_progress()
    else:
        progress = agentgrep.ConsoleSearchProgress(
            enabled=True,
            color_mode=args.color_mode,
            answer_now_hint=answer_now_enabled,
        )
    if listener is not None:
        listener.start()
    try:
        records = agentgrep.run_search_query(
            pathlib.Path.home(),
            query,
            progress=progress,
            control=control,
        )
    finally:
        if listener is not None:
            listener.stop()
    query_text = " ".join(args.terms)
    answered_early = control.answer_now_requested()
    if args.no_rank or answered_early or not query_text:
        scored: list[tuple[agentgrep.SearchRecord, float]] = [(r, 0.0) for r in records]
    else:
        from agentgrep.ranking import rank_search_records

        scored = rank_search_records(records, query_text, threshold=args.threshold)
    if args.limit is not None:
        scored = scored[: args.limit]
    from agentgrep.ranking import group_by_session

    grouped = group_by_session([(r, s, 0) for r, s in scored])
    _print_search_text(grouped, args)
    return 0 if scored else 1


def _compile_search_patterns(args: SearchArgs) -> list[re.Pattern[str]]:
    """Compile search terms to regex for snippet highlighting."""
    flags = 0 if args.case_sensitive else re.IGNORECASE
    compiled: list[re.Pattern[str]] = []
    for term in args.terms:
        if ":" in term:
            continue
        source = term if args.regex else re.escape(term)
        try:
            compiled.append(re.compile(source, flags))
        except re.error:
            continue
    return compiled


def _print_search_text(
    groups: list[tuple[str | None, list[tuple[agentgrep.SearchRecord, float, int]]]],
    args: SearchArgs,
) -> None:
    """Render ranked search results with pretty snippets."""
    colors = agentgrep.AnsiColors.for_stream(args.color_mode, sys.stdout)
    patterns = _compile_search_patterns(args)
    first_group = True
    for session_id, entries in groups:
        if not first_group:
            print()
        first_group = False
        if session_id is not None and not args.no_group:
            print(colors.heading(f"[session {session_id[:12]}]"))
        for record, _score, _similar in entries:
            lines: list[str] = []
            if record.text:
                snippet, remaining = extract_search_snippet(record.text, patterns)
                highlighted = highlight_search_spans(snippet, patterns, colors=colors)
                lines.append(highlighted)
                if remaining > 0:
                    lines.append(colors.dim(f"  ... {remaining} more lines"))
            provenance_parts: list[str] = [record.agent, record.kind]
            if record.timestamp:
                provenance_parts.append(format_relative_time(record.timestamp))
            provenance_parts.append(
                colors.path(agentgrep.format_display_path(record.path)),
            )
            lines.append(colors.dim(f"  {' · '.join(provenance_parts)}"))
            print("\n".join(lines))
            print()


def _run_search_eager(args: SearchArgs, query: agentgrep.SearchQuery) -> int:
    """Eager search for JSON/NDJSON output with ranking but no pairwise dedup."""
    control = agentgrep.SearchControl()
    records = agentgrep.run_search_query(
        pathlib.Path.home(),
        query,
        progress=agentgrep.noop_search_progress(),
        control=control,
    )
    query_text = " ".join(args.terms)
    if args.no_rank or not query_text:
        scored: list[tuple[agentgrep.SearchRecord, float]] = [(r, 0.0) for r in records]
    else:
        from agentgrep.ranking import rank_search_records

        scored = rank_search_records(records, query_text, threshold=args.threshold)
    if args.limit is not None:
        scored = scored[: args.limit]
    from agentgrep.ranking import group_by_session

    grouped = group_by_session([(r, s, 0) for r, s in scored])
    serialize_search, _, serialize_envelope = maybe_build_pydantic()
    results: list[dict[str, object]] = []
    for session_id, entries in grouped:
        for record, score, _similar in entries:
            entry = dict(serialize_search(record))
            entry["score"] = score
            if session_id is not None:
                entry["group_session_id"] = session_id
            results.append(entry)
    if args.output_mode == "json":
        query_data: dict[str, object] = {
            "terms": list(args.terms),
            "agents": list(args.agents),
            "threshold": args.threshold,
            "no_rank": args.no_rank,
            "no_group": args.no_group,
        }
        payload = serialize_envelope("search", query_data, results)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(json.dumps(result, ensure_ascii=False))
    return 0 if results else 1


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
    colors: agentgrep.AnsiColors,
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
        if spans:
            yield line_number, line, _merge_overlapping_spans(spans)


def format_grep_line(
    line_number: int,
    line_text: str,
    match_spans: list[tuple[int, int]],
    *,
    colors: agentgrep.AnsiColors,
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
    record: agentgrep.SearchRecord,
    *,
    colors: agentgrep.AnsiColors,
) -> str:
    """Format the per-record heading line for heading-mode grep output.

    Shape: ``agent  [timestamp]  path``, all in muted gray except the
    path which gets the rg-shaped magenta. Empty timestamps are
    suppressed so synthetic records without one don't carry a stray
    double-space.
    """
    path = agentgrep.format_display_path(record.path)
    pieces = [colors.muted(record.agent)]
    if record.timestamp:
        pieces.append(colors.muted(record.timestamp))
    pieces.append(colors.path(path))
    return "  ".join(pieces)


def build_grep_query(args: GrepArgs) -> agentgrep.SearchQuery:
    r"""Translate :class:`GrepArgs` into a :class:`agentgrep.SearchQuery`.

    Encodes rg's smart-case and pattern-mode resolution: ``-i`` forces
    case-insensitive, ``-s`` forces case-sensitive, otherwise smart-case
    derives from the presence of uppercase in any pattern. ``-w`` wraps
    each pattern in ``\b…\b`` so word-regexp semantics survive into the
    engine's per-term matching.
    """
    if args.case_mode == "ignore":
        case_sensitive = False
    elif args.case_mode == "respect":
        case_sensitive = True
    else:  # smart
        case_sensitive = any(any(ch.isupper() for ch in pattern) for pattern in args.patterns)

    regex = args.pattern_mode != "fixed"
    if args.pattern_mode == "word":
        terms = tuple(rf"\b{pattern}\b" for pattern in args.patterns)
    else:
        terms = args.patterns

    return agentgrep.SearchQuery(
        terms=terms,
        search_type=args.search_type,
        any_term=False,
        regex=regex,
        case_sensitive=case_sensitive,
        agents=args.agents,
        limit=args.max_count,
        dedupe=not args.no_dedupe,
        compiled=args.compiled,
    )


def serialize_grep_record(
    record: agentgrep.SearchRecord,
    *,
    line_number: int | None = None,
) -> dict[str, object]:
    """Serialize a search record for ``grep --json`` event-stream output.

    Mirrors rg's ``--json`` shape at a high level: a ``match`` event
    carries the source path, the matched text, optional line number,
    and origin metadata (agent / store / session).

    Kept for backward compatibility (it's in the public re-export
    surface). Live ``--json`` / ``--ndjson`` output uses the per-line
    :func:`serialize_grep_begin`, :func:`serialize_grep_match_line`,
    and :func:`serialize_grep_end` helpers instead.
    """
    return {
        "type": "match",
        "data": {
            "agent": record.agent,
            "store": record.store,
            "adapter_id": record.adapter_id,
            "path": agentgrep.format_display_path(record.path),
            "line_number": line_number,
            "text": record.text,
            "timestamp": record.timestamp,
            "session_id": record.session_id,
            "conversation_id": record.conversation_id,
        },
    }


def serialize_grep_begin(record: agentgrep.SearchRecord) -> dict[str, object]:
    """Emit the ``begin`` event that opens each record in ``--json``.

    Mirrors rg's per-file ``begin`` envelope, adapted for agentgrep —
    carries the record's origin metadata so downstream consumers can
    route events by agent / store / session without waiting for the
    first ``match`` event.
    """
    return {
        "type": "begin",
        "data": {
            "path": {"text": agentgrep.format_display_path(record.path)},
            "agent": record.agent,
            "store": record.store,
            "adapter_id": record.adapter_id,
            "timestamp": record.timestamp,
            "session_id": record.session_id,
            "conversation_id": record.conversation_id,
        },
    }


def serialize_grep_match_line(
    record: agentgrep.SearchRecord,
    line_number: int,
    line_text: str,
    match_spans: list[tuple[int, int]],
) -> dict[str, object]:
    """Emit one rg-shaped ``match`` event per matching line.

    Mirrors rg's ``--json`` per-line event vocabulary: nested
    ``path.text`` and ``lines.text``, 1-indexed ``line_number``, and
    ``submatches`` as ``[{"match": {"text": ...}, "start": int,
    "end": int}, ...]`` carrying byte offsets within the line. Each
    submatch's ``text`` is the substring sliced from ``line_text``.
    """
    submatches = [
        {"match": {"text": line_text[start:end]}, "start": start, "end": end}
        for start, end in match_spans
    ]
    return {
        "type": "match",
        "data": {
            "path": {"text": agentgrep.format_display_path(record.path)},
            "line_number": line_number,
            "lines": {"text": line_text},
            "submatches": submatches,
        },
    }


def serialize_grep_end(
    record: agentgrep.SearchRecord,
    *,
    matched_lines: int,
    matches: int,
) -> dict[str, object]:
    """Emit the ``end`` event that closes each record in ``--json``.

    Carries the per-record tallies (matched lines vs total match spans)
    so downstream consumers can build summaries without re-counting.
    """
    return {
        "type": "end",
        "data": {
            "path": {"text": agentgrep.format_display_path(record.path)},
            "stats": {
                "matched_lines": matched_lines,
                "matches": matches,
            },
        },
    }


def _iter_grep_json_events(
    records: list[agentgrep.SearchRecord],
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

    def add(self, record: agentgrep.SearchRecord) -> None:
        """Record one emitted search result."""
        self.total += 1
        self.per_agent[record.agent] = self.per_agent.get(record.agent, 0) + 1

    def format(self, *, colors: agentgrep.AnsiColors) -> str:
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
    record: agentgrep.SearchRecord,
    args: GrepArgs,
    *,
    colors: agentgrep.AnsiColors,
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
    display_path = agentgrep.format_display_path(record.path)
    provenance_parts.append(colors.path(display_path))
    provenance = " · ".join(provenance_parts)
    lines.append(colors.dim(f"  {provenance}"))

    return "\n".join(lines)


def format_grep_record(record: agentgrep.SearchRecord, args: GrepArgs) -> str:
    """Format one matching record for text-mode ``grep`` output.

    Default shape (rg-faithful): ``path:text`` on pipe, ``text`` rows
    grouped under a heading line on TTY. ``-n`` / ``--column`` /
    ``--vimgrep`` add line and column prefixes per rg's resolution.

    ``--vimgrep`` emits one row per match span (one line can produce
    multiple rows). ``-o`` / ``--only-matching`` emits only the matched
    substrings; ``-l`` / ``-L`` emit just the path.
    """
    path = agentgrep.format_display_path(record.path)
    if args.files_with_matches or args.files_without_match:
        return path
    colors = agentgrep.AnsiColors.for_stream(args.color_mode, sys.stdout)
    matches = list(iter_match_lines(record.text, args))

    if args.only_matching:
        chunks: list[str] = []
        for _, line, spans in matches:
            for start, end in spans:
                chunks.append(line[start:end])
        return "\n".join(chunks)

    if args.vimgrep:
        rows: list[str] = []
        for line_no, line, spans in matches:
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


def print_grep_results(records: list[agentgrep.SearchRecord], args: GrepArgs) -> int:
    """Emit grep results and return the rg-style exit code."""
    if args.invert_match:
        if args.count_only:
            print("0" if records else "1")
            return 1 if records else 0
        if args.files_without_match:
            return _print_files_without_match(args)
        print(
            "error: --invert-match/-v is supported with -c and -L only; "
            "engine-level line inversion is tracked at "
            "https://github.com/tony/agentgrep/issues/8",
            file=sys.stderr,
        )
        return 2

    if args.output_mode == "json":
        events = list(_iter_grep_json_events(records, args))
        total_match_count = sum(1 for event in events if event.get("type") == "match")
        events.append({"type": "summary", "data": {"matches": total_match_count}})
        print(json.dumps({"command": "grep", "events": events}, ensure_ascii=False, indent=2))
        return 0 if total_match_count > 0 else 1
    if args.output_mode == "ndjson":
        emitted_matches = 0
        for event in _iter_grep_json_events(records, args):
            print(json.dumps(event, ensure_ascii=False))
            if event.get("type") == "match":
                emitted_matches += 1
        return 0 if emitted_matches > 0 else 1

    if args.count_only:
        colors = agentgrep.AnsiColors.for_stream(args.color_mode, sys.stdout)
        per_record_counts: list[tuple[agentgrep.SearchRecord, int]] = []
        for record in records:
            count = sum(1 for _ in iter_match_lines(record.text, args))
            per_record_counts.append((record, count))
        # rg parity: single-file emits just N; multi-file emits path:N per file.
        if len(per_record_counts) == 1:
            print(per_record_counts[0][1])
        else:
            for record, count in per_record_counts:
                path = agentgrep.format_display_path(record.path)
                print(f"{colors.path(path)}:{count}")
        return 0 if records else 1
    if args.files_with_matches:
        seen: set[str] = set()
        for record in records:
            path = agentgrep.format_display_path(record.path)
            if path not in seen:
                seen.add(path)
                print(path)
        return 0 if records else 1
    if args.files_without_match:
        return _print_files_without_match(args)

    if not records:
        if args.output_mode == "text":
            print("No matches found.", file=sys.stderr)
        return 1
    for record in records:
        print(format_grep_record(record, args))
        if not args.only_matching and (
            args.heading is True or (args.heading is None and sys.stdout.isatty())
        ):
            print()
    return 0


def _print_files_without_match(args: GrepArgs) -> int:
    """Print sources whose records produced no matches (rg ``-L`` parity).

    Runs the same query the engine would run, collects the set of paths
    that emitted at least one :class:`agentgrep.events.RecordEmitted`,
    then prints the complement against the engine's planned-source list
    so the user gets the file-level "no match" view rg's ``-L`` exposes.

    Re-uses the public :func:`agentgrep.discover_sources` and
    :func:`agentgrep.plan_search_sources` helpers so this consumer-layer
    implementation tracks any future changes to the engine's source
    selection logic without duplicating filter rules.

    Returns ``0`` when at least one path is printed (the "no-match
    file" is itself a positive result for ``-L``), ``1`` otherwise.
    """
    from agentgrep import events

    query = build_grep_query(args)
    home = pathlib.Path.home()

    matched_paths: set[pathlib.Path] = set()
    for event in agentgrep.iter_search_events(
        home,
        query,
        control=agentgrep.SearchControl(),
    ):
        if isinstance(event, events.RecordEmitted):
            matched_paths.add(event.record.path)

    backends = agentgrep.select_backends()
    discovered = agentgrep.discover_sources(home, args.agents, backends)
    planned = agentgrep.plan_search_sources(query, discovered, backends)

    colors = agentgrep.AnsiColors.for_stream(args.color_mode, sys.stdout)
    seen: set[str] = set()
    printed = 0
    for source in planned:
        if source.path in matched_paths:
            continue
        display = agentgrep.format_display_path(source.path)
        if display in seen:
            continue
        seen.add(display)
        print(colors.path(display))
        printed += 1
    return 0 if printed > 0 else 1


def fuzzy_filter_lines(
    lines: list[str],
    args: FuzzyArgs,
) -> list[tuple[str, float]]:
    """Apply fzf ``--filter`` semantics to ``lines`` and return ranked pairs.

    Selects between exact-substring and fuzzy scoring based on
    ``args.exact``, honors the extended-search token grammar when
    ``args.extended`` is set, and respects sort / no-sort. Field
    delimiter / nth / with-nth are applied before scoring so the
    user-facing fzf model holds.
    """
    from agentgrep import fuzzy as _fuzzy_lib

    case_mode: _fuzzy_lib.CaseSensitivity = args.case_mode
    algo: _fuzzy_lib.FuzzyAlgo = args.algo
    transformed = _apply_field_selection(lines, args)
    if args.exact:
        matched: list[tuple[str, float]] = []
        if args.extended:
            for original, display in transformed:
                if _fuzzy_lib.extended_match(args.query, display, case=case_mode):
                    matched.append((original, 1.0))
        else:
            case_sensitive = _fuzzy_lib.resolve_case_sensitivity(args.query, case_mode)
            needle = args.query if case_sensitive else args.query.casefold()
            for original, display in transformed:
                haystack = display if case_sensitive else display.casefold()
                if needle in haystack:
                    matched.append((original, 1.0))
        if args.sort:
            return sorted(matched, key=lambda pair: pair[1], reverse=True)
        return matched
    matched = list(
        _fuzzy_lib.rank_lines(
            args.query,
            (display for _, display in transformed),
            case=case_mode,
            algo=algo,
            extended=args.extended,
            sort=args.sort,
            limit=None,
        ),
    )
    display_to_original: dict[str, str] = {display: original for original, display in transformed}
    return [(display_to_original.get(display, display), score) for display, score in matched]


def _apply_field_selection(
    lines: list[str],
    args: FuzzyArgs,
) -> list[tuple[str, str]]:
    """Apply ``--delimiter`` / ``--nth`` / ``--with-nth`` field selection.

    Returns ``(original, display)`` pairs. ``original`` is the raw input
    line, ``display`` is what's scored / printed. When no field selectors
    are set, the two are equal.
    """
    delimiter = args.delimiter
    if delimiter is None and args.nth is None and args.with_nth is None:
        return [(line, line) for line in lines]
    sep = delimiter if delimiter is not None else None
    pairs: list[tuple[str, str]] = []
    for line in lines:
        fields = line.split(sep) if sep is not None else line.split()
        if args.nth is not None and 1 <= args.nth <= len(fields):
            score_target = fields[args.nth - 1]
        else:
            score_target = line
        if args.with_nth is not None and 1 <= args.with_nth <= len(fields):
            display = fields[args.with_nth - 1]
        else:
            display = score_target
        pairs.append((line, display))
    return pairs


def run_fuzzy_command(args: FuzzyArgs) -> int:
    """Execute ``agentgrep fuzzy``.

    Reads lines from stdin (NUL- or newline-delimited per ``--read0``),
    applies the fzf-style filter, and prints matching lines to stdout.
    Exits 0 when at least one line matches, 1 when nothing matches.

    The ``--ui`` overlay opens the Textual explorer pre-filled with the
    fuzzy query so users can browse interactively from the same
    invocation (the ``tig`` model).
    """
    if args.output_mode == "ui":
        query = agentgrep.SearchQuery(
            terms=(args.query,) if args.query else (),
            search_type="all",
            any_term=False,
            regex=False,
            case_sensitive=args.case_mode == "respect",
            agents=args.agents,
            limit=None,
        )
        agentgrep.run_ui(
            pathlib.Path.home(),
            query,
            control=agentgrep.SearchControl(),
            initial_search_text=args.query or None,
        )
        return 0
    separator = "\0" if args.read0 else "\n"
    raw = sys.stdin.read()
    lines = [line for line in raw.split(separator) if line]
    ranked = fuzzy_filter_lines(lines, args)
    out_sep = "\0" if args.print0 else "\n"
    out = sys.stdout

    def _emit(text: str) -> None:
        out.write(text)
        out.write(out_sep)

    if args.print_query:
        _emit(args.query)
    for original, _ in ranked:
        _emit(original)
    out.flush()
    return 0 if ranked else 1


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


def _grep_path_is_eager(args: GrepArgs) -> bool:
    """Return ``True`` when grep's output mode needs the full record list.

    The eager outputs need a final tally or cross-record deduplication that
    only makes sense after every match is known. The streaming outputs
    (text, NDJSON, vimgrep, only-matching) can emit per record as they
    arrive.
    """
    return (
        args.output_mode == "json"
        or args.count_only
        or args.files_with_matches
        or args.files_without_match
        or args.invert_match
    )


def stream_grep_results(args: GrepArgs) -> int:
    """Stream grep matches to stdout as the engine emits them.

    Consumes :func:`agentgrep.iter_search_events` and filters for
    :class:`agentgrep.events.RecordEmitted`. Prints each match and flushes
    stdout when stdout is a TTY so live terminals see rows as they arrive
    rather than waiting for a block-buffer flush. Returns the rg-style
    exit code (``0`` if any match was emitted, ``1`` otherwise).

    Only the streaming-friendly output modes route here — :func:`run_grep_command`
    picks :func:`print_grep_results` for JSON, ``-c``, ``-l``, ``-L``,
    and ``-v`` paths that need the full record list up front.
    """
    from agentgrep import events

    query = build_grep_query(args)
    control = agentgrep.SearchControl()
    is_tty = sys.stdout.isatty()
    match_count = 0
    pretty = args.style == "pretty"
    summary = GrepSummary() if pretty else None
    for event in agentgrep.iter_search_events(
        pathlib.Path.home(),
        query,
        control=control,
    ):
        if isinstance(event, events.RecordEmitted):
            if args.output_mode == "ndjson":
                for json_event in _iter_grep_json_events([event.record], args):
                    print(json.dumps(json_event, ensure_ascii=False))
                    if json_event.get("type") == "match":
                        match_count += 1
            else:
                print(format_grep_record(event.record, args))
                if pretty or (
                    not args.only_matching
                    and (args.heading is True or (args.heading is None and is_tty))
                ):
                    print()
                match_count += 1
                if summary is not None:
                    summary.add(event.record)
            if is_tty:
                sys.stdout.flush()
        elif isinstance(event, events.SearchFinished) and summary is not None:
            summary.elapsed = event.elapsed_seconds
    if is_tty and summary is not None and summary.total > 0:
        footer = summary.format(colors=agentgrep.AnsiColors.for_stream(args.color_mode, sys.stderr))
        if footer:
            print(footer, file=sys.stderr)
    if match_count == 0 and args.output_mode == "text":
        print("No matches found.", file=sys.stderr)
    return 0 if match_count > 0 else 1


def run_grep_command(args: GrepArgs) -> int:
    """Execute ``agentgrep grep``.

    Routes the request through either the live streaming path
    (:func:`stream_grep_results`) or the eager list path
    (:func:`print_grep_results`), depending on the requested output mode.
    See :func:`_grep_path_is_eager` for the routing decision.
    """
    if not args.patterns:
        msg = "grep requires at least one pattern"
        raise SystemExit(msg)
    query = build_grep_query(args)
    if args.output_mode == "ui":
        agentgrep.run_ui(
            pathlib.Path.home(),
            query,
            control=agentgrep.SearchControl(),
            initial_search_text=args.raw_query or None,
        )
        return 0
    if not _grep_path_is_eager(args):
        return stream_grep_results(args)
    control = agentgrep.SearchControl()
    human_output = args.output_mode in {"text", "ui"}
    progress_enabled = args.progress_mode == "always" or (
        args.progress_mode == "auto" and human_output
    )
    progress: agentgrep.SearchProgress
    if not progress_enabled:
        progress = agentgrep.noop_search_progress()
    else:
        progress = agentgrep.ConsoleSearchProgress(
            enabled=True,
            color_mode=args.color_mode,
            answer_now_hint=False,
        )
    records = agentgrep.run_search_query(
        pathlib.Path.home(),
        query,
        progress=progress,
        control=control,
    )
    return print_grep_results(records, args)
