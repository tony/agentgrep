"""Subcommand dispatch for the agentgrep CLI.

Routes parsed ``grep`` / ``find`` / ``search`` / ``ui`` arguments to the
engine and the chosen output mode, picking the streaming or eager path per
subcommand and handing records to the right formatter. The JSON payload
serializers live in :mod:`agentgrep.cli.serializers` and the text formatters
in :mod:`agentgrep.cli.renderers`.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
import typing as t

from agentgrep import run_ui
from agentgrep._engine import iter_find_events, iter_search_events, run_search_result
from agentgrep._query_gate import UnregisteredFieldToken
from agentgrep._text import AnsiColors, format_display_path
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
    serialize_query_diagnostics,
    serialize_run_summary,
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
from agentgrep.records import (
    AGENT_CHOICES,
    ColorMode,
    FindRecord,
    OutputMode,
    SearchEffort,
    SearchQuery,
    SearchRecord,
    SearchScope,
    SearchScopeProvenance,
)
from agentgrep.results import RunSummary

if t.TYPE_CHECKING:
    from agentgrep.db import DbRuntime, SyncResult
    from agentgrep.records import SourceHandle

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


def _print_query_diagnostics(diagnostics: tuple[UnregisteredFieldToken, ...]) -> None:
    """Warn on stderr for each non-fatal query diagnostic.

    Called once per invocation ahead of any other output, for every
    non-``--ui`` output mode. ``--ui`` skips this: Textual's alt-screen
    takeover would immediately erase a stderr line printed here, and the
    interactive search box already surfaces the same warning through
    :func:`agentgrep.query.build_query_from_input` once a query is
    (re)typed there.
    """
    for diagnostic in diagnostics:
        print(f"warning: {diagnostic.message}", file=sys.stderr)


def _json_ready(value: object) -> object:
    """Convert dataclasses and paths into JSON-serializable values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_ready(dataclasses.asdict(value))
    if isinstance(value, pathlib.Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _print_json_or_text(payload: object, *, output_mode: OutputMode) -> None:
    """Print a small command payload in its requested output mode."""
    if output_mode == "json":
        print(json.dumps(_json_ready(payload), ensure_ascii=False, indent=2))
        return
    if output_mode == "ndjson":
        rows = payload if isinstance(payload, (list, tuple)) else (payload,)
        for row in rows:
            print(json.dumps(_json_ready(row), ensure_ascii=False))
        return
    print(_json_ready(payload))


class ConsoleDbSyncProgress:
    """Small DB sync progress adapter retained until the full renderer lands."""

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
        self._tty = tty
        self._color_mode = color_mode
        self._refresh_interval = refresh_interval
        self._heartbeat_interval = heartbeat_interval
        self._answer_now_hint = answer_now_hint

    def start(self, total_sources: int) -> None:
        """Report the start of a DB sync."""
        self._write(f"DB sync: {total_sources} sources")

    def source_started(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        result: SyncResult,
    ) -> None:
        """Accept a source-start notification."""
        _ = (index, total, source, result)

    def source_finished(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        records_indexed: int,
        records_removed: int,
        result: SyncResult,
    ) -> None:
        """Accept a source-finish notification."""
        _ = (index, total, source, records_indexed, records_removed, result)

    def finish(self, result: SyncResult) -> None:
        """Report a completed DB sync."""
        self._write(f"Sync complete: {result.records_indexed} indexed")

    def exiting_early(self, result: SyncResult) -> None:
        """Report a cooperatively shortened DB sync."""
        hint = " [Press enter, exit early]" if self._answer_now_hint else ""
        self._write(f"Exiting early: {result.records_indexed} indexed{hint}")

    def _write(self, text: str) -> None:
        """Write one progress line when reporting is enabled."""
        if not self._enabled:
            return
        if self._color_mode == "always":
            text = f"\x1b[33m{text}\x1b[0m"
        _ = self._stream.write(f"{text}\n")
        self._stream.flush()


def _open_db_runtime(db_path: str | None) -> DbRuntime:
    """Open the DB runtime lazily."""
    from agentgrep.db import DbRuntime

    return DbRuntime.open(pathlib.Path(db_path) if db_path is not None else None)


def run_db_command(args: DbArgs) -> int:
    """Execute a DB command through the full implementation added later."""
    _ = args
    return 0


def _launch_ui(
    query: SearchQuery,
    *,
    initial_search_text: str | None = None,
    base_scope: SearchScope | None = None,
    base_effort: SearchEffort | None = None,
    base_scope_provenance: SearchScopeProvenance | None = None,
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
            base_effort=base_effort,
            base_scope_provenance=base_scope_provenance,
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
        payload = {
            **build_envelope(
                "find",
                query_data,
                [dict(serialize_find_record(record)) for record in records],
            ),
            "warnings": serialize_query_diagnostics(args.diagnostics),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.output_mode == "ndjson":
        # find's --ndjson stream is a flat sequence of bare find records with
        # no wrapping event type (unlike grep/search's ndjson, which already
        # has a terminal "summary" line). Adding a "warnings" line here would
        # be the first non-record shape on this stream — a breaking format
        # change for existing consumers, so it's out of scope here. --json
        # above and the stderr warning (run_find_command) already carry the
        # diagnostic for find.
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
    if args.output_mode != "ui":
        _print_query_diagnostics(args.diagnostics)
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
            base_effort="exhaustive",
            base_scope_provenance="inferred",
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
        base_effort="prompt",
        base_scope_provenance="inferred",
    )
    return 0


def _build_search_feedback(
    control: SearchControl,
    *,
    color_mode: ColorMode,
    progress_enabled: bool,
    answer_now_enabled: bool,
) -> tuple[SearchProgress, AnswerNowInputListener | None]:
    """Return the stderr progress reporter and the input listener wired to it.

    The listener publishes the user's keypress straight to the reporter so the
    progress line can acknowledge it while the collected records are still
    being ranked. Building the pair together keeps that subscription from
    being dropped by a later edit to either half.

    Parameters
    ----------
    control : SearchControl
        Control the keypress requests a partial answer on.
    color_mode : ColorMode
        ``--color`` selection applied to the progress line.
    progress_enabled : bool
        Whether a progress line is drawn at all.
    answer_now_enabled : bool
        Whether stdin and stderr are both interactive, so a keypress can be
        read and its acknowledgment seen.

    Returns
    -------
    tuple
        The reporter, and the listener when one is warranted.
    """
    if not progress_enabled:
        return noop_search_progress(), None
    progress = ConsoleSearchProgress(
        enabled=True,
        color_mode=color_mode,
        answer_now_hint=answer_now_enabled,
    )
    if not answer_now_enabled:
        return progress, None
    return progress, AnswerNowInputListener(control, on_request=progress.answer_now_pending)


def run_search_command(args: SearchArgs) -> int:
    """Execute ``agentgrep search`` with ranked, pretty output.

    Collects all matching records eagerly with a progress spinner,
    scores them by rapidfuzz partial_ratio (skipped with ``--no-rank``
    or on answer-now), groups by session (skipped with ``--no-group``),
    and renders with snippet-first pretty output.  Returns ``0`` when
    at least one result survives, ``1`` otherwise.
    """
    if args.output_mode != "ui":
        _print_query_diagnostics(args.diagnostics)
    if (
        not args.terms
        and args.compiled is None
        and args.origin_filter is None
        and args.output_mode != "ui"
    ):
        msg = "search requires at least one term unless --ui is used"
        raise SystemExit(msg)
    relevance_order = not args.no_rank and args.output_mode != "ui"
    query_text = " ".join(args.terms)
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
        effort=args.effort,
        order="relevance" if relevance_order else "newest",
        scope_provenance=args.scope_provenance,
        conversation_limit=args.conversation_limit,
        relevance_threshold=args.threshold if query_text else 0,
        origin_boost=args.origin_boost,
    )
    if args.output_mode == "ui":
        _launch_ui(
            query,
            initial_search_text=args.raw_query or None,
            base_scope=args.base_scope,
            base_effort=args.base_effort,
            base_scope_provenance=args.base_scope_provenance,
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
    progress, listener = _build_search_feedback(
        control,
        color_mode=args.color_mode,
        progress_enabled=progress_enabled,
        answer_now_enabled=answer_now_enabled,
    )
    if listener is not None:
        listener.start()
    try:
        run = run_search_result(
            pathlib.Path.home(),
            query,
            progress=progress,
            control=control,
        )
    finally:
        if listener is not None:
            listener.stop()
    answered_early = control.answer_now_requested()
    scored = _score_search_records(
        list(run.records),
        args,
        answered_early=answered_early,
    )
    from agentgrep.ranking import group_by_session

    grouped = group_by_session([(r, s, 0) for r, s in scored])
    _print_search_text(grouped, args)
    if not scored:
        print(_empty_search_message(run.summary), file=sys.stderr)
    _print_search_depth_hint(run.summary)
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


def _empty_search_message(
    summary: RunSummary | None,
    *,
    displayed_matches: int = 0,
) -> str:
    """Return human empty-result copy grounded in engine terminal evidence."""
    if summary is None:
        return "No matches found."
    if displayed_matches == 0 and summary.match_count > 0:
        return "No matches met the output filters."
    return {
        "no_prompt_match": "No prompt matches found.",
        "no_candidate_conversation": "No candidate conversations found.",
        "no_selected_conversation_match": ("No matches found in selected conversations."),
        "no_exhaustive_match": "No matches found in readable conversations.",
        "undetermined": "Search incomplete; coverage could not be determined.",
    }.get(summary.outcome, "No matches found.")


def _print_search_depth_hint(summary: RunSummary | None) -> None:
    """Write one interactive hint for engine-authored conversation follow-ups."""
    if summary is None or not bool(getattr(sys.stderr, "isatty", lambda: False)()):
        return
    actions = {action.action_id: action for action in summary.next_actions}
    if targeted_action := actions.get("search.targeted"):
        if targeted_action.requires_confirmation:
            print(
                "Searched prompts only. Change the explicit scope to all "
                "before using --deep or --exhaustive.",
                file=sys.stderr,
            )
        else:
            print(
                "Searched prompts only. Use --deep to search selected "
                "conversations, or --exhaustive to search all readable "
                "conversations.",
                file=sys.stderr,
            )
    elif "search.exhaustive" in actions:
        coverage = summary.coverage
        print(
            "Targeted search read "
            f"{coverage.conversations_completed}/"
            f"{coverage.conversations_selected} selected conversations. "
            "Use --exhaustive to search all readable conversations.",
            file=sys.stderr,
        )


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

    from agentgrep.ranking import score_search_records

    return score_search_records(
        records,
        query_text,
        origin_boost=args.origin_boost,
    )


def _run_search_eager(args: SearchArgs, query: SearchQuery) -> int:
    """Eager search for JSON/NDJSON output with ranking but no pairwise dedup."""
    control = SearchControl()
    run = run_search_result(
        pathlib.Path.home(),
        query,
        control=control,
    )
    records = list(run.records)
    scored = _score_search_records(records, args)
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
            "scope": args.scope,
            "effort": args.effort,
        }
        payload = {
            **build_envelope("search", query_data, results),
            "summary": serialize_run_summary(run.summary),
            "warnings": serialize_query_diagnostics(args.diagnostics),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(json.dumps(result, ensure_ascii=False))
        print(
            json.dumps(
                {
                    "type": "summary",
                    "data": serialize_run_summary(run.summary),
                    "warnings": serialize_query_diagnostics(args.diagnostics),
                },
                ensure_ascii=False,
            ),
        )
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
        effort=args.effort,
        order="scan",
        scope_provenance=args.scope_provenance,
        conversation_limit=args.conversation_limit,
    )


def print_grep_results(
    records: list[SearchRecord],
    args: GrepArgs,
    *,
    run_summary: RunSummary | None = None,
) -> int:
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
        summary_data: dict[str, object] = {"matches": total_match_count}
        if run_summary is not None:
            summary_data["run"] = serialize_run_summary(run_summary)
        json_events.append({"type": "summary", "data": summary_data})
        payload = {
            "command": "grep",
            "events": json_events,
            "warnings": serialize_query_diagnostics(args.diagnostics),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
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
            print(_empty_search_message(run_summary), file=sys.stderr)
            _print_search_depth_hint(run_summary)
        return 1
    for record in records:
        print(format_grep_record(record, args))
        if not args.only_matching and (
            args.heading is True or (args.heading is None and sys.stdout.isatty())
        ):
            print()
    if args.output_mode == "text":
        _print_search_depth_hint(run_summary)
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
    run_summary = None
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
        elif isinstance(event, events.SearchFinished):
            run_summary = event.summary
            if summary is not None:
                summary.elapsed = event.elapsed_seconds
    if args.output_mode == "ndjson":
        if run_summary is None:
            msg = "search event stream ended without SearchFinished"
            raise RuntimeError(msg)
        print(
            json.dumps(
                {
                    "type": "summary",
                    "data": {
                        "matches": match_count,
                        "run": serialize_run_summary(run_summary),
                    },
                    "warnings": serialize_query_diagnostics(args.diagnostics),
                },
                ensure_ascii=False,
            ),
        )
    if is_tty and summary is not None and summary.total > 0:
        footer = summary.format(colors=AnsiColors.for_stream(args.color_mode, sys.stderr))
        if footer:
            print(footer, file=sys.stderr)
    if match_count == 0 and args.output_mode == "text":
        print(_empty_search_message(run_summary), file=sys.stderr)
    if args.output_mode == "text":
        _print_search_depth_hint(run_summary)
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
    if args.output_mode != "ui":
        _print_query_diagnostics(args.diagnostics)
    query = build_grep_query(args)
    if args.output_mode == "ui":
        _launch_ui(
            query,
            initial_search_text=args.raw_query or None,
            base_scope=args.base_scope,
            base_effort=args.base_effort,
            base_scope_provenance=args.base_scope_provenance,
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
    if args.output_mode == "json":
        run = run_search_result(
            pathlib.Path.home(),
            query,
            control=control,
        )
        return print_grep_results(
            list(run.records),
            args,
            run_summary=run.summary,
        )
    run = run_search_result(
        pathlib.Path.home(),
        query,
        progress=progress,
        control=control,
    )
    return print_grep_results(
        list(run.records),
        args,
        run_summary=run.summary,
    )
