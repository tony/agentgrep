"""pi (earendil-works/pi) store parsers and registry fragment."""

from __future__ import annotations

import collections.abc as cabc
import sqlite3
import typing as t

from agentgrep.adapters._common import (
    _path_like_str,
    _record_origin,
    _unix_millis_to_isoformat,
)
from agentgrep.adapters._extract import (
    build_search_record,
    flatten_content_value,
)
from agentgrep.adapters._registry import AnyParserSpec, ParserSpec, StreamParserSpec
from agentgrep.origin import (
    origin_cwd_hash,
)
from agentgrep.readers import (
    _PI_SESSION_HEADER_MARKER,
    _iter_jsonl,
    _keep_jsonl_header_lines,
    _read_first_matching_jsonl_record,
    as_optional_str,
    open_readonly_sqlite,
    sqlite_column_expr,
    sqlite_column_names,
    sqlite_table_names,
)
from agentgrep.records import (
    JSONValue,
    MessageCandidate,
    RawJsonlSkipLine,
    RecordOrigin,
    SearchRecord,
    SourceHandle,
)


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
        header = _read_first_matching_jsonl_record(
            source.path,
            _PI_SESSION_HEADER_MARKER,
            accept_record=lambda record: record.get("type") == "session",
        )
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


_PI_CWD_DIGEST_LENGTH: int = 16
"""Hex length of the digest Pi names a context-mode database after.

Pi truncates ``sha256(project_dir)`` to its first 16 characters, so the shape
guard in :func:`~agentgrep.origin.origin_cwd_hash` needs this width rather than
the 32-character default.
"""


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

    The directory that digest hashes is not lost: each row carries the absolute
    ``project_dir`` beside its payload, so a record gets a literal ``cwd`` *and*
    the ``cwd_hash`` the store named its file after. The two are the same fact
    in two encodings — ``sha256(cwd)[:16]`` reproduces the stem — so ``cwd_hash``
    keeps answering the hashed identity Pi itself uses while ``cwd`` makes the
    store reachable from a repo-scoped filter.

    ``project_dir`` is projected through
    :func:`~agentgrep.readers.sqlite_column_expr`: the column arrived in a
    migration, and naming it unconditionally would raise
    :exc:`sqlite3.OperationalError` into the swallowing ``except`` below, turning
    an older database into zero records rather than into records without a cwd.
    The stem is admitted as a ``cwd_hash`` only when it has a digest's shape, so
    a hand-copied ``backup.db`` sitting in the same directory does not publish
    its own name as a searchable project identity.
    """
    connection = open_readonly_sqlite(source.path)
    hash_origin = _record_origin(
        cwd_hash=origin_cwd_hash(source.path.stem, length=_PI_CWD_DIGEST_LENGTH),
    )
    # One database is one project directory, so the per-row column repeats. Memo
    # the origins rather than rebuilding one per event.
    origins: dict[str | None, RecordOrigin | None] = {}
    try:
        if "session_events" not in sqlite_table_names(connection):
            return
        columns = sqlite_column_names(connection, "session_events")
        project_dir_expr = sqlite_column_expr(columns, "project_dir")
        cursor = connection.execute(
            f"SELECT session_id, type, data, created_at, {project_dir_expr} "
            "FROM session_events ORDER BY id",
        )
        for session_id_raw, type_raw, data_raw, created_raw, project_dir_raw in cursor:
            data_text = as_optional_str(data_raw)
            if not data_text or not data_text.strip():
                continue
            event_type = as_optional_str(type_raw) or "event"
            session_id = as_optional_str(session_id_raw)
            project_dir = _path_like_str(project_dir_raw)
            if project_dir not in origins:
                origins[project_dir] = _record_origin(cwd=project_dir, fallback=hash_origin)
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
                origin=origins[project_dir],
            )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


_PI_PARSERS: tuple[AnyParserSpec, ...] = (
    StreamParserSpec("pi.sessions_jsonl.v1", parse_pi_session_file),
    ParserSpec("pi.context_mode_sqlite.v1", parse_pi_context_mode_db),
)
"""Dispatch rows for every ``pi.*`` adapter id."""
