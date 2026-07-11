"""Antigravity CLI store parsers and registry fragment."""

from __future__ import annotations

import collections.abc as cabc
import sqlite3
import typing as t

from agentgrep.adapters._common import (
    _catalog_uuid_path_token,
    _path_like_str,
    _record_origin,
    _unix_millis_to_isoformat,
)
from agentgrep.adapters._extract import _record_position
from agentgrep.adapters._generic import (
    parse_text_store_file,
)
from agentgrep.adapters._registry import AnyParserSpec, ParserSpec, StreamParserSpec
from agentgrep.readers import (
    _iter_jsonl,
    as_optional_str,
    isoformat_from_mtime_ns,
    iter_protobuf_text_fields,
    open_readonly_sqlite,
    sqlite_column_names,
    sqlite_table_names,
)
from agentgrep.records import (
    RawJsonlSkipLine,
    SearchRecord,
    SourceHandle,
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
    has_forward_source_order = not reverse and raw_skip_line is None
    for raw_index, event in enumerate(events):
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
            identity_namespace=("antigravity.conversation" if session_id is not None else None),
            position=_record_position(
                ordinal=raw_index if has_forward_source_order else None,
            ),
        )


_ANTIGRAVITY_PROTOBUF_MIN_TEXT = 16
"""Shortest decoded protobuf run treated as Antigravity transcript text.

Applies to the plaintext protobuf blobs inside the Antigravity CLI
``conversations/<uuid>.db`` ``steps`` rows. The loose ``implicit/*.pb`` and
``conversations/*.pb`` artifacts are encrypted and are not parsed at all.
"""


_ANTIGRAVITY_MODEL_TABLES: tuple[str, ...] = ("gen_metadata", "executor_metadata")
"""Metadata tables whose protobuf ``data`` blob names the conversation's model.

``gen_metadata`` is the one that carries it; ``executor_metadata`` is read as a
fallback for databases that shape the metadata differently. Neither table is
guaranteed to exist: a conversation database written before Antigravity added
them has ``steps`` and nothing else, and must still yield its records.
"""


_ANTIGRAVITY_MODEL_PREFIX = "gemini-"
"""Prefix the ``model_enum`` values carry (``gemini-pro-agent``).

The blob is an unschema'd protobuf ``Struct``, so agentgrep matches the value's
shape rather than reading a key: the run stored *next to* the ``model_enum`` key
is an internal placeholder token, not a slug, so a key-directed lookup would
surface a name no user ever typed and no model breakdown could group.
"""


def _antigravity_conversation_model(connection: sqlite3.Connection) -> str | None:
    """Read the conversation model from an Antigravity CLI metadata table.

    The ``steps`` table this adapter parses carries no model. The model sits one
    table over, in a protobuf ``Struct`` in ``gen_metadata.data``, as a coarse
    ``model_enum`` value (``gemini-pro-agent``) rather than a version-pinned
    slug. It is a session-level property, so one read serves every record the
    database yields.

    Every lookup is guarded rather than attempted. The caller wraps its scan in
    ``except sqlite3.DatabaseError``, so naming a table or a column an older
    database lacks would not fail loudly — it would swallow the error and turn a
    readable conversation into zero records.

    Parameters
    ----------
    connection : sqlite3.Connection
        Read-only connection to one ``conversations/<uuid>.db``.

    Returns
    -------
    str or None
        The model enum value, or ``None`` when no metadata table names one.
    """
    tables = sqlite_table_names(connection)
    for table in _ANTIGRAVITY_MODEL_TABLES:
        if table not in tables or "data" not in sqlite_column_names(connection, table):
            continue
        rows = t.cast(
            "cabc.Iterable[tuple[object]]",
            # The table name is one of two module constants, never user input.
            connection.execute(f"SELECT data FROM {table}"),
        )
        for (blob,) in rows:
            if not isinstance(blob, (bytes, bytearray)):
                continue
            for text in iter_protobuf_text_fields(bytes(blob), min_length=1):
                value = text.strip()
                if value.startswith(_ANTIGRAVITY_MODEL_PREFIX):
                    return value
    return None


def parse_antigravity_cli_conversation_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Best-effort parse of an Antigravity CLI conversation SQLite database.

    The model is not in ``steps``; see :func:`_antigravity_conversation_model`.
    """
    session_id = source.path.stem
    native_session_id = _catalog_uuid_path_token(source)
    timestamp = isoformat_from_mtime_ns(source.mtime_ns)
    connection = open_readonly_sqlite(source.path)
    try:
        if "steps" not in sqlite_table_names(connection):
            return
        model = _antigravity_conversation_model(connection)
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
                    model=model,
                    session_id=session_id,
                    conversation_id=session_id,
                    metadata={
                        "step_index": idx,
                        "step_format": step_format,
                    },
                    identity_namespace=(
                        "antigravity.conversation" if native_session_id is not None else None
                    ),
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
    native_conversation_id = _catalog_uuid_path_token(source)
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
        step_index_raw = mapping.get("step_index")
        step_index = (
            step_index_raw
            if isinstance(step_index_raw, int)
            and not isinstance(step_index_raw, bool)
            and step_index_raw >= 0
            else None
        )
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
            identity_namespace=(
                "antigravity.conversation" if native_conversation_id is not None else None
            ),
            position=_record_position(native_id=step_index, ordinal=step_index),
        )


_ANTIGRAVITY_CLI_PARSERS: tuple[AnyParserSpec, ...] = (
    StreamParserSpec("antigravity_cli.history_jsonl.v1", parse_antigravity_cli_history_file),
    ParserSpec(
        "antigravity_cli.conversations_sqlite_protobuf.v1", parse_antigravity_cli_conversation_db
    ),
    ParserSpec("antigravity_cli.brain_text.v1", parse_text_store_file),
    ParserSpec("antigravity_cli.transcript_jsonl.v1", parse_antigravity_cli_transcript),
)
"""Dispatch rows for every ``antigravity_cli.*`` adapter id."""
