"""VS Code (GitHub Copilot Chat) store parsers and registry fragment."""

from __future__ import annotations

import collections.abc as cabc
import pathlib
import re
import sqlite3
import typing as t
import urllib.parse

from agentgrep.adapters._common import (
    _record_origin,
    _unix_millis_to_isoformat,
)
from agentgrep.adapters._registry import AnyParserSpec, ParserSpec
from agentgrep.readers import (
    as_optional_str,
    decode_sqlite_value,
    isoformat_from_mtime_ns,
    iter_jsonl,
    iter_key_value_rows,
    open_readonly_sqlite,
    parse_embedded_json,
    read_json_file,
    sqlite_table_names,
)
from agentgrep.records import (
    SearchRecord,
    SourceHandle,
)


def parse_vscode_chat_session(source: SourceHandle) -> cabc.Iterator[SearchRecord]:
    """Parse a VS Code GitHub Copilot Chat ``chatSessions/<uuid>.json``.

    Each ``requests[]`` turn yields a user-prompt record (``message.text``) and,
    when present, an assistant record (the bare ``MarkdownString`` response parts
    with no ``kind``, joined). Tool-call names and the resolved workspace cwd are
    attached as metadata. Tolerates empty/draft turns and absent ``result``.

    Current sessions are a ``.jsonl`` mutation log rebuilt by
    :func:`_read_vscode_jsonl_session`; older ``.json`` sessions are one object.
    """
    if source.source_kind == "jsonl":
        mapping = _read_vscode_jsonl_session(source.path)
        if mapping is None:
            return
    else:
        payload = read_json_file(source.path)
        if not isinstance(payload, dict):
            return
        mapping = t.cast("dict[str, object]", payload)
    requests = mapping.get("requests")
    if not isinstance(requests, list):
        return
    session_id = as_optional_str(mapping.get("sessionId"))
    base_metadata: dict[str, object] = {}
    cwd = _vscode_workspace_cwd(source.path)
    origin = _record_origin(cwd=cwd)
    if cwd:
        base_metadata["cwd"] = cwd
    title: str | None = None
    for entry in requests:
        if not isinstance(entry, dict):
            continue
        request = t.cast("dict[str, object]", entry)
        timestamp = _unix_millis_to_isoformat(request.get("timestamp"))
        message = request.get("message")
        prompt = (
            as_optional_str(t.cast("dict[str, object]", message).get("text"))
            if isinstance(message, dict)
            else None
        )
        if prompt and title is None:
            title = prompt[:80]
        if prompt:
            yield SearchRecord(
                kind="prompt",
                agent=source.agent,
                store=source.store,
                adapter_id=source.adapter_id,
                path=source.path,
                text=prompt,
                title=title,
                role="user",
                timestamp=timestamp,
                session_id=session_id,
                conversation_id=session_id,
                origin=origin,
                metadata=dict(base_metadata),
            )
        reply = _vscode_response_text(request.get("response"))
        if reply:
            metadata = dict(base_metadata)
            tools = _vscode_tool_names(request.get("result"))
            if tools:
                metadata["tools"] = tools
            yield SearchRecord(
                kind="history",
                agent=source.agent,
                store=source.store,
                adapter_id=source.adapter_id,
                path=source.path,
                text=reply,
                title=title,
                role="assistant",
                timestamp=timestamp,
                model=as_optional_str(request.get("modelId")),
                session_id=session_id,
                conversation_id=session_id,
                origin=origin,
                metadata=metadata,
            )


def _vscode_child(node: object, key: object) -> object:
    """Return ``node[key]`` for a dict string key or list int index, else ``None``."""
    if isinstance(node, dict) and isinstance(key, str):
        return t.cast("dict[str, object]", node).get(key)
    if isinstance(node, list) and isinstance(key, int) and 0 <= key < len(node):
        return node[key]
    return None


def _read_vscode_jsonl_session(path: pathlib.Path) -> dict[str, object] | None:
    """Reconstruct a Copilot Chat session from a ``chatSessions/<uuid>.jsonl`` log.

    The newer serialization is a JSON-mutation log: the first ``kind: 0`` line
    carries the full session snapshot under ``v``; ``kind: 1`` lines set a value
    at key-path ``k``, and ``kind: 2`` lines replace the array at ``k`` from
    index ``i`` onward with ``v`` (truncate to ``i``, then append ``v``; a
    missing ``i`` appends, a missing ``v`` truncates). Replaying the log in file
    order rebuilds the ``requests`` list the single-object ``.json`` form stores
    directly, so one extraction handles both shapes.
    """
    session: dict[str, object] = {}
    for record in iter_jsonl(path):
        if not isinstance(record, dict):
            continue
        event = t.cast("dict[str, object]", record)
        kind = event.get("kind")
        value = event.get("v")
        if kind == 0:
            if isinstance(value, dict):
                session = t.cast("dict[str, object]", value)
            continue
        keys = event.get("k")
        if not (isinstance(keys, list) and keys):
            continue
        node: object = session
        for key in keys[:-1]:
            node = _vscode_child(node, key)
            if node is None:
                break
        else:
            last = keys[-1]
            if kind == 1 and isinstance(node, dict) and isinstance(last, str):
                t.cast("dict[str, object]", node)[last] = value
            elif (
                kind == 1
                and isinstance(node, list)
                and isinstance(last, int)
                and 0 <= last < len(node)
            ):
                t.cast("list[object]", node)[last] = value
            elif kind == 2:
                target = _vscode_child(node, last)
                if isinstance(target, list):
                    index = event.get("i")
                    tail = value if isinstance(value, list) else []
                    # kind:2 replaces the array from index `i` onward with `v`
                    # (truncate to `i`, then append `v`); a missing `i` appends
                    # and a missing `v` truncates.
                    cut = (
                        index
                        if isinstance(index, int) and 0 <= index <= len(target)
                        else len(target)
                    )
                    t.cast("list[object]", target)[cut:] = t.cast("list[object]", tail)
    return session or None


def _vscode_response_text(response: object) -> str | None:
    """Join the readable Markdown parts of a Copilot Chat response array.

    Assistant prose lives in the bare ``MarkdownString`` parts (no ``kind``,
    shape ``{value, supportHtml, supportThemeIcons}``); a forward-compatible
    ``markdownContent`` kind is also accepted. Tool-invocation, reference,
    progress, and warning parts are skipped.
    """
    if not isinstance(response, list):
        return None
    chunks: list[str] = []
    for part in response:
        if not isinstance(part, dict):
            continue
        mapping = t.cast("dict[str, object]", part)
        kind = mapping.get("kind")
        if kind is not None and kind != "markdownContent":
            continue
        value = mapping.get("value")
        if not isinstance(value, str):
            content = mapping.get("content")
            value = (
                t.cast("dict[str, object]", content).get("value")
                if isinstance(content, dict)
                else None
            )
        if isinstance(value, str):
            chunks.append(value)
    text = "".join(chunks).strip()
    return text or None


def _vscode_tool_names(result: object) -> list[str] | None:
    """Extract the tool names a Copilot Chat agent turn invoked, if any."""
    if not isinstance(result, dict):
        return None
    metadata = t.cast("dict[str, object]", result).get("metadata")
    if not isinstance(metadata, dict):
        return None
    rounds = t.cast("dict[str, object]", metadata).get("toolCallRounds")
    if not isinstance(rounds, list):
        return None
    names: list[str] = []
    for round_ in rounds:
        if not isinstance(round_, dict):
            continue
        calls = t.cast("dict[str, object]", round_).get("toolCalls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if isinstance(call, dict):
                name = as_optional_str(t.cast("dict[str, object]", call).get("name"))
                if name:
                    names.append(name)
    return names or None


def _vscode_workspace_cwd(session_path: pathlib.Path) -> str | None:
    """Resolve the project cwd for a chat session via its ``workspace.json``.

    ``workspaceStorage/<hash>/chatSessions/<uuid>.json`` has a sibling
    ``workspace.json`` whose ``folder`` URI names the opened directory. A
    ``vscode-remote://wsl+<distro>/<path>`` URI maps to the Linux path
    ``<path>``; ``file://`` URIs are unquoted. Returns ``None`` for windowless
    sessions (``globalStorage/emptyWindowChatSessions/``) with no workspace.
    """
    workspace_json = session_path.parent.parent / "workspace.json"
    payload = read_json_file(workspace_json)
    if not isinstance(payload, dict):
        return None
    folder = as_optional_str(t.cast("dict[str, object]", payload).get("folder"))
    if not folder:
        return None
    return _vscode_uri_to_path(folder)


def _vscode_uri_to_path(uri: str) -> str | None:
    """Map a VS Code folder URI to a local filesystem path.

    ``vscode-remote://wsl+<distro>/home/u/proj`` -> ``/home/u/proj``;
    ``file:///home/u/proj`` -> ``/home/u/proj``. Other remotes (ssh, dev
    container) return their path component too, best-effort.
    """
    remote = re.match(r"vscode-remote://[^/]+(/.*)$", uri)
    if remote:
        return urllib.parse.unquote(remote.group(1)) or None
    if uri.startswith("file://"):
        return urllib.parse.unquote(uri[len("file://") :]) or None
    return None


def parse_vscode_inline_history(source: SourceHandle) -> cabc.Iterator[SearchRecord]:
    """Parse the VS Code global ``state.vscdb`` inline-edit prompt history.

    The ``inline-chat-history`` ``ItemTable`` key holds a JSON array of the
    user's Ctrl+I inline-edit prompts. Token-filtered to that key alone, so the
    ``secret://`` auth keys in the same database are never read.
    """
    connection = open_readonly_sqlite(source.path)
    try:
        if "ItemTable" not in sqlite_table_names(connection):
            return
        for _key, raw_value in iter_key_value_rows(
            connection,
            "ItemTable",
            exact_keys=("inline-chat-history",),
        ):
            decoded = decode_sqlite_value(raw_value)
            if decoded is None:
                continue
            parsed = parse_embedded_json(decoded)
            if not isinstance(parsed, list):
                continue
            for item in parsed:
                if isinstance(item, str) and item.strip():
                    yield SearchRecord(
                        kind="prompt",
                        agent=source.agent,
                        store=source.store,
                        adapter_id=source.adapter_id,
                        path=source.path,
                        text=item,
                        title="VS Code inline chat",
                        role="user",
                        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
                    )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


_VSCODE_PARSERS: tuple[AnyParserSpec, ...] = (
    ParserSpec("vscode.chat_sessions_json.v1", parse_vscode_chat_session),
    ParserSpec("vscode.inline_history_sqlite.v1", parse_vscode_inline_history),
)
"""Dispatch rows for every ``vscode.*`` adapter id."""
