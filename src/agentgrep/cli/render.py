"""Subcommand dispatch for the agentgrep CLI.

Routes parsed ``grep`` / ``find`` / ``search`` / ``ui`` arguments to the
engine and the chosen output mode, picking the streaming or eager path per
subcommand and handing records to the right formatter. The JSON payload
serializers live in :mod:`agentgrep.cli.serializers` and the text formatters
in :mod:`agentgrep.cli.renderers`.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import json
import logging
import pathlib
import sys
import time
import typing as t

from agentgrep import _telemetry, run_ui
from agentgrep._engine import iter_find_events, iter_search_events
from agentgrep._engine.orchestration import run_search_query
from agentgrep._text import AnsiColors, format_display_path
from agentgrep.cli.parser import FindArgs, GrepArgs, SearchArgs, UIArgs
from agentgrep.cli.renderers import (
    GrepSummary,
    _compile_search_patterns,
    _find_record_passes,
    _format_find_path,
    _format_find_text_line,
    _iter_grep_json_events,
    extract_search_snippet,
    filter_find_records,
    format_grep_heading,
    format_grep_line,
    format_grep_record,
    format_grep_record_pretty,
    format_relative_time,
    highlight_search_spans,
    iter_match_lines,
)
from agentgrep.cli.serializers import (
    build_envelope,
    serialize_find_record,
    serialize_grep_record,
    serialize_search_record,
    serialize_source_handle,
)
from agentgrep.progress import (
    AnswerNowInputListener,
    ConsoleSearchProgress,
    SearchControl,
    SearchProgress,
    noop_search_progress,
)
from agentgrep.query.compile import CompiledQuery
from agentgrep.records import AGENT_CHOICES, FindRecord, SearchQuery, SearchRecord, SearchScope

logger = logging.getLogger(__name__)

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
    "highlight_search_spans",
    "iter_match_lines",
    "print_find_results",
    "print_grep_results",
    "run_find_command",
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


def _launch_ui(
    query: SearchQuery,
    *,
    initial_search_text: str | None = None,
    base_scope: SearchScope | None = None,
) -> None:
    """Launch the UI and translate factory validation into a CLI diagnostic."""
    from agentgrep.ui.app import UiQueryTooLongError

    try:
        run_ui(
            pathlib.Path.home(),
            query,
            control=SearchControl(),
            initial_search_text=initial_search_text,
            base_scope=base_scope,
        )
    except UiQueryTooLongError as error:
        raise SystemExit(str(error)) from None


def print_find_results(records: list[FindRecord], args: FindArgs) -> None:
    """Emit find results in the requested format.

    ``--list-details`` switches to a one-line-per-record long format with
    agent / kind / store / adapter_id / path columns. ``--print0``
    separates records with NUL instead of newline (for ``xargs -0``) and,
    like ``--absolute-path``, emits real filesystem paths; other modes
    collapse the home directory to ``~``. ``--json`` / ``--ndjson`` are
    unaffected by these flags.
    """
    query_data: dict[str, object] = {
        "pattern": args.pattern,
        "agents": list(args.agents),
        "limit": args.limit,
        "pattern_mode": args.pattern_mode,
        "type_filter": args.type_filter,
        "extensions": list(args.extensions),
    }
    if args.output_mode == "json":
        payload = build_envelope(
            "find",
            query_data,
            [dict(serialize_find_record(record)) for record in records],
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.output_mode == "ndjson":
        for record in records:
            print(json.dumps(serialize_find_record(record), ensure_ascii=False))
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
        print(_format_find_path(record, args))


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
    # Lazy import: ``agentgrep.events`` stays off the eager ``import
    # agentgrep`` path (pinned by tests/test_import_time.py) — only the running
    # subcommand pulls in the event-stream types.
    from agentgrep import events

    is_tty = sys.stdout.isatty()
    match_count = 0
    for event in iter_find_events(
        pathlib.Path.home(),
        args.agents,
        pattern=None,
        limit=None,
        compiled=args.compiled,
        type_filter=args.type_filter,
    ):
        if not isinstance(event, events.FindRecordEmitted):
            continue
        if not _find_record_passes(event.record, args):
            continue
        if args.output_mode == "ndjson":
            print(json.dumps(serialize_find_record(event.record), ensure_ascii=False))
        elif args.print0:
            sys.stdout.write(_format_find_text_line(event.record, args))
            sys.stdout.write("\0")
        else:
            print(_format_find_path(event.record, args))
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
    :class:`SearchQuery` seeded with the same agent / scope narrowing,
    then opens the Textual explorer. This mirrors the ``tig`` model:
    same query semantics, different presentation.
    """
    if args.output_mode == "ui":
        query = SearchQuery(
            terms=(args.pattern,) if args.pattern else (),
            scope="all",
            any_term=False,
            regex=args.pattern_mode == "regex",
            case_sensitive=args.case_mode == "respect",
            agents=args.agents,
            limit=args.limit,
            compiled=args.compiled,
        )
        _launch_ui(
            query,
            initial_search_text=args.raw_query or None,
            base_scope="all",
        )
        return 0

    if not _find_path_is_eager(args):
        return stream_find_results(args)
    # Lazy import keeps ``agentgrep.events`` off the eager ``import
    # agentgrep`` path (pinned by tests/test_import_time.py).
    from agentgrep import events

    # Eager output modes (--json, --list-details) need the full
    # record list up front. Drain :func:`agentgrep.iter_find_events`
    # with ``compiled`` so source-level field predicates
    # (``agent:``, ``path:``, ``store:``, ``mtime:``) prune sources;
    # without it, every agent's sources are returned unfiltered.
    raw_records: list[FindRecord] = [
        event.record
        for event in iter_find_events(
            pathlib.Path.home(),
            args.agents,
            pattern=None,
            limit=None,
            compiled=args.compiled,
            type_filter=args.type_filter,
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
    from agentgrep.query import build_query_from_input, default_registry

    base = SearchQuery(
        terms=(),
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=AGENT_CHOICES,
        limit=None,
    )
    result = build_query_from_input(args.initial_query, base, default_registry())
    query = result.query or dataclasses.replace(
        base,
        terms=tuple(args.initial_query.split()),
    )
    _launch_ui(
        query,
        initial_search_text=args.initial_query or None,
        base_scope="prompts",
    )
    return 0


def run_search_command(args: SearchArgs) -> int:
    """Execute ``agentgrep search`` with ranked, pretty output.

    Collects all matching records eagerly with a progress spinner,
    scores them by rapidfuzz partial_ratio (skipped with ``--no-rank``
    or on answer-now), groups by session (skipped with ``--no-group``),
    and renders with snippet-first pretty output.  Returns ``0`` when
    at least one result survives, ``1`` otherwise.
    """
    if (
        not args.terms
        and args.compiled is None
        and args.origin_filter is None
        and args.output_mode != "ui"
    ):
        msg = "search requires at least one term unless --ui is used"
        raise SystemExit(msg)
    query = SearchQuery(
        terms=args.terms,
        scope=args.scope,
        any_term=False,
        regex=False,
        case_sensitive=args.case_sensitive,
        agents=args.agents,
        limit=args.limit,
        compiled=args.compiled,
        origin_filter=args.origin_filter,
    )
    if args.output_mode == "ui":
        _launch_ui(
            query,
            initial_search_text=args.raw_query or None,
            base_scope=args.base_scope,
        )
        return 0
    if args.output_mode in ("json", "ndjson"):
        return _run_search_eager(args, query)
    control = SearchControl()
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
    listener = AnswerNowInputListener(control) if answer_now_enabled else None
    progress: SearchProgress
    if not progress_enabled:
        progress = noop_search_progress()
    else:
        progress = ConsoleSearchProgress(
            enabled=True,
            color_mode=args.color_mode,
            answer_now_hint=answer_now_enabled,
        )
    if listener is not None:
        listener.start()
    try:
        records = run_search_query(
            pathlib.Path.home(),
            query,
            progress=progress,
            control=control,
        )
    finally:
        if listener is not None:
            listener.stop()
    answered_early = control.answer_now_requested()
    scored = _score_search_records(records, args, answered_early=answered_early)
    if args.limit is not None:
        scored = scored[: args.limit]
    from agentgrep.ranking import group_by_session

    grouped = group_by_session([(r, s, 0) for r, s in scored])
    _print_search_text(grouped, args)
    return 0 if scored else 1


def _print_search_text(
    groups: list[tuple[str | None, list[tuple[SearchRecord, float, int]]]],
    args: SearchArgs,
) -> None:
    """Render ranked search results with pretty snippets."""
    colors = AnsiColors.for_stream(args.color_mode, sys.stdout)
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
                colors.path(format_display_path(record.path)),
            )
            lines.append(colors.dim(f"  {' · '.join(provenance_parts)}"))
            print("\n".join(lines))
            print()


def _score_search_records(
    records: list[SearchRecord],
    args: SearchArgs,
    *,
    answered_early: bool = False,
) -> list[tuple[SearchRecord, float]]:
    """Rank search records when text relevance or origin boost can affect order."""
    query_text = " ".join(args.terms)
    if args.no_rank or answered_early or (not query_text and args.origin_boost is None):
        return [(r, 0.0) for r in records]

    from agentgrep.ranking import rank_search_records

    return rank_search_records(
        records,
        query_text,
        threshold=args.threshold if query_text else 0,
        origin_boost=args.origin_boost,
    )


def _run_search_eager(args: SearchArgs, query: SearchQuery) -> int:
    """Eager search for JSON/NDJSON output with ranking but no pairwise dedup."""
    control = SearchControl()
    records = run_search_query(
        pathlib.Path.home(),
        query,
        progress=noop_search_progress(),
        control=control,
    )
    scored = _score_search_records(records, args)
    if args.limit is not None:
        scored = scored[: args.limit]
    from agentgrep.ranking import group_by_session

    grouped = group_by_session([(r, s, 0) for r, s in scored])
    results: list[dict[str, object]] = []
    for session_id, entries in grouped:
        for record, score, _similar in entries:
            entry = dict(serialize_search_record(record))
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
        payload = build_envelope("search", query_data, results)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(json.dumps(result, ensure_ascii=False))
    return 0 if results else 1


def build_grep_query(args: GrepArgs) -> SearchQuery:
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

    terms_for_query = () if args.invert_match else terms
    compiled = _grep_candidate_compiled(args)
    return SearchQuery(
        terms=terms_for_query,
        scope=args.scope,
        any_term=False,
        regex=regex,
        case_sensitive=case_sensitive,
        agents=args.agents,
        limit=args.limit,
        dedupe=not args.no_dedupe,
        compiled=compiled,
        match_surface="text",
    )


def _grep_candidate_compiled(args: GrepArgs) -> CompiledQuery | None:
    """Return compiled predicates safe for grep candidate enumeration."""
    compiled = args.compiled
    if compiled is None or not args.invert_match:
        return compiled
    if compiled.source_predicate is None:
        return None
    return CompiledQuery(
        source_predicate=compiled.source_predicate,
        record_predicate=None,
        text_terms=(),
        is_pure_text=False,
    )


def _grep_emitted_count(records: cabc.Sequence[SearchRecord], args: GrepArgs) -> int:
    """Return the bounded record/line count emitted by eager grep paths."""
    if args.output_mode in {"json", "ndjson"}:
        return sum(
            1 for event in _iter_grep_json_events(list(records), args) if event["type"] == "match"
        )
    if args.count_only:
        return sum(1 for record in records if any(iter_match_lines(record.text, args)))
    if args.files_with_matches:
        seen: set[str] = set()
        for record in records:
            if not any(iter_match_lines(record.text, args)):
                continue
            seen.add(format_display_path(record.path))
        return len(seen)
    return sum(1 for record in records if format_grep_record(record, args))


def _record_grep_telemetry(
    args: GrepArgs,
    *,
    candidate_count: int,
    emitted_count: int,
    duration_ms: float,
    outcome: str,
) -> None:
    """Emit sparse grep dispatcher telemetry."""
    if not args.invert_match:
        return
    attributes = {
        "agentgrep_surface": "cli",
        "agentgrep_operation": "grep.invert",
        "agentgrep_command": "grep",
        "agentgrep_scope": args.scope,
        "agentgrep_output_mode": args.output_mode,
        "agentgrep_grep_invert": True,
        "agentgrep_grep_candidate_strategy": "all_candidates",
        "agentgrep_candidate_count": candidate_count,
        "agentgrep_emitted_count": emitted_count,
        "agentgrep_outcome": outcome,
        "agentgrep_duration_ms": duration_ms,
    }
    _telemetry.set_span_attribute("agentgrep_grep_invert", True)
    _telemetry.set_span_attribute(
        "agentgrep_grep_candidate_strategy",
        "all_candidates",
    )
    _telemetry.record_metric(
        "agentgrep.grep.candidate.count",
        candidate_count,
        **attributes,
    )
    _telemetry.record_metric(
        "agentgrep.grep.emitted.count",
        emitted_count,
        **attributes,
    )
    _telemetry.record_metric(
        "agentgrep.grep.duration",
        duration_ms,
        **attributes,
    )
    logger.info("grep invert completed", extra=attributes)


def print_grep_results(records: list[SearchRecord], args: GrepArgs) -> int:
    """Emit grep results and return the rg-style exit code."""
    if args.output_mode == "json":
        json_events = list(_iter_grep_json_events(records, args))
        total_match_count = sum(1 for event in json_events if event.get("type") == "match")
        json_events.append({"type": "summary", "data": {"matches": total_match_count}})
        print(json.dumps({"command": "grep", "events": json_events}, ensure_ascii=False, indent=2))
        return 0 if total_match_count > 0 else 1
    if args.output_mode == "ndjson":
        emitted_matches = 0
        for event in _iter_grep_json_events(records, args):
            print(json.dumps(event, ensure_ascii=False))
            if event.get("type") == "match":
                emitted_matches += 1
        return 0 if emitted_matches > 0 else 1

    if args.count_only:
        colors = AnsiColors.for_stream(args.color_mode, sys.stdout)
        per_record_counts: list[tuple[SearchRecord, int]] = []
        for record in records:
            count = sum(1 for _ in iter_match_lines(record.text, args))
            per_record_counts.append((record, count))
        # rg parity: single-file emits just N; multi-file emits path:N per file.
        if len(per_record_counts) == 1:
            print(per_record_counts[0][1])
        else:
            for record, count in per_record_counts:
                path = format_display_path(record.path)
                print(f"{colors.path(path)}:{count}")
        if args.invert_match:
            return 0 if any(count > 0 for _record, count in per_record_counts) else 1
        return 0 if records else 1
    if args.files_with_matches:
        seen: set[str] = set()
        for record in records:
            if args.invert_match and not any(iter_match_lines(record.text, args)):
                continue
            path = format_display_path(record.path)
            if path not in seen:
                seen.add(path)
                print(path)
        return 0 if seen else 1

    if not records:
        if args.output_mode == "text":
            print("No matches found.", file=sys.stderr)
        return 1
    emitted = False
    for record in records:
        text = format_grep_record(record, args)
        if not text:
            continue
        print(text)
        emitted = True
        if not args.only_matching and (
            args.heading is True or (args.heading is None and sys.stdout.isatty())
        ):
            print()
    if not emitted:
        if args.output_mode == "text":
            print("No matches found.", file=sys.stderr)
        return 1
    return 0


def _grep_path_is_eager(args: GrepArgs) -> bool:
    """Return ``True`` when grep's output mode needs the full record list.

    The eager outputs need a final tally or cross-record deduplication that
    only makes sense after every match is known. The streaming outputs
    (text, NDJSON, vimgrep, only-matching) can emit per record as they
    arrive.
    """
    return args.output_mode == "json" or args.count_only or args.files_with_matches


def stream_grep_results(args: GrepArgs) -> int:
    """Stream grep matches to stdout as the engine emits them.

    Consumes :func:`agentgrep.iter_search_events` and filters for
    :class:`agentgrep.events.RecordEmitted`. Prints each match and flushes
    stdout when stdout is a TTY so live terminals see rows as they arrive
    rather than waiting for a block-buffer flush. Returns the rg-style
    exit code (``0`` if any match was emitted, ``1`` otherwise).

    Only the streaming-friendly output modes route here — :func:`run_grep_command`
    picks :func:`print_grep_results` for JSON, ``-c``, ``-l``, and ``-L``
    paths that need the full record list up front.
    """
    # Lazy import keeps ``agentgrep.events`` off the eager ``import
    # agentgrep`` path (pinned by tests/test_import_time.py).
    from agentgrep import events

    started_at = time.monotonic()
    query = build_grep_query(args)
    control = SearchControl()
    is_tty = sys.stdout.isatty()
    match_count = 0
    candidate_count = 0
    pretty = args.style == "pretty"
    summary = GrepSummary() if pretty else None
    for event in iter_search_events(
        pathlib.Path.home(),
        query,
        control=control,
    ):
        if isinstance(event, events.RecordEmitted):
            candidate_count += 1
            if args.output_mode == "ndjson":
                for json_event in _iter_grep_json_events([event.record], args):
                    print(json.dumps(json_event, ensure_ascii=False))
                    if json_event.get("type") == "match":
                        match_count += 1
            else:
                text = format_grep_record(event.record, args)
                if not text:
                    continue
                print(text)
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
        footer = summary.format(colors=AnsiColors.for_stream(args.color_mode, sys.stderr))
        if footer:
            print(footer, file=sys.stderr)
    if match_count == 0 and args.output_mode == "text":
        print("No matches found.", file=sys.stderr)
    _record_grep_telemetry(
        args,
        candidate_count=candidate_count,
        emitted_count=match_count,
        duration_ms=(time.monotonic() - started_at) * 1000.0,
        outcome="match" if match_count > 0 else "no_match",
    )
    return 0 if match_count > 0 else 1


def run_grep_command(args: GrepArgs) -> int:
    """Execute ``agentgrep grep``.

    Routes the request through either the live streaming path
    (:func:`stream_grep_results`) or the eager list path
    (:func:`print_grep_results`), depending on the requested output mode.
    See :func:`_grep_path_is_eager` for the routing decision.
    """
    started_at = time.monotonic()
    if not args.patterns:
        msg = "grep requires at least one pattern"
        raise SystemExit(msg)
    query = build_grep_query(args)
    if args.output_mode == "ui":
        _launch_ui(
            query,
            initial_search_text=args.raw_query or None,
            base_scope=args.base_scope,
        )
        return 0
    if not _grep_path_is_eager(args):
        return stream_grep_results(args)
    control = SearchControl()
    human_output = args.output_mode in {"text", "ui"}
    progress_enabled = args.progress_mode == "always" or (
        args.progress_mode == "auto" and human_output
    )
    progress: SearchProgress
    if not progress_enabled:
        progress = noop_search_progress()
    else:
        progress = ConsoleSearchProgress(
            enabled=True,
            color_mode=args.color_mode,
            answer_now_hint=False,
        )
    records = run_search_query(
        pathlib.Path.home(),
        query,
        progress=progress,
        control=control,
    )
    exit_code = print_grep_results(records, args)
    _record_grep_telemetry(
        args,
        candidate_count=len(records),
        emitted_count=_grep_emitted_count(records, args),
        duration_ms=(time.monotonic() - started_at) * 1000.0,
        outcome="match" if exit_code == 0 else "no_match",
    )
    return exit_code
