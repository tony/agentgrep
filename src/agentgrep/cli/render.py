"""Subcommand dispatch for the agentgrep CLI.

Routes parsed ``grep`` / ``find`` / ``search`` / ``ui`` arguments to the
engine and the chosen output mode, picking the streaming or eager path per
subcommand and handing records to the right formatter. The JSON payload
serializers live in :mod:`agentgrep.cli.serializers` and the text formatters
in :mod:`agentgrep.cli.renderers`.
"""

from __future__ import annotations

import json
import pathlib
import sys
import typing as t

from agentgrep import identity, run_ui
from agentgrep._engine import iter_find_events, iter_search_events
from agentgrep._engine.orchestration import run_search_query
from agentgrep._text import AnsiColors, format_display_path
from agentgrep.cli.parser import FindArgs, GrepArgs, SearchArgs, SimilarArgs, UIArgs
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
    maybe_build_pydantic,
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
from agentgrep.records import AGENT_CHOICES, FindRecord, SearchQuery, SearchRecord

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
    "maybe_build_pydantic",
    "print_find_results",
    "print_grep_results",
    "run_find_command",
    "run_grep_command",
    "run_search_command",
    "run_similar_command",
    "run_ui_command",
    "serialize_find_record",
    "serialize_grep_record",
    "serialize_search_record",
    "serialize_source_handle",
    "stream_find_results",
    "stream_grep_results",
]


def print_find_results(records: list[FindRecord], args: FindArgs) -> None:
    """Emit find results in the requested format.

    ``--list-details`` switches to a one-line-per-record long format with
    agent / kind / store / adapter_id / path columns. ``--print0``
    separates records with NUL instead of newline (for ``xargs -0``) and,
    like ``--absolute-path``, emits real filesystem paths; other modes
    collapse the home directory to ``~``. ``--json`` / ``--ndjson`` are
    unaffected by these flags.
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
    serialize_find: t.Callable[[FindRecord], dict[str, object]] | None = None
    if args.output_mode == "ndjson":
        _, serialize_find, _ = maybe_build_pydantic()
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
        if args.output_mode == "ndjson" and serialize_find is not None:
            print(json.dumps(serialize_find(event.record), ensure_ascii=False))
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
        run_ui(
            pathlib.Path.home(),
            query,
            control=SearchControl(),
            initial_search_text=args.raw_query or None,
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
    initial_terms = tuple(args.initial_query.split()) if args.initial_query else ()
    query = SearchQuery(
        terms=initial_terms,
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=AGENT_CHOICES,
        limit=None,
    )
    run_ui(
        pathlib.Path.home(),
        query,
        control=SearchControl(),
        layout=args.layout,
        workflow=args.workflow,
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
    if not args.terms and args.compiled is None and args.output_mode != "ui":
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
    )
    if args.output_mode == "ui":
        run_ui(
            pathlib.Path.home(),
            query,
            control=SearchControl(),
            initial_search_text=args.raw_query or None,
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
    query_text = " ".join(args.terms)
    answered_early = control.answer_now_requested()
    if args.no_rank or answered_early or not query_text:
        scored: list[tuple[SearchRecord, float]] = [(r, 0.0) for r in records]
    else:
        from agentgrep.ranking import rank_search_records

        scored = rank_search_records(records, query_text, threshold=args.threshold)
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
            content_id = identity.record_content_id(record)
            provenance_parts: list[str] = [
                identity.short_id(content_id),
                record.agent,
                record.kind,
            ]
            if record.timestamp:
                provenance_parts.append(format_relative_time(record.timestamp))
            provenance_parts.append(
                colors.path(format_display_path(record.path)),
            )
            lines.append(colors.dim(f"  {' · '.join(provenance_parts)}"))
            print("\n".join(lines))
            print()


def _run_search_eager(args: SearchArgs, query: SearchQuery) -> int:
    """Eager search for JSON/NDJSON output with ranking but no pairwise dedup."""
    control = SearchControl()
    records = run_search_query(
        pathlib.Path.home(),
        query,
        progress=noop_search_progress(),
        control=control,
    )
    query_text = " ".join(args.terms)
    if args.no_rank or not query_text:
        scored: list[tuple[SearchRecord, float]] = [(r, 0.0) for r in records]
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
    for event in iter_search_events(
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
        run_ui(
            pathlib.Path.home(),
            query,
            control=SearchControl(),
            initial_search_text=args.raw_query or None,
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
    return print_grep_results(records, args)


def run_similar_command(args: SimilarArgs) -> int:
    """Execute ``agentgrep similar``.

    Collects the scope-narrowed corpus and ranks it against the seed text with
    the shared :func:`agentgrep.similar.run_find_similar` helper, then renders
    neighbors best-first with the score inline.
    """
    from agentgrep.similar import run_find_similar

    matches = run_find_similar(
        pathlib.Path.home(),
        seed_text=args.seed_text,
        agents=args.agents,
        scope=args.scope,
        top_k=args.top_k,
        threshold=args.threshold,
        exclude_exact=args.exclude_exact,
    )
    if args.output_mode in ("json", "ndjson"):
        serialize_search, _, serialize_envelope = maybe_build_pydantic()
        rows: list[dict[str, object]] = []
        for record, score in matches:
            row = dict(serialize_search(record))
            row["score"] = score
            rows.append(row)
        if args.output_mode == "ndjson":
            for row in rows:
                print(json.dumps(row, ensure_ascii=False))
        else:
            envelope = serialize_envelope(
                "similar",
                {"top_k": args.top_k, "threshold": args.threshold},
                rows,
            )
            print(json.dumps(envelope, ensure_ascii=False, indent=2))
        return 0 if matches else 1
    colors = AnsiColors.for_stream(args.color_mode, sys.stdout)
    if not matches:
        print(colors.dim("no similar records"))
        return 1
    for record, score in matches:
        short = identity.short_id(identity.record_content_id(record))
        snippet = record.text.strip().splitlines()[0][:100] if record.text.strip() else ""
        print(f"{score:.2f}  {colors.heading(short)}  {colors.dim(record.agent)}  {snippet}")
    return 0
