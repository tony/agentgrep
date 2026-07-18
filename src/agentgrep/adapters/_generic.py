"""Format-generic parsers shared by multiple agent families.

Text, JSON-summary, hooks-summary, file-metadata, and TOML-summary
parsers keyed by shape rather than agent. Per-agent registry
fragments bind their own adapter ids to these callables, using a
``label`` partial where a summary needs a display label.
"""

from __future__ import annotations

import collections.abc as cabc
import pathlib
import tomllib
import typing as t

from agentgrep.readers import (
    _file_size,
    isoformat_from_mtime_ns,
    read_json_file,
    read_text_file,
)
from agentgrep.records import (
    SearchRecord,
    SourceHandle,
)


def _json_value_shape(value: object) -> str:
    """Return a value-free shape label for safe config/app-state summaries."""
    if isinstance(value, dict):
        return f"object[{len(value)}]"
    if isinstance(value, list):
        return f"array[{len(value)}]"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if value is None:
        return "null"
    return type(value).__name__


def _safe_mapping_summary(label: str, payload: dict[str, object]) -> str:
    """Summarize mapping keys and value shapes without including raw values."""
    key_shapes = [
        f"{key} ({_json_value_shape(payload[key])})" for key in sorted(payload) if key.strip()
    ]
    return f"{label} keys: {', '.join(key_shapes)}"


def parse_json_summary_file(
    source: SourceHandle,
    *,
    label: str,
) -> cabc.Iterator[SearchRecord]:
    """Parse a JSON object as a key/type summary without raw values."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    mapping = t.cast("dict[str, object]", payload)
    if not mapping:
        return
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=_safe_mapping_summary(label, mapping),
        title=source.path.name,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"key_count": len(mapping)},
    )


def _safe_nested_keys(payload: dict[str, object], key: str) -> list[str]:
    """Return sorted keys from a nested object without exposing values."""
    nested = payload.get(key)
    if not isinstance(nested, dict):
        return []
    return sorted(nested_key for nested_key in nested if isinstance(nested_key, str))


def parse_hooks_summary_file(
    source: SourceHandle,
    *,
    label: str,
) -> cabc.Iterator[SearchRecord]:
    """Parse hook JSON as event/key summaries without raw commands."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    mapping = t.cast("dict[str, object]", payload)
    if not mapping:
        return
    hook_events = _safe_nested_keys(mapping, "hooks")
    text = _safe_mapping_summary(label, mapping)
    if hook_events:
        text = f"{text}; hook events: {', '.join(hook_events)}"
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        title=source.path.name,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"key_count": len(mapping), "hook_event_count": len(hook_events)},
    )


def _line_count(path: pathlib.Path) -> int:
    """Count text lines without exposing their contents."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def parse_file_metadata_summary_file(
    source: SourceHandle,
    *,
    label: str,
) -> cabc.Iterator[SearchRecord]:
    """Parse raw/cache text files as metadata-only summaries."""
    byte_size = _file_size(source.path)
    line_count = _line_count(source.path)
    suffix = source.path.suffix or "<none>"
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=(
            f"{label} file metadata: name={source.path.name}, "
            f"suffix={suffix}, bytes={byte_size}, lines={line_count}"
        ),
        title=source.path.name,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"byte_size": byte_size, "line_count": line_count},
    )


def parse_toml_summary_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a TOML file as a key/type summary without raw values."""
    try:
        payload = tomllib.loads(source.path.read_text(encoding="utf-8"))
    except OSError, tomllib.TOMLDecodeError:
        return
    if not payload:
        return
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=_safe_mapping_summary("Codex config", t.cast("dict[str, object]", payload)),
        title=source.path.name,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"key_count": len(payload)},
    )


def parse_text_store_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse opt-in plain-text inventory stores as one sample record."""
    text = read_text_file(source.path).strip()
    if not text:
        return
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        title=source.store,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"coverage": source.coverage.value},
    )
