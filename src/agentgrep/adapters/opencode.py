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
    _iter_jsonl,
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


def _opencode_expand_pastes(text: str, parts: object) -> str:
    r"""Splice each pasted part's text over its placeholder in ``text``.

    A part's ``source.text`` span names where its placeholder sits in the
    typed prompt. A span that does not match ``text`` leaves the placeholder
    as typed, the way a missing Claude Code paste keeps its own.

    Examples
    --------
    >>> span = {"start": 4, "end": 21, "value": "[Pasted ~2 lines]"}
    >>> part = {"type": "text", "text": "a\nb", "source": {"text": span}}
    >>> _opencode_expand_pastes("fix [Pasted ~2 lines] now", [part])
    'fix a\nb now'
    >>> _opencode_expand_pastes("fix it", [part])
    'fix it'
    """
    if not isinstance(parts, list):
        return text
    spans: list[tuple[int, int, str]] = []
    for part in t.cast("list[object]", parts):
        if not isinstance(part, dict):
            continue
        mapping = t.cast("dict[str, object]", part)
        pasted = as_optional_str(mapping.get("text"))
        source = mapping.get("source")
        span = t.cast("dict[str, object]", source).get("text") if isinstance(source, dict) else None
        if pasted is None or not isinstance(span, dict):
            continue
        span_map = t.cast("dict[str, object]", span)
        start, end = span_map.get("start"), span_map.get("end")
        if (
            isinstance(start, int)
            and isinstance(end, int)
            and 0 <= start <= end <= len(text)
            and text[start:end] == span_map.get("value")
        ):
            spans.append((start, end, pasted))
    last_start = len(text)
    for start, end, pasted in sorted(spans, reverse=True):
        if end > last_start:
            continue
        text = text[:start] + pasted + text[end:]
        last_start = start
    return text


def parse_opencode_prompt_history(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse OpenCode's ``prompt-history.jsonl`` recall log.

    Each line is ``{input, parts, mode}``: the prompt as typed, where a
    pasted block shows as a placeholder such as ``[Pasted ~43 lines]``, plus
    one ``parts`` entry per paste. Each paste is spliced back over its
    placeholder, so pasted text is searchable.

    The log carries no timestamp and no session id, and none is invented:
    the records sort after dated ones, date filters exclude them, and each
    belongs to no session. ``mode`` is kept as metadata.
    """
    for event in _iter_jsonl(source.path):
        if not isinstance(event, dict):
            continue
        mapping = t.cast("dict[str, object]", event)
        typed = as_optional_str(mapping.get("input"))
        if not typed:
            continue
        mode = as_optional_str(mapping.get("mode"))
        yield SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=_opencode_expand_pastes(typed, mapping.get("parts")),
            title="OpenCode prompt history",
            role="user",
            metadata={"mode": mode} if mode else {},
        )


_OPENCODE_PARSERS: tuple[AnyParserSpec, ...] = (
    ParserSpec("opencode.db_sqlite.v1", parse_opencode_db),
    ParserSpec("opencode.prompt_history_jsonl.v1", parse_opencode_prompt_history),
)
"""Dispatch rows for every ``opencode.*`` adapter id."""
