"""Cursor CLI store parsers and registry fragment."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import json
import sqlite3
import typing as t

from agentgrep.adapters._common import (
    _discovered_origin,
    _record_origin,
)
from agentgrep.adapters._extract import (
    build_search_record,
    flatten_summary_bullets,
    iter_message_candidates,
)
from agentgrep.adapters._generic import (
    parse_text_store_file,
)
from agentgrep.adapters._registry import AnyParserSpec, ParserSpec
from agentgrep.origin import (
    origin_cwd_hash,
)
from agentgrep.readers import (
    as_optional_str,
    decode_sqlite_value,
    isoformat_from_mtime_ns,
    iter_conversation_summaries,
    iter_jsonl,
    iter_protobuf_text_fields,
    open_readonly_sqlite,
    read_json_file,
    sqlite_column_names,
    sqlite_table_names,
)
from agentgrep.records import (
    SearchRecord,
    SourceHandle,
)


def parse_cursor_cli_transcript(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a Cursor CLI agent transcript JSONL file.

    Each line is ``{"role": "user" | "assistant", "message": {"content": [...]}}``;
    ``iter_message_candidates`` handles the nested shape directly. Cursor
    transcripts carry no native per-turn timestamp, so the file's mtime is
    used as a session-level fallback.

    The working directory is not in the file either: Cursor CLI dash-encodes it
    into the ``projects/<name>/`` path segment, and discovery decodes that name
    against the filesystem. Records therefore carry ``origin.cwd`` only when the
    name reconstructs to exactly one directory that exists — a name consistent
    with two paths, or with none, leaves ``cwd`` unset rather than guessing at
    the repo the user would then filter on.
    """
    conversation_id = source.path.stem
    fallback_timestamp = isoformat_from_mtime_ns(source.mtime_ns)
    session_origin = _discovered_origin(source)
    seen: set[tuple[str | None, str, str | None, str | None]] = set()
    for event in iter_jsonl(source.path):
        for candidate in iter_message_candidates(
            event,
            fallback_conversation_id=conversation_id,
            fallback_origin=session_origin,
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


_CURSOR_CLI_MODEL_SENTINEL = "default"
"""``lastUsedModel`` placeholder Cursor CLI writes before a model is chosen.

It names no model. Surfacing it would make ``model:default`` a searchable
identity and would put a fake slug next to the real ones in a model breakdown.
"""


def _cursor_cli_chats_model(connection: sqlite3.Connection) -> str | None:
    """Read the session model from a Cursor CLI chat store's ``meta`` table.

    ``meta`` holds a single row keyed ``'0'`` whose value is hex-encoded UTF-8
    JSON; ``lastUsedModel`` inside it is the model the whole ``store.db``
    ran on, so one read serves every record the store yields.

    The projection is guarded rather than attempted: the caller wraps its scan
    in ``except sqlite3.DatabaseError``, so naming a column an older store lacks
    would not fail loudly — it would swallow the error and turn the entire store
    into zero records.
    """
    if "meta" not in sqlite_table_names(connection):
        return None
    if not {"key", "value"} <= sqlite_column_names(connection, "meta"):
        return None
    row = t.cast(
        "tuple[object] | None",
        connection.execute("SELECT value FROM meta WHERE key = '0'").fetchone(),
    )
    if row is None:
        return None
    encoded = as_optional_str(decode_sqlite_value(row[0]))
    if encoded is None:
        return None
    try:
        payload: object = json.loads(bytes.fromhex(encoded))
    except ValueError:
        # Covers the hex decode, the UTF-8 decode, and the JSON parse: the
        # value is unofficial, and a shape change must degrade to "no model".
        return None
    if not isinstance(payload, dict):
        return None
    model = as_optional_str(t.cast("dict[str, object]", payload).get("lastUsedModel"))
    if model is None or model == _CURSOR_CLI_MODEL_SENTINEL:
        return None
    return model


def parse_cursor_cli_chats_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Best-effort parse of a Cursor CLI ``chats/*/store.db`` blob store.

    The CLI persists each session as content-addressed protobuf blobs in
    a ``blobs(id, data)`` table; agentgrep reads every blob, and the sibling
    ``meta`` row names the session's model.
    Cursor publishes no schema, so agentgrep walks the protobuf wire
    format generically (:func:`iter_protobuf_text_fields`) and surfaces
    the readable UTF-8 runs it finds. The adapter is versioned by
    observation date (``cursor_cli.chats_protobuf.v1``) because the layout
    is unofficial and may shift. The session UUID comes from the parent
    directory name.

    The grandparent segment is a workspace digest, so it is a ``cwd_hash`` and
    never a ``cwd``: the literal path appears in this store only as unstructured
    bytes inside the blobs, interleaved with unrelated file paths, with no key
    that reliably yields it. ``origin.cwd`` therefore stays unset here — and a
    ``cwd_hash`` is never manufactured by hashing a path recovered elsewhere.
    """
    session_uuid = source.path.parent.name
    timestamp = isoformat_from_mtime_ns(source.mtime_ns)
    origin = _record_origin(cwd_hash=origin_cwd_hash(source.path.parent.parent.name))
    connection = open_readonly_sqlite(source.path)
    try:
        if "blobs" not in sqlite_table_names(connection):
            return
        model = _cursor_cli_chats_model(connection)
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
                    model=model,
                    session_id=session_uuid,
                    conversation_id=session_uuid,
                    origin=origin,
                )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


_CURSOR_CLI_PARSERS: tuple[AnyParserSpec, ...] = (
    ParserSpec("cursor_cli.skills_text.v1", parse_text_store_file),
    ParserSpec("cursor_cli.uploads_text.v1", parse_text_store_file),
    ParserSpec("cursor_cli.agent_tools_text.v1", parse_text_store_file),
    ParserSpec("cursor_cli.ai_tracking_sqlite.v1", parse_cursor_ai_tracking_db),
    ParserSpec("cursor_cli.transcripts_jsonl.v1", parse_cursor_cli_transcript),
    ParserSpec("cursor_cli.prompt_history_json.v1", parse_cursor_prompt_history),
    ParserSpec("cursor_cli.chats_protobuf.v1", parse_cursor_cli_chats_db),
)
"""Dispatch rows for every ``cursor_cli.*`` adapter id."""
