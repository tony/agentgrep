"""Grok CLI store parsers and registry fragment."""

from __future__ import annotations

import collections.abc as cabc
import pathlib
import sqlite3
import typing as t

from agentgrep.adapters._common import (
    _record_origin,
    _unix_to_isoformat,
)
from agentgrep.adapters._extract import (
    flatten_content_value,
)
from agentgrep.adapters._generic import (
    parse_text_store_file,
)
from agentgrep.adapters._registry import AnyParserSpec, ParserSpec, StreamParserSpec
from agentgrep.origin import (
    OriginEncoding,
    decode_project_dir,
)
from agentgrep.readers import (
    _iter_jsonl,
    as_optional_str,
    open_readonly_sqlite,
    read_json_file,
)
from agentgrep.records import (
    JSONValue,
    RawJsonlSkipLine,
    RecordOrigin,
    SearchRecord,
    SourceHandle,
)


def _grok_project_dir_origin(directory: pathlib.Path) -> RecordOrigin | None:
    """Return the ``cwd`` origin for a Grok project directory.

    Grok keys its session tree by the working directory with the separators
    percent-escaped (``sessions/%2Fwork%2Fproj/``). ``%2F`` is a lossless
    escape, so the decode is a recovery rather than a guess — and it recovers
    the same absolute path ``grok.session_search`` records literally in
    ``session_docs.cwd``, which is what lets the JSONL transcript and the FTS
    index answer a ``cwd:`` filter with one working directory instead of two.

    A directory whose decoded name is not path-shaped is not a project
    directory and yields no origin.

    Examples
    --------
    >>> origin = _grok_project_dir_origin(pathlib.Path("%2Fwork%2Fproj"))
    >>> origin.cwd if origin else None
    '/work/proj'
    >>> _grok_project_dir_origin(pathlib.Path("session-1234")) is None
    True
    """
    return _record_origin(
        cwd=decode_project_dir(directory.name, encoding=OriginEncoding.URL),
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

    No line carries a working directory, but the file's own parent does:
    ``sessions/<url-encoded-cwd>/prompt_history.jsonl``. Decoding that name
    gives every prompt the ``cwd`` its session ran in.
    """
    session_origin = _grok_project_dir_origin(source.path.parent)
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
            origin=session_origin,
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

    An ``assistant`` line names the model that answered in ``model_id``. That
    key is Grok's alone — ``extract_model`` reads the ``model``/``modelName``
    spellings the other stores use, and teaching it ``model_id`` would apply a
    Grok-specific guess to every store's payload — so the parser names it here.

    The transcript sits one level below the project directory
    (``sessions/<url-encoded-cwd>/<session_uuid>/chat_history.jsonl``), so the
    ``cwd`` is decoded from the grandparent rather than the parent.
    """
    conversation_id = source.path.parent.name
    session_origin = _grok_project_dir_origin(source.path.parent.parent)
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
            model=as_optional_str(mapping.get("model_id")),
            session_id=conversation_id,
            conversation_id=conversation_id,
            origin=session_origin,
        )


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


_GROK_PARSERS: tuple[AnyParserSpec, ...] = (
    ParserSpec("grok.plans_text.v1", parse_text_store_file),
    ParserSpec("grok.memory_text.v1", parse_text_store_file),
    StreamParserSpec("grok.prompt_history_jsonl.v1", parse_grok_prompt_history),
    StreamParserSpec("grok.sessions_jsonl.v1", parse_grok_chat_history),
    ParserSpec("grok.session_search_sqlite.v1", parse_grok_session_search_db),
    ParserSpec("grok.subagents_json.v1", parse_grok_subagents),
)
"""Dispatch rows for every ``grok.*`` adapter id."""
