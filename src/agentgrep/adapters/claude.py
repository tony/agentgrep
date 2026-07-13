"""Claude Code store parsers and registry fragment."""

from __future__ import annotations

import collections.abc as cabc
import functools
import pathlib
import re
import sqlite3
import typing as t

from agentgrep.adapters._common import (
    _path_like_str,
    _record_origin,
    _unix_millis_to_isoformat,
)
from agentgrep.adapters._extract import (
    _record_position,
    build_search_record,
    extract_message_id,
    extract_parent_message_id,
    extract_session_id,
    iter_message_candidates,
)
from agentgrep.adapters._generic import (
    parse_file_metadata_summary_file,
    parse_hooks_summary_file,
    parse_json_summary_file,
    parse_text_store_file,
)
from agentgrep.adapters._registry import AnyParserSpec, ParserSpec, StreamParserSpec
from agentgrep.readers import (
    _iter_jsonl_positioned,
    as_optional_str,
    decode_sqlite_value,
    isoformat_from_mtime_ns,
    iter_jsonl,
    open_readonly_sqlite,
    read_json_file,
    read_text_file,
    sqlite_table_names,
)
from agentgrep.records import (
    RawJsonlSkipLine,
    SearchRecord,
    SourceHandle,
)


def parse_claude_project_file(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse Claude Code project JSONL files using lightweight heuristics."""
    conversation_id = source.path.stem
    events = (
        _iter_jsonl_positioned(
            source.path,
            skip_line=raw_skip_line,
            skip_line_mode="line",
            reverse=reverse,
        )
        if raw_skip_line is not None
        else _iter_jsonl_positioned(source.path, reverse=reverse)
    )
    for positioned_event in events:
        event = positioned_event.value
        if isinstance(event, dict) and event.get("isCompactSummary") is True:
            # `/compact` machine summaries are derived recaps, not user turns.
            continue
        mapping = t.cast("dict[str, object]", event) if isinstance(event, dict) else None
        session_id = extract_session_id(mapping) if mapping is not None else None
        candidates = iter_message_candidates(
            event,
            fallback_conversation_id=conversation_id,
        )
        for within_line, candidate in enumerate(candidates):
            candidate.session_id = session_id or candidate.session_id
            candidate.identity_namespace = "claude.session" if session_id is not None else None
            candidate.position = _record_position(
                native_id=extract_message_id(mapping) if mapping is not None else None,
                parent_native_id=(
                    extract_parent_message_id(mapping) if mapping is not None else None
                ),
                ordinal=positioned_event.source_ordinal(within_line),
            )
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
    for raw_index, event in enumerate(iter_jsonl(source.path)):
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
            identity_namespace=("claude.session" if session_id is not None else None),
            position=_record_position(ordinal=raw_index),
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
                    identity_namespace=("claude.session" if session_id is not None else None),
                    position=_record_position(native_id=uuid),
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
                    identity_namespace=("claude.session" if session_id is not None else None),
                    position=_record_position(native_id=uuid),
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
                    identity_namespace=("claude.session" if session_id is not None else None),
                )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


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
        identity_namespace=("claude.session" if session_id is not None else None),
    )


_CLAUDE_PARSERS: tuple[AnyParserSpec, ...] = (
    ParserSpec("claude.history_jsonl.v1", parse_claude_history_file),
    StreamParserSpec("claude.projects_jsonl.v1", parse_claude_project_file),
    ParserSpec("claude.store_sqlite.v1", parse_claude_store_db),
    ParserSpec("claude.tasks_json.v1", parse_claude_task_file),
    ParserSpec("claude.todos_json.v1", parse_claude_todo_file),
    ParserSpec("claude.teams_json.v1", parse_claude_team_file),
    ParserSpec("claude.settings_json.v1", parse_claude_settings_file),
    ParserSpec(
        "claude.app_state_json_summary.v1",
        functools.partial(parse_json_summary_file, label="Claude app state"),
    ),
    ParserSpec(
        "claude.file_metadata_summary.v1",
        functools.partial(parse_file_metadata_summary_file, label="Claude raw state"),
    ),
    ParserSpec(
        "claude.plugin_manifest_json.v1",
        functools.partial(parse_json_summary_file, label="Claude plugin manifest"),
    ),
    ParserSpec(
        "claude.plugin_hooks_json.v1",
        functools.partial(parse_hooks_summary_file, label="Claude plugin hooks"),
    ),
    ParserSpec("claude.commands_text.v1", parse_text_store_file),
    ParserSpec("claude.memory_text.v1", parse_text_store_file),
    ParserSpec("claude.projects_memory_text.v1", parse_text_store_file),
    ParserSpec("claude.plugin_instruction_text.v1", parse_text_store_file),
    ParserSpec("claude.project_instruction_text.v1", parse_text_store_file),
    ParserSpec("claude.session_memory_text.v1", parse_text_store_file),
    ParserSpec("claude.skills_text.v1", parse_text_store_file),
    ParserSpec("claude.plans_text.v1", parse_text_store_file),
    ParserSpec("claude.workflow_scripts_text.v1", parse_text_store_file),
    ParserSpec("claude.usage_facets_json.v1", parse_claude_usage_facet),
)
"""Dispatch rows for every ``claude.*`` adapter id."""
