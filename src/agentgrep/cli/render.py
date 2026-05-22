"""CLI output rendering and subcommand dispatch for agentgrep.

This module owns the eager-list rendering paths for the existing
``search`` and ``find`` subcommands, plus the dispatcher functions that
glue parsed arguments to the engine and the chosen output format.

A streaming-aware printer for the upcoming ``grep`` subcommand will live
alongside these helpers in a future commit; the existing ``search``
deliberately keeps its eager-list path because it builds a summary
header that needs the full record count.

Runtime callables (engines, helpers, classes) are accessed through the
``agentgrep`` namespace at call time rather than imported by name, so
tests that monkeypatch attributes such as ``agentgrep.run_search_query``
continue to see their patches honored when the dispatchers run.

Symbols defined here are re-exported from :mod:`agentgrep` for backward
compatibility with imports such as ``agentgrep.print_search_results``
and ``agentgrep.run_search_command``.
"""

from __future__ import annotations

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
    fuzzy as _fuzzy_lib,
)
from agentgrep.cli.parser import FindArgs, FuzzyArgs, GrepArgs, SearchArgs, UIArgs

__all__ = [
    "build_envelope",
    "build_grep_query",
    "filter_find_records",
    "format_grep_record",
    "fuzzy_filter_lines",
    "maybe_build_pydantic",
    "print_find_results",
    "print_grep_results",
    "print_search_results",
    "run_find_command",
    "run_fuzzy_command",
    "run_grep_command",
    "run_search_command",
    "run_ui_command",
    "serialize_find_record",
    "serialize_grep_record",
    "serialize_search_record",
    "serialize_source_handle",
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
        "search_root": (
            None
            if source.search_root is None
            else agentgrep.format_display_path(source.search_root, directory=True)
        ),
        "mtime_ns": source.mtime_ns,
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


def print_search_results(records: list[SearchRecord], args: SearchArgs) -> None:
    """Emit search results in the requested format."""
    serialize_search, _, serialize_envelope = maybe_build_pydantic()
    query_data: dict[str, object] = {
        "terms": list(args.terms),
        "agents": list(args.agents),
        "type": args.search_type,
        "any": args.any_term,
        "regex": args.regex,
        "case_sensitive": args.case_sensitive,
        "limit": args.limit,
    }
    if args.output_mode == "json":
        payload = serialize_envelope(
            "search",
            query_data,
            [serialize_search(record) for record in records],
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.output_mode == "ndjson":
        for record in records:
            print(json.dumps(serialize_search(record), ensure_ascii=False))
        return
    for index, record in enumerate(records, start=1):
        heading = f"[{index}] {record.agent} {record.kind} {record.store}"
        details = [
            record.timestamp,
            record.model,
            agentgrep.format_display_path(record.path),
        ]
        print(heading)
        print(" | ".join(detail for detail in details if detail))
        if record.title:
            print(record.title)
        print()
        print(record.text)
        print()


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
        print(f"{record.agent} {record.path_kind} {record.store}")
        print(agentgrep.format_display_path(record.path))
        print()


def _format_find_text_line(record: FindRecord, args: FindArgs) -> str:
    """Compose one line for ``--list-details`` / ``--print0`` output."""
    path = agentgrep.format_display_path(record.path)
    if args.list_details:
        return f"{record.agent}\t{record.path_kind}\t{record.store}\t{record.adapter_id}\t{path}"
    return path


def run_search_command(args: SearchArgs) -> int:
    """Execute ``agentgrep search``."""
    if not args.terms and args.output_mode != "ui":
        msg = "search requires at least one term unless --ui is used"
        raise SystemExit(msg)
    query = agentgrep.make_search_query(args)
    if args.output_mode == "ui":
        agentgrep.run_ui(pathlib.Path.home(), query, control=agentgrep.SearchControl())
        return 0
    answer_now_enabled = agentgrep.should_enable_answer_now(args)
    control = agentgrep.SearchControl()
    listener = agentgrep.AnswerNowInputListener(control) if answer_now_enabled else None
    progress = agentgrep.build_search_progress(args, answer_now_hint=answer_now_enabled)
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
    print_search_results(records, args)
    if records:
        return 0
    if args.output_mode == "text":
        print("No matches found.", file=sys.stderr)
    return 1


def _resolve_find_case_sensitive(pattern: str | None, mode: agentgrep.CaseMode) -> bool:
    """Apply fd's smart-case rule to a find pattern."""
    if mode == "respect":
        return True
    if mode == "ignore":
        return False
    return pattern is not None and any(ch.isupper() for ch in pattern)


def _pattern_matches(record: FindRecord, args: FindArgs) -> bool:
    """Decide whether a find record satisfies the requested pattern mode."""
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
        return fnmatch.fnmatchcase(haystack, needle if case_sensitive else needle.casefold())
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.search(args.pattern, haystack, flags) is not None
    except re.error:
        return False


def _type_matches(record: FindRecord, args: FindArgs) -> bool:
    """Apply the ``-t/--type`` filter against the record's source kind."""
    if args.type_filter == "all":
        return True
    source_kind = t.cast("str | None", record.metadata.get("source_kind"))
    if source_kind is None:
        return False
    if args.type_filter == "sessions":
        return "session" in source_kind
    return args.type_filter in source_kind


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


def run_find_command(args: FindArgs) -> int:
    """Execute ``agentgrep find``."""
    raw_records = agentgrep.run_find_query(
        pathlib.Path.home(),
        args.agents,
        pattern=None,
        limit=None,
    )
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


def format_grep_record(record: agentgrep.SearchRecord, args: GrepArgs) -> str:
    """Format one matching record for text-mode ``grep`` output.

    Heading-grouped on TTY: ``[agent store path]`` on its own line, then
    the matched text. ``--vimgrep`` collapses to a single
    ``path:line:col:text`` line. ``-l`` / ``-L`` emit just the path.
    ``-c`` is aggregated by the caller, not formatted per-record.
    """
    path = agentgrep.format_display_path(record.path)
    if args.files_with_matches or args.files_without_match:
        return path
    if args.vimgrep:
        return f"{path}:1:1:{record.text}"
    if args.only_matching:
        return record.text
    heading_on = args.heading if args.heading is not None else sys.stdout.isatty()
    text = record.text
    if heading_on:
        return f"[{record.agent} {record.store} {path}]\n{text}"
    prefix = f"{record.agent}:{record.store}:{path}"
    return f"{prefix}:{text}"


def print_grep_results(records: list[agentgrep.SearchRecord], args: GrepArgs) -> int:
    """Emit grep results and return the rg-style exit code."""
    if args.invert_match:
        # The engine returns matches; invert by recomputing against the raw
        # candidate set is non-trivial. As a v1 simplification, --invert-match
        # is honored only at the count-only / files-without-match level, where
        # the question collapses to "did anything match?". A future commit
        # can fold inversion deeper into the engine.
        if args.count_only:
            print("0" if records else "1")
            return 1 if records else 0
        if args.files_without_match:
            return _print_files_without_match(args)

    if args.output_mode == "json":
        events: list[dict[str, object]] = [serialize_grep_record(record) for record in records]
        summary: dict[str, object] = {"type": "summary", "data": {"matches": len(records)}}
        events.append(summary)
        print(json.dumps({"command": "grep", "events": events}, ensure_ascii=False, indent=2))
        return 0 if records else 1
    if args.output_mode == "ndjson":
        for record in records:
            print(json.dumps(serialize_grep_record(record), ensure_ascii=False))
        return 0 if records else 1

    if args.count_only:
        print(str(len(records)))
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
        if args.heading is True or (args.heading is None and sys.stdout.isatty()):
            print()
    return 0


def _print_files_without_match(args: GrepArgs) -> int:
    """Print sources with no matches.

    Engine support for inverted file enumeration isn't wired yet — this
    stub keeps the CLI grammar valid and emits the rg-style "no matches"
    exit code. A follow-up will compute the complement against
    discovered sources.
    """
    return 0


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
    """
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


def run_grep_command(args: GrepArgs) -> int:
    """Execute ``agentgrep grep``."""
    if not args.patterns:
        msg = "grep requires at least one pattern"
        raise SystemExit(msg)
    query = build_grep_query(args)
    if args.output_mode == "ui":
        agentgrep.run_ui(pathlib.Path.home(), query, control=agentgrep.SearchControl())
        return 0
    control = agentgrep.SearchControl()
    progress = agentgrep.build_search_progress(
        # GrepArgs structurally matches what build_search_progress reads
        # (output_mode + progress_mode + color_mode). The dispatcher uses
        # the cast rather than a parallel signature so the existing helper
        # stays single-purpose.
        t.cast("agentgrep.SearchArgs", args),
        answer_now_hint=False,
    )
    records = agentgrep.run_search_query(
        pathlib.Path.home(),
        query,
        progress=progress,
        control=control,
    )
    return print_grep_results(records, args)
