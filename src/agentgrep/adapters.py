"""Per-agent store parsers, the record-normalization layer, and dispatch.

Reads Codex, Claude, Cursor, Gemini, Antigravity, Grok, Pi, and OpenCode store
files and databases into normalized :class:`~agentgrep.records.SearchRecord`
objects. ``iter_source_records`` dispatches a discovered source to the right
parser by ``adapter_id``; the ``extract_*`` / ``build_search_record`` helpers
are the shared normalization seam. Depends on the readers (I/O floor), the
record types, the store catalog, and stdlib; it imports no engine or frontend.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import datetime
import functools
import itertools
import json
import pathlib
import re
import sqlite3
import tomllib
import typing as t
import urllib.parse

from agentgrep.readers import (
    _CODEX_RAW_SKIP_MIN_BYTES,
    _CODEX_SESSION_META_MARKER,
    _PI_SESSION_HEADER_MARKER,
    _file_size,
    _is_codex_function_call_output_line,
    _iter_jsonl,
    _keep_jsonl_header_lines,
    _read_first_jsonl_header,
    as_optional_str,
    decode_sqlite_value,
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
    CONVERSATION_STORE_ROLES,
    CURSOR_STATE_TOKENS,
    PROMPT_HISTORY_STORE_ROLES,
    USER_ROLES,
    DiscoveryStoreRoles,
    FindSourceTypeFilter,
    JSONValue,
    MessageCandidate,
    RawJsonlSkipLine,
    RecordOrigin,
    SearchRecord,
    SourceHandle,
)
from agentgrep.stores import StoreDescriptor, StoreRole


def iter_source_records(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Dispatch to the adapter parser for one source."""
    if source.adapter_id == "codex.sessions_jsonl.v1":
        yield from parse_codex_session_file(
            source,
            raw_skip_line=raw_skip_line,
            reverse=reverse,
        )
        return
    if source.adapter_id == "codex.sessions_legacy_json.v1":
        yield from parse_codex_legacy_session_file(source)
        return
    if source.adapter_id in {"codex.history_json.v1", "codex.history_jsonl.v1"}:
        yield from parse_codex_history_file(
            source,
            raw_skip_line=raw_skip_line,
            reverse=reverse,
        )
        return
    if source.adapter_id == "codex.session_index_jsonl.v1":
        yield from parse_codex_session_index_file(source)
        return
    if source.adapter_id == "claude.history_jsonl.v1":
        yield from parse_claude_history_file(source)
        return
    if source.adapter_id == "antigravity_cli.history_jsonl.v1":
        yield from parse_antigravity_cli_history_file(
            source,
            raw_skip_line=raw_skip_line,
            reverse=reverse,
        )
        return
    if source.adapter_id == "antigravity_cli.conversations_sqlite_protobuf.v1":
        yield from parse_antigravity_cli_conversation_db(source)
        return
    if source.adapter_id in {
        "antigravity_cli.implicit_protobuf.v1",
        "antigravity_ide.conversations_protobuf.v1",
        "antigravity_ide.implicit_protobuf.v1",
    }:
        yield from parse_antigravity_protobuf_file(source)
        return
    if source.adapter_id == "claude.projects_jsonl.v1":
        yield from parse_claude_project_file(
            source,
            raw_skip_line=raw_skip_line,
            reverse=reverse,
        )
        return
    if source.adapter_id == "claude.store_sqlite.v1":
        yield from parse_claude_store_db(source)
        return
    if source.adapter_id == "claude.tasks_json.v1":
        yield from parse_claude_task_file(source)
        return
    if source.adapter_id == "claude.todos_json.v1":
        yield from parse_claude_todo_file(source)
        return
    if source.adapter_id == "claude.teams_json.v1":
        yield from parse_claude_team_file(source)
        return
    if source.adapter_id == "claude.settings_json.v1":
        yield from parse_claude_settings_file(source)
        return
    if source.adapter_id == "claude.app_state_json_summary.v1":
        yield from parse_json_summary_file(source, label="Claude app state")
        return
    if source.adapter_id == "claude.file_metadata_summary.v1":
        yield from parse_file_metadata_summary_file(source, label="Claude raw state")
        return
    if source.adapter_id == "claude.plugin_manifest_json.v1":
        yield from parse_json_summary_file(source, label="Claude plugin manifest")
        return
    if source.adapter_id == "claude.plugin_hooks_json.v1":
        yield from parse_hooks_summary_file(source, label="Claude plugin hooks")
        return
    if source.adapter_id in {
        "claude.commands_text.v1",
        "claude.memory_text.v1",
        "claude.projects_memory_text.v1",
        "claude.plugin_instruction_text.v1",
        "claude.project_instruction_text.v1",
        "claude.session_memory_text.v1",
        "claude.skills_text.v1",
        "claude.plans_text.v1",
        "codex.instructions_text.v1",
        "codex.memories_text.v1",
        "codex.plugin_instruction_text.v1",
        "codex.project_skill_text.v1",
        "codex.rules_text.v1",
        "codex.skills_text.v1",
        "antigravity_cli.brain_text.v1",
        "antigravity_ide.brain_text.v1",
        "antigravity_ide.brain_resolved_text.v1",
        "antigravity_ide.skills_text.v1",
        "claude.workflow_scripts_text.v1",
        "cursor_cli.skills_text.v1",
        "cursor_cli.uploads_text.v1",
        "cursor_cli.agent_tools_text.v1",
        "gemini.memory_text.v1",
        "gemini.tool_outputs_text.v1",
        "grok.plans_text.v1",
        "grok.memory_text.v1",
    }:
        yield from parse_text_store_file(source)
        return
    if source.adapter_id in {
        "codex.config_toml.v1",
        "codex.config_backup_toml.v1",
        "codex.project_config_toml.v1",
    }:
        yield from parse_toml_summary_file(source)
        return
    if source.adapter_id == "codex.app_state_json_summary.v1":
        yield from parse_json_summary_file(source, label="Codex app state")
        return
    if source.adapter_id == "codex.file_metadata_summary.v1":
        yield from parse_file_metadata_summary_file(source, label="Codex raw state")
        return
    if source.adapter_id == "codex.hooks_json.v1":
        yield from parse_hooks_summary_file(source, label="Codex hooks")
        return
    if source.adapter_id == "codex.plugin_hooks_json.v1":
        yield from parse_hooks_summary_file(source, label="Codex plugin hooks")
        return
    if source.adapter_id == "codex.plugin_manifest_json.v1":
        yield from parse_json_summary_file(source, label="Codex plugin manifest")
        return
    if source.adapter_id == "codex.plugin_marketplace_json.v1":
        yield from parse_json_summary_file(source, label="Codex plugin marketplace")
        return
    if source.adapter_id == "codex.state_sqlite.v1":
        yield from parse_codex_state_db(source)
        return
    if source.adapter_id == "codex.logs_sqlite.v1":
        yield from parse_codex_logs_db(source)
        return
    if source.adapter_id == "codex.memories_sqlite.v1":
        yield from parse_codex_memories_db(source)
        return
    if source.adapter_id == "codex.goals_sqlite.v1":
        yield from parse_codex_goals_db(source)
        return
    if source.adapter_id == "codex.external_imports_json.v1":
        yield from parse_codex_external_imports_file(source)
        return
    if source.adapter_id == "cursor_cli.ai_tracking_sqlite.v1":
        yield from parse_cursor_ai_tracking_db(source)
        return
    if source.adapter_id in {
        "cursor_ide.state_vscdb_modern.v1",
        "cursor_ide.state_vscdb_legacy.v1",
    }:
        yield from parse_cursor_state_db(source)
        return
    if source.adapter_id == "cursor_cli.transcripts_jsonl.v1":
        yield from parse_cursor_cli_transcript(source)
        return
    if source.adapter_id == "cursor_cli.prompt_history_json.v1":
        yield from parse_cursor_prompt_history(source)
        return
    if source.adapter_id == "cursor_cli.chats_protobuf.v1":
        yield from parse_cursor_cli_chats_db(source)
        return
    if source.adapter_id == "gemini.tmp_chats_jsonl.v1":
        yield from parse_gemini_chat_file(source)
        return
    if source.adapter_id == "gemini.tmp_chats_legacy_json.v1":
        yield from parse_gemini_chat_legacy_file(source)
        return
    if source.adapter_id == "gemini.tmp_logs_json.v1":
        yield from parse_gemini_logs_file(source)
        return
    if source.adapter_id == "grok.prompt_history_jsonl.v1":
        yield from parse_grok_prompt_history(
            source,
            raw_skip_line=raw_skip_line,
            reverse=reverse,
        )
        return
    if source.adapter_id == "grok.sessions_jsonl.v1":
        yield from parse_grok_chat_history(
            source,
            raw_skip_line=raw_skip_line,
            reverse=reverse,
        )
        return
    if source.adapter_id == "grok.session_search_sqlite.v1":
        yield from parse_grok_session_search_db(source)
        return
    if source.adapter_id == "pi.sessions_jsonl.v1":
        yield from parse_pi_session_file(
            source,
            raw_skip_line=raw_skip_line,
            reverse=reverse,
        )
        return
    if source.adapter_id == "opencode.db_sqlite.v1":
        yield from parse_opencode_db(source)
        return
    if source.adapter_id == "antigravity_cli.transcript_jsonl.v1":
        yield from parse_antigravity_cli_transcript(source)
        return
    if source.adapter_id == "claude.usage_facets_json.v1":
        yield from parse_claude_usage_facet(source)
        return
    if source.adapter_id == "grok.subagents_json.v1":
        yield from parse_grok_subagents(source)
        return
    if source.adapter_id == "pi.context_mode_sqlite.v1":
        yield from parse_pi_context_mode_db(source)
        return
    if source.adapter_id == "vscode.chat_sessions_json.v1":
        yield from parse_vscode_chat_session(source)
        return
    if source.adapter_id == "vscode.inline_history_sqlite.v1":
        yield from parse_vscode_inline_history(source)
        return


def _record_origin(
    *,
    cwd: str | None = None,
    repo: str | None = None,
    worktree: str | None = None,
    branch: str | None = None,
    remote: str | None = None,
    cwd_hash: str | None = None,
    fallback: RecordOrigin | None = None,
) -> RecordOrigin | None:
    """Build a non-empty origin, inheriting omitted values from ``fallback``."""
    origin = RecordOrigin(
        cwd=cwd or (fallback.cwd if fallback is not None else None),
        repo=repo or (fallback.repo if fallback is not None else None),
        worktree=worktree or (fallback.worktree if fallback is not None else None),
        branch=branch or (fallback.branch if fallback is not None else None),
        remote=remote or (fallback.remote if fallback is not None else None),
        cwd_hash=cwd_hash or (fallback.cwd_hash if fallback is not None else None),
    )
    return None if origin.is_empty() else origin


def _path_like_str(value: object) -> str | None:
    """Accept a mapping value as an origin path only when it looks like one.

    Store blobs reuse key names like ``workspace`` or ``branch`` for
    non-filesystem values (workspace UUIDs, UI state); a bare token
    without a separator or home prefix must not become an origin path.
    """
    text = as_optional_str(value)
    if text is None:
        return None
    if "/" in text or "\\" in text or text == "~":
        return text
    return None


def _origin_from_mapping(
    mapping: dict[str, object],
    *,
    fallback: RecordOrigin | None = None,
) -> RecordOrigin | None:
    """Extract common cwd/branch/project-hash fields from a JSON mapping."""
    git = mapping.get("git")
    git_mapping = t.cast("dict[str, object]", git) if isinstance(git, dict) else {}
    cwd = (
        _path_like_str(mapping.get("cwd"))
        or _path_like_str(mapping.get("workspace"))
        or _path_like_str(mapping.get("directory"))
    )
    repo = _path_like_str(mapping.get("repo")) or _path_like_str(mapping.get("repository"))
    worktree = _path_like_str(mapping.get("worktree"))
    # Bare `branch`/`remote` are generic UI-state words in store blobs;
    # accept them only alongside git or path evidence from the same
    # mapping.
    trusted = bool(git_mapping) or "gitBranch" in mapping or bool(cwd or repo or worktree)
    return _record_origin(
        cwd=cwd,
        repo=repo,
        worktree=worktree,
        branch=(
            as_optional_str(mapping.get("gitBranch"))
            or as_optional_str(git_mapping.get("branch"))
            or (as_optional_str(mapping.get("branch")) if trusted else None)
        ),
        remote=(
            as_optional_str(git_mapping.get("repository_url"))
            or as_optional_str(git_mapping.get("repositoryUrl"))
            or as_optional_str(git_mapping.get("remote"))
            or (as_optional_str(mapping.get("remote")) if trusted else None)
        ),
        cwd_hash=(
            as_optional_str(mapping.get("cwd_hash"))
            or as_optional_str(mapping.get("project_hash"))
            or as_optional_str(mapping.get("projectHash"))
        ),
        fallback=fallback,
    )


# Every key _origin_from_mapping reads; lets hot walks skip extraction
# for the many nodes that carry none of them.
_ORIGIN_MAPPING_KEYS: frozenset[str] = frozenset(
    {
        "git",
        "cwd",
        "workspace",
        "directory",
        "repo",
        "repository",
        "worktree",
        "gitBranch",
        "branch",
        "remote",
        "cwd_hash",
        "project_hash",
        "projectHash",
    },
)


def parse_codex_session_file(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse Codex session JSONL files."""
    session_id = source.path.stem
    session_model: str | None = None
    session_origin: RecordOrigin | None = None
    if reverse:
        # Reverse iteration reads the leading session_meta header last,
        # so seed its state up front to keep emitted records canonical.
        header = _read_first_jsonl_header(source.path, _CODEX_SESSION_META_MARKER)
        header_payload = header.get("payload") if header is not None else None
        if (
            header is not None
            and str(header.get("type", "")) == "session_meta"
            and isinstance(header_payload, dict)
        ):
            payload = t.cast("dict[str, object]", header_payload)
            session_id = as_optional_str(payload.get("id")) or session_id
            session_origin = _origin_from_mapping(payload, fallback=session_origin)
            session_model = (
                as_optional_str(payload.get("model"))
                or as_optional_str(payload.get("model_name"))
                or as_optional_str(payload.get("model_provider"))
                or session_model
            )
    codex_skip_line = (
        _is_codex_function_call_output_line
        if _file_size(source.path) >= _CODEX_RAW_SKIP_MIN_BYTES
        else None
    )
    # The session_meta header feeds session_id/model into later records,
    # so the text prefilter must never drop it.
    kept_raw_skip = (
        None
        if raw_skip_line is None
        else _keep_jsonl_header_lines(raw_skip_line, _CODEX_SESSION_META_MARKER)
    )
    if codex_skip_line is not None:
        # Keep the cheap prefix-mode tool-output skip even when a raw text
        # prefilter is active: the prefix predicate discards oversized
        # function_call_output lines in chunks while the text prefilter
        # still sees every surviving line in full before JSON decode.
        events = _iter_jsonl(
            source.path,
            skip_line=codex_skip_line,
            skip_line_mode="prefix",
            full_line_skip=kept_raw_skip,
            reverse=reverse,
        )
    elif kept_raw_skip is not None:
        events = _iter_jsonl(
            source.path,
            skip_line=kept_raw_skip,
            skip_line_mode="line",
            reverse=reverse,
        )
    else:
        events = _iter_jsonl(source.path, reverse=reverse)
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", ""))
        payload = event.get("payload")
        if event_type == "session_meta" and isinstance(payload, dict):
            payload_map = t.cast("dict[str, object]", payload)
            session_id = as_optional_str(payload_map.get("id")) or session_id
            session_origin = _origin_from_mapping(payload_map, fallback=session_origin)
            session_model = (
                as_optional_str(payload_map.get("model"))
                or as_optional_str(payload_map.get("model_name"))
                or as_optional_str(payload_map.get("model_provider"))
                or session_model
            )
            continue
        if event_type != "response_item" or not isinstance(payload, dict):
            continue
        candidate = candidate_from_mapping(
            t.cast("dict[str, object]", payload),
            timestamp=as_optional_str(event.get("timestamp")),
            model=session_model,
            session_id=session_id,
            conversation_id=session_id,
            origin=session_origin,
        )
        if candidate is None:
            continue
        yield build_search_record(source, candidate)


def parse_codex_legacy_session_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse legacy root-level Codex ``rollout-*.json`` session files."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    session_raw = payload.get("session")
    session = t.cast("dict[str, object]", session_raw) if isinstance(session_raw, dict) else {}
    session_id = as_optional_str(session.get("id")) or source.path.stem
    timestamp = as_optional_str(session.get("timestamp")) or as_optional_str(
        session.get("created_at"),
    )
    model = (
        as_optional_str(session.get("model"))
        or as_optional_str(session.get("model_name"))
        or as_optional_str(session.get("modelProvider"))
    )
    items = payload.get("items")
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = candidate_from_mapping(
            t.cast("dict[str, object]", item),
            timestamp=as_optional_str(item.get("timestamp")) or timestamp,
            model=model,
            session_id=session_id,
            conversation_id=session_id,
        )
        if candidate is None:
            continue
        yield build_search_record(source, candidate)


def parse_codex_history_file(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse Codex prompt/command history files."""
    entries: cabc.Iterable[JSONValue]
    if source.source_kind == "json":
        payload = read_json_file(source.path)
        entries = payload if isinstance(payload, list) else []
    else:
        entries = (
            _iter_jsonl(
                source.path,
                skip_line=raw_skip_line,
                skip_line_mode="line",
                reverse=reverse,
            )
            if raw_skip_line is not None
            else _iter_jsonl(source.path, reverse=reverse)
        )

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = as_optional_str(entry.get("text")) or as_optional_str(entry.get("command"))
        if not text:
            continue
        session_id = as_optional_str(entry.get("session_id"))
        timestamp = as_optional_str(entry.get("timestamp"))
        ts = entry.get("ts")
        if timestamp is None and isinstance(ts, int):
            timestamp = (
                datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
        yield SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=text,
            title="Codex prompt history",
            role="user",
            timestamp=timestamp,
            session_id=session_id,
            conversation_id=session_id,
        )


def parse_codex_session_index_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Codex ``session_index.jsonl`` records as opt-in thread summaries."""
    for entry in iter_jsonl(source.path):
        if not isinstance(entry, dict):
            continue
        mapping = t.cast("dict[str, object]", entry)
        thread_name = as_optional_str(mapping.get("thread_name"))
        if not thread_name:
            continue
        session_id = as_optional_str(mapping.get("id"))
        yield SearchRecord(
            kind="history",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=thread_name,
            title=thread_name,
            role="assistant",
            timestamp=as_optional_str(mapping.get("updated_at")),
            session_id=session_id,
            conversation_id=session_id,
        )


def parse_claude_project_file(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse Claude Code project JSONL files using lightweight heuristics."""
    conversation_id = source.path.stem
    seen: set[tuple[str | None, str, str | None, str | None]] = set()
    events = (
        _iter_jsonl(
            source.path,
            skip_line=raw_skip_line,
            skip_line_mode="line",
            reverse=reverse,
        )
        if raw_skip_line is not None
        else _iter_jsonl(source.path, reverse=reverse)
    )
    for event in events:
        if isinstance(event, dict) and event.get("isCompactSummary") is True:
            # `/compact` machine summaries are derived recaps, not user turns.
            continue
        for candidate in iter_message_candidates(
            event,
            fallback_conversation_id=conversation_id,
        ):
            key = (
                candidate.role,
                candidate.text,
                candidate.timestamp,
                candidate.conversation_id,
            )
            if key in seen:
                continue
            seen.add(key)
            yield build_search_record(source, candidate)


def _json_string_list(value: object) -> list[str]:
    """Return a list of non-empty strings from a JSON list-like field."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def parse_claude_task_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Claude Code task JSON files as opt-in task samples."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    mapping = t.cast("dict[str, object]", payload)
    subject = as_optional_str(mapping.get("subject"))
    description = as_optional_str(mapping.get("description"))
    text = "\n\n".join(part for part in (subject, description) if part)
    if not text:
        return
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        title=subject,
        role="task",
        timestamp=as_optional_str(mapping.get("updatedAt"))
        or as_optional_str(mapping.get("updated_at"))
        or isoformat_from_mtime_ns(source.mtime_ns),
        session_id=as_optional_str(mapping.get("id")),
        metadata={
            "status": as_optional_str(mapping.get("status")) or "",
            "task_id": as_optional_str(mapping.get("id")) or "",
            "blocks": _json_string_list(mapping.get("blocks")),
            "blocked_by": _json_string_list(mapping.get("blockedBy")),
        },
    )


def _json_value_shape(value: object) -> str:
    """Return a value-free shape label for safe config/app-state summaries."""
    if isinstance(value, dict):
        return f"object[{len(value)}]"
    if isinstance(value, list):
        return f"array[{len(value)}]"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if value is None:
        return "null"
    return type(value).__name__


def _safe_mapping_summary(label: str, payload: dict[str, object]) -> str:
    """Summarize mapping keys and value shapes without including raw values."""
    key_shapes = [
        f"{key} ({_json_value_shape(payload[key])})" for key in sorted(payload) if key.strip()
    ]
    return f"{label} keys: {', '.join(key_shapes)}"


def parse_json_summary_file(
    source: SourceHandle,
    *,
    label: str,
) -> cabc.Iterator[SearchRecord]:
    """Parse a JSON object as a key/type summary without raw values."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    mapping = t.cast("dict[str, object]", payload)
    if not mapping:
        return
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=_safe_mapping_summary(label, mapping),
        title=source.path.name,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"key_count": len(mapping)},
    )


def _safe_nested_keys(payload: dict[str, object], key: str) -> list[str]:
    """Return sorted keys from a nested object without exposing values."""
    nested = payload.get(key)
    if not isinstance(nested, dict):
        return []
    return sorted(nested_key for nested_key in nested if isinstance(nested_key, str))


def parse_hooks_summary_file(
    source: SourceHandle,
    *,
    label: str,
) -> cabc.Iterator[SearchRecord]:
    """Parse hook JSON as event/key summaries without raw commands."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    mapping = t.cast("dict[str, object]", payload)
    if not mapping:
        return
    hook_events = _safe_nested_keys(mapping, "hooks")
    text = _safe_mapping_summary(label, mapping)
    if hook_events:
        text = f"{text}; hook events: {', '.join(hook_events)}"
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        title=source.path.name,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"key_count": len(mapping), "hook_event_count": len(hook_events)},
    )


def _line_count(path: pathlib.Path) -> int:
    """Count text lines without exposing their contents."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def parse_file_metadata_summary_file(
    source: SourceHandle,
    *,
    label: str,
) -> cabc.Iterator[SearchRecord]:
    """Parse raw/cache text files as metadata-only summaries."""
    byte_size = _file_size(source.path)
    line_count = _line_count(source.path)
    suffix = source.path.suffix or "<none>"
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=(
            f"{label} file metadata: name={source.path.name}, "
            f"suffix={suffix}, bytes={byte_size}, lines={line_count}"
        ),
        title=source.path.name,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"byte_size": byte_size, "line_count": line_count},
    )


def parse_toml_summary_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a TOML file as a key/type summary without raw values."""
    try:
        payload = tomllib.loads(source.path.read_text(encoding="utf-8"))
    except OSError, tomllib.TOMLDecodeError:
        return
    if not payload:
        return
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=_safe_mapping_summary("Codex config", t.cast("dict[str, object]", payload)),
        title=source.path.name,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"key_count": len(payload)},
    )


def _iter_todo_mappings(payload: object) -> cabc.Iterator[dict[str, object]]:
    """Yield task-like mappings from common Claude todo container shapes."""
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield t.cast("dict[str, object]", item)
        return
    if not isinstance(payload, dict):
        return
    mapping = t.cast("dict[str, object]", payload)
    if any(key in mapping for key in ("content", "text", "subject", "description", "title")):
        yield mapping
    for key in ("todos", "items", "tasks"):
        nested = mapping.get(key)
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    yield t.cast("dict[str, object]", item)


def parse_claude_todo_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Claude todo JSON files as opt-in todo samples."""
    payload = read_json_file(source.path)
    for mapping in _iter_todo_mappings(payload):
        first_line = (
            as_optional_str(mapping.get("content"))
            or as_optional_str(mapping.get("text"))
            or as_optional_str(mapping.get("subject"))
            or as_optional_str(mapping.get("title"))
        )
        description = as_optional_str(mapping.get("description"))
        text = "\n\n".join(part for part in (first_line, description) if part)
        if not text:
            continue
        todo_id = as_optional_str(mapping.get("id"))
        yield SearchRecord(
            kind="history",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=text,
            title=first_line,
            role="todo",
            timestamp=as_optional_str(mapping.get("updatedAt"))
            or as_optional_str(mapping.get("updated_at"))
            or isoformat_from_mtime_ns(source.mtime_ns),
            session_id=todo_id,
            metadata={
                "status": as_optional_str(mapping.get("status")) or "",
                "todo_id": todo_id or "",
            },
        )


def parse_claude_team_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Claude team config JSON as opt-in team instruction samples."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    mapping = t.cast("dict[str, object]", payload)
    parts: list[str] = []
    team_name = as_optional_str(mapping.get("name"))
    description = as_optional_str(mapping.get("description"))
    if team_name:
        parts.append(f"Team: {team_name}")
    if description:
        parts.append(description)
    members = mapping.get("members")
    member_count = len(members) if isinstance(members, list) else 0
    if isinstance(members, list):
        for member in members:
            if not isinstance(member, dict):
                continue
            member_mapping = t.cast("dict[str, object]", member)
            prompt = as_optional_str(member_mapping.get("prompt"))
            if not prompt:
                continue
            name = as_optional_str(member_mapping.get("name")) or "member"
            parts.append(f"{name}: {prompt}")
    text = "\n\n".join(parts)
    if not text:
        return
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        title=team_name or source.path.parent.name,
        role="team",
        timestamp=_unix_millis_to_isoformat(mapping.get("createdAt"))
        or isoformat_from_mtime_ns(source.mtime_ns),
        session_id=as_optional_str(mapping.get("leadSessionId")),
        metadata={"member_count": member_count},
    )


def parse_claude_settings_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Claude settings JSON as a key summary without raw values."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    keys = sorted(key for key in payload if key.strip())
    if not keys:
        return
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=f"Claude settings keys: {', '.join(keys)}",
        title=source.path.name,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"key_count": len(keys)},
    )


CLAUDE_PASTE_REF_RE = re.compile(
    r"\[(?:Pasted text|Image|\.\.\.Truncated text) #(?P<id>\d+)(?: \+\d+ lines)?\.*\]",
)
CLAUDE_PASTE_HASH_RE = re.compile(r"^[0-9a-fA-F]{16}$")


def parse_claude_history_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Claude Code's global ``history.jsonl`` prompt audit log."""
    paste_cache_dir = source.path.parent / "paste-cache"
    for event in iter_jsonl(source.path):
        if not isinstance(event, dict):
            continue
        mapping = t.cast("dict[str, object]", event)
        display = as_optional_str(mapping.get("display"))
        if not display:
            continue
        session_id = as_optional_str(mapping.get("sessionId"))
        project = as_optional_str(mapping.get("project"))
        yield SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=expand_claude_history_pastes(
                display,
                mapping.get("pastedContents"),
                paste_cache_dir,
            ),
            title="Claude prompt history",
            role="user",
            timestamp=_unix_millis_to_isoformat(mapping.get("timestamp")),
            session_id=session_id,
            conversation_id=session_id,
            origin=_record_origin(cwd=_path_like_str(project)),
            metadata={"project": project or ""},
        )


def parse_antigravity_cli_history_file(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse Antigravity CLI's ``history.jsonl`` prompt recall log."""
    events = (
        _iter_jsonl(
            source.path,
            skip_line=raw_skip_line,
            skip_line_mode="line",
            reverse=reverse,
        )
        if raw_skip_line is not None
        else _iter_jsonl(source.path, reverse=reverse)
    )
    for event in events:
        if not isinstance(event, dict):
            continue
        mapping = t.cast("dict[str, object]", event)
        display = as_optional_str(mapping.get("display"))
        if not display:
            continue
        session_id = as_optional_str(mapping.get("conversationId"))
        workspace = as_optional_str(mapping.get("workspace"))
        yield SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=display,
            title="Antigravity CLI prompt history",
            role="user",
            timestamp=_unix_millis_to_isoformat(mapping.get("timestamp")),
            session_id=session_id,
            conversation_id=session_id,
            origin=_record_origin(cwd=_path_like_str(workspace)),
            metadata={
                "workspace": workspace or "",
                "type": as_optional_str(mapping.get("type")) or "",
            },
        )


def expand_claude_history_pastes(
    display: str,
    pasted_contents: object,
    paste_cache_dir: pathlib.Path,
) -> str:
    """Replace Claude history paste placeholders with stored text when available.

    Examples
    --------
    >>> expand_claude_history_pastes(
    ...     "Review [Pasted text #1]",
    ...     {"1": {"type": "text", "content": "inline text"}},
    ...     pathlib.Path("/missing"),
    ... )
    'Review inline text'
    >>> expand_claude_history_pastes(
    ...     "Review [Image #1]",
    ...     {"1": {"type": "image", "content": "ignored"}},
    ...     pathlib.Path("/missing"),
    ... )
    'Review [Image #1]'
    """
    if not isinstance(pasted_contents, dict):
        return display
    refs = t.cast("dict[object, object]", pasted_contents)

    def replace(match: re.Match[str]) -> str:
        ref_id = match.group("id")
        stored = refs.get(ref_id)
        replacement = claude_history_paste_text(stored, paste_cache_dir)
        return replacement if replacement is not None else match.group(0)

    return CLAUDE_PASTE_REF_RE.sub(replace, display)


def claude_history_paste_text(
    stored: object,
    paste_cache_dir: pathlib.Path,
) -> str | None:
    """Return stored Claude pasted text, resolving content hashes if needed."""
    if not isinstance(stored, dict):
        return None
    mapping = t.cast("dict[str, object]", stored)
    if mapping.get("type") != "text":
        return None
    content = mapping.get("content")
    if isinstance(content, str) and content:
        return content
    content_hash = as_optional_str(mapping.get("contentHash"))
    if content_hash is None or CLAUDE_PASTE_HASH_RE.fullmatch(content_hash) is None:
        return None
    cached = read_text_file(paste_cache_dir / f"{content_hash}.txt")
    return cached or None


def parse_cursor_cli_transcript(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a Cursor CLI agent transcript JSONL file.

    Each line is ``{"role": "user" | "assistant", "message": {"content": [...]}}``;
    ``iter_message_candidates`` handles the nested shape directly. Cursor
    transcripts carry no native per-turn timestamp, so the file's mtime is
    used as a session-level fallback.
    """
    conversation_id = source.path.stem
    fallback_timestamp = isoformat_from_mtime_ns(source.mtime_ns)
    seen: set[tuple[str | None, str, str | None, str | None]] = set()
    for event in iter_jsonl(source.path):
        for candidate in iter_message_candidates(
            event,
            fallback_conversation_id=conversation_id,
        ):
            if candidate.timestamp is None and fallback_timestamp is not None:
                candidate = dataclasses.replace(candidate, timestamp=fallback_timestamp)
            key = (
                candidate.role,
                candidate.text,
                candidate.timestamp,
                candidate.conversation_id,
            )
            if key in seen:
                continue
            seen.add(key)
            yield build_search_record(source, candidate)


def _gemini_thoughts_text(thoughts: object) -> str:
    """Flatten Gemini's ``thoughts[]`` into a single searchable string.

    Each entry carries ``subject`` (short label) and ``description``
    (multi-sentence reasoning). Concatenating them per-record keeps the
    conversation-turn boundary intact while still surfacing the assistant's
    output in the search corpus.
    """
    if not isinstance(thoughts, list):
        return ""
    parts: list[str] = []
    for entry in thoughts:
        if not isinstance(entry, dict):
            continue
        mapping = t.cast("dict[str, object]", entry)
        subject = as_optional_str(mapping.get("subject"))
        description = as_optional_str(mapping.get("description"))
        if subject:
            parts.append(subject)
        if description:
            parts.append(description)
    return "\n".join(parts)


def _gemini_tool_calls_text(tool_calls: object) -> str:
    """Flatten Gemini's ``toolCalls[]`` into a searchable string.

    ``name`` and ``description`` carry the human-readable text; ``args`` is
    JSON-shaped and contributes lower-signal noise, so it is omitted.
    """
    if not isinstance(tool_calls, list):
        return ""
    parts: list[str] = []
    for entry in tool_calls:
        if not isinstance(entry, dict):
            continue
        mapping = t.cast("dict[str, object]", entry)
        name = as_optional_str(mapping.get("name"))
        description = as_optional_str(mapping.get("description"))
        if name:
            parts.append(name)
        if description:
            parts.append(description)
    return "\n".join(parts)


def _gemini_message_record_to_candidate(
    mapping: dict[str, object],
    session_id: str | None,
    origin: RecordOrigin | None = None,
) -> MessageCandidate | None:
    """Extract a ``MessageCandidate`` from one Gemini MessageRecord.

    Only ``user`` and ``gemini`` conversation turns are surfaced; CLI
    ``info``/``error``/``warning`` records are skipped. For user records the
    searchable text is the ``content`` field. For gemini-typed records the
    model's prose often lives in ``thoughts[]`` (with ``content`` empty) and
    tool invocations live in ``toolCalls[]``; both are concatenated into the
    candidate's text. Returns ``None`` when no field carries any text.
    """
    role = as_optional_str(mapping.get("type"))
    if not role:
        return None
    if role not in {"user", "gemini"}:
        # info/error/warning are CLI system messages, not conversation turns.
        return None
    text_parts: list[str] = []
    content_text = flatten_content_value(
        t.cast("JSONValue | None", mapping.get("content")),
    )
    if content_text:
        text_parts.append(content_text)
    if role == "gemini":
        thoughts_text = _gemini_thoughts_text(mapping.get("thoughts"))
        if thoughts_text:
            text_parts.append(thoughts_text)
        tool_calls_text = _gemini_tool_calls_text(mapping.get("toolCalls"))
        if tool_calls_text:
            text_parts.append(tool_calls_text)
    if not text_parts:
        return None
    return MessageCandidate(
        role=role,
        text="\n".join(text_parts),
        timestamp=as_optional_str(mapping.get("timestamp")),
        model=as_optional_str(mapping.get("model")),
        session_id=session_id or as_optional_str(mapping.get("sessionId")),
        conversation_id=session_id,
        origin=_origin_from_mapping(mapping, fallback=origin),
    )


def parse_gemini_chat_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a Gemini CLI chat session JSONL file.

    The file mixes record kinds: a leading ``SessionMetadataRecord``
    (``{"sessionId", "projectHash", "startTime", "lastUpdated", "kind"}``),
    ``MessageRecord`` turns (``{"id", "timestamp", "type": "user"|"gemini",
    "content"}``), and ``MetadataUpdateRecord`` updates (``{"$set": {...}}``).
    Gemini stores the role in a ``type`` key — not the ``role`` key the
    shared ``extract_role`` helper recognises — so this adapter extracts
    fields directly rather than going through ``iter_message_candidates``.
    """
    session_id: str | None = None
    session_origin: RecordOrigin | None = _record_origin(cwd_hash=source.path.parent.parent.name)
    for event in iter_jsonl(source.path):
        if not isinstance(event, dict):
            continue
        mapping = t.cast("dict[str, object]", event)
        if "$set" in mapping:
            continue
        if "kind" in mapping:
            # SessionMetadataRecord: upstream discriminates by ``kind``
            # (e.g. ``"main"``) rather than by the absence of ``type``,
            # so this stays correct even if a future schema adds a
            # ``type`` field to the metadata record.
            session_id = as_optional_str(mapping.get("sessionId"))
            session_origin = _origin_from_mapping(mapping, fallback=session_origin)
            continue
        candidate = _gemini_message_record_to_candidate(mapping, session_id, session_origin)
        if candidate is None:
            continue
        yield build_search_record(source, candidate)


def parse_gemini_chat_legacy_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a pre-Feb 2026 Gemini CLI single-file ``.json`` chat session.

    The legacy format is a JSON object with session metadata at the top
    level and the full conversation under a ``messages`` array. Upstream
    still reads this shape via the ``isLegacyRecord`` discriminator at
    ``packages/core/src/services/chatRecordingService.ts``. Each entry of
    ``messages`` carries the same per-turn fields the JSONL format uses,
    so record extraction is shared with :func:`parse_gemini_chat_file`.
    """
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    container = t.cast("dict[str, object]", payload)
    session_id = as_optional_str(container.get("sessionId"))
    session_origin = _origin_from_mapping(container)
    messages = container.get("messages")
    if not isinstance(messages, list):
        return
    for entry in messages:
        if not isinstance(entry, dict):
            continue
        mapping = t.cast("dict[str, object]", entry)
        candidate = _gemini_message_record_to_candidate(mapping, session_id, session_origin)
        if candidate is None:
            continue
        yield build_search_record(source, candidate)


def parse_gemini_logs_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a Gemini CLI ``logs.json`` file (flat JSON array of LogEntry).

    Records are emitted as ``kind="prompt"`` — the file is an audit log of
    user prompts, the same role ``codex.history`` plays for Codex.
    """
    payload = read_json_file(source.path)
    origin = _record_origin(cwd_hash=source.path.parent.name)
    entries = payload if isinstance(payload, list) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mapping = t.cast("dict[str, object]", entry)
        message = as_optional_str(mapping.get("message"))
        if not message:
            continue
        session_id = as_optional_str(mapping.get("sessionId"))
        yield SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=message,
            title="Gemini prompt history",
            role=as_optional_str(mapping.get("type")) or "user",
            timestamp=as_optional_str(mapping.get("timestamp")),
            session_id=session_id,
            conversation_id=session_id,
            origin=_origin_from_mapping(mapping, fallback=origin),
        )


def parse_grok_prompt_history(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse a Grok CLI ``prompt_history.jsonl`` file.

    Each line is ``{"timestamp": "…", "session_id": "…", "prompt": "…",
    "is_bash": bool}`` — one record per user prompt, append-only across
    all sessions within one project directory.
    """
    events = (
        _iter_jsonl(
            source.path,
            skip_line=raw_skip_line,
            skip_line_mode="line",
            reverse=reverse,
        )
        if raw_skip_line is not None
        else _iter_jsonl(source.path, reverse=reverse)
    )
    for event in events:
        if not isinstance(event, dict):
            continue
        mapping = t.cast("dict[str, object]", event)
        prompt = as_optional_str(mapping.get("prompt"))
        if not prompt:
            continue
        session_id = as_optional_str(mapping.get("session_id"))
        yield SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=prompt,
            title="Grok prompt history",
            role="user",
            timestamp=as_optional_str(mapping.get("timestamp")),
            session_id=session_id,
            conversation_id=session_id,
            metadata={"is_bash": mapping.get("is_bash", False)},
        )


def parse_grok_chat_history(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse a Grok CLI ``chat_history.jsonl`` session transcript.

    Lines carry a ``type`` field (system / user / assistant / reasoning /
    tool_result / backend_tool_call) and ``content`` (text or content-blocks
    array). Records without ``content`` — every ``reasoning`` and
    ``backend_tool_call`` record, plus any ``assistant`` record whose content
    is empty — are skipped.
    """
    conversation_id = source.path.parent.name
    events = (
        _iter_jsonl(
            source.path,
            skip_line=raw_skip_line,
            skip_line_mode="line",
            reverse=reverse,
        )
        if raw_skip_line is not None
        else _iter_jsonl(source.path, reverse=reverse)
    )
    for event in events:
        if not isinstance(event, dict):
            continue
        mapping = t.cast("dict[str, object]", event)
        record_type = as_optional_str(mapping.get("type"))
        if not record_type:
            continue
        content_text = flatten_content_value(
            t.cast("JSONValue | None", mapping.get("content")),
        )
        if not content_text:
            continue
        yield SearchRecord(
            kind="prompt" if record_type == "user" else "history",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=content_text,
            role=record_type,
            timestamp=as_optional_str(mapping.get("timestamp")),
            session_id=conversation_id,
            conversation_id=conversation_id,
        )


def _unix_to_isoformat(value: object) -> str | None:
    """Convert a unix-seconds integer to an ISO-8601 UTC timestamp.

    Examples
    --------
    >>> _unix_to_isoformat(1700000000)
    '2023-11-14T22:13:20Z'
    >>> _unix_to_isoformat(0) is None
    True
    >>> _unix_to_isoformat(float("nan")) is None
    True
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return (
            datetime.datetime.fromtimestamp(value, tz=datetime.UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except ValueError, OSError, OverflowError:
        return None


def _unix_millis_to_isoformat(value: object) -> str | None:
    """Convert a unix-milliseconds timestamp to ISO-8601 UTC.

    Examples
    --------
    >>> _unix_millis_to_isoformat(1700000000000)
    '2023-11-14T22:13:20Z'
    >>> _unix_millis_to_isoformat(0) is None
    True
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return (
            datetime.datetime.fromtimestamp(value / 1000, tz=datetime.UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except ValueError, OSError, OverflowError:
        return None


def _pi_bash_execution_text(message_map: dict[str, object]) -> str | None:
    """Join a ``bashExecution`` turn's command and output into searchable text.

    ``bashExecution`` messages have no ``content``; the shell command and its
    captured output live in the ``command`` and ``output`` string fields.
    """
    command = as_optional_str(message_map.get("command"))
    output = as_optional_str(message_map.get("output"))
    return "\n".join(part for part in (command, output) if part) or None


def _pi_message_candidate(
    entry: dict[str, object],
    entry_timestamp: str | None,
    session_id: str | None,
    conversation_id: str | None,
    origin: RecordOrigin | None = None,
) -> MessageCandidate | None:
    """Build a candidate from a pi ``message`` session entry.

    The entry wraps an LLM message under ``message`` (``role`` plus
    ``content`` that is a string or content-blocks array). The
    entry-level ISO timestamp is preferred; the inner unix-milliseconds
    ``timestamp`` is the fallback for v1 entries that lack one.
    ``bashExecution`` turns carry no ``content``; their command and output
    are joined instead.
    """
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    message_map = t.cast("dict[str, object]", message)
    role = as_optional_str(message_map.get("role"))
    text = flatten_content_value(t.cast("JSONValue | None", message_map.get("content")))
    if not text and role == "bashExecution":
        text = _pi_bash_execution_text(message_map)
    if role is None or not text:
        return None
    timestamp = entry_timestamp or _unix_millis_to_isoformat(message_map.get("timestamp"))
    return MessageCandidate(
        role=role,
        text=text,
        timestamp=timestamp,
        model=as_optional_str(message_map.get("model")),
        session_id=session_id,
        conversation_id=conversation_id,
        origin=origin,
    )


def _pi_entry_text(entry_type: str, entry: dict[str, object]) -> str | None:
    """Return searchable text from a non-message pi session entry.

    ``compaction``/``branch_summary`` carry a ``summary``; ``session_info``
    carries a user-set ``name``. Other entry types (model/thinking-level
    changes, custom, label) are metadata-only and yield no text.
    """
    if entry_type in {"compaction", "branch_summary"}:
        return as_optional_str(entry.get("summary"))
    if entry_type == "session_info":
        return as_optional_str(entry.get("name"))
    return None


def parse_pi_session_file(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse a pi (earendil-works/pi) session JSONL transcript.

    Line 1 is a ``type:"session"`` header (capturing ``id``/``cwd``);
    ``version`` may be absent in v1 files. Each later line is a
    ``SessionEntry`` tagged union. ``message`` entries become candidates
    whose role drives the prompt/history split (user turns are prompts);
    ``compaction``/``branch_summary`` summaries and ``session_info`` names
    are emitted as history text. Metadata-only entries are skipped.
    """
    session_id: str | None = source.path.stem
    conversation_id: str | None = None
    session_origin: RecordOrigin | None = None
    if reverse:
        # Reverse iteration reads the leading session header last, so
        # seed its state up front to keep emitted records canonical.
        header = _read_first_jsonl_header(source.path, _PI_SESSION_HEADER_MARKER)
        if header is not None and as_optional_str(header.get("type")) == "session":
            session_id = as_optional_str(header.get("id")) or session_id
            conversation_id = as_optional_str(header.get("cwd"))
            session_origin = _record_origin(cwd=conversation_id)
    # The session header feeds session_id/cwd into later records, so the
    # text prefilter must never drop it.
    events = (
        _iter_jsonl(
            source.path,
            skip_line=_keep_jsonl_header_lines(raw_skip_line, _PI_SESSION_HEADER_MARKER),
            skip_line_mode="line",
            reverse=reverse,
        )
        if raw_skip_line is not None
        else _iter_jsonl(source.path, reverse=reverse)
    )
    for event in events:
        if not isinstance(event, dict):
            continue
        mapping = t.cast("dict[str, object]", event)
        entry_type = as_optional_str(mapping.get("type"))
        if not entry_type:
            continue
        if entry_type == "session":
            session_id = as_optional_str(mapping.get("id")) or session_id
            conversation_id = as_optional_str(mapping.get("cwd"))
            session_origin = _record_origin(cwd=conversation_id, fallback=session_origin)
            continue
        entry_timestamp = as_optional_str(mapping.get("timestamp"))
        if entry_type == "message":
            candidate = _pi_message_candidate(
                mapping,
                entry_timestamp,
                session_id,
                conversation_id,
                session_origin,
            )
            if candidate is not None:
                yield build_search_record(source, candidate)
            continue
        text = _pi_entry_text(entry_type, mapping)
        if not text:
            continue
        yield SearchRecord(
            kind="history",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=text,
            role=entry_type,
            timestamp=entry_timestamp,
            session_id=session_id,
            conversation_id=conversation_id,
            origin=session_origin,
        )


def parse_text_store_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse opt-in plain-text inventory stores as one sample record."""
    text = read_text_file(source.path).strip()
    if not text:
        return
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        title=source.store,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"coverage": source.coverage.value},
    )


def parse_claude_store_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse opt-in Claude Code ``__store.db`` message samples."""
    connection = open_readonly_sqlite(source.path)
    try:
        tables = sqlite_table_names(connection)
        has_base = "base_messages" in tables
        if "user_messages" in tables:
            query = (
                """
                SELECT u.uuid, u.message, u.timestamp, b.session_id
                FROM user_messages u
                LEFT JOIN base_messages b ON b.uuid = u.uuid
                """
                if has_base
                else "SELECT uuid, message, timestamp, NULL FROM user_messages"
            )
            rows = t.cast(
                "cabc.Iterable[tuple[object, object, object, object]]",
                connection.execute(query),
            )
            for uuid, message, timestamp, session in rows:
                text = decode_sqlite_value(message) or as_optional_str(message)
                if not text:
                    continue
                session_id = as_optional_str(session)
                yield SearchRecord(
                    kind="prompt",
                    agent=source.agent,
                    store=source.store,
                    adapter_id=source.adapter_id,
                    path=source.path,
                    text=text,
                    title="Claude SQLite user message",
                    role="user",
                    timestamp=as_optional_str(timestamp),
                    session_id=session_id,
                    conversation_id=session_id or as_optional_str(uuid),
                )
        if "assistant_messages" in tables:
            query = (
                """
                SELECT a.uuid, a.message, a.timestamp, a.model, b.session_id
                FROM assistant_messages a
                LEFT JOIN base_messages b ON b.uuid = a.uuid
                """
                if has_base
                else "SELECT uuid, message, timestamp, model, NULL FROM assistant_messages"
            )
            rows = t.cast(
                "cabc.Iterable[tuple[object, object, object, object, object]]",
                connection.execute(query),
            )
            for uuid, message, timestamp, model, session in rows:
                text = decode_sqlite_value(message) or as_optional_str(message)
                if not text:
                    continue
                session_id = as_optional_str(session)
                yield SearchRecord(
                    kind="history",
                    agent=source.agent,
                    store=source.store,
                    adapter_id=source.adapter_id,
                    path=source.path,
                    text=text,
                    title="Claude SQLite assistant message",
                    role="assistant",
                    timestamp=as_optional_str(timestamp),
                    model=as_optional_str(model),
                    session_id=session_id,
                    conversation_id=session_id or as_optional_str(uuid),
                )
        if "conversation_summaries" in tables:
            query = (
                """
                SELECT c.leaf_uuid, c.summary, c.updated_at, b.session_id
                FROM conversation_summaries c
                LEFT JOIN base_messages b ON b.uuid = c.leaf_uuid
                """
                if has_base
                else "SELECT leaf_uuid, summary, updated_at, NULL FROM conversation_summaries"
            )
            rows = t.cast(
                "cabc.Iterable[tuple[object, object, object, object]]",
                connection.execute(query),
            )
            for leaf_uuid, summary, updated_at, session in rows:
                text = decode_sqlite_value(summary) or as_optional_str(summary)
                if not text:
                    continue
                session_id = as_optional_str(session)
                yield SearchRecord(
                    kind="history",
                    agent=source.agent,
                    store=source.store,
                    adapter_id=source.adapter_id,
                    path=source.path,
                    text=text,
                    title="Claude conversation summary",
                    role="assistant",
                    timestamp=as_optional_str(updated_at),
                    session_id=session_id,
                    conversation_id=session_id or as_optional_str(leaf_uuid),
                )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_codex_state_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse opt-in Codex ``state_5.sqlite`` prompt-bearing fields."""
    connection = open_readonly_sqlite(source.path)
    try:
        tables = sqlite_table_names(connection)
        if "threads" in tables:
            columns = sqlite_column_names(connection, "threads")
            if {"id", "first_user_message"}.issubset(columns):
                preview_expr = "preview" if "preview" in columns else "NULL"
                title_expr = "title" if "title" in columns else "NULL"
                updated_expr = "updated_at_ms" if "updated_at_ms" in columns else "NULL"
                rows = t.cast(
                    "cabc.Iterable[tuple[object, object, object, object, object]]",
                    connection.execute(
                        "SELECT id, first_user_message, "
                        f"{preview_expr}, {title_expr}, {updated_expr} FROM threads",
                    ),
                )
                for thread_id, first_message, preview, title, updated_at in rows:
                    conversation_id = as_optional_str(thread_id)
                    thread_title = as_optional_str(title)
                    timestamp = _unix_millis_to_isoformat(updated_at)
                    text = decode_sqlite_value(first_message) or as_optional_str(first_message)
                    if text:
                        yield SearchRecord(
                            kind="prompt",
                            agent=source.agent,
                            store=source.store,
                            adapter_id=source.adapter_id,
                            path=source.path,
                            text=text,
                            title=thread_title or "Codex thread first prompt",
                            role="user",
                            timestamp=timestamp,
                            session_id=conversation_id,
                            conversation_id=conversation_id,
                        )
                    preview_text = decode_sqlite_value(preview) or as_optional_str(preview)
                    if preview_text and preview_text != text:
                        yield SearchRecord(
                            kind="history",
                            agent=source.agent,
                            store=source.store,
                            adapter_id=source.adapter_id,
                            path=source.path,
                            text=preview_text,
                            title=thread_title or "Codex thread preview",
                            role="assistant",
                            timestamp=timestamp,
                            session_id=conversation_id,
                            conversation_id=conversation_id,
                            metadata={"field": "preview"},
                        )
        if "agent_jobs" in tables:
            columns = sqlite_column_names(connection, "agent_jobs")
            if {"id", "instruction"}.issubset(columns):
                thread_expr = "thread_id" if "thread_id" in columns else "NULL"
                updated_expr = "updated_at_ms" if "updated_at_ms" in columns else "NULL"
                rows = t.cast(
                    "cabc.Iterable[tuple[object, object, object, object]]",
                    connection.execute(
                        f"SELECT id, {thread_expr}, instruction, {updated_expr} FROM agent_jobs",
                    ),
                )
                for job_id, thread_id, instruction, updated_at in rows:
                    text = decode_sqlite_value(instruction) or as_optional_str(instruction)
                    if not text:
                        continue
                    conversation_id = as_optional_str(thread_id)
                    yield SearchRecord(
                        kind="prompt",
                        agent=source.agent,
                        store=source.store,
                        adapter_id=source.adapter_id,
                        path=source.path,
                        text=text,
                        title="Codex agent job instruction",
                        role="user",
                        timestamp=_unix_millis_to_isoformat(updated_at),
                        session_id=conversation_id,
                        conversation_id=conversation_id,
                        metadata={"job_id": as_optional_str(job_id) or ""},
                    )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_codex_logs_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse opt-in Codex ``logs_2.sqlite`` feedback log bodies."""
    connection = open_readonly_sqlite(source.path)
    try:
        tables = sqlite_table_names(connection)
        if "logs" not in tables:
            return
        columns = sqlite_column_names(connection, "logs")
        if "feedback_log_body" not in columns:
            return
        id_expr = "id" if "id" in columns else "NULL"
        ts_expr = "ts" if "ts" in columns else "NULL"
        level_expr = "level" if "level" in columns else "NULL"
        target_expr = "target" if "target" in columns else "NULL"
        thread_expr = "thread_id" if "thread_id" in columns else "NULL"
        rows = t.cast(
            "cabc.Iterable[tuple[object, object, object, object, object, object]]",
            connection.execute(
                f"SELECT {id_expr}, {ts_expr}, {level_expr}, {target_expr}, "
                f"feedback_log_body, {thread_expr} FROM logs",
            ),
        )
        for row_id, timestamp, level, target, body, thread_id in rows:
            text = decode_sqlite_value(body) or as_optional_str(body)
            if not text:
                continue
            conversation_id = as_optional_str(thread_id)
            metadata: dict[str, object] = {}
            level_text = as_optional_str(level)
            target_text = as_optional_str(target)
            if level_text:
                metadata["level"] = level_text
            if target_text:
                metadata["target"] = target_text
            log_id = as_optional_str(row_id)
            if log_id and not metadata:
                metadata["log_id"] = log_id
            yield SearchRecord(
                kind="history",
                agent=source.agent,
                store=source.store,
                adapter_id=source.adapter_id,
                path=source.path,
                text=text,
                title="Codex feedback log",
                role="system",
                timestamp=as_optional_str(timestamp),
                session_id=conversation_id,
                conversation_id=conversation_id,
                metadata=metadata,
            )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_codex_memories_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse opt-in Codex ``memories_1.sqlite`` memory summaries."""
    connection = open_readonly_sqlite(source.path)
    try:
        tables = sqlite_table_names(connection)
        if "stage1_outputs" not in tables:
            return
        columns = sqlite_column_names(connection, "stage1_outputs")
        if not {"thread_id", "raw_memory"}.issubset(columns):
            return
        summary_expr = "rollout_summary" if "rollout_summary" in columns else "NULL"
        slug_expr = "rollout_slug" if "rollout_slug" in columns else "NULL"
        rows = t.cast(
            "cabc.Iterable[tuple[object, object, object, object]]",
            connection.execute(
                f"SELECT thread_id, raw_memory, {summary_expr}, {slug_expr} FROM stage1_outputs",
            ),
        )
        for thread_id, raw_memory, rollout_summary, rollout_slug in rows:
            conversation_id = as_optional_str(thread_id)
            for field_name, value in (
                ("raw_memory", raw_memory),
                ("rollout_summary", rollout_summary),
            ):
                text = decode_sqlite_value(value) or as_optional_str(value)
                if not text:
                    continue
                yield SearchRecord(
                    kind="history",
                    agent=source.agent,
                    store=source.store,
                    adapter_id=source.adapter_id,
                    path=source.path,
                    text=text,
                    title=as_optional_str(rollout_slug) or "Codex memory",
                    role="assistant",
                    session_id=conversation_id,
                    conversation_id=conversation_id,
                    metadata={"field": field_name},
                )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_codex_external_imports_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Codex external-agent session import ledgers as opt-in summaries."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    records = payload.get("records")
    if not isinstance(records, list):
        return
    for entry in records:
        if not isinstance(entry, dict):
            continue
        mapping = t.cast("dict[str, object]", entry)
        thread_id = (
            as_optional_str(mapping.get("imported_thread_id"))
            or as_optional_str(mapping.get("thread_id"))
            or as_optional_str(mapping.get("id"))
        )
        if not thread_id:
            continue
        source_path = as_optional_str(mapping.get("source_path"))
        metadata: dict[str, object] = {}
        content_hash = as_optional_str(mapping.get("content_hash"))
        if content_hash:
            metadata["content_hash"] = content_hash
        if source_path:
            metadata["source_name"] = pathlib.PurePath(source_path).name
        yield SearchRecord(
            kind="history",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=f"Imported external agent session {thread_id}",
            title="Codex external import",
            timestamp=as_optional_str(mapping.get("imported_at")),
            session_id=thread_id,
            conversation_id=thread_id,
            metadata=metadata,
        )


def parse_codex_goals_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse opt-in Codex ``goals_1.sqlite`` goal objectives."""
    connection = open_readonly_sqlite(source.path)
    try:
        tables = sqlite_table_names(connection)
        if "thread_goals" not in tables:
            return
        columns = sqlite_column_names(connection, "thread_goals")
        if not {"thread_id", "goal_id", "objective"}.issubset(columns):
            return
        status_expr = "status" if "status" in columns else "NULL"
        updated_expr = "updated_at_ms" if "updated_at_ms" in columns else "NULL"
        rows = t.cast(
            "cabc.Iterable[tuple[object, object, object, object, object]]",
            connection.execute(
                f"SELECT thread_id, goal_id, objective, {status_expr}, {updated_expr} "
                "FROM thread_goals",
            ),
        )
        for thread_id, goal_id, objective, status, updated_at in rows:
            text = decode_sqlite_value(objective) or as_optional_str(objective)
            if not text:
                continue
            conversation_id = as_optional_str(thread_id)
            yield SearchRecord(
                kind="prompt",
                agent=source.agent,
                store=source.store,
                adapter_id=source.adapter_id,
                path=source.path,
                text=text,
                title="Codex goal objective",
                role="user",
                timestamp=_unix_millis_to_isoformat(updated_at),
                session_id=conversation_id,
                conversation_id=conversation_id,
                metadata={
                    "goal_id": as_optional_str(goal_id) or "",
                    "status": as_optional_str(status) or "",
                },
            )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_grok_session_search_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse the Grok CLI ``session_search.sqlite`` FTS5 index.

    Table ``session_docs`` has columns: ``session_id``, ``cwd``,
    ``updated_at`` (unix seconds), ``title`` (generated), ``content``
    (full-text indexed session body), ``content_hash``.
    """
    connection = open_readonly_sqlite(source.path)
    try:
        try:
            cursor = connection.execute(
                "SELECT session_id, cwd, title, content, updated_at FROM session_docs",
            )
        except sqlite3.OperationalError:
            # Databases written before Grok recorded cwd lack the column;
            # keep their records searchable, just without origin metadata.
            cursor = connection.execute(
                "SELECT session_id, NULL, title, content, updated_at FROM session_docs",
            )
        for row in cursor:
            session_id_raw, cwd_raw, title_raw, content_raw, updated_at_raw = row
            text = content_raw if isinstance(content_raw, str) and content_raw.strip() else None
            if not text:
                continue
            session_id = as_optional_str(session_id_raw)
            yield SearchRecord(
                kind="history",
                agent=source.agent,
                store=source.store,
                adapter_id=source.adapter_id,
                path=source.path,
                text=text,
                title=title_raw if isinstance(title_raw, str) else None,
                role="assistant",
                timestamp=_unix_to_isoformat(updated_at_raw),
                session_id=session_id,
                conversation_id=session_id,
                origin=_record_origin(cwd=as_optional_str(cwd_raw)),
            )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def _opencode_json_object(raw: object) -> dict[str, object] | None:
    """Parse a JSON object from an OpenCode SQLite ``data`` text column."""
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except ValueError, TypeError:
        return None
    return t.cast("dict[str, object]", value) if isinstance(value, dict) else None


def _opencode_part_text(part_type: str, part_data: dict[str, object]) -> str | None:
    """Return the searchable text for an OpenCode message part.

    ``text``/``reasoning`` parts carry the prompt, reply, or model thinking
    under ``text``; ``subtask`` parts carry a ``prompt``/``description``.
    Other part types (tool, file, snapshot, patch, step markers, …) are
    metadata or opt-in and contribute no default-search text.
    """
    if part_type in {"text", "reasoning"}:
        return as_optional_str(part_data.get("text"))
    if part_type == "subtask":
        return as_optional_str(part_data.get("prompt")) or as_optional_str(
            part_data.get("description"),
        )
    return None


def _opencode_message_model(message_data: dict[str, object]) -> str | None:
    """Return a message's model id.

    Assistant messages carry a top-level ``modelID``; user messages nest the
    selected model under ``model.modelID`` (``{providerID, modelID}``).
    """
    model = as_optional_str(message_data.get("modelID"))
    if model:
        return model
    nested = message_data.get("model")
    if isinstance(nested, dict):
        return as_optional_str(t.cast("dict[str, object]", nested).get("modelID"))
    return None


def parse_opencode_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse an OpenCode ``opencode.db`` SQLite store.

    Joins ``part`` -> ``message`` -> ``session``: each text-bearing part
    becomes one record whose ``kind`` is derived from the joined message
    ``role`` (user -> prompt, else history), with the session title,
    working directory, and the message model/timestamp attached. The model
    id is top-level ``modelID`` on assistant messages and nested under
    ``model.modelID`` on user messages. Degrades gracefully when the expected
    tables or columns are absent.
    """
    connection = open_readonly_sqlite(source.path)
    try:
        if not {"session", "message", "part"}.issubset(sqlite_table_names(connection)):
            return
        cursor = connection.execute(
            "SELECT p.data, m.data, s.title, s.directory, s.id "
            "FROM part p "
            "JOIN message m ON p.message_id = m.id "
            "JOIN session s ON p.session_id = s.id "
            "ORDER BY s.id, m.id, p.id",
        )
        for part_raw, message_raw, title_raw, directory_raw, session_id_raw in cursor:
            part_data = _opencode_json_object(part_raw)
            if part_data is None:
                continue
            part_type = as_optional_str(part_data.get("type"))
            if not part_type:
                continue
            text = _opencode_part_text(part_type, part_data)
            if not text:
                continue
            message_data = _opencode_json_object(message_raw) or {}
            role = as_optional_str(message_data.get("role")) or "assistant"
            kind: t.Literal["prompt", "history"] = (
                "prompt" if role.casefold() in USER_ROLES else "history"
            )
            time_obj = message_data.get("time")
            created = (
                t.cast("dict[str, object]", time_obj).get("created")
                if isinstance(time_obj, dict)
                else None
            )
            session_id = as_optional_str(session_id_raw)
            directory = as_optional_str(directory_raw)
            yield SearchRecord(
                kind=kind,
                agent=source.agent,
                store=source.store,
                adapter_id=source.adapter_id,
                path=source.path,
                text=text,
                title=as_optional_str(title_raw),
                role=role,
                timestamp=_unix_millis_to_isoformat(created),
                model=_opencode_message_model(message_data),
                session_id=session_id,
                conversation_id=session_id,
                origin=_record_origin(cwd=directory),
                metadata={"directory": directory} if directory else {},
            )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_cursor_ai_tracking_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Cursor AI tracking summaries."""
    connection = open_readonly_sqlite(source.path)
    try:
        for row in iter_conversation_summaries(connection):
            (
                conversation_id,
                title,
                tldr,
                overview,
                bullets,
                model,
                mode,
                updated_at,
            ) = row
            text_parts = [
                part
                for part in (
                    as_optional_str(title),
                    as_optional_str(tldr),
                    as_optional_str(overview),
                    flatten_summary_bullets(bullets),
                )
                if part
            ]
            if not text_parts:
                continue
            yield SearchRecord(
                kind="history",
                agent=source.agent,
                store=source.store,
                adapter_id=source.adapter_id,
                path=source.path,
                text="\n\n".join(text_parts),
                title=as_optional_str(title),
                role="assistant",
                timestamp=as_optional_str(updated_at),
                model=as_optional_str(model),
                conversation_id=as_optional_str(conversation_id),
                metadata={"mode": as_optional_str(mode) or ""},
            )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_cursor_prompt_history(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a Cursor CLI ``prompt_history.json`` file.

    The file is a flat JSON array of strings — one entry per prompt the
    user typed into ``cursor-agent``, oldest first. It is the CLI's
    up-arrow recall buffer, giving Cursor the same prompt-history store
    the ``claude``/``codex``/``grok`` backends already expose. The file
    carries no per-entry timestamps, so the file mtime is used as a
    shared fallback.
    """
    payload = read_json_file(source.path)
    if not isinstance(payload, list):
        return
    timestamp = isoformat_from_mtime_ns(source.mtime_ns)
    seen: set[str] = set()
    for entry in payload:
        prompt = as_optional_str(entry)
        if prompt is None:
            continue
        prompt = prompt.strip()
        if not prompt or prompt in seen:
            continue
        seen.add(prompt)
        yield SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=prompt,
            title="Cursor CLI prompt history",
            role="user",
            timestamp=timestamp,
        )


_CURSOR_CHATS_MIN_TEXT = 16
"""Shortest decoded protobuf run treated as Cursor CLI chat text.

Long enough to drop field junk (model ids, UUIDs, time zones) while
keeping real prompts and assistant turns. Content-addressed child
hashes are stored as raw bytes, so they fail the UTF-8 gate before this
length check ever applies.
"""

_ANTIGRAVITY_PROTOBUF_MIN_TEXT = 16
"""Shortest decoded protobuf run treated as Antigravity transcript text."""


def parse_antigravity_cli_conversation_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Best-effort parse of an Antigravity CLI conversation SQLite database."""
    session_id = source.path.stem
    timestamp = isoformat_from_mtime_ns(source.mtime_ns)
    connection = open_readonly_sqlite(source.path)
    try:
        if "steps" not in sqlite_table_names(connection):
            return
        rows = t.cast(
            "cabc.Iterable[tuple[object, object, object]]",
            connection.execute("SELECT idx, step_payload, step_format FROM steps ORDER BY idx"),
        )
        seen: set[str] = set()
        for idx, payload, step_format in rows:
            if not isinstance(payload, (bytes, bytearray)):
                continue
            for text in iter_protobuf_text_fields(
                bytes(payload),
                min_length=_ANTIGRAVITY_PROTOBUF_MIN_TEXT,
            ):
                normalized = text.strip()
                if len(normalized) < _ANTIGRAVITY_PROTOBUF_MIN_TEXT or normalized in seen:
                    continue
                seen.add(normalized)
                yield SearchRecord(
                    kind="history",
                    agent=source.agent,
                    store=source.store,
                    adapter_id=source.adapter_id,
                    path=source.path,
                    text=normalized,
                    title="Antigravity CLI conversation",
                    role=None,
                    timestamp=timestamp,
                    session_id=session_id,
                    conversation_id=session_id,
                    metadata={
                        "step_index": idx,
                        "step_format": step_format,
                    },
                )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_antigravity_protobuf_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Best-effort parse of an Antigravity protobuf transcript file."""
    try:
        payload = source.path.read_bytes()
    except OSError:
        return
    session_id = source.path.stem
    timestamp = isoformat_from_mtime_ns(source.mtime_ns)
    title = (
        "Antigravity CLI transcript"
        if source.agent == "antigravity-cli"
        else "Antigravity IDE transcript"
    )
    seen: set[str] = set()
    for text in iter_protobuf_text_fields(payload, min_length=_ANTIGRAVITY_PROTOBUF_MIN_TEXT):
        normalized = text.strip()
        if len(normalized) < _ANTIGRAVITY_PROTOBUF_MIN_TEXT or normalized in seen:
            continue
        seen.add(normalized)
        yield SearchRecord(
            kind="history",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=normalized,
            title=title,
            role=None,
            timestamp=timestamp,
            session_id=session_id,
            conversation_id=session_id,
        )


def parse_cursor_cli_chats_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Best-effort parse of a Cursor CLI ``chats/*/store.db`` blob store.

    The CLI persists each session as content-addressed protobuf blobs in
    a ``blobs(id, data)`` table; agentgrep reads every blob (the sibling
    ``meta`` row's hex-encoded JSON metadata is not required).
    Cursor publishes no schema, so agentgrep walks the protobuf wire
    format generically (:func:`iter_protobuf_text_fields`) and surfaces
    the readable UTF-8 runs it finds. The adapter is versioned by
    observation date (``cursor_cli.chats_protobuf.v1``) because the layout
    is unofficial and may shift. The session UUID comes from the parent
    directory name.
    """
    session_uuid = source.path.parent.name
    timestamp = isoformat_from_mtime_ns(source.mtime_ns)
    connection = open_readonly_sqlite(source.path)
    try:
        if "blobs" not in sqlite_table_names(connection):
            return
        rows = t.cast(
            "cabc.Iterable[tuple[object]]",
            connection.execute("SELECT data FROM blobs"),
        )
        seen: set[str] = set()
        for (blob,) in rows:
            if not isinstance(blob, (bytes, bytearray)):
                continue
            for text in iter_protobuf_text_fields(bytes(blob), min_length=_CURSOR_CHATS_MIN_TEXT):
                normalized = text.strip()
                if len(normalized) < _CURSOR_CHATS_MIN_TEXT or normalized in seen:
                    continue
                seen.add(normalized)
                yield SearchRecord(
                    kind="history",
                    agent=source.agent,
                    store=source.store,
                    adapter_id=source.adapter_id,
                    path=source.path,
                    text=normalized,
                    title="Cursor CLI chat",
                    role=None,
                    timestamp=timestamp,
                    session_id=session_uuid,
                    conversation_id=session_uuid,
                )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_cursor_state_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Cursor ``state.vscdb`` tables with generic JSON extraction."""
    connection = open_readonly_sqlite(source.path)
    try:
        tables = sqlite_table_names(connection)
        candidate_tables = [name for name in ("ItemTable", "cursorDiskKV") if name in tables]
        source_origin = _cursor_workspace_hash_origin(source)
        seen: set[tuple[str | None, str, str | None, str | None]] = set()
        for table in candidate_tables:
            for key, raw_value in iter_key_value_rows(
                connection,
                table,
                key_tokens=CURSOR_STATE_TOKENS,
            ):
                decoded = decode_sqlite_value(raw_value)
                if decoded is None:
                    continue
                parsed = parse_embedded_json(decoded)
                if parsed is None:
                    continue
                candidates = itertools.chain(
                    iter_message_candidates(
                        parsed,
                        fallback_title=key,
                        fallback_conversation_id=key,
                        fallback_origin=source_origin,
                    ),
                    iter_cursor_prompt_candidates(
                        parsed,
                        fallback_conversation_id=key,
                        fallback_origin=source_origin,
                    ),
                )
                for candidate in candidates:
                    entry_key = (
                        candidate.role,
                        candidate.text,
                        candidate.timestamp,
                        candidate.conversation_id,
                    )
                    if entry_key in seen:
                        continue
                    seen.add(entry_key)
                    yield build_search_record(source, candidate)
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def _cursor_workspace_hash_origin(source: SourceHandle) -> RecordOrigin | None:
    """Return a hash-only origin for per-workspace Cursor state stores."""
    parent_name = source.path.parent.name
    if parent_name in {"globalStorage", "User"}:
        return None
    if source.path.name != "state.vscdb":
        return None
    return _record_origin(cwd_hash=parent_name)


def candidate_from_mapping(
    mapping: dict[str, object],
    *,
    timestamp: str | None,
    model: str | None,
    session_id: str | None,
    conversation_id: str | None,
    origin: RecordOrigin | None = None,
) -> MessageCandidate | None:
    """Extract one message candidate from a known message-like mapping."""
    role = extract_role(mapping)
    if role is None:
        return None
    text = extract_message_text(mapping)
    if not text:
        return None
    return MessageCandidate(
        role=role,
        text=text,
        title=extract_title(mapping),
        timestamp=timestamp or extract_timestamp(mapping),
        model=model or extract_model(mapping),
        session_id=session_id or extract_session_id(mapping),
        conversation_id=conversation_id or extract_conversation_id(mapping),
        origin=_origin_from_mapping(mapping, fallback=origin),
    )


def iter_message_candidates(
    value: object,
    *,
    fallback_title: str | None = None,
    fallback_conversation_id: str | None = None,
    fallback_origin: RecordOrigin | None = None,
) -> cabc.Iterator[MessageCandidate]:
    """Recursively walk a JSON value and yield message candidates.

    ``value`` is typed ``object`` so the recursive descent into dict values
    and list items needs no per-node ``cast`` — the ``isinstance`` guards
    below narrow it, and scalars are ignored.
    """
    if isinstance(value, dict):
        mapping = t.cast("dict[str, object]", value)
        # Origin extraction is ~14 lookups plus an allocation; skip it
        # for the many structural nodes without any origin key.
        origin = (
            _origin_from_mapping(mapping, fallback=fallback_origin)
            if mapping.keys() & _ORIGIN_MAPPING_KEYS
            else fallback_origin
        )
        role = extract_role(mapping)
        # Text extraction drives the recursive content flatten; it is only
        # used when a role is present, so skip it for the many role-less nodes.
        text = extract_message_text(mapping) if role is not None else None
        if role is not None and text:
            yield MessageCandidate(
                role=role,
                text=text,
                title=extract_title(mapping) or fallback_title,
                timestamp=extract_timestamp(mapping),
                model=extract_model(mapping),
                session_id=extract_session_id(mapping),
                conversation_id=extract_conversation_id(mapping) or fallback_conversation_id,
                origin=origin,
            )
        for nested in mapping.values():
            yield from iter_message_candidates(
                nested,
                fallback_title=fallback_title,
                fallback_conversation_id=fallback_conversation_id,
                fallback_origin=origin,
            )
    elif isinstance(value, list):
        for item in value:
            yield from iter_message_candidates(
                item,
                fallback_title=fallback_title,
                fallback_conversation_id=fallback_conversation_id,
                fallback_origin=fallback_origin,
            )


def iter_cursor_prompt_candidates(
    value: JSONValue | None,
    *,
    fallback_conversation_id: str | None = None,
    fallback_origin: RecordOrigin | None = None,
) -> cabc.Iterator[MessageCandidate]:
    """Yield user-prompt candidates from Cursor ``aiService.prompts`` data.

    Cursor stores typed prompts as ``{"prompts": [{"text": ...,
    "commandType": int}]}`` (or a bare list of such entries). These carry
    no ``role`` field, so :func:`iter_message_candidates` skips them even
    though every entry is a user prompt. This recovers them for both the
    global and per-workspace ``state.vscdb`` stores.
    """
    entries: list[object] = []
    if isinstance(value, dict):
        prompts = t.cast("dict[str, object]", value).get("prompts")
        if isinstance(prompts, list):
            entries = list(t.cast("list[object]", prompts))
    elif isinstance(value, list):
        entries = [
            item
            for item in t.cast("list[object]", value)
            if isinstance(item, dict) and "commandType" in t.cast("dict[str, object]", item)
        ]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = as_optional_str(t.cast("dict[str, object]", entry).get("text"))
        if not text:
            continue
        yield MessageCandidate(
            role="user",
            text=text,
            title=None,
            timestamp=None,
            model=None,
            session_id=None,
            conversation_id=fallback_conversation_id,
            origin=fallback_origin,
        )


def extract_role(mapping: dict[str, object]) -> str | None:
    """Extract a normalized role from a mapping."""
    for key in ("role", "sender", "author", "speaker"):
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested_mapping = t.cast("dict[str, object]", value)
            nested = as_optional_str(nested_mapping.get("role")) or as_optional_str(
                nested_mapping.get("name"),
            )
            if nested is not None:
                return nested
    return None


def extract_message_text(mapping: dict[str, object]) -> str | None:
    """Extract message text from common content fields."""
    for key in ("content", "text", "message", "body", "prompt", "value", "parts"):
        if key in mapping:
            flattened = flatten_content_value(mapping[key])
            if flattened:
                return flattened
    return None


def flatten_content_value(value: object) -> str | None:
    """Flatten a message content payload into text."""
    parts = list(iter_text_fragments(value))
    if not parts:
        return None
    return "\n".join(part for part in parts if part.strip()).strip() or None


def iter_text_fragments(
    value: object,
) -> cabc.Iterator[str]:
    """Yield text fragments from a nested content payload."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            yield stripped
        return
    if isinstance(value, list):
        for item in value:
            yield from iter_text_fragments(item)
        return
    if isinstance(value, dict):
        mapping = t.cast("dict[str, object]", value)
        for key in ("text", "content", "message", "body", "prompt", "value", "parts"):
            if key in mapping:
                yield from iter_text_fragments(mapping[key])


def extract_title(mapping: dict[str, object]) -> str | None:
    """Extract a title-like field."""
    for key in ("title", "name", "topic"):
        title = as_optional_str(mapping.get(key))
        if title is not None:
            return title
    return None


def extract_timestamp(mapping: dict[str, object]) -> str | None:
    """Extract a timestamp-like field."""
    for key in ("timestamp", "updatedAt", "createdAt", "ts"):
        timestamp = as_optional_str(mapping.get(key))
        if timestamp is not None:
            return timestamp
    return None


def extract_model(mapping: dict[str, object]) -> str | None:
    """Extract a model name."""
    for key in ("model", "modelName", "model_name"):
        model = as_optional_str(mapping.get(key))
        if model is not None:
            return model
    return None


def extract_session_id(mapping: dict[str, object]) -> str | None:
    """Extract a session identifier."""
    for key in ("session_id", "sessionId", "id"):
        value = as_optional_str(mapping.get(key))
        if value is not None:
            return value
    return None


def extract_conversation_id(mapping: dict[str, object]) -> str | None:
    """Extract a conversation identifier."""
    for key in ("conversation_id", "conversationId", "threadId"):
        value = as_optional_str(mapping.get(key))
        if value is not None:
            return value
    return None


def flatten_summary_bullets(value: object) -> str | None:
    """Flatten Cursor summary bullets."""
    if value is None:
        return None
    if isinstance(value, str):
        parsed = parse_embedded_json(value)
        if isinstance(parsed, list):
            bullets = [item for item in parsed if isinstance(item, str) and item.strip()]
            return "\n".join(f"- {item}" for item in bullets) if bullets else value.strip() or None
        return value.strip() or None
    if isinstance(value, (bytes, bytearray)):
        decoded = decode_sqlite_value(value)
        return flatten_summary_bullets(decoded)
    return None


def build_search_record(source: SourceHandle, candidate: MessageCandidate) -> SearchRecord:
    """Convert a parsed candidate into a normalized search record."""
    role = candidate.role.casefold() if candidate.role is not None else None
    kind: t.Literal["prompt", "history"] = "prompt" if role in USER_ROLES else "history"
    return SearchRecord(
        kind=kind,
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=candidate.text,
        title=candidate.title,
        role=candidate.role,
        timestamp=candidate.timestamp,
        model=candidate.model,
        session_id=candidate.session_id,
        conversation_id=candidate.conversation_id,
        origin=candidate.origin,
    )


def find_store_roles_for_type_filter(
    type_filter: FindSourceTypeFilter,
) -> DiscoveryStoreRoles:
    """Return catalogue roles that can satisfy a ``find --type`` filter."""
    if type_filter in {"prompts", "history"}:
        return PROMPT_HISTORY_STORE_ROLES
    if type_filter == "sessions":
        return CONVERSATION_STORE_ROLES
    return None


@functools.cache
def store_descriptor_for_record(store: str, adapter_id: str) -> StoreDescriptor | None:
    """Return the catalog descriptor for a normalized record's source store."""
    from agentgrep.store_catalog import CATALOG

    for descriptor in CATALOG.stores:
        for spec in descriptor.discovery:
            if spec.store == store and spec.adapter_id == adapter_id:
                return descriptor
    return None


def store_role_for_record(store: str, adapter_id: str) -> StoreRole | None:
    """Return the catalog role for a normalized record's source store."""
    descriptor = store_descriptor_for_record(store, adapter_id)
    if descriptor is None:
        return None
    return descriptor.role


def parse_grok_subagents(source: SourceHandle) -> cabc.Iterator[SearchRecord]:
    """Parse a Grok CLI subagent ``meta.json`` dispatch record.

    Each ``sessions/<project>/<session>/subagents/<subagent>/meta.json`` is a
    single JSON object describing one dispatched subagent: ``prompt`` (the
    delegated instruction), ``description``, ``subagent_type``, ``tool_calls``,
    and parent/child session linkage. The subagent's own conversation is not
    stored elsewhere, so the dispatch prompt is the only searchable record of
    the delegation — emitted here as supplementary conversation content.
    """
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    mapping = t.cast("dict[str, object]", payload)
    prompt = as_optional_str(mapping.get("prompt"))
    description = as_optional_str(mapping.get("description"))
    text = prompt or description
    if not text:
        return
    child_session_id = as_optional_str(mapping.get("child_session_id"))
    parent_session_id = as_optional_str(mapping.get("parent_session_id"))
    subagent_type = as_optional_str(mapping.get("subagent_type"))
    metadata: dict[str, object] = {}
    if subagent_type:
        metadata["subagent_type"] = subagent_type
    if parent_session_id:
        metadata["parent_session_id"] = parent_session_id
    yield SearchRecord(
        kind="prompt",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        title=description or "Grok subagent",
        role="user",
        timestamp=as_optional_str(mapping.get("started_at")),
        session_id=child_session_id,
        conversation_id=child_session_id or parent_session_id,
        metadata=metadata,
    )


def parse_claude_usage_facet(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a Claude Code ``usage-data/facets/<session>.json`` reflection.

    Each facet is Claude's own derived summary of a session; the readable
    natural-language fields are ``brief_summary``, ``underlying_goal``, and
    ``friction_detail``. Derived state, not transcript — emitted as one
    inspectable record.
    """
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    mapping = t.cast("dict[str, object]", payload)
    parts = [
        as_optional_str(mapping.get(key))
        for key in ("brief_summary", "underlying_goal", "friction_detail")
    ]
    text = "\n\n".join(part for part in parts if part)
    if not text:
        return
    session_id = as_optional_str(mapping.get("session_id"))
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        title="Claude session reflection",
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        session_id=session_id,
        conversation_id=session_id,
        metadata={"coverage": source.coverage.value},
    )


def parse_pi_context_mode_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a Pi context-mode session SQLite database.

    The ``session_events`` table records events (`type` =
    role/intent/decision/tool_call/file_read/blocker_resolved/data) with a
    JSON ``data`` payload. Each event's payload is emitted as one inspectable
    record. Rooted under ``~/.pi/context-mode/sessions/``; the file stem is
    ``sha256(project_dir)[:16]`` — a hashed ``cwd`` grouping holding multiple
    sessions, with each row carrying its own ``session_id``.
    """
    connection = open_readonly_sqlite(source.path)
    origin = _record_origin(cwd_hash=source.path.stem)
    try:
        if "session_events" not in sqlite_table_names(connection):
            return
        cursor = connection.execute(
            "SELECT session_id, type, data, created_at FROM session_events ORDER BY id",
        )
        for session_id_raw, type_raw, data_raw, created_raw in cursor:
            data_text = as_optional_str(data_raw)
            if not data_text or not data_text.strip():
                continue
            event_type = as_optional_str(type_raw) or "event"
            session_id = as_optional_str(session_id_raw)
            yield SearchRecord(
                kind="history",
                agent=source.agent,
                store=source.store,
                adapter_id=source.adapter_id,
                path=source.path,
                text=data_text,
                title=f"Pi context-mode {event_type}",
                role=event_type,
                timestamp=as_optional_str(created_raw),
                session_id=session_id,
                conversation_id=session_id,
                origin=origin,
            )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_antigravity_cli_transcript(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse an Antigravity CLI brain transcript JSONL log.

    Each line is a step record (`type`, `source`, `status`, `created_at`,
    `content`). Only string-valued `content` carries readable text — the
    assistant and tool turns here are the readable counterpart to the opaque
    protobuf ``conversations/<uuid>.db`` that the brain Markdown glob cannot
    reach.
    """
    parents = source.path.parents
    conversation_id = parents[2].name if len(parents) > 2 else None
    for event in _iter_jsonl(source.path):
        if not isinstance(event, dict):
            continue
        mapping = t.cast("dict[str, object]", event)
        content = mapping.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        record_type = as_optional_str(mapping.get("type")) or ""
        is_user = record_type == "USER_INPUT"
        metadata: dict[str, object] = {}
        if record_type:
            metadata["type"] = record_type
        yield SearchRecord(
            kind="prompt" if is_user else "history",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=content,
            role="user" if is_user else "assistant",
            timestamp=as_optional_str(mapping.get("created_at")),
            session_id=conversation_id,
            conversation_id=conversation_id,
            metadata=metadata,
        )


def parse_vscode_chat_session(source: SourceHandle) -> cabc.Iterator[SearchRecord]:
    """Parse a VS Code GitHub Copilot Chat ``chatSessions/<uuid>.json``.

    Each ``requests[]`` turn yields a user-prompt record (``message.text``) and,
    when present, an assistant record (the bare ``MarkdownString`` response parts
    with no ``kind``, joined). Tool-call names and the resolved workspace cwd are
    attached as metadata. Tolerates empty/draft turns and absent ``result``.

    Current sessions are a ``.jsonl`` mutation log rebuilt by
    :func:`_read_vscode_jsonl_session`; older ``.json`` sessions are one object.
    """
    if source.source_kind == "jsonl":
        mapping = _read_vscode_jsonl_session(source.path)
        if mapping is None:
            return
    else:
        payload = read_json_file(source.path)
        if not isinstance(payload, dict):
            return
        mapping = t.cast("dict[str, object]", payload)
    requests = mapping.get("requests")
    if not isinstance(requests, list):
        return
    session_id = as_optional_str(mapping.get("sessionId"))
    base_metadata: dict[str, object] = {}
    cwd = _vscode_workspace_cwd(source.path)
    origin = _record_origin(cwd=cwd)
    if cwd:
        base_metadata["cwd"] = cwd
    title: str | None = None
    for entry in requests:
        if not isinstance(entry, dict):
            continue
        request = t.cast("dict[str, object]", entry)
        timestamp = _unix_millis_to_isoformat(request.get("timestamp"))
        message = request.get("message")
        prompt = (
            as_optional_str(t.cast("dict[str, object]", message).get("text"))
            if isinstance(message, dict)
            else None
        )
        if prompt and title is None:
            title = prompt[:80]
        if prompt:
            yield SearchRecord(
                kind="prompt",
                agent=source.agent,
                store=source.store,
                adapter_id=source.adapter_id,
                path=source.path,
                text=prompt,
                title=title,
                role="user",
                timestamp=timestamp,
                session_id=session_id,
                conversation_id=session_id,
                origin=origin,
                metadata=dict(base_metadata),
            )
        reply = _vscode_response_text(request.get("response"))
        if reply:
            metadata = dict(base_metadata)
            tools = _vscode_tool_names(request.get("result"))
            if tools:
                metadata["tools"] = tools
            yield SearchRecord(
                kind="history",
                agent=source.agent,
                store=source.store,
                adapter_id=source.adapter_id,
                path=source.path,
                text=reply,
                title=title,
                role="assistant",
                timestamp=timestamp,
                model=as_optional_str(request.get("modelId")),
                session_id=session_id,
                conversation_id=session_id,
                origin=origin,
                metadata=metadata,
            )


def _vscode_child(node: object, key: object) -> object:
    """Return ``node[key]`` for a dict string key or list int index, else ``None``."""
    if isinstance(node, dict) and isinstance(key, str):
        return t.cast("dict[str, object]", node).get(key)
    if isinstance(node, list) and isinstance(key, int) and 0 <= key < len(node):
        return node[key]
    return None


def _read_vscode_jsonl_session(path: pathlib.Path) -> dict[str, object] | None:
    """Reconstruct a Copilot Chat session from a ``chatSessions/<uuid>.jsonl`` log.

    The newer serialization is a JSON-mutation log: the first ``kind: 0`` line
    carries the full session snapshot under ``v``; ``kind: 1`` lines set a value
    at key-path ``k``, and ``kind: 2`` lines replace the array at ``k`` from
    index ``i`` onward with ``v`` (truncate to ``i``, then append ``v``; a
    missing ``i`` appends, a missing ``v`` truncates). Replaying the log in file
    order rebuilds the ``requests`` list the single-object ``.json`` form stores
    directly, so one extraction handles both shapes.
    """
    session: dict[str, object] = {}
    for record in iter_jsonl(path):
        if not isinstance(record, dict):
            continue
        event = t.cast("dict[str, object]", record)
        kind = event.get("kind")
        value = event.get("v")
        if kind == 0:
            if isinstance(value, dict):
                session = t.cast("dict[str, object]", value)
            continue
        keys = event.get("k")
        if not (isinstance(keys, list) and keys):
            continue
        node: object = session
        for key in keys[:-1]:
            node = _vscode_child(node, key)
            if node is None:
                break
        else:
            last = keys[-1]
            if kind == 1 and isinstance(node, dict) and isinstance(last, str):
                t.cast("dict[str, object]", node)[last] = value
            elif (
                kind == 1
                and isinstance(node, list)
                and isinstance(last, int)
                and 0 <= last < len(node)
            ):
                t.cast("list[object]", node)[last] = value
            elif kind == 2:
                target = _vscode_child(node, last)
                if isinstance(target, list):
                    index = event.get("i")
                    tail = value if isinstance(value, list) else []
                    # kind:2 replaces the array from index `i` onward with `v`
                    # (truncate to `i`, then append `v`); a missing `i` appends
                    # and a missing `v` truncates.
                    cut = (
                        index
                        if isinstance(index, int) and 0 <= index <= len(target)
                        else len(target)
                    )
                    t.cast("list[object]", target)[cut:] = t.cast("list[object]", tail)
    return session or None


def _vscode_response_text(response: object) -> str | None:
    """Join the readable Markdown parts of a Copilot Chat response array.

    Assistant prose lives in the bare ``MarkdownString`` parts (no ``kind``,
    shape ``{value, supportHtml, supportThemeIcons}``); a forward-compatible
    ``markdownContent`` kind is also accepted. Tool-invocation, reference,
    progress, and warning parts are skipped.
    """
    if not isinstance(response, list):
        return None
    chunks: list[str] = []
    for part in response:
        if not isinstance(part, dict):
            continue
        mapping = t.cast("dict[str, object]", part)
        kind = mapping.get("kind")
        if kind is not None and kind != "markdownContent":
            continue
        value = mapping.get("value")
        if not isinstance(value, str):
            content = mapping.get("content")
            value = (
                t.cast("dict[str, object]", content).get("value")
                if isinstance(content, dict)
                else None
            )
        if isinstance(value, str):
            chunks.append(value)
    text = "".join(chunks).strip()
    return text or None


def _vscode_tool_names(result: object) -> list[str] | None:
    """Extract the tool names a Copilot Chat agent turn invoked, if any."""
    if not isinstance(result, dict):
        return None
    metadata = t.cast("dict[str, object]", result).get("metadata")
    if not isinstance(metadata, dict):
        return None
    rounds = t.cast("dict[str, object]", metadata).get("toolCallRounds")
    if not isinstance(rounds, list):
        return None
    names: list[str] = []
    for round_ in rounds:
        if not isinstance(round_, dict):
            continue
        calls = t.cast("dict[str, object]", round_).get("toolCalls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if isinstance(call, dict):
                name = as_optional_str(t.cast("dict[str, object]", call).get("name"))
                if name:
                    names.append(name)
    return names or None


def _vscode_workspace_cwd(session_path: pathlib.Path) -> str | None:
    """Resolve the project cwd for a chat session via its ``workspace.json``.

    ``workspaceStorage/<hash>/chatSessions/<uuid>.json`` has a sibling
    ``workspace.json`` whose ``folder`` URI names the opened directory. A
    ``vscode-remote://wsl+<distro>/<path>`` URI maps to the Linux path
    ``<path>``; ``file://`` URIs are unquoted. Returns ``None`` for windowless
    sessions (``globalStorage/emptyWindowChatSessions/``) with no workspace.
    """
    workspace_json = session_path.parent.parent / "workspace.json"
    payload = read_json_file(workspace_json)
    if not isinstance(payload, dict):
        return None
    folder = as_optional_str(t.cast("dict[str, object]", payload).get("folder"))
    if not folder:
        return None
    return _vscode_uri_to_path(folder)


def _vscode_uri_to_path(uri: str) -> str | None:
    """Map a VS Code folder URI to a local filesystem path.

    ``vscode-remote://wsl+<distro>/home/u/proj`` -> ``/home/u/proj``;
    ``file:///home/u/proj`` -> ``/home/u/proj``. Other remotes (ssh, dev
    container) return their path component too, best-effort.
    """
    remote = re.match(r"vscode-remote://[^/]+(/.*)$", uri)
    if remote:
        return urllib.parse.unquote(remote.group(1)) or None
    if uri.startswith("file://"):
        return urllib.parse.unquote(uri[len("file://") :]) or None
    return None


def parse_vscode_inline_history(source: SourceHandle) -> cabc.Iterator[SearchRecord]:
    """Parse the VS Code global ``state.vscdb`` inline-edit prompt history.

    The ``inline-chat-history`` ``ItemTable`` key holds a JSON array of the
    user's Ctrl+I inline-edit prompts. Token-filtered to that key alone, so the
    ``secret://`` auth keys in the same database are never read.
    """
    connection = open_readonly_sqlite(source.path)
    try:
        if "ItemTable" not in sqlite_table_names(connection):
            return
        for _key, raw_value in iter_key_value_rows(
            connection,
            "ItemTable",
            key_tokens=("inline-chat-history",),
        ):
            decoded = decode_sqlite_value(raw_value)
            if decoded is None:
                continue
            parsed = parse_embedded_json(decoded)
            if not isinstance(parsed, list):
                continue
            for item in parsed:
                if isinstance(item, str) and item.strip():
                    yield SearchRecord(
                        kind="prompt",
                        agent=source.agent,
                        store=source.store,
                        adapter_id=source.adapter_id,
                        path=source.path,
                        text=item,
                        title="VS Code inline chat",
                        role="user",
                        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
                    )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()
