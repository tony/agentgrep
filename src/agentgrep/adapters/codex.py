"""Codex store parsers and registry fragment."""

from __future__ import annotations

import collections.abc as cabc
import datetime
import functools
import pathlib
import sqlite3
import typing as t

from agentgrep.adapters._common import (
    _path_like_str,
    _record_origin,
    _unix_millis_to_isoformat,
)
from agentgrep.adapters._extract import (
    _origin_from_mapping,
    _record_position,
    build_search_record,
    candidate_from_mapping,
    extract_message_id,
)
from agentgrep.adapters._generic import (
    parse_file_metadata_summary_file,
    parse_hooks_summary_file,
    parse_json_summary_file,
    parse_text_store_file,
    parse_toml_summary_file,
)
from agentgrep.adapters._registry import AnyParserSpec, ParserSpec, StreamParserSpec
from agentgrep.readers import (
    _CODEX_RAW_SKIP_MIN_BYTES,
    _CODEX_SESSION_META_MARKER,
    _file_size,
    _is_codex_function_call_output_line,
    _iter_jsonl,
    _keep_jsonl_header_lines,
    _read_first_matching_jsonl_record,
    as_optional_str,
    decode_sqlite_value,
    iter_jsonl,
    open_readonly_sqlite,
    read_json_file,
    sqlite_column_expr,
    sqlite_column_names,
    sqlite_table_names,
)
from agentgrep.records import (
    JSONValue,
    RawJsonlSkipLine,
    RecordOrigin,
    SearchRecord,
    SourceHandle,
)

_CODEX_TURN_CONTEXT_MARKER = '"type":"turn_context"'
"""Space-stripped prefix marker for a Codex per-turn context line."""


def _codex_session_meta_model(payload: dict[str, object]) -> str | None:
    """Read whatever model identity ``session_meta`` carries.

    In practice this is ``model_provider`` — a provider id, not a slug. It is
    the fallback for a rollout with no ``turn_context`` record, never the
    preferred value.

    Parameters
    ----------
    payload : dict[str, object]
        The ``session_meta`` event's payload mapping.

    Returns
    -------
    str or None
        The model identity, or ``None`` when the header names none.

    Examples
    --------
    >>> _codex_session_meta_model({"model_provider": "openai"})
    'openai'
    >>> _codex_session_meta_model({"id": "session-1"}) is None
    True
    """
    return (
        as_optional_str(payload.get("model"))
        or as_optional_str(payload.get("model_name"))
        or as_optional_str(payload.get("model_provider"))
    )


def _codex_turn_context_model(path: pathlib.Path) -> str | None:
    """Read the model slug from a rollout's first ``turn_context`` record.

    The complete-record scanner checks bounded prefixes and discards unrelated
    oversized lines in chunks, so model discovery is independent of an
    arbitrary byte offset without materializing those lines.

    Parameters
    ----------
    path : pathlib.Path
        The rollout JSONL file.

    Returns
    -------
    str or None
        The model slug, or ``None`` when the rollout carries no valid
        ``turn_context``.
    """

    def has_model(event: dict[str, object]) -> bool:
        if event.get("type") != "turn_context":
            return False
        payload = event.get("payload")
        return isinstance(payload, dict) and as_optional_str(payload.get("model")) is not None

    event = _read_first_matching_jsonl_record(
        path,
        _CODEX_TURN_CONTEXT_MARKER,
        accept_record=has_model,
    )
    payload = event.get("payload") if event is not None else None
    if not isinstance(payload, dict):
        return None
    return as_optional_str(t.cast("dict[str, object]", payload).get("model"))


def parse_codex_session_file(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse Codex session JSONL files.

    The model slug is not in the ``session_meta`` header: that header names
    ``model_provider``, the provider id (``openai``). Codex writes the slug
    into its per-turn ``turn_context`` records, so the session model is seeded
    from the first of those and takes precedence over anything ``session_meta``
    offers.

    Codex can change model mid-session, but attributing the change per record
    would mean decoding every ``turn_context`` line — thousands per session —
    through the raw text prefilter that exists to avoid exactly that, and the
    prefilter is installed on some read paths and not others, so the model
    would then depend on which one ran. A prefix-gated forward scan finds the
    first valid turn model and keeps it identical under forward, reverse, and
    prefiltered iteration.
    """
    session_id = source.path.stem
    native_session_id: str | None = None
    session_model: str | None = _codex_turn_context_model(source.path)
    session_origin: RecordOrigin | None = None
    if reverse:
        # Reverse iteration reads the leading session_meta header last,
        # so seed its state up front to keep emitted records canonical.
        header = _read_first_matching_jsonl_record(
            source.path,
            _CODEX_SESSION_META_MARKER,
            accept_record=lambda record: record.get("type") == "session_meta",
        )
        header_payload = header.get("payload") if header is not None else None
        if (
            header is not None
            and str(header.get("type", "")) == "session_meta"
            and isinstance(header_payload, dict)
        ):
            payload = t.cast("dict[str, object]", header_payload)
            native_session_id = as_optional_str(payload.get("id"))
            session_id = native_session_id or session_id
            session_origin = _origin_from_mapping(payload, fallback=session_origin)
            session_model = session_model or _codex_session_meta_model(payload)
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
    ordinal_is_available = not reverse and raw_skip_line is None and codex_skip_line is None
    for raw_index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", ""))
        payload = event.get("payload")
        if event_type == "session_meta" and isinstance(payload, dict):
            payload_map = t.cast("dict[str, object]", payload)
            observed_session_id = as_optional_str(payload_map.get("id"))
            if observed_session_id is not None:
                native_session_id = observed_session_id
                session_id = observed_session_id
            session_origin = _origin_from_mapping(payload_map, fallback=session_origin)
            # The turn_context slug wins: session_meta offers only a provider id.
            session_model = session_model or _codex_session_meta_model(payload_map)
            continue
        if event_type != "response_item" or not isinstance(payload, dict):
            continue
        payload_map = t.cast("dict[str, object]", payload)
        candidate = candidate_from_mapping(
            payload_map,
            timestamp=as_optional_str(event.get("timestamp")),
            model=session_model,
            session_id=session_id,
            conversation_id=session_id,
            origin=session_origin,
        )
        if candidate is None:
            continue
        candidate.identity_namespace = "codex.session" if native_session_id is not None else None
        candidate.position = _record_position(
            native_id=payload_map.get("id"),
            ordinal=raw_index if ordinal_is_available else None,
        )
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
    native_session_id = as_optional_str(session.get("id"))
    session_id = native_session_id or source.path.stem
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
    for raw_index, item in enumerate(items):
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
        candidate.identity_namespace = "codex.session" if native_session_id is not None else None
        candidate.position = _record_position(
            native_id=extract_message_id(t.cast("dict[str, object]", item)),
            ordinal=raw_index,
        )
        yield build_search_record(source, candidate)


def parse_codex_history_file(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse Codex prompt/command history files.

    Two shapes share this parser. The current ``history.jsonl`` entry is
    ``{session_id, ts, text}`` with ``ts`` in unix **seconds**; the legacy
    ``history.json`` entry is ``{command, timestamp}`` with ``timestamp`` a
    number in **milliseconds**, not the ISO string its name suggests, and no
    ``ts`` key to fall back on.

    Neither shape records a cwd, a branch, or a model: this is a flat prompt
    log, so ``origin`` and ``model`` stay ``None`` by design.
    """
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

    ordinal_is_available = not reverse and raw_skip_line is None
    for raw_index, entry in enumerate(entries):
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
        if timestamp is None:
            # Legacy history.json: `timestamp` is a millisecond number, so the
            # string accessor above yields None for it.
            timestamp = _unix_millis_to_isoformat(entry.get("timestamp"))
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
            identity_namespace=("codex.session" if session_id is not None else None),
            position=_record_position(
                ordinal=raw_index if ordinal_is_available else None,
            ),
        )


def parse_codex_session_index_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Codex ``session_index.jsonl`` records as opt-in thread summaries."""
    for raw_index, entry in enumerate(iter_jsonl(source.path)):
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
            identity_namespace=("codex.session" if session_id is not None else None),
            position=_record_position(ordinal=raw_index),
        )


_CodexThreadRow = tuple[
    object,  # id
    object,  # rollout_path
    object,  # first_user_message
    object,  # preview
    object,  # title
    object,  # updated_at_ms
    object,  # model
    object,  # cwd
    object,  # git_branch
    object,  # git_origin_url
]
"""Shape of the ``threads`` projection in :func:`parse_codex_state_db`."""


def parse_codex_state_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse opt-in Codex ``state_5.sqlite`` prompt-bearing fields.

    A ``threads`` row carries its canonical ``rollout_path``, ``model`` slug,
    and git context next to the text columns, so both records a thread yields
    are identified without re-reading the rollout file. ``git_origin_url`` is
    a remote URL and lands on :attr:`~agentgrep.records.RecordOrigin.remote`;
    ``git_sha`` has no origin field and is left on the row. Every one of those
    columns arrived in a migration, so each is projected through
    :func:`~agentgrep.readers.sqlite_column_expr` and an older database keeps
    working with ``NULL``.

    ``agent_jobs`` rows stay origin-less: the shipped table has no
    ``thread_id`` column to reach a ``threads`` row through, so there is
    nothing to join the model and cwd back from.
    """
    connection = open_readonly_sqlite(source.path)
    try:
        tables = sqlite_table_names(connection)
        if "threads" in tables:
            columns = sqlite_column_names(connection, "threads")
            if {"id", "first_user_message"}.issubset(columns):
                rollout_path_expr = sqlite_column_expr(columns, "rollout_path")
                preview_expr = sqlite_column_expr(columns, "preview")
                title_expr = sqlite_column_expr(columns, "title")
                updated_expr = sqlite_column_expr(columns, "updated_at_ms")
                model_expr = sqlite_column_expr(columns, "model")
                cwd_expr = sqlite_column_expr(columns, "cwd")
                branch_expr = sqlite_column_expr(columns, "git_branch")
                remote_expr = sqlite_column_expr(columns, "git_origin_url")
                thread_rows = t.cast(
                    "cabc.Iterable[_CodexThreadRow]",
                    connection.execute(
                        f"SELECT id, {rollout_path_expr}, first_user_message, "
                        f"{preview_expr}, {title_expr}, {updated_expr}, "
                        f"{model_expr}, {cwd_expr}, {branch_expr}, {remote_expr} "
                        "FROM threads",
                    ),
                )
                for (
                    thread_id,
                    rollout_path_raw,
                    first_message,
                    preview,
                    title,
                    updated_at,
                    model_raw,
                    cwd_raw,
                    branch_raw,
                    remote_raw,
                ) in thread_rows:
                    conversation_id = as_optional_str(thread_id)
                    rollout_path = decode_sqlite_value(rollout_path_raw) or as_optional_str(
                        rollout_path_raw,
                    )
                    thread_title = as_optional_str(title)
                    timestamp = _unix_millis_to_isoformat(updated_at)
                    model = as_optional_str(model_raw)
                    origin = _record_origin(
                        cwd=_path_like_str(cwd_raw),
                        branch=as_optional_str(branch_raw),
                        remote=as_optional_str(remote_raw),
                    )
                    text = decode_sqlite_value(first_message) or as_optional_str(first_message)
                    if text:
                        metadata: dict[str, object] = {"field": "first_user_message"}
                        if rollout_path is not None:
                            metadata["rollout_path"] = rollout_path
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
                            model=model,
                            session_id=conversation_id,
                            conversation_id=conversation_id,
                            metadata=metadata,
                            origin=origin,
                            identity_namespace=(
                                "codex.session" if conversation_id is not None else None
                            ),
                        )
                    preview_text = decode_sqlite_value(preview) or as_optional_str(preview)
                    if preview_text and preview_text != text:
                        metadata = {"field": "preview"}
                        if rollout_path is not None:
                            metadata["rollout_path"] = rollout_path
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
                            model=model,
                            session_id=conversation_id,
                            conversation_id=conversation_id,
                            metadata=metadata,
                            origin=origin,
                            identity_namespace=(
                                "codex.session" if conversation_id is not None else None
                            ),
                        )
        if "agent_jobs" in tables:
            columns = sqlite_column_names(connection, "agent_jobs")
            if {"id", "instruction"}.issubset(columns):
                thread_expr = sqlite_column_expr(columns, "thread_id")
                updated_expr = sqlite_column_expr(columns, "updated_at_ms")
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
                        identity_namespace=(
                            "codex.session" if conversation_id is not None else None
                        ),
                        position=_record_position(native_id=job_id),
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
        id_expr = sqlite_column_expr(columns, "id")
        ts_expr = sqlite_column_expr(columns, "ts")
        level_expr = sqlite_column_expr(columns, "level")
        target_expr = sqlite_column_expr(columns, "target")
        thread_expr = sqlite_column_expr(columns, "thread_id")
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
                identity_namespace=("codex.session" if conversation_id is not None else None),
                position=_record_position(native_id=row_id),
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
        summary_expr = sqlite_column_expr(columns, "rollout_summary")
        slug_expr = sqlite_column_expr(columns, "rollout_slug")
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
                    identity_namespace=("codex.session" if conversation_id is not None else None),
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
        status_expr = sqlite_column_expr(columns, "status")
        updated_expr = sqlite_column_expr(columns, "updated_at_ms")
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
                identity_namespace=("codex.session" if conversation_id is not None else None),
                position=_record_position(native_id=goal_id),
            )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


_CODEX_PARSERS: tuple[AnyParserSpec, ...] = (
    StreamParserSpec("codex.sessions_jsonl.v1", parse_codex_session_file),
    ParserSpec("codex.sessions_legacy_json.v1", parse_codex_legacy_session_file),
    StreamParserSpec("codex.history_json.v1", parse_codex_history_file),
    StreamParserSpec("codex.history_jsonl.v1", parse_codex_history_file),
    ParserSpec("codex.session_index_jsonl.v1", parse_codex_session_index_file),
    ParserSpec("codex.instructions_text.v1", parse_text_store_file),
    ParserSpec("codex.memories_text.v1", parse_text_store_file),
    ParserSpec("codex.plugin_instruction_text.v1", parse_text_store_file),
    ParserSpec("codex.project_skill_text.v1", parse_text_store_file),
    ParserSpec("codex.rules_text.v1", parse_text_store_file),
    ParserSpec("codex.skills_text.v1", parse_text_store_file),
    ParserSpec("codex.config_toml.v1", parse_toml_summary_file),
    ParserSpec("codex.config_backup_toml.v1", parse_toml_summary_file),
    ParserSpec("codex.project_config_toml.v1", parse_toml_summary_file),
    ParserSpec(
        "codex.app_state_json_summary.v1",
        functools.partial(parse_json_summary_file, label="Codex app state"),
    ),
    ParserSpec(
        "codex.file_metadata_summary.v1",
        functools.partial(parse_file_metadata_summary_file, label="Codex raw state"),
    ),
    ParserSpec(
        "codex.hooks_json.v1",
        functools.partial(parse_hooks_summary_file, label="Codex hooks"),
    ),
    ParserSpec(
        "codex.plugin_hooks_json.v1",
        functools.partial(parse_hooks_summary_file, label="Codex plugin hooks"),
    ),
    ParserSpec(
        "codex.plugin_manifest_json.v1",
        functools.partial(parse_json_summary_file, label="Codex plugin manifest"),
    ),
    ParserSpec(
        "codex.plugin_marketplace_json.v1",
        functools.partial(parse_json_summary_file, label="Codex plugin marketplace"),
    ),
    ParserSpec("codex.state_sqlite.v1", parse_codex_state_db),
    ParserSpec("codex.logs_sqlite.v1", parse_codex_logs_db),
    ParserSpec("codex.memories_sqlite.v1", parse_codex_memories_db),
    ParserSpec("codex.goals_sqlite.v1", parse_codex_goals_db),
    ParserSpec("codex.external_imports_json.v1", parse_codex_external_imports_file),
)
"""Dispatch rows for every ``codex.*`` adapter id."""
