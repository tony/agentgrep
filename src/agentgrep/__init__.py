#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# dependencies = ["pydantic>=2.11.3", "textual>=3.2.0"]
# ///
"""Search local AI agent prompts and conversations without mutating agent stores.

The tool discovers known read-only stores under ``~/.codex``, ``~/.claude``,
``~/.cursor``, and Cursor's official IDE storage locations, then normalizes
results through named adapters.

Examples
--------
List prompts containing both ``serenity`` and ``bliss``:

>>> query = SearchQuery(
...     terms=("serenity", "bliss"),
...     scope="prompts",
...     any_term=False,
...     regex=False,
...     case_sensitive=False,
...     agents=("codex",),
...     limit=None,
... )
>>> matches_text("A serenity prompt with bliss inside.", query)
True
>>> matches_text("Only serenity appears here.", query)
False
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import dataclasses
import datetime
import functools
import importlib
import itertools
import json
import logging
import os
import pathlib
import re
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import tomllib
import typing as t
import urllib.parse

import pydantic
from rich.console import Group as _RichGroup
from rich.markdown import Markdown as _RichMarkdown
from rich.syntax import Syntax as _RichSyntax
from rich.text import Text as _RichText

# orjson is an optional JSON-decode accelerator (the ``speedups`` extra).
# Pure-Python ``json`` stays the semantic source of truth — see ADR 0002 — so
# ``_loads`` below behaves identically whether or not orjson is installed.
try:
    import orjson as _orjson
except ImportError:
    # Keep _orjson typed as the module so _loads resolves .loads /
    # .JSONDecodeError; the runtime None check guards the absent case.
    _orjson = None  # ty: ignore[invalid-assignment]

# Records, payloads, and shared vocabulary live in agentgrep.records.
# Structural typing shims live in agentgrep._types.
# Text-presentation helpers live in agentgrep._text.
from agentgrep._text import (
    ANSI_CSI_RE,
    CLI_DESCRIPTION,
    DETAIL_BODY_MAX_LINES,
    FIND_DESCRIPTION,
    GREP_DESCRIPTION,
    INLINE_CODE_RE,
    QUERY_BOOLEAN_KEYWORDS,
    QUERY_FIELD_TOKEN_RE,
    QUERY_HIGHLIGHT_ROLES,
    QUERY_TOKEN_RE,
    SEARCH_DESCRIPTION,
    SHELL_TOKEN_RE,
    UI_DESCRIPTION,
    AnsiColors,
    ContentFormat,
    PrivatePath,
    _hard_truncate_ansi,
    _visible_width,
    build_description,
    detect_content_format,
    find_first_match_line,
    format_compact_path,
    format_display_path,
    highlight_matches,
    highlight_query_spans,
    should_enable_color,
    truncate_lines,
)
from agentgrep._types import (
    HelpTheme,
    PydanticModule,
    PydanticTypeAdapter,
    PydanticTypeAdapterFactory,
    QueryAppLike,
    RichTextModule,
    RunnableAppLike,
    SearchColors,
    StaticLike,
    StreamingAppLike,
    TextualAppModule,
    TextualBindingModule,
    TextualContainersModule,
    TextualMessageModule,
    TextualOptionListInternalsModule,
    TextualWidgetsModule,
)

# Store parsers + record normalization live in agentgrep.adapters.
from agentgrep.adapters import (
    CLAUDE_PASTE_HASH_RE,
    CLAUDE_PASTE_REF_RE,
    _vscode_uri_to_path,
    _vscode_workspace_cwd,
    build_search_record,
    candidate_from_mapping,
    claude_history_paste_text,
    expand_claude_history_pastes,
    extract_conversation_id,
    extract_message_text,
    extract_model,
    extract_role,
    extract_session_id,
    extract_timestamp,
    extract_title,
    find_store_roles_for_type_filter,
    flatten_content_value,
    flatten_summary_bullets,
    iter_cursor_prompt_candidates,
    iter_message_candidates,
    iter_source_records,
    iter_text_fragments,
    parse_antigravity_cli_conversation_db,
    parse_antigravity_cli_history_file,
    parse_antigravity_cli_transcript,
    parse_antigravity_protobuf_file,
    parse_claude_history_file,
    parse_claude_project_file,
    parse_claude_settings_file,
    parse_claude_store_db,
    parse_claude_task_file,
    parse_claude_team_file,
    parse_claude_todo_file,
    parse_claude_usage_facet,
    parse_codex_external_imports_file,
    parse_codex_goals_db,
    parse_codex_history_file,
    parse_codex_legacy_session_file,
    parse_codex_logs_db,
    parse_codex_memories_db,
    parse_codex_session_file,
    parse_codex_session_index_file,
    parse_codex_state_db,
    parse_cursor_ai_tracking_db,
    parse_cursor_cli_chats_db,
    parse_cursor_cli_transcript,
    parse_cursor_prompt_history,
    parse_cursor_state_db,
    parse_file_metadata_summary_file,
    parse_gemini_chat_file,
    parse_gemini_chat_legacy_file,
    parse_gemini_logs_file,
    parse_grok_chat_history,
    parse_grok_prompt_history,
    parse_grok_session_search_db,
    parse_grok_subagents,
    parse_hooks_summary_file,
    parse_json_summary_file,
    parse_opencode_db,
    parse_pi_context_mode_db,
    parse_pi_session_file,
    parse_text_store_file,
    parse_toml_summary_file,
    parse_vscode_chat_session,
    parse_vscode_inline_history,
    store_descriptor_for_record,
    store_role_for_record,
)

# Store discovery lives in agentgrep.discovery.
from agentgrep.discovery import (
    _catalog_version_detection,
    _claude_project_roots,
    _claude_source_version_detection,
    _codex_client_version_from_cache,
    _codex_project_roots,
    _codex_project_roots_from_legacy_sessions,
    _codex_source_version_detection,
    _codex_sqlite_home_from_config,
    _cursor_ide_workspace_root,
    _first_json_array_mapping,
    _first_jsonl_mapping,
    _json_mapping,
    _project_roots_from_jsonl_sessions,
    _resolve_optional_root,
    _safe_project_root,
    build_discovery_version_context,
    detect_source_version,
    discover_antigravity_cli_sources,
    discover_antigravity_ide_sources,
    discover_claude_sources,
    discover_codex_sources,
    discover_cursor_cli_sources,
    discover_cursor_ide_sources,
    discover_from_catalog,
    discover_gemini_sources,
    discover_grok_sources,
    discover_opencode_sources,
    discover_pi_sources,
    discover_sources,
    discover_vscode_sources,
    format_timestamp_tig,
    handles_from_discovery,
    resolve_codex_sqlite_root,
    resolve_env_root,
)

# Progress reporting lives in agentgrep.progress.
from agentgrep.progress import (
    _SOURCE_PROGRESS_RECORD_INTERVAL,
    AnswerNowInputListener,
    ConsoleSearchProgress,
    FilterCompletedPayload,
    FilterRequestedPayload,
    NoopSearchProgress,
    ProgressSnapshot,
    ProgressUpdatedPayload,
    RecordsAppendedPayload,
    SearchControl,
    SearchFinishedPayload,
    SearchProgress,
    SearchRequestedPayload,
    SourceProgressCallback,
    StreamingRecordsBatch,
    StreamingSearchFinished,
    StreamingSearchProgress,
    _format_search_progress_line,
    _report_source_progress,
    format_match_count,
    format_search_progress_line,
    format_source_progress_detail,
    noop_search_progress,
)

# Low-level read-only I/O primitives live in agentgrep.readers.
from agentgrep.readers import (
    _CODEX_RAW_SKIP_MIN_BYTES,
    _CODEX_SESSION_META_MARKER,
    _JSONL_PREFIX_BYTES,
    _JSONL_REVERSE_CHUNK_BYTES,
    _JSONL_SKIP_CHUNK_BYTES,
    _JSONL_YIELD_INTERVAL_SECONDS,
    _PI_SESSION_HEADER_MARKER,
    _SKIPPED_JSONL_LINE,
    _combine_raw_skip_lines,
    _decode_jsonl_raw_line,
    _decode_protobuf_text,
    _discard_rest_of_line,
    _file_size,
    _is_codex_function_call_output_line,
    _iter_jsonl,
    _iter_jsonl_reverse,
    _iter_jsonl_with_raw_line_skip,
    _iter_jsonl_with_raw_prefix_skip,
    _keep_jsonl_header_lines,
    _loads,
    _looks_like_protobuf_message,
    _PeriodicYield,
    _read_first_jsonl_header,
    _read_varint,
    _record_engine_profile_sample,
    _record_readonly_command_profile,
    as_optional_str,
    decode_sqlite_value,
    file_mtime_ns,
    isoformat_from_mtime_ns,
    iter_conversation_summaries,
    iter_jsonl,
    iter_key_value_rows,
    iter_protobuf_text_fields,
    list_files_matching,
    open_readonly_sqlite,
    parse_embedded_json,
    read_json_file,
    read_text_file,
    run_readonly_command,
    select_backends,
    sqlite_column_names,
    sqlite_table_names,
    which_first,
)
from agentgrep.records import (
    AGENT_CHOICES,
    CONVERSATION_STORE_ROLES,
    CURSOR_STATE_TOKENS,
    ITER_SOURCE_RECORD_ADAPTERS,
    JSON_FILE_SUFFIXES,
    OFFICIAL_CURSOR_STATE_PATHS,
    PROMPT_HISTORY_STORE_ROLES,
    SCHEMA_VERSION,
    USER_ROLES,
    AgentName,
    BackendSelection,
    ColorMode,
    DiscoveryRoot,
    DiscoveryStoreRoles,
    DiscoveryVersionContext,
    DiscoveryVersionDetail,
    EnvelopeFactory,
    EnvelopePayload,
    FindRecord,
    FindRecordPayload,
    FindSourceTypeFilter,
    GrepStyle,
    JSONScalar,
    JSONValue,
    KeyValueRow,
    MessageCandidate,
    OutputMode,
    ProgressMode,
    RawJsonlSkipLine,
    SearchMatchSurface,
    SearchQuery,
    SearchRecord,
    SearchRecordPayload,
    SearchScope,
    SourceHandle,
    SourceHandlePayload,
    SourceVersionDetection,
    SourceVersionDetectionPayload,
    SummaryRow,
)
from agentgrep.stores import (
    DiscoverySpec,
    PathKind,
    SourceKind,
    StoreCoverage,
    StoreDescriptor,
    StoreRole,
    VersionDetectionConfidence,
    VersionDetectionStrategy,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from agentgrep._engine.planning import PhysicalSearchPlan
    from agentgrep._engine.runtime import SearchRuntime
    from agentgrep.query.compile import CompiledQuery

    PrivatePathBase = pathlib.Path
else:
    PrivatePathBase = type(pathlib.Path())


def maybe_use_pydantic() -> tuple[
    t.Callable[[SearchRecord], dict[str, object]],
    t.Callable[[FindRecord], dict[str, object]],
    EnvelopeFactory,
]:
    """Return serializers backed by Pydantic when available."""
    pydantic_module = t.cast(
        "PydanticModule",
        t.cast("object", importlib.import_module("pydantic")),
    )
    search_adapter = pydantic_module.TypeAdapter(SearchRecordPayload)
    find_adapter = pydantic_module.TypeAdapter(FindRecordPayload)
    envelope_adapter = pydantic_module.TypeAdapter(EnvelopePayload)

    def pydantic_search(record: SearchRecord) -> dict[str, object]:
        payload = search_adapter.validate_python(serialize_search_record(record))
        dumped = search_adapter.dump_python(payload, mode="json")
        return t.cast("dict[str, object]", dumped)

    def pydantic_find(record: FindRecord) -> dict[str, object]:
        payload = find_adapter.validate_python(serialize_find_record(record))
        dumped = find_adapter.dump_python(payload, mode="json")
        return t.cast("dict[str, object]", dumped)

    def pydantic_envelope(
        command: str,
        query_data: dict[str, object],
        results: list[dict[str, object]],
    ) -> dict[str, object]:
        payload = envelope_adapter.validate_python(
            build_envelope(command, query_data, results),
        )
        dumped = envelope_adapter.dump_python(payload, mode="json")
        return t.cast("dict[str, object]", dumped)

    return pydantic_search, pydantic_find, pydantic_envelope


def run_ui(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
    initial_search_text: str | None = None,
) -> None:
    """Launch the streaming Textual explorer for ``query``.

    Thin wrapper that imports the real implementation from
    :mod:`agentgrep.ui.app` lazily so a bare ``import agentgrep`` never
    pulls in Textual.

    ``initial_search_text`` populates the TUI search box on open so a
    launch like ``agentgrep search --ui agent:codex bliss`` shows the
    full query string (not just the text terms). ``None`` falls back
    to the space-joined ``query.terms`` for compatibility with the
    pre-query-language callers.
    """
    from agentgrep.ui.app import run_ui as _run_ui

    _run_ui(
        home,
        query,
        control=control,
        initial_search_text=initial_search_text,
    )


def build_streaming_ui_app(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
    initial_search_text: str | None = None,
) -> object:
    """Construct the streaming Textual app without entering its run loop.

    Thin wrapper that imports the real factory from :mod:`agentgrep.ui.app`
    lazily — Textual is only required at the moment the UI is actually
    built, never at import time of the top-level package.
    """
    from agentgrep.ui.app import build_streaming_ui_app as _build

    return _build(
        home,
        query,
        control=control,
        initial_search_text=initial_search_text,
    )


def _exit_on_sigint() -> t.NoReturn:
    """Terminate with Ctrl-C signal semantics where the platform supports them."""
    if sys.platform == "win32":
        raise SystemExit(130)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.raise_signal(signal.SIGINT)
    raise SystemExit(130)  # pragma: no cover


def _write_interrupt_notice() -> None:
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.write("Interrupted by user.\n")
        sys.stderr.flush()


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Run the CLI."""
    from agentgrep import _telemetry

    telemetry = _telemetry.setup(repo_root=pathlib.Path(__file__).resolve().parents[2])
    try:
        with _telemetry.span(
            "agentgrep.cli.invocation",
            agentgrep_surface="cli",
            agentgrep_arg_count=_cli_arg_count(argv),
        ):
            parsed: FindArgs | UIArgs | GrepArgs | SearchArgs | None = None
            parse_exit: SystemExit | None = None
            with _telemetry.span("agentgrep.cli.parse", agentgrep_surface="cli"):
                try:
                    parsed = parse_args(argv)
                except SystemExit as exc:
                    parse_exit = exc
                    _telemetry.set_span_attribute("agentgrep_outcome", "parse_error")
                    _telemetry.set_span_attribute(
                        "agentgrep_exit_code",
                        _system_exit_code(exc),
                    )
            if parse_exit is not None:
                exit_code = _system_exit_code(parse_exit)
                command = _command_name_for_argv(argv)
                outcome = "help" if exit_code == 0 else "parse_error"
                _telemetry.set_span_attribute("agentgrep_command", command)
                _telemetry.set_span_attribute("agentgrep_outcome", outcome)
                _telemetry.set_span_attribute("agentgrep_exit_code", exit_code)
                logger.info(
                    "cli command completed",
                    extra={
                        "agentgrep_surface": "cli",
                        "agentgrep_command": command,
                        "agentgrep_outcome": outcome,
                        "agentgrep_exit_code": exit_code,
                    },
                )
                if exit_code == 0:
                    return 0
                raise parse_exit
            if parsed is None:
                _telemetry.set_span_attribute("agentgrep_command", "help")
                _telemetry.set_span_attribute("agentgrep_outcome", "help")
                _telemetry.set_span_attribute("agentgrep_exit_code", 0)
                logger.info(
                    "cli command completed",
                    extra={
                        "agentgrep_surface": "cli",
                        "agentgrep_command": "help",
                        "agentgrep_outcome": "help",
                        "agentgrep_exit_code": 0,
                    },
                )
                return 0
            command = _command_name_for_args(parsed)
            _telemetry.set_span_attribute("agentgrep_command", command)
            logger.info(
                "cli command started",
                extra={
                    "agentgrep_surface": "cli",
                    "agentgrep_command": command,
                },
            )
            try:
                with _telemetry.span(
                    "agentgrep.cli.dispatch",
                    agentgrep_surface="cli",
                    agentgrep_command=command,
                ):
                    if isinstance(parsed, GrepArgs):
                        exit_code = run_grep_command(parsed)
                    elif isinstance(parsed, SearchArgs):
                        exit_code = run_search_command(parsed)
                    elif isinstance(parsed, UIArgs):
                        exit_code = run_ui_command(parsed)
                    else:
                        exit_code = run_find_command(parsed)
            except BaseException:
                _telemetry.set_span_attribute("agentgrep_outcome", "error")
                logger.info(
                    "cli command failed",
                    extra={
                        "agentgrep_surface": "cli",
                        "agentgrep_command": command,
                        "agentgrep_outcome": "error",
                    },
                )
                raise
            _telemetry.set_span_attribute("agentgrep_outcome", "ok")
            _telemetry.set_span_attribute("agentgrep_exit_code", exit_code)
            logger.info(
                "cli command completed",
                extra={
                    "agentgrep_surface": "cli",
                    "agentgrep_command": command,
                    "agentgrep_outcome": "ok",
                    "agentgrep_exit_code": exit_code,
                },
            )
            return exit_code
    except KeyboardInterrupt:
        _write_interrupt_notice()
        _exit_on_sigint()
    finally:
        telemetry.shutdown()


def _command_name_for_args(args: object) -> str:
    """Return a stable CLI command name for parsed args."""
    if isinstance(args, GrepArgs):
        return "grep"
    if isinstance(args, SearchArgs):
        return "search"
    if isinstance(args, UIArgs):
        return "ui"
    return "find"


def _cli_arg_count(argv: cabc.Sequence[str] | None) -> int:
    """Return the CLI argument count without recording raw argv."""
    return len(sys.argv[1:] if argv is None else argv)


def _system_exit_code(exc: SystemExit) -> int:
    """Return a numeric ``SystemExit`` code."""
    if isinstance(exc.code, int):
        return exc.code
    if exc.code is None:
        return 0
    return 1


def _command_name_for_argv(argv: cabc.Sequence[str] | None) -> str:
    """Infer a safe command label from raw argv without recording it."""
    effective_argv = sys.argv[1:] if argv is None else argv
    for token in effective_argv:
        if token in {"grep", "search", "find", "ui"}:
            return token
        if token in {"-h", "--help"}:
            return "help"
    return "unknown"


from agentgrep._engine import (  # noqa: E402  (re-exports must follow main definition)
    SearchRuntime,
    SourceScanCache,
    SourceScanCacheStats,
    aiter_search_events,
    iter_find_events,
    iter_search_events,
)
from agentgrep._engine.orchestration import (  # noqa: E402  (re-exports must follow main definition)
    _source_profile_attributes,
    build_grep_command,
    build_record_match_surface,
    build_search_haystack,
    cached_haystack,
    clear_haystack_cache,
    collect_search_records,
    collect_search_records_from_plan,
    compute_filter_matches,
    direct_source_matches,
    discover_sources_for_search,
    find_sources,
    flatten_json_strings_with_tool,
    grep_file_matches,
    grep_root_paths,
    matches_record,
    matches_text,
    plan_search_sources,
    prefilter_sources_by_root,
    prompt_history_agents_for_sources,
    record_dedupe_key,
    record_matches_scope,
    run_find_query,
    run_search_query,
    search_record_sort_key,
    search_sources,
    source_matches_scope,
    source_order_key,
)
from agentgrep.cli.help_theme import (  # noqa: E402  (re-exports must follow main definition)
    OPTIONS_EXPECTING_VALUE,
    OPTIONS_FLAG_ONLY,
    AgentGrepHelpFormatter,
    AnsiHelpTheme,
    create_themed_formatter,
    should_enable_help_color,
)
from agentgrep.cli.parser import (  # noqa: E402  (re-exports must follow main definition)
    CaseMode,
    FindArgs,
    FindPatternMode,
    FindTypeFilter,
    GrepArgs,
    ParserBundle,
    PatternMode,
    SearchArgs,
    UIArgs,
    add_common_agent_options,
    add_output_mode_options,
    build_docs_parser,
    configured_color_environment,
    create_parser,
    normalize_color_mode,
    parse_agents,
    parse_args,
    parse_output_mode,
)
from agentgrep.cli.render import (  # noqa: E402  (re-exports must follow main definition)
    build_envelope,
    build_grep_query,
    filter_find_records,
    format_grep_record,
    maybe_build_pydantic,
    print_find_results,
    print_grep_results,
    run_find_command,
    run_grep_command,
    run_search_command,
    run_ui_command,
    serialize_find_record,
    serialize_grep_record,
    serialize_search_record,
    serialize_source_handle,
    stream_find_results,
    stream_grep_results,
)

__all__ = (
    "AGENT_CHOICES",
    "ANSI_CSI_RE",
    "CLAUDE_PASTE_HASH_RE",
    "CLAUDE_PASTE_REF_RE",
    "CLI_DESCRIPTION",
    "CONVERSATION_STORE_ROLES",
    "CURSOR_STATE_TOKENS",
    "DETAIL_BODY_MAX_LINES",
    "FIND_DESCRIPTION",
    "GREP_DESCRIPTION",
    "INLINE_CODE_RE",
    "ITER_SOURCE_RECORD_ADAPTERS",
    "JSON_FILE_SUFFIXES",
    "OFFICIAL_CURSOR_STATE_PATHS",
    "OPTIONS_EXPECTING_VALUE",
    "OPTIONS_FLAG_ONLY",
    "PROMPT_HISTORY_STORE_ROLES",
    "QUERY_BOOLEAN_KEYWORDS",
    "QUERY_FIELD_TOKEN_RE",
    "QUERY_HIGHLIGHT_ROLES",
    "QUERY_TOKEN_RE",
    "SCHEMA_VERSION",
    "SEARCH_DESCRIPTION",
    "SHELL_TOKEN_RE",
    "UI_DESCRIPTION",
    "USER_ROLES",
    "AgentGrepHelpFormatter",
    "AgentName",
    "AnsiColors",
    "AnsiHelpTheme",
    "AnswerNowInputListener",
    "BackendSelection",
    "CaseMode",
    "ColorMode",
    "ConsoleSearchProgress",
    "ContentFormat",
    "DiscoveryRoot",
    "DiscoverySpec",
    "DiscoveryStoreRoles",
    "DiscoveryVersionContext",
    "DiscoveryVersionDetail",
    "EnvelopeFactory",
    "EnvelopePayload",
    "FilterCompletedPayload",
    "FilterRequestedPayload",
    "FindArgs",
    "FindPatternMode",
    "FindRecord",
    "FindRecordPayload",
    "FindSourceTypeFilter",
    "FindTypeFilter",
    "GrepArgs",
    "GrepStyle",
    "HelpTheme",
    "JSONScalar",
    "JSONValue",
    "KeyValueRow",
    "MessageCandidate",
    "NoopSearchProgress",
    "OutputMode",
    "ParserBundle",
    "PathKind",
    "PatternMode",
    "PrivatePath",
    "ProgressMode",
    "ProgressSnapshot",
    "ProgressUpdatedPayload",
    "PydanticModule",
    "PydanticTypeAdapter",
    "PydanticTypeAdapterFactory",
    "QueryAppLike",
    "RawJsonlSkipLine",
    "RecordsAppendedPayload",
    "RichTextModule",
    "RunnableAppLike",
    "SearchArgs",
    "SearchColors",
    "SearchControl",
    "SearchFinishedPayload",
    "SearchMatchSurface",
    "SearchProgress",
    "SearchQuery",
    "SearchRecord",
    "SearchRecordPayload",
    "SearchRequestedPayload",
    "SearchRuntime",
    "SearchScope",
    "SourceHandle",
    "SourceHandlePayload",
    "SourceKind",
    "SourceProgressCallback",
    "SourceScanCache",
    "SourceScanCacheStats",
    "SourceVersionDetection",
    "SourceVersionDetectionPayload",
    "StaticLike",
    "StoreCoverage",
    "StoreDescriptor",
    "StoreRole",
    "StreamingAppLike",
    "StreamingRecordsBatch",
    "StreamingSearchFinished",
    "StreamingSearchProgress",
    "SummaryRow",
    "TextualAppModule",
    "TextualBindingModule",
    "TextualContainersModule",
    "TextualMessageModule",
    "TextualOptionListInternalsModule",
    "TextualWidgetsModule",
    "UIArgs",
    "VersionDetectionConfidence",
    "VersionDetectionStrategy",
    "add_common_agent_options",
    "add_output_mode_options",
    "aiter_search_events",
    "as_optional_str",
    "build_description",
    "build_discovery_version_context",
    "build_docs_parser",
    "build_envelope",
    "build_grep_command",
    "build_grep_query",
    "build_record_match_surface",
    "build_search_haystack",
    "build_search_record",
    "build_streaming_ui_app",
    "cached_haystack",
    "candidate_from_mapping",
    "claude_history_paste_text",
    "clear_haystack_cache",
    "collect_search_records",
    "collect_search_records_from_plan",
    "compute_filter_matches",
    "configured_color_environment",
    "create_parser",
    "create_themed_formatter",
    "decode_sqlite_value",
    "detect_content_format",
    "detect_source_version",
    "direct_source_matches",
    "discover_antigravity_cli_sources",
    "discover_antigravity_ide_sources",
    "discover_claude_sources",
    "discover_codex_sources",
    "discover_cursor_cli_sources",
    "discover_cursor_ide_sources",
    "discover_from_catalog",
    "discover_gemini_sources",
    "discover_grok_sources",
    "discover_opencode_sources",
    "discover_pi_sources",
    "discover_sources",
    "discover_sources_for_search",
    "discover_vscode_sources",
    "expand_claude_history_pastes",
    "extract_conversation_id",
    "extract_message_text",
    "extract_model",
    "extract_role",
    "extract_session_id",
    "extract_timestamp",
    "extract_title",
    "file_mtime_ns",
    "filter_find_records",
    "find_first_match_line",
    "find_sources",
    "find_store_roles_for_type_filter",
    "flatten_content_value",
    "flatten_json_strings_with_tool",
    "flatten_summary_bullets",
    "format_compact_path",
    "format_display_path",
    "format_grep_record",
    "format_match_count",
    "format_search_progress_line",
    "format_source_progress_detail",
    "format_timestamp_tig",
    "grep_file_matches",
    "grep_root_paths",
    "handles_from_discovery",
    "highlight_matches",
    "highlight_query_spans",
    "isoformat_from_mtime_ns",
    "iter_conversation_summaries",
    "iter_cursor_prompt_candidates",
    "iter_find_events",
    "iter_jsonl",
    "iter_key_value_rows",
    "iter_message_candidates",
    "iter_protobuf_text_fields",
    "iter_search_events",
    "iter_source_records",
    "iter_text_fragments",
    "list_files_matching",
    "main",
    "matches_record",
    "matches_text",
    "maybe_build_pydantic",
    "maybe_use_pydantic",
    "noop_search_progress",
    "normalize_color_mode",
    "open_readonly_sqlite",
    "parse_agents",
    "parse_antigravity_cli_conversation_db",
    "parse_antigravity_cli_history_file",
    "parse_antigravity_cli_transcript",
    "parse_antigravity_protobuf_file",
    "parse_args",
    "parse_claude_history_file",
    "parse_claude_project_file",
    "parse_claude_settings_file",
    "parse_claude_store_db",
    "parse_claude_task_file",
    "parse_claude_team_file",
    "parse_claude_todo_file",
    "parse_claude_usage_facet",
    "parse_codex_external_imports_file",
    "parse_codex_goals_db",
    "parse_codex_history_file",
    "parse_codex_legacy_session_file",
    "parse_codex_logs_db",
    "parse_codex_memories_db",
    "parse_codex_session_file",
    "parse_codex_session_index_file",
    "parse_codex_state_db",
    "parse_cursor_ai_tracking_db",
    "parse_cursor_cli_chats_db",
    "parse_cursor_cli_transcript",
    "parse_cursor_prompt_history",
    "parse_cursor_state_db",
    "parse_embedded_json",
    "parse_file_metadata_summary_file",
    "parse_gemini_chat_file",
    "parse_gemini_chat_legacy_file",
    "parse_gemini_logs_file",
    "parse_grok_chat_history",
    "parse_grok_prompt_history",
    "parse_grok_session_search_db",
    "parse_grok_subagents",
    "parse_hooks_summary_file",
    "parse_json_summary_file",
    "parse_opencode_db",
    "parse_output_mode",
    "parse_pi_context_mode_db",
    "parse_pi_session_file",
    "parse_text_store_file",
    "parse_toml_summary_file",
    "parse_vscode_chat_session",
    "parse_vscode_inline_history",
    "plan_search_sources",
    "prefilter_sources_by_root",
    "print_find_results",
    "print_grep_results",
    "prompt_history_agents_for_sources",
    "read_json_file",
    "read_text_file",
    "record_dedupe_key",
    "record_matches_scope",
    "resolve_codex_sqlite_root",
    "resolve_env_root",
    "run_find_command",
    "run_find_query",
    "run_grep_command",
    "run_readonly_command",
    "run_search_command",
    "run_search_query",
    "run_ui",
    "run_ui_command",
    "search_record_sort_key",
    "search_sources",
    "select_backends",
    "serialize_find_record",
    "serialize_grep_record",
    "serialize_search_record",
    "serialize_source_handle",
    "should_enable_color",
    "should_enable_help_color",
    "source_matches_scope",
    "source_order_key",
    "sqlite_column_names",
    "sqlite_table_names",
    "store_descriptor_for_record",
    "store_role_for_record",
    "stream_find_results",
    "stream_grep_results",
    "truncate_lines",
    "which_first",
)

if __name__ == "__main__":
    raise SystemExit(main())
