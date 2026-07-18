"""Gemini CLI store parsers and registry fragment."""

from __future__ import annotations

import collections.abc as cabc
import pathlib
import typing as t

from agentgrep.adapters._common import (
    _path_like_str,
    _record_origin,
)
from agentgrep.adapters._extract import (
    _origin_from_mapping,
    build_search_record,
    flatten_content_value,
)
from agentgrep.adapters._generic import (
    parse_text_store_file,
)
from agentgrep.adapters._registry import AnyParserSpec, ParserSpec
from agentgrep.readers import (
    as_optional_str,
    iter_jsonl,
    read_json_file,
    read_text_file,
)
from agentgrep.records import (
    JSONValue,
    MessageCandidate,
    RecordOrigin,
    SearchRecord,
    SourceHandle,
)


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


_GEMINI_PROJECT_ROOT_FILE = ".project_root"


def _gemini_project_root_cwd(project_dir: pathlib.Path) -> str | None:
    """Resolve the literal cwd of a Gemini ``tmp/<project_hash>/`` directory.

    Gemini names the directory after a hash of the project and writes the
    literal path into a sibling ``.project_root`` file. All three Gemini
    prompt stores live under that one directory, so all three resolve their
    ``cwd`` here.

    A missing ``.project_root`` is ordinary — older trees have none — and
    yields ``None`` rather than raising; the ``cwd_hash`` taken from the
    directory name stands on its own.

    The resolved path is a *record* fact, not a source-level completeness
    claim: the per-record walk in :func:`_origin_from_mapping` can still read
    a different directory out of the payload, and a source that claimed
    ``cwd`` completeness while emitting a different ``cwd`` would prune away
    its own matching record.
    """
    return _path_like_str(read_text_file(project_dir / _GEMINI_PROJECT_ROOT_FILE))


def _gemini_directories_cwd(mapping: dict[str, object]) -> str | None:
    """Read ``directories[0]`` from a Gemini session-metadata record.

    Gemini names the session directory with a plural array where every other
    store uses a scalar, which is why ``_ORIGIN_MAPPING_KEYS`` — which knows
    ``cwd``, ``directory``, and ``workspace`` — cannot see it. The key is
    named here rather than added to that shared set, because the set is
    consulted at every nested node of every store's document walk.
    """
    directories = mapping.get("directories")
    if not isinstance(directories, list) or not directories:
        return None
    return _path_like_str(t.cast("list[object]", directories)[0])


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

    The literal ``cwd`` is the metadata record's ``directories[0]``, falling
    back to the sibling ``.project_root`` when the array is absent. The file
    sits at ``tmp/<project_hash>/chats/session-*.jsonl``, so the project
    directory — the ``cwd_hash`` — is two levels up.
    """
    session_id: str | None = None
    project_dir = source.path.parent.parent
    session_origin: RecordOrigin | None = _record_origin(
        cwd=_gemini_project_root_cwd(project_dir),
        cwd_hash=project_dir.name,
    )
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
            session_origin = _record_origin(
                cwd=_gemini_directories_cwd(mapping),
                fallback=_origin_from_mapping(mapping, fallback=session_origin),
            )
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

    The legacy record names only ``projectHash`` and never the path, so the
    literal ``cwd`` can come only from the sibling ``.project_root``. The
    file sits at ``tmp/<project_hash>/chats/session-*.json``.
    """
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    container = t.cast("dict[str, object]", payload)
    session_id = as_optional_str(container.get("sessionId"))
    project_dir = source.path.parent.parent
    session_origin = _origin_from_mapping(
        container,
        fallback=_record_origin(
            cwd=_gemini_project_root_cwd(project_dir),
            cwd_hash=project_dir.name,
        ),
    )
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

    No log entry carries a working directory, so the literal ``cwd`` comes
    from the sibling ``.project_root``. The file sits at
    ``tmp/<project_hash>/logs.json``, so the project directory is its parent.
    """
    payload = read_json_file(source.path)
    project_dir = source.path.parent
    origin = _record_origin(
        cwd=_gemini_project_root_cwd(project_dir),
        cwd_hash=project_dir.name,
    )
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


_GEMINI_PARSERS: tuple[AnyParserSpec, ...] = (
    ParserSpec("gemini.memory_text.v1", parse_text_store_file),
    ParserSpec("gemini.tool_outputs_text.v1", parse_text_store_file),
    ParserSpec("gemini.tmp_chats_jsonl.v1", parse_gemini_chat_file),
    ParserSpec("gemini.tmp_chats_legacy_json.v1", parse_gemini_chat_legacy_file),
    ParserSpec("gemini.tmp_logs_json.v1", parse_gemini_logs_file),
)
"""Dispatch rows for every ``gemini.*`` adapter id."""
