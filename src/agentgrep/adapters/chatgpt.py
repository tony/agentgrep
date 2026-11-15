"""ChatGPT desktop adapters: the Windows conversation lists, and metadata summaries.

The Windows app keeps no conversation bodies on disk, but it caches the
sidebar's conversation list (id, title, timestamps) twice: in Local Storage,
which :func:`parse_chatgpt_conversation_list` reads, and in the HTTP cache,
whose list responses also cover project conversations and whose scheduled-task
list holds the prompts the user scheduled, which :func:`parse_chatgpt_http_cache`
reads. The macOS app's conversation and draft files are encrypted, and the
Windows IndexedDB holds no conversations, so those rows stay catalog-only and
inventory surfaces describe them by size and modification time only.
"""

from __future__ import annotations

import datetime
import functools
import json
import math
import re
import typing as t

from agentgrep._chromium_cache import read_blockfile_cache
from agentgrep._chromium_storage import local_storage_values
from agentgrep.adapters._common import _unix_to_isoformat
from agentgrep.adapters._generic import parse_file_metadata_summary_file
from agentgrep.adapters._registry import AnyParserSpec, ParserSpec
from agentgrep.readers import as_optional_str, isoformat_from_mtime_ns
from agentgrep.records import SearchRecord

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from agentgrep.records import SourceHandle

CHATGPT_ORIGIN = "https://chatgpt.com"
"""Origin whose Local Storage the Windows app writes."""

_CONVERSATION_LIST_SUFFIXES = ("/conversation-history", "/conversation-history-without-projects")
"""Local Storage key endings of the cached conversation list.

Keys are ``cache/user-<account>/<id>/conversation-history`` and the same with
``-without-projects``; their values are the app's cached first page of
``/backend-api/conversations``. No other key in the store is decoded.
"""


def _conversation_items(payload: object) -> cabc.Iterator[dict[str, object]]:
    """Yield the conversation items of one cached ``conversation-history`` value."""
    if not isinstance(payload, dict):
        return
    value = t.cast("dict[str, object]", payload).get("value")
    pages = t.cast("dict[str, object]", value).get("pages") if isinstance(value, dict) else None
    if not isinstance(pages, list):
        return
    for page in pages:
        items = t.cast("dict[str, object]", page).get("items") if isinstance(page, dict) else None
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    yield t.cast("dict[str, object]", item)


def _item_time(item: dict[str, object], key: str) -> str | None:
    """Return an item timestamp as ISO 8601; the API sends strings, older caches epoch seconds."""
    value = item.get(key)
    return value if isinstance(value, str) and value else _unix_to_isoformat(value)


def _item_order(item: dict[str, object]) -> float:
    """Return an item's ``update_time`` as epoch seconds for newest-wins, ``-inf`` if unknown.

    Comparing the ISO strings would misorder an epoch value rendered without a
    fraction (``...:20Z``) against an API string with one (``...:20.900000Z``).
    """
    value = item.get("update_time")
    if isinstance(value, bool):
        return -math.inf
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value).timestamp()
        except ValueError:
            return -math.inf
    return -math.inf


def _keep_newest(
    newest: dict[str, dict[str, object]],
    items: cabc.Iterable[dict[str, object]],
) -> None:
    """Keep the newest titled snapshot of each conversation, by ``update_time``."""
    for item in items:
        conversation_id = as_optional_str(item.get("id"))
        if not conversation_id or not as_optional_str(item.get("title")):
            continue
        current = newest.get(conversation_id)
        if current is None or _item_order(item) >= _item_order(current):
            newest[conversation_id] = item


def _title_records(
    source: SourceHandle,
    newest: dict[str, dict[str, object]],
) -> cabc.Iterator[SearchRecord]:
    """Yield one title record per conversation, keyed by its ChatGPT id."""
    for conversation_id, item in newest.items():
        title = as_optional_str(item.get("title")) or ""
        metadata: dict[str, object] = {
            key: value
            for key, value in (
                ("created_at", _item_time(item, "create_time")),
                ("project_id", as_optional_str(item.get("gizmo_id"))),
            )
            if value
        }
        for flag, name in (
            ("is_archived", "archived"),
            ("is_starred", "starred"),
            ("is_temporary_chat", "temporary"),
        ):
            if item.get(flag) is True:
                metadata[name] = True
        yield SearchRecord(
            kind="history",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=title,
            title=title,
            role="assistant",
            timestamp=_item_time(item, "update_time")
            or _item_time(item, "create_time")
            or isoformat_from_mtime_ns(source.mtime_ns),
            session_id=conversation_id,
            conversation_id=conversation_id,
            metadata=metadata,
        )


def parse_chatgpt_conversation_list(source: SourceHandle) -> cabc.Iterator[SearchRecord]:
    """Parse ChatGPT desktop's cached conversation list as one title record each.

    ``source.path`` is the ``CURRENT`` file of the app's ``Local Storage/leveldb``
    directory; the whole directory is read, without a LevelDB library and
    without taking its lock. Only the conversation-list keys are decoded: the
    store's other entries (session data, device ids, secrets) are never read.
    Each conversation id appears in several cached snapshots; the newest one
    (by ``update_time``) wins. The list is the app's cache, not the account's
    history: it keeps every conversation any cached snapshot lists (one per
    account and per projects view), so a conversation deleted or moved off
    the first page since the app last refreshed can still appear.

    Parameters
    ----------
    source : SourceHandle
        The ``CURRENT`` file of ``Local Storage/leveldb`` in the app's profile.

    Yields
    ------
    SearchRecord
        One ``history`` record per titled conversation; its ``session_id`` is
        the ChatGPT conversation id.
    """
    values = local_storage_values(
        source.path.parent,
        CHATGPT_ORIGIN,
        lambda key: key.endswith(_CONVERSATION_LIST_SUFFIXES),
    )
    newest: dict[str, dict[str, object]] = {}
    for raw in values.values():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        _keep_newest(newest, _conversation_items(payload))
    yield from _title_records(source, newest)


_CACHED_LIST_URL = re.compile(
    r"^https://chatgpt\.com/backend-api/"
    r"(?P<kind>conversations|gizmos/[^/?#]+/conversations|tasks)(?:[?#]|$)",
)
"""Cached API responses read: the conversation list, a project's list, and scheduled tasks."""


def _dict_items(payload: object, key: str) -> list[dict[str, object]]:
    """Return the dict entries of list ``payload[key]``, or none."""
    values = t.cast("dict[str, object]", payload).get(key) if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return []
    return [t.cast("dict[str, object]", value) for value in values if isinstance(value, dict)]


def parse_chatgpt_http_cache(source: SourceHandle) -> cabc.Iterator[SearchRecord]:
    """Parse ChatGPT desktop's HTTP cache: conversation titles and scheduled-task prompts.

    ``source.path`` is the ``index`` file of the app's ``Cache/Cache_Data``
    blockfile cache, read by :func:`agentgrep._chromium_cache.read_blockfile_cache`
    without Chromium and without its lock. Only three kinds of cached response
    are read: the conversation list, each project's conversation list, and the
    scheduled-task list. Other entries, including account and session
    responses, are never read past their URL. Conversations yield title records
    like :func:`parse_chatgpt_conversation_list` (newest snapshot per id), and a
    scheduled task with a non-empty ``prompt`` yields that prompt as a user
    record; a task's model output (``messages``, ``final_message``) is not read.

    Parameters
    ----------
    source : SourceHandle
        The ``index`` file of ``Cache/Cache_Data`` in the app's profile.

    Yields
    ------
    SearchRecord
        ``history`` title records keyed by conversation id, then ``prompt``
        records for scheduled tasks.
    """
    responses = read_blockfile_cache(
        source.path.parent,
        lambda url: _CACHED_LIST_URL.match(url) is not None,
    )
    newest: dict[str, dict[str, object]] = {}
    tasks: dict[str, dict[str, object]] = {}
    for response in responses:
        match = _CACHED_LIST_URL.match(response.url)
        try:
            payload = json.loads(response.body)
        except ValueError:
            continue
        if match is not None and match.group("kind") == "tasks":
            for task in _dict_items(payload, "tasks"):
                task_id = as_optional_str(task.get("task_id"))
                if task_id and as_optional_str(task.get("prompt")):
                    tasks[task_id] = task
        else:
            _keep_newest(newest, _dict_items(payload, "items"))
    yield from _title_records(source, newest)
    for task_id, task in tasks.items():
        prompt = (as_optional_str(task.get("prompt")) or "").strip()
        conversation_id = as_optional_str(task.get("conversation_id"))
        metadata: dict[str, object] = {"task_id": task_id}
        if status := as_optional_str(task.get("status")):
            metadata["task_status"] = status
        yield SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=prompt,
            title=as_optional_str(task.get("title")),
            role="user",
            timestamp=_item_time(task, "created_at")
            or _item_time(task, "updated_at")
            or isoformat_from_mtime_ns(source.mtime_ns),
            session_id=conversation_id or task_id,
            conversation_id=conversation_id or task_id,
            metadata=metadata,
        )


_CHATGPT_PARSERS: tuple[AnyParserSpec, ...] = (
    ParserSpec("chatgpt.local_storage_conversation_list.v1", parse_chatgpt_conversation_list),
    ParserSpec("chatgpt.http_cache_json.v1", parse_chatgpt_http_cache),
    ParserSpec(
        "chatgpt.file_metadata_summary.v1",
        functools.partial(parse_file_metadata_summary_file, label="ChatGPT desktop (unreadable)"),
    ),
)
