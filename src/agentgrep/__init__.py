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

# Records, payloads, and shared vocabulary live in agentgrep.records (ADR 0008).
# Structural typing shims live in agentgrep._types (ADR 0008).
# Text-presentation helpers live in agentgrep._text (ADR 0008).
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

# Store parsers + record normalization live in agentgrep.adapters (ADR 0010).
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

# Store discovery lives in agentgrep.discovery (ADR 0010).
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

# Progress reporting lives in agentgrep.progress (ADR 0010).
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

# Low-level read-only I/O primitives live in agentgrep.readers (ADR 0010).
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


def search_sources(
    query: SearchQuery,
    sources: list[SourceHandle],
    backends: BackendSelection,
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
    runtime: SearchRuntime | None = None,
) -> list[SearchRecord]:
    """Parse and filter search results across all selected sources."""
    active_progress = noop_search_progress() if progress is None else progress
    active_control = SearchControl() if control is None else control
    # Apply the compiled-query source predicate before planning so the
    # ripgrep prefilter (which is the heavy step in
    # ``plan_search_sources``) runs on the smaller set. Without this
    # the per-file prefilter runs against every discovered source even
    # when ``agent:codex`` could rule most out from metadata alone.
    if query.compiled is not None and query.compiled.source_predicate is not None:
        sources = [s for s in sources if query.compiled.source_predicate(s)]
    from agentgrep._engine.planning import build_physical_search_plan

    plan = build_physical_search_plan(
        query,
        sources,
        backends,
        progress=active_progress,
        control=active_control,
    )
    if active_control.answer_now_requested():
        active_progress.answer_now(0)
        return []
    active_progress.sources_planned(len(plan.tasks), len(sources))
    records = collect_search_records_from_plan(
        query,
        plan,
        progress=active_progress,
        control=active_control,
        runtime=runtime,
    )
    if active_control.answer_now_requested():
        active_progress.answer_now(len(records))
    else:
        active_progress.finish(len(records))
    return records


def run_search_query(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    backends: BackendSelection | None = None,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
    runtime: SearchRuntime | None = None,
) -> list[SearchRecord]:
    """Discover sources and run a normalized search query."""
    active_backends = select_backends() if backends is None else backends
    active_progress = noop_search_progress() if progress is None else progress
    active_control = SearchControl() if control is None else control
    active_progress.start(query)
    interrupted = False
    try:
        sources = discover_sources_for_search(
            home,
            query,
            active_backends,
            version_detail="none",
        )
        active_progress.sources_discovered(len(sources))
        return search_sources(
            query,
            sources,
            active_backends,
            progress=active_progress,
            control=active_control,
            runtime=runtime,
        )
    except KeyboardInterrupt:
        interrupted = True
        active_progress.interrupt()
        raise
    finally:
        if not interrupted:
            active_progress.close()


def plan_search_sources(
    query: SearchQuery,
    sources: list[SourceHandle],
    backends: BackendSelection,
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
) -> list[SourceHandle]:
    """Return the candidate sources to parse for a search query."""
    from agentgrep._engine.planning import build_physical_search_plan

    plan = build_physical_search_plan(
        query,
        sources,
        backends,
        progress=progress,
        control=control,
    )
    return [task.source for task in plan.tasks]


def source_order_key(source: SourceHandle) -> tuple[int, str]:
    """Return a newest-first search order key for sources."""
    return (-source.mtime_ns, str(source.path))


def _source_profile_attributes(source: SourceHandle) -> dict[str, JSONScalar]:
    """Return privacy-safe profiler attributes for a source handle."""
    return {
        "agentgrep_agent": source.agent,
        "agentgrep_store": source.store,
        "agentgrep_adapter_id": source.adapter_id,
        "agentgrep_path_kind": source.path_kind,
        "agentgrep_source_kind": source.source_kind,
    }


def prefilter_sources_by_root(
    query: SearchQuery,
    sources: list[SourceHandle],
    grep_program: str,
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
) -> list[SourceHandle]:
    """Prefilter file-backed sources by searching each root once."""
    active_progress = noop_search_progress() if progress is None else progress
    active_control = SearchControl() if control is None else control
    matched_paths_by_root: dict[pathlib.Path, set[pathlib.Path] | None] = {}
    filtered_sources: list[SourceHandle] = []
    for source in sources:
        if active_control.answer_now_requested():
            break
        if source.source_kind == "sqlite":
            filtered_sources.append(source)
            continue
        search_root = source.search_root
        if search_root is None:
            filtered_sources.append(source)
            continue

        if search_root not in matched_paths_by_root:
            active_progress.prefilter_started(search_root)
            started_at = time.perf_counter()
            matched_paths_by_root[search_root] = grep_root_paths(
                search_root,
                query,
                grep_program,
                control=active_control,
            )
            matched_paths = matched_paths_by_root[search_root]
            _record_engine_profile_sample(
                "search.plan.prefilter_root",
                time.perf_counter() - started_at,
                # SQLite candidates bypass root prefiltering above, so they
                # do not count toward the sources this grep pass covers.
                agentgrep_source_count=sum(
                    1
                    for candidate in sources
                    if candidate.search_root == search_root and candidate.source_kind != "sqlite"
                ),
                agentgrep_matched_source_count=len(matched_paths)
                if matched_paths is not None
                else None,
                agentgrep_unknown=matched_paths is None,
            )
            if active_control.answer_now_requested():
                break

        matched_paths = matched_paths_by_root[search_root]
        if matched_paths is None or source.path in matched_paths:
            filtered_sources.append(source)
    return filtered_sources


def grep_root_paths(
    search_root: pathlib.Path,
    query: SearchQuery,
    grep_program: str,
    *,
    control: SearchControl | None = None,
) -> set[pathlib.Path] | None:
    """Return file paths matched by a whole-root grep."""
    active_control = SearchControl() if control is None else control
    matched_sets: list[set[pathlib.Path]] = []
    for term in query.terms:
        if active_control.answer_now_requested():
            return set()
        command = build_grep_command(
            grep_program,
            term,
            search_root,
            regex=query.regex,
            case_sensitive=query.case_sensitive,
        )
        completed = run_readonly_command(command, control=active_control)
        if active_control.answer_now_requested():
            return set()
        if completed.returncode not in {0, 1}:
            return None
        matched_sets.append(
            {pathlib.Path(line) for line in completed.stdout.splitlines() if line.strip()},
        )

    if not matched_sets:
        return set()
    if query.any_term:
        merged: set[pathlib.Path] = set()
        for matched in matched_sets:
            merged.update(matched)
        return merged

    intersection = matched_sets[0].copy()
    for matched in matched_sets[1:]:
        intersection.intersection_update(matched)
    return intersection


def direct_source_matches(
    source: SourceHandle,
    query: SearchQuery,
    backends: BackendSelection,
    control: SearchControl | None = None,
) -> bool:
    """Return whether a direct source should be parsed."""
    active_control = SearchControl() if control is None else control
    started_at = time.perf_counter()
    matched = False
    aborted = False
    if active_control.answer_now_requested():
        return False
    try:
        if query.compiled is not None and query.compiled.record_predicate is not None:
            # A compiled boolean/field query carries its own record
            # predicate; the flat-term text prefilter ANDs the terms and
            # would wrongly drop OR/NOT matches. Field-level source pruning
            # already ran via the compiled source_predicate during planning,
            # so admit and let the record matcher decide.
            matched = True
            return matched
        if source.adapter_id == "claude.history_jsonl.v1":
            # Claude history expands sibling paste-cache files into record
            # text, so a query term can match content that no grep over
            # history.jsonl itself can see. Admission must stay
            # unconditional; the record matcher filters after expansion.
            matched = True
            return matched
        if source.source_kind == "sqlite":
            matched = True
            return matched
        if backends.grep_tool is not None:
            grep_match = grep_file_matches(
                source.path,
                query,
                backends.grep_tool,
                control=active_control,
            )
            if active_control.answer_now_requested():
                aborted = True
                return False
            if grep_match is not None:
                matched = grep_match
                return matched
        if source.path.suffix in JSON_FILE_SUFFIXES and backends.json_tool is not None:
            extracted = flatten_json_strings_with_tool(
                source.path,
                backends.json_tool,
                control=active_control,
            )
            if active_control.answer_now_requested():
                aborted = True
                return False
            if extracted is not None:
                matched = matches_text(extracted, query)
                return matched
        matched = matches_text(read_text_file(source.path), query)
        return matched
    finally:
        # An answer-now abort is not a non-match; record nothing, matching
        # the pre-try early return above.
        if not aborted:
            _record_engine_profile_sample(
                "search.plan.direct_source",
                time.perf_counter() - started_at,
                **_source_profile_attributes(source),
                agentgrep_matched=matched,
            )


def collect_search_records(
    query: SearchQuery,
    sources: list[SourceHandle],
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
    runtime: SearchRuntime | None = None,
) -> list[SearchRecord]:
    """Parse candidate sources and collect matching records."""
    from agentgrep._engine.planning import (
        PhysicalSearchPlan,
        SourceTask,
        build_logical_search_plan,
    )

    plan = PhysicalSearchPlan(
        logical=build_logical_search_plan(query),
        tasks=tuple(
            SourceTask(
                source=source,
                strategy="direct_full_scan",
                record_order="unknown",
                limit_behavior="drain_source",
                can_stream_records=True,
                restore_order_key=source_order_key(source),
            )
            for source in sources
        ),
        decisions=(),
    )
    return collect_search_records_from_plan(
        query,
        plan,
        progress=progress,
        control=control,
        runtime=runtime,
    )


def collect_search_records_from_plan(
    query: SearchQuery,
    plan: PhysicalSearchPlan,
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
    runtime: SearchRuntime | None = None,
) -> list[SearchRecord]:
    """Execute a physical search plan and collect matching records.

    Parameters
    ----------
    query : SearchQuery
        Compiled query — terms, agents, dedup choice, limit.
    plan : PhysicalSearchPlan
        Planned source tasks from :func:`build_physical_search_plan`.
    progress : SearchProgress or None
        Progress sink for source and record events. ``None`` uses the
        no-op sink.
    control : SearchControl or None
        Optional control handle polled between records so consumers
        can stop the scan early.
    runtime : SearchRuntime or None
        Optional reusable runtime state; supplies the source-scan
        cache when one is configured.

    Returns
    -------
    list of SearchRecord
        Matching records sorted newest-first by
        :func:`search_record_sort_key`, truncated to ``query.limit``
        when set.
    """
    from agentgrep._engine.execution import ExecutionRecordEmitted, select_execution_driver

    results = [
        event.record
        for event in select_execution_driver(query, plan).iter_search_plan(
            query,
            plan,
            progress=progress,
            control=control,
            runtime=runtime,
        )
        if isinstance(event, ExecutionRecordEmitted)
    ]
    results.sort(key=search_record_sort_key, reverse=True)
    return results


def find_sources(
    pattern: str | None,
    sources: list[SourceHandle],
    limit: int | None,
) -> list[FindRecord]:
    """Build filtered ``find`` results from discovered sources."""
    query = pattern.casefold() if pattern is not None else None
    results: list[FindRecord] = []
    for source in sources:
        record = FindRecord(
            kind="find",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            path_kind=source.path_kind,
            metadata={"source_kind": source.source_kind},
        )
        if query is not None:
            haystack = " ".join(
                (
                    record.agent,
                    record.store,
                    record.adapter_id,
                    str(record.path),
                    record.path_kind,
                ),
            ).casefold()
            if query not in haystack:
                continue
        results.append(record)
        if limit is not None and len(results) >= limit:
            break
    return results


def run_find_query(
    home: pathlib.Path,
    agents: tuple[AgentName, ...],
    *,
    pattern: str | None,
    limit: int | None,
    backends: BackendSelection | None = None,
) -> list[FindRecord]:
    """Discover sources and build normalized ``find`` results."""
    active_backends = select_backends() if backends is None else backends
    sources = discover_sources(home, agents, active_backends, version_detail="none")
    return find_sources(pattern, sources, limit)


def build_grep_command(
    grep_program: str,
    term: str,
    target: pathlib.Path,
    *,
    regex: bool,
    case_sensitive: bool,
) -> list[str]:
    """Build a read-only grep command for one term and target.

    Always passes flags that disable ignore-file semantics — agent stores live
    inside the user's ``$HOME`` and may sit beneath a ``.gitignore`` from a
    dotfile manager (yadm, chezmoi, stow, bare-git). The grep tools would
    otherwise silently skip everything.
    """
    if grep_program.endswith("rg"):
        ignore_flags = ["--no-ignore", "--hidden"]
        fixed_flag = "-F"
    else:
        ignore_flags = ["--unrestricted", "--hidden"]
        fixed_flag = "-Q"
    command = [grep_program, *ignore_flags, "-l", term, str(target)]
    if not regex:
        command.insert(command.index("-l"), fixed_flag)
    if not case_sensitive:
        command.insert(1, "-i")
    return command


def flatten_json_strings_with_tool(
    path: pathlib.Path,
    program: str,
    *,
    control: SearchControl | None = None,
) -> str | None:
    """Return flattened JSON strings using ``jq`` or ``jaq``."""
    command = [program, "-r", ".. | strings", str(path)]
    completed = run_readonly_command(command, control=control)
    if completed.returncode != 0:
        return None
    return completed.stdout


def grep_file_matches(
    path: pathlib.Path,
    query: SearchQuery,
    program: str,
    *,
    control: SearchControl | None = None,
) -> bool | None:
    """Use ``rg`` or ``ag`` as a read-only prefilter."""
    active_control = SearchControl() if control is None else control
    matchers = [
        run_readonly_command(
            build_grep_command(
                program,
                term,
                path,
                regex=query.regex,
                case_sensitive=query.case_sensitive,
            ),
            control=active_control,
        ).returncode
        == 0
        for term in query.terms
        if not active_control.answer_now_requested()
    ]
    if active_control.answer_now_requested():
        return False
    return any(matchers) if query.any_term else all(matchers)


def record_matches_scope(record: SearchRecord, scope: SearchScope) -> bool:
    """Return whether ``record`` belongs to the requested search scope."""
    if scope == "all":
        return True
    if scope == "prompts":
        return record.kind == "prompt"
    role = store_role_for_record(record.store, record.adapter_id)
    return role in CONVERSATION_STORE_ROLES


def prompt_history_agents_for_sources(sources: cabc.Iterable[SourceHandle]) -> frozenset[str]:
    """Return agents with a dedicated prompt-history source in ``sources``."""
    return frozenset(
        source.agent
        for source in sources
        if store_role_for_record(source.store, source.adapter_id) == StoreRole.PROMPT_HISTORY
    )


def discover_sources_for_search(
    home: pathlib.Path,
    query: SearchQuery,
    backends: BackendSelection,
    *,
    version_detail: DiscoveryVersionDetail = "none",
) -> list[SourceHandle]:
    """Discover only the source roles needed for a search query scope."""
    from agentgrep._engine.planning import build_logical_search_plan

    logical_plan = build_logical_search_plan(query)
    if query.scope == "all":
        return discover_sources(
            home,
            query.agents,
            backends,
            version_detail=version_detail,
        )
    if query.scope == "conversations":
        return discover_sources(
            home,
            query.agents,
            backends,
            version_detail=version_detail,
            store_roles=logical_plan.initial_store_roles,
        )

    prompt_sources = discover_sources(
        home,
        query.agents,
        backends,
        version_detail=version_detail,
        store_roles=logical_plan.initial_store_roles,
    )
    agents_with_prompt_history = frozenset(
        source.agent
        for source in prompt_sources
        if store_role_for_record(source.store, source.adapter_id) == StoreRole.PROMPT_HISTORY
    )
    fallback_agents = tuple(
        agent for agent in query.agents if agent not in agents_with_prompt_history
    )
    if not fallback_agents:
        return prompt_sources

    sources = [
        *prompt_sources,
        *discover_sources(
            home,
            fallback_agents,
            backends,
            version_detail=version_detail,
            store_roles=CONVERSATION_STORE_ROLES,
        ),
    ]
    deduped: list[SourceHandle] = []
    seen: set[tuple[AgentName, str, str, pathlib.Path]] = set()
    for source in sources:
        key = (source.agent, source.store, source.adapter_id, source.path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(source)
    return deduped


def source_matches_scope(
    source: SourceHandle,
    scope: SearchScope,
    *,
    prompt_history_agents: frozenset[str] = frozenset(),
) -> bool:
    """Return whether ``source`` can yield records for the requested scope."""
    if scope == "all":
        return True
    role = store_role_for_record(source.store, source.adapter_id)
    if scope == "conversations":
        return role in CONVERSATION_STORE_ROLES
    if role == StoreRole.PROMPT_HISTORY:
        return True
    if role in CONVERSATION_STORE_ROLES:
        return source.agent not in prompt_history_agents
    return True


def matches_record(record: SearchRecord, query: SearchQuery) -> bool:
    """Return whether a normalized record should be included.

    When ``query.compiled`` carries a record-level predicate, the
    record must satisfy it in addition to the existing text + scope
    checks. Pure-text queries skip the predicate evaluation since
    the compiler leaves ``compiled = None`` for them.
    """
    from agentgrep._engine.matching import matches_record as compiled_matches_record

    return compiled_matches_record(record, query)


def build_record_match_surface(record: SearchRecord, surface: SearchMatchSurface) -> str:
    """Build the text surface used for unfielded query terms."""
    if surface == "text":
        return record.text
    return build_search_haystack(record)


def build_search_haystack(record: SearchRecord) -> str:
    """Build a searchable text surface for a record."""
    parts = [
        record.title or "",
        record.text,
        record.model or "",
        record.role or "",
        str(record.path),
    ]
    return "\n".join(part for part in parts if part)


_HAYSTACK_CACHE: dict[int, str] = {}


def cached_haystack(record: SearchRecord) -> str:
    """Return the casefolded haystack for ``record``, memoized by ``id``.

    The filter worker scans every loaded record on every keystroke;
    recomputing ``build_search_haystack(...).casefold()`` per record per
    pass dominates filter latency once the result set grows past a few
    thousand records. Memoizing by ``id`` is safe because the app
    retains every record in ``AgentGrepApp.all_records`` for the
    lifetime of one search, so Python cannot recycle a collected
    record's id while its entry sits in :data:`_HAYSTACK_CACHE`.

    Callers that need to invalidate (because a new search will allocate
    new records) should call :func:`clear_haystack_cache`.
    """
    key = id(record)
    cached = _HAYSTACK_CACHE.get(key)
    if cached is None:
        cached = build_search_haystack(record).casefold()
        _HAYSTACK_CACHE[key] = cached
    return cached


def clear_haystack_cache() -> None:
    """Drop every memoized haystack — call before allocating a new record set."""
    _HAYSTACK_CACHE.clear()


def compute_filter_matches(
    records: cabc.Sequence[SearchRecord],
    text: str,
) -> tuple[SearchRecord, ...]:
    """Return the subset of ``records`` whose haystack contains ``text`` (case-fold).

    Used by the TUI's filter worker. Pure function so the filter logic is
    directly unit-testable without spinning up a Textual app.

    Parameters
    ----------
    records : Sequence[SearchRecord]
        Records to test.
    text : str
        Filter text. Whitespace-trimmed and case-folded before matching.
        An empty (or whitespace-only) ``text`` returns all records.

    Returns
    -------
    tuple[SearchRecord, ...]
        Matching records in input order.
    """
    normalized = text.strip().casefold()
    if not normalized:
        return tuple(records)
    return tuple(record for record in records if normalized in cached_haystack(record))


def matches_text(text: str, query: SearchQuery) -> bool:
    """Return whether ``text`` matches the query."""
    if not query.terms:
        return True
    if query.regex:
        flags = 0 if query.case_sensitive else re.IGNORECASE
        results = [re.search(term, text, flags) is not None for term in query.terms]
    else:
        haystack = text if query.case_sensitive else text.casefold()
        needles = (
            query.terms if query.case_sensitive else tuple(term.casefold() for term in query.terms)
        )
        results = [needle in haystack for needle in needles]
    return any(results) if query.any_term else all(results)


def search_record_sort_key(record: SearchRecord) -> tuple[str, str, str]:
    """Return a stable sort key."""
    return (record.timestamp or "", record.agent, str(record.path))


def record_dedupe_key(record: SearchRecord) -> tuple[str, str, str, str, str]:
    """Return the per-session dedupe key for a search record."""
    session_identity = record.session_id or record.conversation_id or str(record.path)
    return (
        record.kind,
        record.agent,
        record.store,
        session_identity,
        record.text,
    )


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
    try:
        parsed = parse_args(argv)
        if parsed is None:
            return 0
        if isinstance(parsed, GrepArgs):
            return run_grep_command(parsed)
        if isinstance(parsed, SearchArgs):
            return run_search_command(parsed)
        if isinstance(parsed, UIArgs):
            return run_ui_command(parsed)
        return run_find_command(parsed)
    except KeyboardInterrupt:
        _write_interrupt_notice()
        _exit_on_sigint()


from agentgrep._engine import (  # noqa: E402  (re-exports must follow main definition)
    SearchRuntime,
    SourceScanCache,
    SourceScanCacheStats,
    aiter_search_events,
    iter_find_events,
    iter_search_events,
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

if __name__ == "__main__":
    raise SystemExit(main())
