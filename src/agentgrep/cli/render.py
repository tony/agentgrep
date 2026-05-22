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
from agentgrep.cli.parser import FindArgs, SearchArgs, UIArgs

__all__ = [
    "build_envelope",
    "maybe_build_pydantic",
    "print_find_results",
    "print_search_results",
    "run_find_command",
    "run_search_command",
    "run_ui_command",
    "serialize_find_record",
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
