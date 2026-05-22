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

import json
import pathlib
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
)
from agentgrep.cli.parser import FindArgs, GrepArgs, SearchArgs, UIArgs

__all__ = [
    "build_envelope",
    "build_grep_query",
    "format_grep_record",
    "maybe_build_pydantic",
    "print_find_results",
    "print_grep_results",
    "print_search_results",
    "run_find_command",
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
    """Emit find results in the requested format."""
    _, serialize_find, serialize_envelope = maybe_build_pydantic()
    query_data: dict[str, object] = {
        "pattern": args.pattern,
        "agents": list(args.agents),
        "limit": args.limit,
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
    for record in records:
        print(f"{record.agent} {record.path_kind} {record.store}")
        print(agentgrep.format_display_path(record.path))
        print()


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


def run_find_command(args: FindArgs) -> int:
    """Execute ``agentgrep find``."""
    records = agentgrep.run_find_query(
        pathlib.Path.home(),
        args.agents,
        pattern=args.pattern,
        limit=args.limit,
    )
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
