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
import importlib
import inspect
import itertools
import json
import os
import pathlib
import shutil
import sys
import threading
import time
import typing as t

from agentgrep import run_ui
from agentgrep._engine import SearchRuntime, iter_find_events, iter_search_events
from agentgrep._engine.orchestration import run_search_query
from agentgrep._text import (
    AnsiColors,
    _hard_truncate_ansi,
    _visible_width,
    format_display_path,
)
from agentgrep.cli.parser import DbArgs, FindArgs, GrepArgs, SearchArgs, UIArgs
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
from agentgrep.records import AGENT_CHOICES, FindRecord, SearchQuery, SearchRecord, SearchScope

if t.TYPE_CHECKING:
    from agentgrep._types import SearchColors
    from agentgrep.db import DbRuntime, SyncResult
    from agentgrep.records import CacheMode, ColorMode, OutputMode, SourceHandle

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
    "run_db_command",
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

    return SearchQuery(
        terms=terms,
        scope=args.scope,
        any_term=False,
        regex=regex,
        case_sensitive=case_sensitive,
        agents=args.agents,
        limit=args.limit,
        dedupe=not args.no_dedupe,
        compiled=args.compiled,
        match_surface="text",
    )


def print_grep_results(records: list[SearchRecord], args: GrepArgs) -> int:
    """Emit grep results and return the rg-style exit code."""
    if args.invert_match:
        if args.count_only:
            print("0" if records else "1")
            return 1 if records else 0
        print(
            "error: --invert-match/-v is supported with -c only; "
            "engine-level line inversion is tracked at "
            "https://github.com/tony/agentgrep/issues/8",
            file=sys.stderr,
        )
        return 2

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
        return 0 if records else 1
    if args.files_with_matches:
        seen: set[str] = set()
        for record in records:
            path = format_display_path(record.path)
            if path not in seen:
                seen.add(path)
                print(path)
        return 0 if records else 1

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
    # Lazy import keeps ``agentgrep.events`` off the eager ``import
    # agentgrep`` path (pinned by tests/test_import_time.py).
    from agentgrep import events

    query = build_grep_query(args)
    control = SearchControl()
    is_tty = sys.stdout.isatty()
    match_count = 0
    pretty = args.style == "pretty"
    summary = GrepSummary() if pretty else None
    for event in _iter_search_events_for_cli(
        pathlib.Path.home(),
        query,
        control=control,
        cache_mode=args.cache_mode,
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
        footer = summary.format(colors=AnsiColors.for_stream(args.color_mode, sys.stderr))
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
    records = _run_search_query_for_cli(
        pathlib.Path.home(),
        query,
        progress=progress,
        control=control,
        cache_mode=args.cache_mode,
    )
    return print_grep_results(records, args)


def _facade() -> t.Any:
    """Return the ``agentgrep`` package object for late-bound engine access.

    The DB-cache search paths and the ``db sync`` command resolve
    ``run_search_query``, ``iter_search_events``, ``select_backends``, and
    ``discover_sources_for_search`` through the package object at call time
    so ``monkeypatch.setattr(agentgrep, ...)`` on those names stays visible
    to them (the cache CLI tests patch these on the facade). It is loaded
    with :func:`importlib.import_module` rather than a bare ``import
    agentgrep`` to preserve the ADR 0010 module-boundary contract.
    """
    return importlib.import_module("agentgrep")


def _json_ready(value: object) -> object:
    """Convert dataclasses and paths into JSON-serializable values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_ready(dataclasses.asdict(t.cast("t.Any", value)))
    if isinstance(value, pathlib.Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


_HUMAN_SAMPLE_LIMIT = 10


def _print_json_or_text(
    payload: object,
    *,
    output_mode: OutputMode,
    color_mode: ColorMode = "auto",
) -> None:
    """Print a small command payload as JSON or human-readable text."""
    if output_mode == "json":
        print(json.dumps(_json_ready(payload), ensure_ascii=False, indent=2))
        return
    if output_mode == "ndjson":
        rows = payload if isinstance(payload, (list, tuple)) else (payload,)
        for row in rows:
            print(json.dumps(_json_ready(row), ensure_ascii=False))
        return
    colors = AnsiColors.for_stream(color_mode, sys.stdout)
    print(_format_structured_text(payload, colors=colors))


def _format_structured_text(payload: object, *, colors: AnsiColors) -> str:
    """Return a terminal-readable summary for small structured payloads."""
    if _has_attributes(payload, ("sources_synced", "records_indexed", "records_removed")):
        return _format_db_sync_result_text(payload, colors=colors)
    if _has_attributes(payload, ("synced_ok", "sync_errors", "answerable")):
        return _format_db_explain_text(payload, colors=colors)
    if _has_attributes(payload, ("db_path", "schema_version", "sources", "records")):
        return _format_db_status_text(payload, colors=colors)
    return _format_generic_structured_text(payload, colors=colors)


def _has_attributes(payload: object, names: cabc.Sequence[str]) -> bool:
    """Return whether ``payload`` has every named attribute."""
    return all(hasattr(payload, name) for name in names)


def _format_db_status_text(payload: object, *, colors: AnsiColors) -> str:
    """Return human-readable DB status text."""
    db_path = _attribute_or_mapping_value(payload, "db_path", "")
    schema_version = _attribute_or_mapping_value(payload, "schema_version", "")
    sources = _as_int_value(_attribute_or_mapping_value(payload, "sources", 0))
    records = _as_int_value(_attribute_or_mapping_value(payload, "records", 0))
    lines = [
        colors.heading("DB status"),
        f"{colors.muted('Path')} | {colors.path(str(db_path))}",
        f"{colors.muted('Schema')} | {colors.warning(str(schema_version))}",
        " | ".join(
            (
                colors.warning(format_db_source_count(sources)),
                colors.warning(_format_count(records, "record")),
            ),
        ),
    ]
    return "\n".join(lines)


def _format_db_explain_text(payload: object, *, colors: AnsiColors) -> str:
    """Return human-readable cache diagnostics."""
    db_path = _attribute_or_mapping_value(payload, "db_path", "")
    schema_version = _attribute_or_mapping_value(payload, "schema_version", "")
    sources = _as_int_value(_attribute_or_mapping_value(payload, "sources", 0))
    records = _as_int_value(_attribute_or_mapping_value(payload, "records", 0))
    synced_ok = _as_int_value(_attribute_or_mapping_value(payload, "synced_ok", 0))
    sync_errors = _as_int_value(_attribute_or_mapping_value(payload, "sync_errors", 0))
    last_synced = _attribute_or_mapping_value(payload, "last_synced_at", None)
    answerable = _attribute_or_mapping_value(payload, "answerable", "")
    lines = [
        colors.heading("DB explain"),
        f"{colors.muted('Path')} | {colors.path(str(db_path))}",
        f"{colors.muted('Schema')} | {colors.warning(str(schema_version))}",
        " | ".join(
            (
                colors.warning(format_db_source_count(sources)),
                colors.warning(_format_count(records, "record")),
            ),
        ),
        " | ".join(
            (
                f"{colors.muted('Sync')} | {colors.success(f'{synced_ok} ok')}",
                colors.error(f"{sync_errors} errors") if sync_errors else colors.muted("0 errors"),
            ),
        ),
    ]
    if last_synced is not None:
        lines.append(f"{colors.muted('Last synced')} | {last_synced}")
    lines.append(f"{colors.muted('Answerable')} | {answerable}")
    lines.append(_format_db_explain_coverage_line(payload, colors=colors))
    return "\n".join(lines)


def _format_db_explain_coverage_line(payload: object, *, colors: AnsiColors) -> str:
    """Return the coverage line for the db explain text summary.

    ``None`` coverage means no completed sync has recorded coverage —
    rendered distinctly from a recorded-but-empty map so cache misses
    in auto mode are explainable.
    """
    coverage = _attribute_or_mapping_value(payload, "coverage", None)
    if coverage is None:
        return f"{colors.muted('Coverage')} | {colors.warning('not recorded')}"
    if not isinstance(coverage, cabc.Mapping) or not coverage:
        return f"{colors.muted('Coverage')} | {colors.warning('none')}"
    mapping = t.cast("cabc.Mapping[str, object]", coverage)
    parts = []
    for agent in sorted(str(key) for key in mapping):
        scopes = mapping.get(agent)
        scope_list = (
            ",".join(str(scope) for scope in t.cast("cabc.Iterable[object]", scopes))
            if isinstance(scopes, list | tuple)
            else str(scopes)
        )
        parts.append(f"{agent}={scope_list}")
    return f"{colors.muted('Coverage')} | {colors.success(' '.join(parts))}"


def _format_db_sync_result_text(payload: object, *, colors: AnsiColors) -> str:
    """Return human-readable DB sync result text."""
    sources_synced = _as_int_value(_attribute_or_mapping_value(payload, "sources_synced", 0))
    records_indexed = _as_int_value(_attribute_or_mapping_value(payload, "records_indexed", 0))
    records_removed = _as_int_value(_attribute_or_mapping_value(payload, "records_removed", 0))
    lines = [
        colors.heading("DB sync"),
        " | ".join(
            (
                colors.warning(format_db_source_count(sources_synced)),
                colors.warning(format_db_indexed_count(records_indexed)),
                colors.warning(format_db_removed_count(records_removed)),
            ),
        ),
    ]
    skipped = _as_int_value(_attribute_or_mapping_value(payload, "sources_skipped", 0))
    if skipped:
        lines.append(colors.warning(format_db_skipped_count(skipped)))
    pruned = _as_int_value(_attribute_or_mapping_value(payload, "sources_pruned", 0))
    if pruned:
        lines.append(colors.warning(format_db_pruned_count(pruned)))
    return "\n".join(lines)


def _format_generic_structured_text(payload: object, *, colors: AnsiColors) -> str:
    """Return a conservative human-readable fallback for structured payloads."""
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        payload = dataclasses.asdict(t.cast("t.Any", payload))
    if isinstance(payload, cabc.Mapping):
        lines = [colors.heading("Summary")]
        for key, value in t.cast("cabc.Mapping[object, object]", payload).items():
            lines.append(f"{colors.muted(str(key))} | {_format_scalar(value)}")
        return "\n".join(lines)
    if isinstance(payload, cabc.Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        lines = [colors.heading("Items")]
        for item in payload[:_HUMAN_SAMPLE_LIMIT]:
            lines.append(f"  {_format_scalar(item)}")
        remaining = len(payload) - _HUMAN_SAMPLE_LIMIT
        if remaining > 0:
            lines.append(colors.dim(f"  ... {remaining} more items"))
        return "\n".join(lines)
    return str(payload)


def _attribute_or_mapping_value(payload: object, name: str, default: object) -> object:
    """Return an attribute or mapping value from a row-like object."""
    if isinstance(payload, cabc.Mapping):
        return t.cast("cabc.Mapping[str, object]", payload).get(name, default)
    return getattr(payload, name, default)


def _as_int_value(value: object) -> int:
    """Return one object as an integer count when possible."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _format_count(count: int, singular: str, plural: str | None = None) -> str:
    """Return a human-readable count with pluralization."""
    label = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {label}"


def _format_scalar(value: object) -> str:
    """Return one scalar-ish value without Python dataclass reprs."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(t.cast("t.Any", value))
    if isinstance(value, pathlib.Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)
    if isinstance(value, cabc.Mapping | cabc.Sequence):
        return json.dumps(_json_ready(value), ensure_ascii=False)
    return str(value)


def _db_runtime_for_cli(
    cache_mode: CacheMode,
) -> SearchRuntime | None:
    """Return a search runtime with read-only DB access.

    Searches only read the cache, so the open must not run schema
    migration or create a missing cache file as a side effect.
    """
    if cache_mode == "off":
        return None
    import sqlite3

    from agentgrep.db import DbRuntime, default_db_path

    db_path = default_db_path()
    if not db_path.exists():
        if cache_mode == "require":
            print(
                f"agentgrep: --cache require needs a synced DB; none found at {db_path}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return None
    db: DbRuntime | None = None
    try:
        db = DbRuntime.open_readonly(db_path)
        # Read-only connects are lazy: probe so a foreign or corrupt
        # file surfaces here instead of mid-search inside the engine.
        _ = db.store.connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'",
        ).fetchone()
    except sqlite3.DatabaseError:
        if db is not None:
            db.close()
        if cache_mode == "require":
            print(
                f"agentgrep: not an agentgrep database: {db_path}",
                file=sys.stderr,
            )
            raise SystemExit(2) from None
        return None
    return SearchRuntime(
        db=db,
        cache_mode=cache_mode,
    )


def _exit_for_required_cache_miss(error: BaseException) -> t.NoReturn:
    """Raise a clean CLI error for cache-required unsupported queries."""
    print(
        f"agentgrep: --cache require cannot satisfy this query from the DB: {error}",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _accepts_runtime_parameter(function: cabc.Callable[..., object]) -> bool:
    """Return whether a possibly monkeypatched runner accepts ``runtime``."""
    try:
        signature = inspect.signature(function)
    except TypeError, ValueError:
        return True
    return "runtime" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _run_search_query_for_cli(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    progress: SearchProgress,
    control: SearchControl,
    cache_mode: CacheMode,
) -> list[SearchRecord]:
    """Call ``run_search_query`` without changing monkeypatch-compatible arity."""
    runtime = _db_runtime_for_cli(cache_mode)
    if runtime is None:
        return run_search_query(
            home,
            query,
            progress=progress,
            control=control,
        )
    from agentgrep.db import DbQueryUnsupported

    runner = _facade().run_search_query
    try:
        if not _accepts_runtime_parameter(runner):
            return runner(
                home,
                query,
                progress=progress,
                control=control,
            )
        return runner(
            home,
            query,
            progress=progress,
            control=control,
            runtime=runtime,
        )
    except DbQueryUnsupported as exc:
        _exit_for_required_cache_miss(exc)
    finally:
        if runtime.db is not None:
            runtime.db.close()


def _iter_search_events_for_cli(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
    cache_mode: CacheMode,
) -> cabc.Iterator[object]:
    """Call ``iter_search_events`` without changing monkeypatch-compatible arity."""
    runtime = _db_runtime_for_cli(cache_mode)
    if runtime is None:
        yield from iter_search_events(
            home,
            query,
            control=control,
        )
        return
    from agentgrep.db import DbQueryUnsupported

    runner = _facade().iter_search_events
    try:
        if not _accepts_runtime_parameter(runner):
            yield from runner(
                home,
                query,
                control=control,
            )
            return
        yield from runner(
            home,
            query,
            control=control,
            runtime=runtime,
        )
    except DbQueryUnsupported as exc:
        _exit_for_required_cache_miss(exc)
    finally:
        # Generator finally: runs on exhaustion, close(), or GC, so an
        # early-breaking consumer still releases the connection.
        if runtime.db is not None:
            runtime.db.close()


@dataclasses.dataclass(frozen=True)
class DbSyncProgressSnapshot:
    """Immutable view of DB sync progress state for one render pass."""

    phase: str
    current: int | None
    total: int | None
    detail: str | None
    sources_synced: int
    records_indexed: int
    records_removed: int
    sources_skipped: int
    elapsed: float


class ConsoleDbSyncProgress:
    """Human progress reporter for DB sync operations."""

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
            tty if tty is not None else bool(getattr(self._stream, "isatty", lambda: False)())
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
        self._phase = "discovering"
        self._detail: str | None = None
        self._current: int | None = None
        self._total: int | None = None
        self._sources_synced = 0
        self._records_indexed = 0
        self._records_removed = 0
        self._sources_skipped = 0
        self._finished = False

    def start_discovery(self) -> None:
        """Begin progress reporting before source discovery."""
        if not self._enabled:
            return
        started_now = self._ensure_started("discovering", detail="sources")
        if started_now and not self._tty:
            self._emit_line(self._start_line())

    def start(self, total_sources: int) -> None:
        """Begin source sync progress after discovery."""
        if not self._enabled:
            return
        started_now = self._ensure_started(
            "syncing",
            current=0,
            total=total_sources,
            detail=f"{total_sources} sources",
        )
        if started_now:
            if self._tty:
                self._ensure_tty_thread()
            else:
                self._emit_line(self._start_line())

    def source_started(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        result: SyncResult,
    ) -> None:
        """Report source transaction start."""
        if not self._enabled:
            return
        self._update_result(result)
        self.set_status(
            "syncing",
            current=index,
            total=total,
            detail=source.path.name,
        )

    def source_finished(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        records_indexed: int,
        records_removed: int,
        result: SyncResult,
    ) -> None:
        """Report source transaction completion."""
        if not self._enabled:
            return
        self._update_result(result)
        self.set_status(
            "syncing",
            current=index,
            total=total,
            detail=(f"{records_indexed} indexed, {records_removed} removed in {source.path.name}"),
        )

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

    def finish(self, result: SyncResult) -> None:
        """Finish progress reporting after a complete sync."""
        if not self._enabled:
            return
        self._update_result(result)
        with self._lock:
            self._phase = "complete"
            self._finished = True
        if self._tty:
            self._stop_tty_thread()
            self._clear_tty_line()
            return
        self._emit_line(self._finish_line(result))

    def exiting_early(self, result: SyncResult) -> None:
        """Finish progress reporting after cooperative early exit."""
        if not self._enabled:
            return
        self._update_result(result)
        with self._lock:
            self._phase = "exiting early"
            self._finished = True
        line = self._exiting_early_line(result)
        if self._tty:
            self._stop_tty_thread()
            self._write_tty_line(line)
            return
        self._emit_line(line)

    def interrupt(self) -> None:
        """Stop progress rendering while preserving the current status."""
        if not self._enabled:
            return
        if self._tty:
            self._stop_tty_thread()
            self._write_tty_summary_line()
            return
        self._emit_line(self._summary())

    def close(self) -> None:
        """Stop any active progress renderer."""
        if not self._enabled:
            return
        if self._tty:
            self._stop_tty_thread()
            if not self._finished:
                self._clear_tty_line()

    def _ensure_started(
        self,
        phase: str,
        *,
        current: int | None = None,
        total: int | None = None,
        detail: str | None = None,
    ) -> bool:
        now = time.monotonic()
        with self._lock:
            already_started = self._started_at is not None
            if not already_started:
                self._started_at = now
                self._last_heartbeat_at = now
                self._finished = False
            self._phase = phase
            self._current = current
            self._total = total
            self._detail = detail
        if not already_started and self._tty:
            self._ensure_tty_thread()
        return not already_started

    def _update_result(self, result: SyncResult) -> None:
        with self._lock:
            self._sources_synced = result.sources_synced
            self._records_indexed = result.records_indexed
            self._records_removed = result.records_removed
            self._sources_skipped = result.sources_skipped

    def _ensure_tty_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._tty_loop,
            daemon=True,
            name="agentgrep-db-sync-progress",
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
        if last is None:
            return
        now = time.monotonic()
        if now - last < self._heartbeat_interval:
            return
        elapsed = self._elapsed_seconds()
        self._emit_line(self._heartbeat_line(elapsed))
        with self._lock:
            self._last_heartbeat_at = now

    def _emit_line(self, line: str) -> None:
        try:
            self._stream.write(line + "\n")
            self._stream.flush()
        except OSError, ValueError:
            pass

    def _summary(self, *, max_width: int | None = None) -> str:
        return format_db_sync_progress_line(
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

    def _snapshot(self) -> DbSyncProgressSnapshot:
        elapsed = self._elapsed_seconds()
        with self._lock:
            return DbSyncProgressSnapshot(
                phase=self._phase,
                current=self._current,
                total=self._total,
                detail=self._detail,
                sources_synced=self._sources_synced,
                records_indexed=self._records_indexed,
                records_removed=self._records_removed,
                sources_skipped=self._sources_skipped,
                elapsed=elapsed,
            )

    def _start_line(self) -> str:
        return f"{self._colors.heading('DB sync')} {self._colors.muted('discovering sources')}"

    def _heartbeat_line(self, elapsed: float) -> str:
        prefix = f"{self._colors.muted('...')} {self._colors.heading('still syncing')}"
        elapsed_text = self._colors.muted(f"{elapsed:.0f}s elapsed")
        return f"{prefix}: {self._summary()} ({elapsed_text})"

    def _finish_line(self, result: SyncResult) -> str:
        return (
            f"{self._colors.success('Sync complete:')} "
            f"{self._colors.warning(format_db_source_count(result.sources_synced))}, "
            f"{self._colors.warning(format_db_indexed_count(result.records_indexed))}, "
            f"{self._colors.warning(format_db_removed_count(result.records_removed))}"
            f"{_format_optional_db_sync_counts(result, colors=self._colors)} "
            f"({self._colors.muted(f'{self._elapsed_seconds():.1f}s elapsed')})"
        )

    def _exiting_early_line(self, result: SyncResult) -> str:
        parts = [
            f"{self._colors.success('Exiting early:')} "
            f"{self._colors.warning(format_db_source_count(result.sources_synced))}, "
            f"{self._colors.warning(format_db_indexed_count(result.records_indexed))}, "
            f"{self._colors.warning(format_db_removed_count(result.records_removed))}"
            f"{_format_optional_db_sync_counts(result, colors=self._colors)}",
        ]
        if self._answer_now_hint:
            parts.append(self._colors.white("[Press enter, exit early]"))
        return " | ".join(parts)

    def _elapsed_seconds(self) -> float:
        with self._lock:
            started = self._started_at
        if started is None:
            return 0.0
        return time.monotonic() - started


def format_db_source_count(count: int) -> str:
    """Return a human-readable DB source count.

    Examples
    --------
    >>> format_db_source_count(1)
    '1 source'
    >>> format_db_source_count(3)
    '3 sources'
    """
    suffix = "source" if count == 1 else "sources"
    return f"{count} {suffix}"


def format_db_indexed_count(count: int) -> str:
    """Return a human-readable indexed-record count.

    Examples
    --------
    >>> format_db_indexed_count(1)
    '1 record indexed'
    >>> format_db_indexed_count(3)
    '3 records indexed'
    """
    suffix = "record indexed" if count == 1 else "records indexed"
    return f"{count} {suffix}"


def format_db_removed_count(count: int) -> str:
    """Return a human-readable removed-record count.

    Examples
    --------
    >>> format_db_removed_count(1)
    '1 record removed'
    >>> format_db_removed_count(3)
    '3 records removed'
    """
    suffix = "record removed" if count == 1 else "records removed"
    return f"{count} {suffix}"


def format_db_skipped_count(count: int) -> str:
    """Return a human-readable skipped-source count.

    Examples
    --------
    >>> format_db_skipped_count(1)
    '1 source skipped'
    >>> format_db_skipped_count(3)
    '3 sources skipped'
    """
    suffix = "source skipped" if count == 1 else "sources skipped"
    return f"{count} {suffix}"


def format_db_pruned_count(count: int) -> str:
    """Return a human-readable pruned-source count.

    Examples
    --------
    >>> format_db_pruned_count(1)
    '1 vanished source pruned'
    >>> format_db_pruned_count(3)
    '3 vanished sources pruned'
    """
    suffix = "vanished source pruned" if count == 1 else "vanished sources pruned"
    return f"{count} {suffix}"


def _format_optional_db_sync_counts(result: SyncResult, *, colors: SearchColors) -> str:
    """Return optional DB sync counters prefixed for inline summaries."""
    parts: list[str] = []
    if result.sources_skipped:
        parts.append(colors.warning(format_db_skipped_count(result.sources_skipped)))
    return ", " + ", ".join(parts) if parts else ""


def format_db_sync_progress_line(
    snapshot: DbSyncProgressSnapshot,
    *,
    colors: SearchColors,
    answer_now_hint: bool = False,
    max_width: int | None = None,
) -> str:
    """Format the single-line DB sync progress summary."""
    variants = (
        (True, answer_now_hint),
        (False, answer_now_hint),
        (False, False),
    )
    for include_detail, include_hint in variants:
        line = _format_db_sync_progress_line(
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


def _format_db_sync_progress_line(
    snapshot: DbSyncProgressSnapshot,
    *,
    colors: SearchColors,
    answer_now_hint: bool,
    include_detail: bool,
) -> str:
    """Build one DB sync progress-line variant."""
    label_part = colors.heading("DB sync")
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
            colors.warning(format_db_source_count(snapshot.sources_synced)),
            colors.warning(format_db_indexed_count(snapshot.records_indexed)),
            colors.warning(format_db_removed_count(snapshot.records_removed)),
        ],
    )
    if snapshot.sources_skipped:
        parts.append(colors.warning(format_db_skipped_count(snapshot.sources_skipped)))
    parts.append(colors.muted(f"{snapshot.elapsed:.1f}s"))
    if answer_now_hint:
        parts.append(colors.white("[Press enter, exit early]"))
    return " | ".join(parts)


def _open_db_runtime(db_path: str | None) -> DbRuntime:
    """Open the DB runtime lazily."""
    from agentgrep.db import DbRuntime

    return DbRuntime.open(pathlib.Path(db_path) if db_path is not None else None)


def run_db_command(args: DbArgs) -> int:
    """Execute ``agentgrep db`` subcommands."""
    if args.action in {"status", "explain"}:
        return _run_db_status_command(args)
    runtime = _open_db_runtime(args.db_path)
    try:
        return _run_db_command_with_runtime(args, runtime)
    finally:
        runtime.close()


def _run_db_status_command(args: DbArgs) -> int:
    """Report db status or diagnostics without writing to the cache."""
    import sqlite3

    from agentgrep.db import (
        ANSWERABLE_QUERY_FORMS,
        SCHEMA_VERSION,
        DbExplain,
        DbRuntime,
        DbStatus,
        default_db_path,
    )

    path = default_db_path() if args.db_path is None else pathlib.Path(args.db_path).expanduser()
    payload: object
    if not path.exists():
        if args.action == "explain":
            payload = DbExplain(
                db_path=path,
                schema_version=SCHEMA_VERSION,
                sources=0,
                records=0,
                synced_ok=0,
                sync_errors=0,
                last_synced_at=None,
                answerable=ANSWERABLE_QUERY_FORMS,
                coverage=None,
            )
        else:
            payload = DbStatus(
                db_path=path,
                schema_version=SCHEMA_VERSION,
                sources=0,
                records=0,
            )
        _print_json_or_text(payload, output_mode=args.output_mode, color_mode=args.color_mode)
        return 0
    try:
        with DbRuntime.open_readonly(path) as runtime:
            payload = runtime.explain() if args.action == "explain" else runtime.status()
    except sqlite3.DatabaseError:
        print(f"agentgrep: not an agentgrep database: {path}", file=sys.stderr)
        return 1
    _print_json_or_text(payload, output_mode=args.output_mode, color_mode=args.color_mode)
    return 0


def _run_db_command_with_runtime(args: DbArgs, runtime: DbRuntime) -> int:
    """Execute one db sync action against an already-open runtime."""
    from agentgrep.db import SyncCoverage

    query = SearchQuery(
        terms=(),
        scope=args.scope,
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=args.agents,
        limit=None,
    )
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
    progress = (
        ConsoleDbSyncProgress(
            enabled=True,
            color_mode=args.color_mode,
            answer_now_hint=answer_now_enabled,
        )
        if progress_enabled
        else None
    )
    listener = AnswerNowInputListener(control) if answer_now_enabled else None
    if listener is not None:
        listener.start()
    if progress is not None:
        progress.start_discovery()
    try:
        backends = _facade().select_backends()
        sources = _facade().discover_sources_for_search(
            pathlib.Path.home(),
            query,
            backends,
            version_detail="none",
        )
        if args.limit_sources is not None:
            sources = sources[: args.limit_sources]
        # A capped source list slices across agents, so it can never
        # claim agent/scope coverage; interrupted and early-exited
        # loops record nothing inside sync_records itself.
        coverage = SyncCoverage(
            agents=args.agents,
            scope=args.scope,
            complete=args.limit_sources is None,
        )
        # Only an uncapped, full-scope, all-agents sync may prune
        # ledger rows for vanished sources - a narrowed run does not
        # observe the full catalog and must not delete what it
        # skipped.
        prune_missing = (
            args.scope == "all"
            and args.limit_sources is None
            and set(args.agents) == set(AGENT_CHOICES)
        )
        result = runtime.sync_sources(
            sources,
            control=control,
            progress=progress,
            force=args.force,
            coverage=coverage,
            prune_missing=prune_missing,
        )
    except KeyboardInterrupt:
        if progress is not None:
            progress.interrupt()
        raise
    finally:
        if listener is not None:
            listener.stop()
        if progress is not None:
            progress.close()
    _print_json_or_text(result, output_mode=args.output_mode, color_mode=args.color_mode)
    return 0
