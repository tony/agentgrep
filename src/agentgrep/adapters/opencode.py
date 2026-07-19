"""OpenCode store parsers and registry fragment."""

from __future__ import annotations

import collections.abc as cabc
import json
import sqlite3
import typing as t

from agentgrep.adapters._common import (
    _record_origin,
    _unix_millis_to_isoformat,
)
from agentgrep.adapters._registry import AnyParserSpec, ParserSpec
from agentgrep.readers import (
    as_optional_str,
    open_readonly_sqlite,
    sqlite_table_names,
)
from agentgrep.records import (
    USER_ROLES,
    SearchRecord,
    SourceHandle,
)


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
            metadata: dict[str, object] = {}
            if directory:
                metadata["directory"] = directory
            # ``kind == "history"`` means a non-user role (assistant/tool output),
            # so tag it the same way build_search_record tags non-human turns.
            if kind == "history":
                metadata["human_typed"] = False
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
                metadata=metadata,
            )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


_OPENCODE_PARSERS: tuple[AnyParserSpec, ...] = (
    ParserSpec("opencode.db_sqlite.v1", parse_opencode_db),
)
"""Dispatch rows for every ``opencode.*`` adapter id."""
