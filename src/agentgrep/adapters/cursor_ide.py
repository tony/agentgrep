"""Cursor IDE (``state.vscdb``) store parsers and registry fragment."""

from __future__ import annotations

import collections.abc as cabc
import itertools
import sqlite3
import typing as t

from agentgrep.adapters._common import (
    _discovered_origin,
    _path_like_str,
    _record_origin,
)
from agentgrep.adapters._extract import (
    build_search_record,
    extract_message_text,
    extract_role,
    extract_timestamp,
    iter_message_candidates,
)
from agentgrep.adapters._registry import AnyParserSpec, ParserSpec
from agentgrep.origin import (
    origin_cwd_hash,
)
from agentgrep.readers import (
    as_optional_str,
    decode_sqlite_value,
    iter_key_value_rows,
    open_readonly_sqlite,
    parse_embedded_json,
    sqlite_table_names,
)
from agentgrep.records import (
    JSONValue,
    MessageCandidate,
    RecordOrigin,
    SearchRecord,
    SourceHandle,
)

_CURSOR_STATE_EXACT_KEYS: tuple[str, ...] = (
    "aiService.prompts",
    "workbench.panel.chat.composerData",
)


_CURSOR_COMPOSER_KEY_PREFIXES: tuple[str, ...] = ("composerData:", "bubbleId:")
"""``cursorDiskKV`` prefixes holding composer turns, model, cwd, and branch.

``composerData:<uuid>`` is the session document — ``modelConfig.modelName``,
``gitWorktree.worktreePath``, ``gitWorktree.branchName`` — and
``bubbleId:<uuid>:<uuid>`` is one turn of it, naming its own model under
``modelInfo.modelName``.

.. note::

   Cursor publishes no schema for these keys, so every read is guarded and a
   schema drift degrades to an absent field rather than a wrong one.
"""


_CURSOR_STATE_KEY_PREFIXES: tuple[str, ...] = (
    "workbench.panel.aichat.view",
    *_CURSOR_COMPOSER_KEY_PREFIXES,
)


_CURSOR_BUBBLE_ROLES: dict[int, str] = {1: "user", 2: "assistant"}
"""Cursor's numeric turn discriminator.

A composer turn carries ``type: 1`` / ``type: 2`` and **no** ``role`` key, so
:func:`extract_role` walks straight past it. Without this map the composer
records are read and then dropped, and the store emits nothing.
"""


def _cursor_nested_model(mapping: dict[str, object]) -> str | None:
    """Read Cursor's nested model name from a composer document or a turn.

    ``modelConfig.modelName`` (the ``composerData:`` document) and
    ``modelInfo.modelName`` (one ``bubbleId:`` turn) both sit one level down,
    where :func:`extract_model` — which reads a top-level ``model`` /
    ``modelName`` / ``model_name`` — cannot see them.

    Examples
    --------
    >>> _cursor_nested_model({"modelConfig": {"modelName": "claude-4.5-sonnet"}})
    'claude-4.5-sonnet'
    >>> _cursor_nested_model({"modelInfo": {"modelName": "gpt-5.4"}})
    'gpt-5.4'
    >>> _cursor_nested_model({"modelConfig": "unstructured"}) is None
    True
    """
    for parent_key in ("modelConfig", "modelInfo"):
        nested = mapping.get(parent_key)
        if not isinstance(nested, dict):
            continue
        name = as_optional_str(t.cast("dict[str, object]", nested).get("modelName"))
        if name:
            return name
    return None


def _cursor_worktree_origin(
    mapping: dict[str, object],
    *,
    fallback: RecordOrigin | None,
) -> RecordOrigin | None:
    """Read Cursor's ``gitWorktree`` block into an origin.

    ``worktreePath`` is the absolute working directory the composer session ran
    in and ``branchName`` is a whole-value branch name. Neither key is one of
    :data:`_ORIGIN_MAPPING_KEYS`, so the generic walk never sees them.
    """
    worktree = mapping.get("gitWorktree")
    if not isinstance(worktree, dict):
        return fallback
    worktree_map = t.cast("dict[str, object]", worktree)
    return _record_origin(
        cwd=_path_like_str(worktree_map.get("worktreePath")),
        branch=as_optional_str(worktree_map.get("branchName")),
        fallback=fallback,
    )


def _cursor_composer_id(key: str) -> str | None:
    """Return the composer uuid encoded in a ``cursorDiskKV`` key.

    ``composerData:<composer>`` and ``bubbleId:<composer>:<bubble>`` name the
    same conversation, so every turn of one session shares an id.

    Examples
    --------
    >>> _cursor_composer_id("composerData:c-1")
    'c-1'
    >>> _cursor_composer_id("bubbleId:c-1:b-9")
    'c-1'
    >>> _cursor_composer_id("aiService.prompts") is None
    True
    """
    parts = key.split(":")
    if len(parts) < 2 or not parts[1]:
        return None
    return parts[1]


def _cursor_bubble_role(mapping: dict[str, object]) -> str | None:
    """Return the role of one Cursor turn.

    The turn's role is the numeric ``type`` discriminator; ``bool`` is excluded
    because it is an ``int`` subclass and ``type: true`` is not a role. The two
    live values are pinned below because transposing them stays silent: the same
    turns are still emitted, only every ``role:`` predicate now answers with the
    other speaker.

    A turn carrying no numeric ``type`` falls back to the generic role walk,
    which is what a ``composerData:`` document itself takes.

    Examples
    --------
    >>> _cursor_bubble_role({"type": 1, "text": "a prompt"})
    'user'
    >>> _cursor_bubble_role({"type": 2, "text": "a reply"})
    'assistant'
    >>> _cursor_bubble_role({"type": 99}) is None
    True
    >>> _cursor_bubble_role({"type": True}) is None
    True
    >>> _cursor_bubble_role({"role": "user"})
    'user'
    """
    bubble_type = mapping.get("type")
    if isinstance(bubble_type, bool) or not isinstance(bubble_type, int):
        return extract_role(mapping)
    return _CURSOR_BUBBLE_ROLES.get(bubble_type)


def _iter_cursor_composer_candidates(
    parsed: object,
    *,
    key: str,
    fallback_origin: RecordOrigin | None,
) -> cabc.Iterator[MessageCandidate]:
    """Yield the turns of one ``composerData:`` or ``bubbleId:`` record.

    The model, working directory, and branch live on the enclosing document, so
    they are threaded down onto every turn it holds; a ``bubbleId:`` record is
    itself the turn, so the document is offered as one too.
    """
    if not isinstance(parsed, dict):
        return
    document = t.cast("dict[str, object]", parsed)
    origin = _cursor_worktree_origin(document, fallback=fallback_origin)
    model = _cursor_nested_model(document)
    conversation_id = as_optional_str(document.get("composerId")) or _cursor_composer_id(key) or key
    bubbles: list[object] = [document]
    conversation = document.get("conversation")
    if isinstance(conversation, list):
        bubbles.extend(t.cast("list[object]", conversation))
    for bubble in bubbles:
        if not isinstance(bubble, dict):
            continue
        bubble_map = t.cast("dict[str, object]", bubble)
        role = _cursor_bubble_role(bubble_map)
        if role is None:
            continue
        text = extract_message_text(bubble_map)
        if not text:
            continue
        yield MessageCandidate(
            role=role,
            text=text,
            title=None,
            timestamp=extract_timestamp(bubble_map),
            model=_cursor_nested_model(bubble_map) or model,
            session_id=conversation_id,
            conversation_id=conversation_id,
            origin=_cursor_worktree_origin(bubble_map, fallback=origin),
        )


def parse_cursor_state_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Cursor ``state.vscdb`` tables with generic JSON extraction.

    ``cursorDiskKV`` also holds the ``composerData:`` and ``bubbleId:`` records
    carrying the model, working directory, and branch. Those need a
    Cursor-shaped reader — a numeric turn ``type``, a nested ``modelConfig`` or
    ``modelInfo``, a ``gitWorktree`` block — so they run through
    :func:`_iter_cursor_composer_candidates` first and the generic walk after;
    the dedupe then keeps the enriched candidate when both produce one turn.
    """
    connection = open_readonly_sqlite(source.path)
    try:
        tables = sqlite_table_names(connection)
        candidate_tables = [name for name in ("ItemTable", "cursorDiskKV") if name in tables]
        source_origin = _cursor_workspace_origin(source)
        seen: set[tuple[str | None, str, str | None, str | None]] = set()
        for table in candidate_tables:
            for key, raw_value in iter_key_value_rows(
                connection,
                table,
                exact_keys=_CURSOR_STATE_EXACT_KEYS,
                key_prefixes=_CURSOR_STATE_KEY_PREFIXES,
            ):
                decoded = decode_sqlite_value(raw_value)
                if decoded is None:
                    continue
                parsed = parse_embedded_json(decoded)
                if parsed is None:
                    continue
                is_composer = key.startswith(_CURSOR_COMPOSER_KEY_PREFIXES)
                conversation_key = (_cursor_composer_id(key) if is_composer else None) or key
                candidate_iters: list[cabc.Iterator[MessageCandidate]] = []
                if is_composer:
                    candidate_iters.append(
                        _iter_cursor_composer_candidates(
                            parsed,
                            key=key,
                            fallback_origin=source_origin,
                        ),
                    )
                candidate_iters.append(
                    iter_message_candidates(
                        parsed,
                        fallback_title=key,
                        fallback_conversation_id=conversation_key,
                        fallback_origin=source_origin,
                    ),
                )
                candidate_iters.append(
                    iter_cursor_prompt_candidates(
                        parsed,
                        fallback_conversation_id=conversation_key,
                        fallback_origin=source_origin,
                    ),
                )
                for candidate in itertools.chain(*candidate_iters):
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


def _cursor_workspace_origin(source: SourceHandle) -> RecordOrigin | None:
    """Return the source-level origin for a per-workspace Cursor state store.

    Only ``workspaceStorage/<md5>/state.vscdb`` carries a workspace digest. The
    global and legacy databases sit under an ordinary directory name, so the
    parent segment is admitted only when it has a digest's shape — otherwise
    the legacy ``~/.cursor/state.vscdb`` would report a ``cwd_hash`` of
    ``.cursor``, a searchable value no Cursor build ever wrote.
    """
    if source.path.name != "state.vscdb":
        return None
    discovered = _discovered_origin(source)
    if discovered is not None:
        return discovered
    return _record_origin(cwd_hash=origin_cwd_hash(source.path.parent.name))


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


_CURSOR_IDE_PARSERS: tuple[AnyParserSpec, ...] = (
    ParserSpec("cursor_ide.state_vscdb_modern.v1", parse_cursor_state_db),
    ParserSpec("cursor_ide.state_vscdb_legacy.v1", parse_cursor_state_db),
)
"""Dispatch rows for every ``cursor_ide.*`` adapter id."""
