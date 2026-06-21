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
    as_optional_str,
    decode_sqlite_value,
    file_mtime_ns,
    isoformat_from_mtime_ns,
    iter_conversation_summaries,
    iter_jsonl,
    iter_key_value_rows,
    iter_protobuf_text_fields,
    open_readonly_sqlite,
    parse_embedded_json,
    read_json_file,
    read_text_file,
    sqlite_column_names,
    sqlite_table_names,
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


def select_backends() -> BackendSelection:
    """Return the best available subprocess helpers."""
    return BackendSelection(
        find_tool=which_first(("fd", "fdfind")),
        grep_tool=which_first(("rg", "ag")),
        json_tool=which_first(("jq", "jaq")),
    )


def which_first(names: tuple[str, ...]) -> str | None:
    """Return the first executable available on ``PATH``."""
    for name in names:
        found = shutil.which(name)
        if found is not None:
            return found
    return None


def run_readonly_command(
    command: list[str],
    *,
    control: SearchControl | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell and capture text output."""
    started_at = time.perf_counter()
    if control is None:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        _record_readonly_command_profile(command, started_at, completed)
        return completed
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.05)
        except subprocess.TimeoutExpired:
            if control.answer_now_requested():
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                completed = subprocess.CompletedProcess(
                    command,
                    process.returncode,
                    stdout,
                    stderr,
                )
                _record_readonly_command_profile(command, started_at, completed)
                return completed
            continue
        completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        _record_readonly_command_profile(command, started_at, completed)
        return completed


def _record_readonly_command_profile(
    command: list[str],
    started_at: float,
    completed: subprocess.CompletedProcess[str],
) -> None:
    """Record optional engine profiling metadata for a completed subprocess."""
    if "agentgrep._engine.profiling" not in sys.modules:
        return
    from agentgrep._engine.profiling import record_subprocess_run

    record_subprocess_run(
        command,
        duration_seconds=time.perf_counter() - started_at,
        completed=completed,
    )


def _record_engine_profile_sample(
    name: str,
    duration_seconds: float,
    **attributes: JSONScalar,
) -> None:
    """Record an optional engine profile sample when profiling is active."""
    if "agentgrep._engine.profiling" not in sys.modules:
        return
    from agentgrep._engine.profiling import current_engine_profiler

    profiler = current_engine_profiler()
    if profiler is None:
        return
    profiler.record(name, duration_seconds, **attributes)


def discover_sources(
    home: pathlib.Path,
    agents: tuple[AgentName, ...],
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover all known parseable sources for the selected agents.

    ``version_detail`` controls how eagerly source handles are enriched:
    ``"none"`` leaves ``version_detection`` empty for fast search paths,
    ``"catalog"`` attaches low-cost catalog observations, and ``"shape"``
    inspects concrete source shape for inventory surfaces. ``store_roles``
    lets latency-sensitive search paths enumerate only the catalogue roles
    that can satisfy a coarse query scope.
    """
    discovered: list[SourceHandle] = []
    for agent in agents:
        if agent == "codex":
            discovered.extend(
                discover_codex_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "claude":
            discovered.extend(
                discover_claude_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "cursor-cli":
            discovered.extend(
                discover_cursor_cli_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "cursor-ide":
            discovered.extend(
                discover_cursor_ide_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "gemini":
            discovered.extend(
                discover_gemini_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "antigravity-cli":
            discovered.extend(
                discover_antigravity_cli_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "antigravity-ide":
            discovered.extend(
                discover_antigravity_ide_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "grok":
            discovered.extend(
                discover_grok_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "pi":
            discovered.extend(
                discover_pi_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "opencode":
            discovered.extend(
                discover_opencode_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "vscode":
            discovered.extend(
                discover_vscode_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
    discovered.sort(key=lambda item: (item.agent, item.store, str(item.path)))
    return discovered


def resolve_env_root(env_var: str, default: pathlib.Path) -> pathlib.Path:
    """Resolve a base directory from an environment variable, with safety.

    When ``env_var`` is set to a non-empty path that is an existing directory,
    return that path. When it is set but points to a non-existent or
    non-directory location, emit a ``WARNING`` log and fall back to
    ``default``. When unset or empty, return ``default``.

    Parameters
    ----------
    env_var : str
        Environment variable name (e.g. ``"CODEX_HOME"``).
    default : pathlib.Path
        Fallback path when the env var is unset, empty, or unusable.

    Returns
    -------
    pathlib.Path
        Resolved base directory.
    """
    value = os.environ.get(env_var)
    if not value:
        return default
    candidate = pathlib.Path(value)
    if candidate.is_dir():
        return candidate
    status = "not_a_directory" if candidate.exists() else "not_found"
    logger.warning(
        "env-override path unavailable, fell back to default",
        extra={
            "agentgrep_env_var": env_var,
            "agentgrep_env_path": value,
            "agentgrep_env_path_status": status,
        },
    )
    return default


def _resolve_optional_root(value: str | None, default: pathlib.Path, *, label: str) -> pathlib.Path:
    """Resolve an optional path override, warning and falling back on bad paths."""
    if not value:
        return default
    candidate = pathlib.Path(os.path.expandvars(value)).expanduser()
    if candidate.is_dir():
        return candidate
    status = "not_a_directory" if candidate.exists() else "not_found"
    logger.warning(
        "path override unavailable, fell back to default",
        extra={
            "agentgrep_override_label": label,
            "agentgrep_override_path": value,
            "agentgrep_override_path_status": status,
        },
    )
    return default


def _codex_sqlite_home_from_config(codex_root: pathlib.Path) -> str | None:
    """Return Codex's configured ``sqlite_home`` value when present."""
    config_path = codex_root / "config.toml"
    if not config_path.is_file():
        return None
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning(
            "codex config parse failed",
            extra={
                "agentgrep_path": str(config_path),
                "agentgrep_error": type(exc).__name__,
            },
        )
        return None
    value = payload.get("sqlite_home")
    return value if isinstance(value, str) else None


def resolve_codex_sqlite_root(codex_root: pathlib.Path) -> pathlib.Path:
    """Resolve Codex's SQLite root from env/config, falling back to ``CODEX_HOME``."""
    env_value = os.environ.get("CODEX_SQLITE_HOME")
    if env_value:
        return _resolve_optional_root(env_value, codex_root, label="CODEX_SQLITE_HOME")
    return _resolve_optional_root(
        _codex_sqlite_home_from_config(codex_root),
        codex_root,
        label="sqlite_home",
    )


def _first_jsonl_mapping(path: pathlib.Path) -> dict[str, JSONValue] | None:
    """Return the first object record from a JSONL file."""
    for value in iter_jsonl(path):
        if isinstance(value, dict):
            return value
    return None


def _first_json_array_mapping(path: pathlib.Path) -> dict[str, JSONValue] | None:
    """Return the first object from a JSON array file."""
    value = read_json_file(path)
    if not isinstance(value, list):
        return None
    for entry in value:
        if isinstance(entry, dict):
            return entry
    return None


def _json_mapping(path: pathlib.Path) -> dict[str, JSONValue] | None:
    """Return a JSON file payload when its top-level value is an object."""
    value = read_json_file(path)
    return value if isinstance(value, dict) else None


def _safe_project_root(value: object) -> pathlib.Path | None:
    """Return a usable project root from session metadata."""
    if not isinstance(value, str) or not value:
        return None
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        return None
    return path


def _project_roots_from_jsonl_sessions(
    session_root: pathlib.Path,
    backends: BackendSelection,
) -> tuple[pathlib.Path, ...]:
    """Derive known project roots from session metadata JSONL files."""
    if not session_root.exists():
        return ()
    roots: set[pathlib.Path] = set()
    for path in list_files_matching(session_root, "*.jsonl", backends.find_tool):
        if "subagents" in path.parts:
            continue
        for index, record in enumerate(iter_jsonl(path)):
            if not isinstance(record, dict):
                if index >= 31:
                    break
                continue
            mapping = t.cast("dict[str, object]", record)
            payload = mapping.get("payload")
            candidates = [mapping.get("cwd"), mapping.get("project")]
            if isinstance(payload, dict):
                payload_mapping = t.cast("dict[str, object]", payload)
                candidates.extend((payload_mapping.get("cwd"), payload_mapping.get("project")))
            found_root = False
            for candidate in candidates:
                root = _safe_project_root(candidate)
                if root is not None:
                    roots.add(root)
                    found_root = True
                    break
            if found_root or index >= 31:
                break
    return tuple(sorted(roots))


def _codex_project_roots_from_legacy_sessions(
    session_root: pathlib.Path,
    backends: BackendSelection,
) -> tuple[pathlib.Path, ...]:
    """Derive known project roots from legacy Codex JSON session files."""
    if not session_root.exists():
        return ()
    roots: set[pathlib.Path] = set()
    for path in list_files_matching(session_root, "rollout-*.json", backends.find_tool):
        payload = read_json_file(path)
        if not isinstance(payload, dict):
            continue
        session = payload.get("session")
        if not isinstance(session, dict):
            continue
        mapping = t.cast("dict[str, object]", session)
        root = _safe_project_root(mapping.get("cwd") or mapping.get("project"))
        if root is not None:
            roots.add(root)
    return tuple(sorted(roots))


def _claude_project_roots(
    root: pathlib.Path,
    backends: BackendSelection,
) -> tuple[pathlib.Path, ...]:
    """Return project roots Claude Code has already referenced in transcripts."""
    return _project_roots_from_jsonl_sessions(root / "projects", backends)


def _codex_project_roots(
    root: pathlib.Path,
    backends: BackendSelection,
) -> tuple[pathlib.Path, ...]:
    """Return project roots Codex has already referenced in transcripts."""
    session_root = root / "sessions"
    return tuple(
        sorted(
            {
                *_project_roots_from_jsonl_sessions(session_root, backends),
                *_codex_project_roots_from_legacy_sessions(session_root, backends),
            },
        ),
    )


def _codex_client_version_from_cache(codex_root: pathlib.Path | None) -> str | None:
    """Return Codex's local client-version hint without spawning the CLI."""
    if codex_root is None:
        return None
    value = read_json_file(codex_root / "models_cache.json")
    if not isinstance(value, dict):
        return None
    return as_optional_str(value.get("client_version"))


def _catalog_version_detection(
    descriptor: StoreDescriptor,
    spec: DiscoverySpec,
    *,
    app_version: str | None = None,
) -> SourceVersionDetection:
    """Build the low-confidence fallback for sources without shape evidence."""
    return SourceVersionDetection(
        app_version=app_version,
        data_version=spec.data_version,
        strategy=VersionDetectionStrategy.CATALOG_OBSERVATION,
        confidence=VersionDetectionConfidence.LOW,
        evidence=f"catalog observed_version: {descriptor.observed_version}",
    )


def _codex_source_version_detection(
    source: SourceHandle,
    descriptor: StoreDescriptor,
    spec: DiscoverySpec,
    context: DiscoveryVersionContext,
) -> SourceVersionDetection:
    """Detect Codex source versions from local metadata and concrete shape."""
    app_version = context.codex_client_version

    if source.adapter_id == "codex.history_jsonl.v1":
        record = _first_jsonl_mapping(source.path)
        if record is not None and {"session_id", "ts", "text"}.issubset(record):
            return SourceVersionDetection(
                app_version=app_version,
                data_version="codex.history_jsonl.current",
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="history.jsonl object keys include session_id, ts, text",
            )
    elif source.adapter_id == "codex.history_json.v1":
        record = _first_json_array_mapping(source.path)
        if record is not None and {"command", "timestamp"}.issubset(record):
            return SourceVersionDetection(
                app_version=app_version,
                data_version="codex.history_json.legacy",
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="history.json array object keys include command, timestamp",
            )
    elif source.adapter_id == "codex.sessions_jsonl.v1":
        record = _first_jsonl_mapping(source.path)
        if record is not None and record.get("type") == "session_meta":
            payload = record.get("payload")
            embedded_version: str | None = None
            if isinstance(payload, dict):
                embedded_version = as_optional_str(payload.get("cli_version"))
            if embedded_version:
                return SourceVersionDetection(
                    app_version=embedded_version,
                    data_version=spec.data_version,
                    strategy=VersionDetectionStrategy.EMBEDDED_METADATA,
                    confidence=VersionDetectionConfidence.HIGH,
                    evidence="session_meta.payload keys include cli_version",
                )
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="jsonl event type includes session_meta",
            )
    elif source.adapter_id == "codex.sessions_legacy_json.v1":
        record = _json_mapping(source.path)
        if record is not None and {"session", "items"}.issubset(record):
            return SourceVersionDetection(
                app_version=app_version,
                data_version="codex.sessions.legacy_json.v1",
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="legacy session JSON object keys include session, items",
            )
    elif source.adapter_id == "codex.session_index_jsonl.v1":
        record = _first_jsonl_mapping(source.path)
        if record is not None and {"id", "thread_name", "updated_at"}.issubset(record):
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="session_index.jsonl object keys include id, thread_name, updated_at",
            )
    elif source.adapter_id == "codex.external_imports_json.v1":
        record = _json_mapping(source.path)
        if record is not None and "records" in record:
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="external import ledger object key includes records",
            )
    elif source.adapter_id == "codex.memories_text.v1":
        return SourceVersionDetection(
            app_version=app_version,
            data_version=spec.data_version,
            strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
            confidence=VersionDetectionConfidence.MEDIUM,
            evidence="markdown memory file discovered under memories",
        )
    elif source.adapter_id in {
        "codex.config_toml.v1",
        "codex.config_backup_toml.v1",
        "codex.project_config_toml.v1",
    }:
        try:
            payload = tomllib.loads(source.path.read_text(encoding="utf-8"))
        except OSError, tomllib.TOMLDecodeError:
            payload = {}
        if payload:
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="TOML top-level keys observed",
            )
    elif source.adapter_id == "codex.app_state_json_summary.v1":
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="app-state JSON object keys observed",
            )
    elif source.adapter_id == "codex.plugin_manifest_json.v1":
        record = _json_mapping(source.path)
        if record is not None and {"name", "description"}.intersection(record):
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="plugin manifest JSON object keys observed",
            )
    elif source.adapter_id in {
        "codex.hooks_json.v1",
        "codex.plugin_hooks_json.v1",
        "codex.plugin_marketplace_json.v1",
    }:
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="JSON object keys observed for Codex hook or plugin metadata",
            )
    elif source.adapter_id in {
        "codex.plugin_instruction_text.v1",
        "codex.project_skill_text.v1",
        "codex.rules_text.v1",
        "codex.skills_text.v1",
    }:
        return SourceVersionDetection(
            app_version=app_version,
            data_version=spec.data_version,
            strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
            confidence=VersionDetectionConfidence.MEDIUM,
            evidence="instruction text file discovered for Codex",
        )
    elif source.adapter_id == "codex.file_metadata_summary.v1":
        return SourceVersionDetection(
            app_version=app_version,
            data_version=spec.data_version,
            strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
            confidence=VersionDetectionConfidence.LOW,
            evidence="metadata-only raw state file observed",
        )
    elif source.source_kind == "sqlite" and spec.data_version is not None:
        match = re.fullmatch(r".+_([0-9]+)\.sqlite", source.path.name)
        if match is not None:
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence=f"filename suffix _{match.group(1)}.sqlite",
            )

    return _catalog_version_detection(descriptor, spec, app_version=app_version)


def _claude_source_version_detection(
    source: SourceHandle,
    descriptor: StoreDescriptor,
    spec: DiscoverySpec,
) -> SourceVersionDetection:
    """Detect Claude Code source versions from embedded metadata and shape."""
    if source.adapter_id == "claude.history_jsonl.v1":
        record = _first_jsonl_mapping(source.path)
        if record is not None and {"display", "timestamp", "project"}.issubset(record):
            return SourceVersionDetection(
                app_version=None,
                data_version="claude.history_jsonl.log_entry.v1",
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="history.jsonl object keys include display, timestamp, project",
            )
    elif source.adapter_id == "claude.projects_jsonl.v1":
        record = _first_jsonl_mapping(source.path)
        if record is not None:
            app_version = as_optional_str(record.get("version")) or as_optional_str(
                record.get("claude_code_version"),
            )
            if app_version:
                return SourceVersionDetection(
                    app_version=app_version,
                    data_version=spec.data_version,
                    strategy=VersionDetectionStrategy.EMBEDDED_METADATA,
                    confidence=VersionDetectionConfidence.HIGH,
                    evidence="project transcript keys include version",
                )
            if {"type", "sessionId", "message"}.issubset(record):
                return SourceVersionDetection(
                    app_version=None,
                    data_version=spec.data_version,
                    strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                    confidence=VersionDetectionConfidence.MEDIUM,
                    evidence="project transcript keys include type, sessionId, message",
                )
    elif source.adapter_id == "claude.tasks_json.v1":
        record = _json_mapping(source.path)
        if record is not None and {"id", "subject", "description", "status"}.issubset(record):
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="task JSON object keys include id, subject, description, status",
            )
    elif source.adapter_id == "claude.settings_json.v1":
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="settings JSON object keys observed",
            )
    elif source.adapter_id == "claude.todos_json.v1":
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="todo JSON object keys observed",
            )
    elif source.adapter_id == "claude.teams_json.v1":
        record = _json_mapping(source.path)
        if record is not None and {"name", "members"}.issubset(record):
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="team config JSON object keys include name, members",
            )
    elif source.adapter_id == "claude.app_state_json_summary.v1":
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="app-state JSON object keys observed",
            )
    elif source.adapter_id in {
        "claude.plugin_hooks_json.v1",
        "claude.plugin_manifest_json.v1",
    }:
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="plugin JSON object keys observed",
            )
    elif source.adapter_id in {
        "claude.commands_text.v1",
        "claude.memory_text.v1",
        "claude.plugin_instruction_text.v1",
        "claude.project_instruction_text.v1",
        "claude.projects_memory_text.v1",
        "claude.session_memory_text.v1",
        "claude.skills_text.v1",
    }:
        return SourceVersionDetection(
            app_version=None,
            data_version=spec.data_version,
            strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
            confidence=VersionDetectionConfidence.MEDIUM,
            evidence="instruction or memory text file discovered for Claude",
        )
    elif source.adapter_id == "claude.file_metadata_summary.v1":
        return SourceVersionDetection(
            app_version=None,
            data_version=spec.data_version,
            strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
            confidence=VersionDetectionConfidence.LOW,
            evidence="metadata-only raw state file observed",
        )

    return _catalog_version_detection(descriptor, spec)


def detect_source_version(
    source: SourceHandle,
    descriptor: StoreDescriptor,
    spec: DiscoverySpec,
    context: DiscoveryVersionContext,
) -> SourceVersionDetection:
    """Detect concrete source version metadata for discovery payloads."""
    if source.agent == "codex":
        return _codex_source_version_detection(source, descriptor, spec, context)
    if source.agent == "claude":
        return _claude_source_version_detection(source, descriptor, spec)
    return _catalog_version_detection(descriptor, spec)


def build_discovery_version_context(
    agent: AgentName,
    primary_roots: dict[str, pathlib.Path],
    version_detail: DiscoveryVersionDetail,
) -> DiscoveryVersionContext:
    """Build cached version metadata for a single discovery pass."""
    codex_client_version: str | None = None
    if agent == "codex" and version_detail != "none":
        codex_client_version = _codex_client_version_from_cache(primary_roots.get("default"))
    return DiscoveryVersionContext(codex_client_version=codex_client_version)


def handles_from_discovery(
    spec: DiscoverySpec,
    agent: AgentName,
    root: pathlib.Path,
    backends: BackendSelection,
    coverage: StoreCoverage,
) -> list[SourceHandle]:
    """Produce ``SourceHandle``s from a :class:`DiscoverySpec`.

    Applies the spec's ``home_subpath`` under ``root`` to derive the search
    root, then enumerates source files via ``files`` (single-file lookups),
    ``glob`` (recursive walk with optional path-part filters), and
    ``platform_paths`` (absolute paths).
    """
    sources: list[SourceHandle] = []
    search_root = root.joinpath(*spec.home_subpath) if spec.home_subpath else root

    for name in spec.files:
        candidate = search_root / name
        if candidate.is_file():
            sources.append(
                SourceHandle(
                    agent=agent,
                    store=spec.store,
                    adapter_id=spec.adapter_id,
                    path=candidate,
                    path_kind=spec.path_kind,
                    source_kind=spec.source_kind,
                    search_root=None,
                    mtime_ns=file_mtime_ns(candidate),
                    coverage=coverage,
                ),
            )

    if spec.glob is not None and search_root.exists():
        required_parts = set(spec.path_parts_required)
        excluded_parts = set(spec.path_parts_excluded)
        for path in list_files_matching(search_root, spec.glob, backends.find_tool):
            if required_parts and not required_parts.issubset(path.parts):
                continue
            if excluded_parts and excluded_parts.intersection(path.parts):
                continue
            sources.append(
                SourceHandle(
                    agent=agent,
                    store=spec.store,
                    adapter_id=spec.adapter_id,
                    path=path,
                    path_kind=spec.path_kind,
                    source_kind=spec.source_kind,
                    search_root=search_root,
                    mtime_ns=file_mtime_ns(path),
                    coverage=coverage,
                ),
            )

    for absolute_path_str in spec.platform_paths:
        candidate = pathlib.Path(absolute_path_str).expanduser()
        if candidate.is_file():
            sources.append(
                SourceHandle(
                    agent=agent,
                    store=spec.store,
                    adapter_id=spec.adapter_id,
                    path=candidate,
                    path_kind=spec.path_kind,
                    source_kind=spec.source_kind,
                    search_root=None,
                    mtime_ns=file_mtime_ns(candidate),
                    coverage=coverage,
                ),
            )

    return sources


def format_timestamp_tig(value: str | None) -> str:
    """Render an ISO-8601 timestamp as ``YYYY-MM-DD HH:MM ±HHMM`` (tig style).

    Localizes to the system timezone before formatting so the displayed
    time matches what the user expects to see — tig's main view does the
    same. Returns ``""`` for ``None`` / empty input and a clipped raw
    string for unparseable input so callers can pad consistently.

    Examples
    --------
    >>> format_timestamp_tig(None)
    ''
    >>> format_timestamp_tig("")
    ''
    >>> # An ISO timestamp with explicit timezone — formatted result keeps
    >>> # the offset for the system's local timezone (whose exact value
    >>> # varies by host, so we just check shape here).
    >>> sample = format_timestamp_tig("2026-05-17T11:59:12+00:00")
    >>> len(sample)
    22
    >>> sample[4], sample[7], sample[10], sample[13], sample[16]
    ('-', '-', ' ', ':', ' ')
    >>> format_timestamp_tig("not-a-real-timestamp")
    'not-a-real-timestamp'
    """
    if not value:
        return ""
    candidate = value.replace("Z", "+00:00")
    try:
        moment = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        return value[:22]
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %z")


def discover_from_catalog(
    home: pathlib.Path,
    agent: AgentName,
    base: pathlib.Path | dict[str, DiscoveryRoot],
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Walk every catalogue row for ``agent`` and emit ``SourceHandle``s.

    Each row's :class:`agentgrep.stores.DiscoverySpec` entries drive
    enumeration via :func:`handles_from_discovery`. Named roots may point to
    one directory or a bounded tuple of known project directories. Rows whose
    ``discovery`` tuple is empty are documentary-only and contribute no sources.
    ``DEFAULT_SEARCH`` rows are emitted by default. Inventory callers can
    set ``include_non_default`` to include ``INSPECTABLE`` and
    ``CATALOG_ONLY`` rows that carry discovery specs. ``PRIVATE`` rows are
    never enumerated from disk. ``version_detail`` lets latency-sensitive
    callers skip source-version enrichment until a metadata-rich surface asks
    for it. ``store_roles`` restricts enumeration before any filesystem walk,
    which lets search avoid stores its scope cannot consume.
    """
    from agentgrep.store_catalog import CATALOG

    roots: dict[str, DiscoveryRoot] = {"default": base} if isinstance(base, pathlib.Path) else base
    primary_roots: dict[str, pathlib.Path] = {}
    for key, value in roots.items():
        if isinstance(value, pathlib.Path):
            primary_roots[key] = value
        elif value:
            primary_roots[key] = value[0]
    version_context = build_discovery_version_context(agent, primary_roots, version_detail)
    sources: list[SourceHandle] = []
    for descriptor in CATALOG.for_agent(agent):
        coverage = descriptor.coverage_level
        if coverage is StoreCoverage.PRIVATE:
            continue
        if store_roles is not None and descriptor.role not in store_roles:
            continue
        if coverage is not StoreCoverage.DEFAULT_SEARCH and not include_non_default:
            continue
        # Per-descriptor dedup: a row whose discovery tuple has more than one
        # spec (e.g. Cursor IDE state.vscdb with both modern platform_paths
        # and a legacy ~/.cursor glob) must not yield the same file twice
        # under different adapter ids on layouts where both specs match.
        seen_paths: set[pathlib.Path] = set()
        for spec in descriptor.discovery:
            root_value = roots.get(spec.root_key)
            if root_value is None:
                continue
            root_paths = root_value if isinstance(root_value, tuple) else (root_value,)
            for root in root_paths:
                for handle in handles_from_discovery(spec, agent, root, backends, coverage):
                    if handle.path in seen_paths:
                        continue
                    seen_paths.add(handle.path)
                    if version_detail == "catalog":
                        handle.version_detection = _catalog_version_detection(
                            descriptor,
                            spec,
                            app_version=version_context.codex_client_version
                            if agent == "codex"
                            else None,
                        )
                    elif version_detail == "shape":
                        handle.version_detection = detect_source_version(
                            handle,
                            descriptor,
                            spec,
                            version_context,
                        )
                    sources.append(handle)
    return sources


def discover_codex_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Codex sessions and command history.

    Honours the ``CODEX_HOME`` environment variable (see upstream
    ``codex-rs/utils/home-dir/src/lib.rs``); falls back to ``${HOME}/.codex``
    when unset or empty. Path roots, globs, file lists, and adapter metadata
    come from the ``codex.*`` rows of
    :data:`agentgrep.store_catalog.CATALOG`.
    """
    root = resolve_env_root("CODEX_HOME", home / ".codex")
    if not root.exists():
        return []
    sqlite_root = resolve_codex_sqlite_root(root)
    roots: dict[str, DiscoveryRoot] = {"default": root, "codex_sqlite": sqlite_root}
    if include_non_default:
        roots["codex_project"] = _codex_project_roots(root, backends)
    return discover_from_catalog(
        home,
        "codex",
        roots,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_claude_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Claude Code project session files.

    Honours ``CLAUDE_CONFIG_DIR`` and otherwise falls back to
    ``${HOME}/.claude``. Path roots, globs, and adapter metadata come from
    the ``claude.*`` rows of :data:`agentgrep.store_catalog.CATALOG`.
    """
    root = resolve_env_root("CLAUDE_CONFIG_DIR", home / ".claude")
    if not root.exists():
        return []
    roots: dict[str, DiscoveryRoot] = {"default": root}
    if include_non_default:
        roots["claude_project"] = _claude_project_roots(root, backends)
    return discover_from_catalog(
        home,
        "claude",
        roots,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_cursor_cli_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Cursor CLI (``cursor-agent``) sources.

    Covers the terminal agent's transcripts under ``~/.cursor/projects``,
    the AI-tracking SQLite, and the lowercase ``~/.config/cursor`` home
    (prompt history and chat ``store.db`` blobs). Driven entirely by the
    ``cursor-cli.*`` catalogue rows.
    """
    return discover_from_catalog(
        home,
        "cursor-cli",
        home,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def _cursor_ide_workspace_root(home: pathlib.Path) -> pathlib.Path:
    """Resolve the Cursor IDE ``workspaceStorage`` directory for this platform."""
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage"
    if sys.platform == "win32":
        return home / "AppData" / "Roaming" / "Cursor" / "User" / "workspaceStorage"
    return home / ".config" / "Cursor" / "User" / "workspaceStorage"


def discover_cursor_ide_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Cursor IDE (desktop app) sources.

    Covers the VS Code-style ``state.vscdb`` databases: the
    platform-specific ``globalStorage`` location, the legacy
    ``~/.cursor/state.vscdb`` glob, and the per-workspace
    ``workspaceStorage/<hash>/state.vscdb`` databases resolved through the
    ``ide_workspace`` root. Driven entirely by the ``cursor-ide.*``
    catalogue rows.
    """
    roots: dict[str, DiscoveryRoot] = {
        "default": home,
        "ide_workspace": _cursor_ide_workspace_root(home),
    }
    return discover_from_catalog(
        home,
        "cursor-ide",
        roots,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def _is_wsl() -> bool:
    """Detect WSL so the Windows ``/mnt/c`` VS Code data is only probed there."""
    try:
        return (
            "microsoft"
            in pathlib.Path("/proc/version").read_text(encoding="utf-8", errors="ignore").casefold()
        )
    except OSError:
        return False


_VSCODE_EDITIONS: tuple[str, ...] = (
    "Code",
    "Code - Insiders",
    "VSCodium",
    "Code - OSS",
)


def _vscode_user_dirs(home: pathlib.Path) -> tuple[pathlib.Path, ...]:
    """Return the existing VS Code ``User/`` directories across editions and OS.

    Covers the native per-platform location and — on WSL, or when
    ``VSCODE_APPDATA`` points at a Windows ``Roaming`` dir — the Windows-host
    mount, since a workspace opened through WSL persists its chat client-side
    on Windows (reachable from the distro via ``/mnt/c``). ``VSCODE_APPDATA``
    pins one ``Roaming`` dir; ``AGENTGREP_WSL_USERS_ROOT`` overrides the
    Windows users mount that the WSL auto-probe globs (default ``/mnt/c/Users``).
    """
    if sys.platform == "darwin":
        native_base = home / "Library" / "Application Support"
    elif sys.platform == "win32":
        native_base = home / "AppData" / "Roaming"
    else:
        native_base = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    roaming_bases: list[pathlib.Path] = [native_base]
    override = os.environ.get("VSCODE_APPDATA")
    if override:
        roaming_bases.append(pathlib.Path(os.path.expandvars(override)).expanduser())
    elif _is_wsl():
        windows_users = pathlib.Path(os.environ.get("AGENTGREP_WSL_USERS_ROOT") or "/mnt/c/Users")
        if windows_users.is_dir():
            roaming_bases.extend(
                user_dir / "AppData" / "Roaming" for user_dir in sorted(windows_users.glob("*"))
            )
    user_dirs = [base / edition / "User" for base in roaming_bases for edition in _VSCODE_EDITIONS]
    return tuple(path for path in dict.fromkeys(user_dirs) if path.is_dir())


def discover_vscode_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover VS Code (GitHub Copilot Chat) sources.

    VS Code persists chat client-side in the workbench ``User/`` directory:
    per-workspace ``workspaceStorage/<hash>/chatSessions/*.json`` transcripts,
    windowless ``globalStorage/emptyWindowChatSessions/*.json``, and the global
    ``globalStorage/state.vscdb`` inline-edit history. Roots span every existing
    edition and OS ``User/`` dir (including the Windows-host mount under WSL);
    globs and adapters come from the ``vscode.*`` rows of
    :data:`agentgrep.store_catalog.CATALOG`.
    """
    user_dirs = _vscode_user_dirs(home)
    if not user_dirs:
        return []
    roots: dict[str, DiscoveryRoot] = {
        "default": home,
        "vscode_workspace": tuple(d / "workspaceStorage" for d in user_dirs),
        "vscode_global": tuple(d / "globalStorage" for d in user_dirs),
    }
    return discover_from_catalog(
        home,
        "vscode",
        roots,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_gemini_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Gemini CLI sessions and prompt logs.

    Honours the ``GEMINI_CLI_HOME`` environment variable (see upstream
    ``packages/cli/index.ts``); falls back to ``${HOME}/.gemini`` when
    unset or empty. Path roots, globs, and adapter metadata come from the
    ``gemini.*`` rows of :data:`agentgrep.store_catalog.CATALOG`.
    """
    base = resolve_env_root("GEMINI_CLI_HOME", home / ".gemini")
    if not base.exists():
        return []
    return discover_from_catalog(
        home,
        "gemini",
        base,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_antigravity_cli_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Google Antigravity CLI stores under ``~/.gemini``."""
    base = home / ".gemini" / "antigravity-cli"
    if not base.exists():
        return []
    return discover_from_catalog(
        home,
        "antigravity-cli",
        base,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_antigravity_ide_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Google Antigravity IDE stores under ``~/.gemini``."""
    base = home / ".gemini" / "antigravity"
    if not base.exists():
        return []
    return discover_from_catalog(
        home,
        "antigravity-ide",
        base,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_grok_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Grok CLI sessions and prompt history.

    Honours the ``GROK_HOME`` environment variable; falls back to
    ``${HOME}/.grok`` when unset or empty. Path roots, globs, file
    lists, and adapter metadata come from the ``grok.*`` rows of
    :data:`agentgrep.store_catalog.CATALOG`.
    """
    base = resolve_env_root("GROK_HOME", home / ".grok")
    if not base.exists():
        return []
    return discover_from_catalog(
        home,
        "grok",
        base,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_pi_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover pi (earendil-works/pi) session transcripts.

    Honours ``PI_CODING_AGENT_DIR`` (pi's agent data directory, used
    verbatim) and falls back to ``${HOME}/.pi/agent``. The optional
    ``PI_CODING_AGENT_SESSION_DIR`` overrides the sessions directory
    directly: when set, pi writes session files flat into it with no
    per-working-directory subdirectory, so it is resolved as a separate
    discovery root. Path roots, globs, and adapter metadata come from
    the ``pi.*`` rows of :data:`agentgrep.store_catalog.CATALOG`.
    """
    agent_dir = resolve_env_root("PI_CODING_AGENT_DIR", home / ".pi" / "agent")
    session_dir = _resolve_optional_root(
        os.environ.get("PI_CODING_AGENT_SESSION_DIR"),
        agent_dir / "sessions",
        label="PI_CODING_AGENT_SESSION_DIR",
    )
    context_mode_dir = home / ".pi" / "context-mode"
    if not agent_dir.exists() and not session_dir.exists() and not context_mode_dir.exists():
        return []
    roots: dict[str, DiscoveryRoot] = {
        "default": agent_dir,
        "pi_session": session_dir,
        "pi_context_mode": context_mode_dir,
    }
    return discover_from_catalog(
        home,
        "pi",
        roots,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_opencode_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover OpenCode (anomalyco/opencode) SQLite databases.

    OpenCode stores conversations in ``opencode.db`` under its XDG data
    directory (``${XDG_DATA_HOME}/opencode``, falling back to
    ``${HOME}/.local/share/opencode``). The store is discovered by
    filename (not a glob) so the binary SQLite file bypasses the
    text prefilter, the same way the Grok SQLite store is.

    ``OPENCODE_DB`` overrides the database location: when it points at an
    absolute file, OpenCode uses that file (any filename) instead of the
    default, so agentgrep discovers that exact file directly — which also
    makes non-stable channel databases (``opencode-<channel>.db``)
    reachable by pointing ``OPENCODE_DB`` at them. The default lookup and
    adapter metadata come from the ``opencode.*`` rows of
    :data:`agentgrep.store_catalog.CATALOG`.
    """
    db_override = os.environ.get("OPENCODE_DB")
    if db_override and db_override != ":memory:":
        candidate = pathlib.Path(os.path.expandvars(db_override)).expanduser()
        if candidate.is_absolute():
            if not candidate.is_file():
                return []
            from agentgrep.store_catalog import CATALOG

            descriptor = CATALOG.by_id("opencode.db")
            if store_roles is not None and descriptor.role not in store_roles:
                return []
            handle = SourceHandle(
                agent="opencode",
                store="opencode.db",
                adapter_id="opencode.db_sqlite.v1",
                path=candidate,
                path_kind="sqlite_db",
                source_kind="sqlite",
                search_root=None,
                mtime_ns=file_mtime_ns(candidate),
            )
            if version_detail == "catalog":
                handle.version_detection = _catalog_version_detection(
                    descriptor,
                    descriptor.discovery[0],
                )
            elif version_detail == "shape":
                handle.version_detection = detect_source_version(
                    handle,
                    descriptor,
                    descriptor.discovery[0],
                    DiscoveryVersionContext(),
                )
            return [handle]
    base = resolve_env_root("XDG_DATA_HOME", home / ".local" / "share") / "opencode"
    if not base.exists():
        return []
    return discover_from_catalog(
        home,
        "opencode",
        base,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def list_files_matching(
    root: pathlib.Path,
    glob_pattern: str,
    fd_program: str | None,
) -> list[pathlib.Path]:
    """List files under ``root`` that match a glob."""
    if not root.exists():
        return []
    if "/" in glob_pattern or "\\" in glob_pattern:
        return sorted(path for path in root.glob(glob_pattern) if path.is_file())
    if fd_program is not None:
        command = [
            fd_program,
            "-H",
            "-I",
            "-t",
            "f",
            "--glob",
            glob_pattern,
            str(root),
        ]
        completed = run_readonly_command(command)
        if completed.returncode == 0:
            return [pathlib.Path(line) for line in completed.stdout.splitlines() if line.strip()]
    return sorted(path for path in root.rglob(glob_pattern) if path.is_file())


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
