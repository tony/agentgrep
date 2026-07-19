"""Low-level read-only file and database primitives.

SQLite (read-only connections, table/column introspection, key-value and
conversation-summary iteration), JSONL streaming (forward, reverse, and
raw-line-skip variants with cooperative yields), protobuf text-field
extraction, and small JSON/text decoders. These are the I/O floor the per-agent
adapters sit on; they depend only on the standard library, the optional orjson
accelerator, and record type aliases.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import typing as t

from agentgrep.records import BackendSelection

try:
    import orjson as _orjson
except ImportError:
    _orjson = None  # ty: ignore[invalid-assignment]

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from agentgrep.progress import SearchControl
    from agentgrep.records import JSONScalar, JSONValue, RawJsonlSkipLine, SummaryRow

__all__ = [
    "as_optional_str",
    "decode_sqlite_value",
    "file_mtime_ns",
    "isoformat_from_mtime_ns",
    "iter_conversation_summaries",
    "iter_jsonl",
    "iter_key_value_rows",
    "iter_protobuf_text_fields",
    "open_readonly_sqlite",
    "parse_embedded_json",
    "read_json_file",
    "read_text_file",
    "sqlite_column_expr",
    "sqlite_column_names",
    "sqlite_table_names",
]


_JSONL_YIELD_INTERVAL_SECONDS = 0.01
"""Wall-clock cadence for cooperative JSONL parser yields.

The scanners run inside a Textual worker thread; an occasional
``time.sleep(0)`` lets the UI thread render. Yielding per decoded line
or per discarded chunk dominated scan time, so the yield is gated on
elapsed wall time instead — the interpreter's own thread-switch
interval already bounds UI latency between yields.
"""


_JSONL_PREFIX_BYTES = 4096
"""Bytes read up front when a raw-line skip predicate is active."""


_JSONL_SKIP_CHUNK_BYTES = 1024 * 1024
"""Chunk size for discarding skipped oversized JSONL lines."""


_JSONL_REVERSE_CHUNK_BYTES = 1024 * 1024
"""Chunk size for reading JSONL files from end to start."""


_CODEX_RAW_SKIP_MIN_BYTES = 1024 * 1024
"""Minimum Codex session size before enabling raw-line output skipping."""


def _read_varint(data: bytes, start: int) -> tuple[int | None, int]:
    """Decode a base-128 varint.

    Returns ``(value, next_index)``; ``value`` is ``None`` when the bytes
    run out mid-varint or the value would exceed 64 bits.
    """
    result = 0
    shift = 0
    index = start
    length = len(data)
    while index < length:
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, index
        shift += 7
        if shift > 63:
            return None, index
    return None, index


def _looks_like_protobuf_message(chunk: bytes) -> bool:
    """Guess whether a length-delimited chunk is a nested message.

    A nested message begins with a tag byte: a low value whose lowest
    three bits are a valid wire type. Real UTF-8 text begins with a
    printable byte (``>= 0x20``) or a multi-byte lead, so the two rarely
    collide.
    """
    if not chunk:
        return False
    first = chunk[0]
    return first < 0x20 and (first & 0x07) in (0, 1, 2, 5)


def _decode_protobuf_text(chunk: bytes, min_length: int) -> str | None:
    """Return ``chunk`` as text when it is a plausible UTF-8 string."""
    if len(chunk) < min_length:
        return None
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return None
    printable = sum(1 for char in text if char.isprintable() or char in "\n\t")
    if printable / len(text) < 0.85:
        return None
    return text


def iter_protobuf_text_fields(
    data: bytes,
    *,
    min_length: int = 2,
    _depth: int = 0,
) -> cabc.Iterator[str]:
    r"""Yield readable UTF-8 runs from an unknown protobuf message.

    Walks the protobuf wire format without a schema: each
    length-delimited (wire type 2) field is decoded as UTF-8 and yielded
    when it looks like text, otherwise recursed into as a nested message.
    A best-effort extractor for opaque protobuf blobs — such as the
    Cursor CLI chat ``store.db`` — whose schema is unofficial and may
    drift. It never raises on malformed input; unparseable bytes simply
    end the walk.

    Parameters
    ----------
    data : bytes
        Raw protobuf message bytes.
    min_length : int
        Shortest decoded string to yield.

    Yields
    ------
    str
        Each plausible text run, in wire order.

    Examples
    --------
    >>> list(iter_protobuf_text_fields(b"\x0a\x05hello"))
    ['hello']
    >>> list(iter_protobuf_text_fields(b"\x0a\x07\x0a\x05world"))
    ['world']
    >>> list(iter_protobuf_text_fields(b"\x08\x96\x01"))
    []
    """
    if _depth > 12:
        return
    index = 0
    length = len(data)
    while index < length:
        tag, index = _read_varint(data, index)
        if tag is None:
            return
        wire_type = tag & 0x07
        if wire_type == 0:
            _, index = _read_varint(data, index)
        elif wire_type == 2:
            size, index = _read_varint(data, index)
            if size is None or index + size > length:
                return
            chunk = data[index : index + size]
            index += size
            if _looks_like_protobuf_message(chunk):
                yield from iter_protobuf_text_fields(
                    chunk, min_length=min_length, _depth=_depth + 1
                )
                continue
            text = _decode_protobuf_text(chunk, min_length)
            if text is not None:
                yield text
            else:
                yield from iter_protobuf_text_fields(
                    chunk, min_length=min_length, _depth=_depth + 1
                )
        elif wire_type == 5:
            index += 4
        elif wire_type == 1:
            index += 8
        else:
            return


def open_readonly_sqlite(path: pathlib.Path) -> sqlite3.Connection:
    """Open a SQLite database with a read-only URI."""
    from agentgrep import _telemetry

    return sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        factory=_telemetry.sqlite_connection_factory(),
    )


def sqlite_table_names(connection: sqlite3.Connection) -> set[str]:
    """Return the table names from a SQLite connection."""
    rows = t.cast(
        "cabc.Iterable[tuple[object]]",
        connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'"),
    )
    names: set[str] = set()
    for row in rows:
        name = row[0]
        if isinstance(name, str):
            names.add(name)
    return names


def sqlite_column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    """Return the column names for a known SQLite table."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        return set()
    rows = t.cast(
        "cabc.Iterable[tuple[object, ...]]",
        connection.execute(f"PRAGMA table_info({table})"),
    )
    columns: set[str] = set()
    for row in rows:
        if len(row) > 1 and isinstance(row[1], str):
            columns.add(row[1])
    return columns


def sqlite_column_expr(columns: cabc.Container[str], name: str) -> str:
    """Return a quoted column reference, or the ``NULL`` literal when absent.

    Agent SQLite stores are migrated in place, so a column present on one
    machine is missing on another. A ``SELECT`` that names a missing column
    raises :exc:`sqlite3.OperationalError`, and the adapters wrap their whole
    query in a ``try``/``except`` — so an unguarded projection does not fail
    loudly, it silently turns an entire store into zero records. Substituting
    ``NULL`` keeps the projection's shape (and therefore the row unpacking)
    stable while the existing per-value ``None`` handling absorbs the miss.

    Only the mechanism is shared: each adapter still names its own columns, so
    a column that exists for one store never becomes vocabulary in another.

    Parameters
    ----------
    columns : collections.abc.Container[str]
        Column names the table actually has, from
        :func:`sqlite_column_names`.
    name : str
        The column the caller wants to project.

    Returns
    -------
    str
        A quoted identifier, or ``"NULL"``.

    Examples
    --------
    >>> sqlite_column_expr({"id", "model"}, "model")
    '"model"'
    >>> sqlite_column_expr({"id"}, "model")
    'NULL'
    """
    if name not in columns:
        return "NULL"
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def iter_key_value_rows(
    connection: sqlite3.Connection,
    table: str,
    *,
    exact_keys: cabc.Sequence[str] | None = None,
    key_prefixes: cabc.Sequence[str] | None = None,
    key_tokens: cabc.Sequence[str] | None = None,
) -> cabc.Iterator[tuple[str, object]]:
    """Yield likely key/value rows, reading values only for matched keys.

    Stage 1 selects keys only so large non-matching ``value`` BLOBs are never
    materialized; on the real Cursor schema the key scan rides a covering
    index. Stage 2 point-fetches ``value`` per distinct matched key, yielding
    every row for keys that repeat in index-less databases.
    """
    if table not in {"ItemTable", "cursorDiskKV"}:
        return
    info = t.cast(
        "cabc.Iterable[tuple[object, ...]]",
        connection.execute(f"PRAGMA table_info({table})"),
    )
    columns = [str(row[1]) for row in info]
    if "key" not in columns or "value" not in columns:
        return
    key_query = f"SELECT key FROM {table}"  # table validated against the allowlist above
    selectors: list[str] = []
    parameters: list[str] = []
    if exact_keys is not None:
        for key in exact_keys:
            if key:
                selectors.append("key = ? COLLATE NOCASE")
                parameters.append(key)
    if key_prefixes is not None:
        for prefix in key_prefixes:
            if prefix:
                selectors.append("(key >= ? AND key < ?)")
                parameters.extend((prefix, _sqlite_prefix_upper_bound(prefix)))
    if selectors:
        key_query = f"{key_query} WHERE {' OR '.join(selectors)}"
    elif key_tokens is not None:
        tokens = tuple(token for token in key_tokens if token)
        if tokens:
            predicates = " OR ".join("key LIKE ? COLLATE NOCASE" for _ in tokens)
            key_query = f"{key_query} WHERE {predicates}"
            parameters = [f"%{token}%" for token in tokens]
    seen_keys: set[str] = set()
    matched_keys: list[str] = []
    key_rows = t.cast(
        "cabc.Iterable[tuple[object]]",
        connection.execute(key_query, tuple(parameters)),
    )
    for (key,) in key_rows:
        if isinstance(key, str) and key not in seen_keys:
            seen_keys.add(key)
            matched_keys.append(key)
    value_query = f"SELECT value FROM {table} WHERE key = ?"  # table validated above
    for key in matched_keys:
        value_rows = t.cast(
            "cabc.Iterable[tuple[object]]",
            connection.execute(value_query, (key,)),
        )
        for (value,) in value_rows:
            yield key, value


def _sqlite_prefix_upper_bound(prefix: str) -> str:
    """Return an exclusive upper bound for a SQLite text prefix range."""
    for index in range(len(prefix) - 1, -1, -1):
        codepoint = ord(prefix[index])
        if codepoint < 0x10FFFF:
            return f"{prefix[:index]}{chr(codepoint + 1)}"
    return f"{prefix}\U0010ffff"


def iter_conversation_summaries(
    connection: sqlite3.Connection,
) -> cabc.Iterator[SummaryRow]:
    """Yield typed rows from Cursor AI tracking summaries."""
    query = """
        SELECT
            conversationId,
            title,
            tldr,
            overview,
            summaryBullets,
            model,
            mode,
            updatedAt
        FROM conversation_summaries
    """
    rows = t.cast("cabc.Iterable[SummaryRow]", connection.execute(query))
    yield from rows


def read_text_file(path: pathlib.Path) -> str:
    """Read a text file with replacement for decode errors."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def read_json_file(path: pathlib.Path) -> JSONValue | None:
    """Read a JSON file."""
    try:
        parsed = t.cast("object", json.loads(path.read_text(encoding="utf-8")))
    except OSError, json.JSONDecodeError:
        return None
    if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
        return t.cast("JSONValue", parsed)
    return None


def iter_jsonl(path: pathlib.Path) -> cabc.Iterator[JSONValue]:
    """Yield decoded JSON objects from a JSONL file."""
    yield from _iter_jsonl(path)


def _loads(text: str) -> object:
    """Decode one JSON document, preferring orjson when it is installed.

    Stdlib :func:`json.loads` is the semantic source of truth (ADR 0002);
    orjson is a drop-in accelerator. orjson rejects a handful of inputs
    stdlib accepts — ``NaN``, ``Infinity``, ``-Infinity`` (which Python's own
    ``json.dumps`` emits) — so any orjson decode error falls back to
    ``json.loads``, recovering the stdlib value or re-raising
    :class:`json.JSONDecodeError` for genuinely invalid input.

    Documented exemption (ADR 0002): orjson decodes integers beyond the
    signed 64-bit range to ``float`` instead of raising, so for those inputs
    the accelerated result is lossy relative to stdlib. Agent-history JSON
    does not carry integers that large — timestamps, ids, and counts fit in
    64 bits or are strings — so the divergence is unreachable in practice.
    """
    if _orjson is None:
        return json.loads(text)
    try:
        return _orjson.loads(text)
    except _orjson.JSONDecodeError:
        return json.loads(text)


class _PeriodicYield:
    """Release the GIL at most once per :data:`_JSONL_YIELD_INTERVAL_SECONDS`.

    One instance is created per JSONL scan and called on every line and
    discarded chunk; it only invokes ``time.sleep(0)`` when the wall-clock
    interval has elapsed, so a hot single-threaded scan pays a cheap
    ``perf_counter`` read per line instead of an OS reschedule.
    """

    __slots__ = ("_deadline",)

    def __init__(self) -> None:
        self._deadline = time.perf_counter() + _JSONL_YIELD_INTERVAL_SECONDS

    def __call__(self) -> None:
        now = time.perf_counter()
        if now >= self._deadline:
            time.sleep(0)
            self._deadline = now + _JSONL_YIELD_INTERVAL_SECONDS


def _iter_jsonl(
    path: pathlib.Path,
    *,
    skip_line: RawJsonlSkipLine | None = None,
    skip_line_mode: t.Literal["prefix", "line"] = "prefix",
    full_line_skip: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[JSONValue]:
    """Yield decoded JSON objects from a JSONL file with an optional raw-line filter.

    ``skip_line`` runs in ``skip_line_mode``: ``"prefix"`` checks only the
    first :data:`_JSONL_PREFIX_BYTES` of each line so oversized lines can be
    discarded in chunks without full allocation, while ``"line"`` checks the
    whole line. ``full_line_skip`` always sees the complete decoded line
    before JSON decode, so predicates that may match past the prefix window
    stay correct alongside a cheap prefix skip. Reverse iteration ignores
    ``skip_line_mode``: both predicates are combined and run against full
    decoded lines, because reverse reads already materialize each line from
    tail chunks.
    """
    if reverse:
        yield from _iter_jsonl_reverse(
            path,
            skip_line=_combine_raw_skip_lines(skip_line, full_line_skip),
        )
        return
    if skip_line is not None:
        if skip_line_mode == "line":
            combined = _combine_raw_skip_lines(skip_line, full_line_skip)
            assert combined is not None
            yield from _iter_jsonl_with_raw_line_skip(path, combined)
        else:
            yield from _iter_jsonl_with_raw_prefix_skip(
                path,
                skip_line,
                full_line_skip=full_line_skip,
            )
        return
    if full_line_skip is not None:
        yield from _iter_jsonl_with_raw_line_skip(path, full_line_skip)
        return
    try:
        with path.open(encoding="utf-8") as handle:
            yield_now = _PeriodicYield()
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                yield_now()
                try:
                    parsed = _loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
                    yield t.cast("JSONValue", parsed)
    except OSError:
        return


def _iter_jsonl_reverse(
    path: pathlib.Path,
    *,
    skip_line: RawJsonlSkipLine | None = None,
) -> cabc.Iterator[JSONValue]:
    """Yield decoded JSONL values from the end of ``path`` toward the start."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            pending = b""
            yield_now = _PeriodicYield()
            while position > 0:
                read_size = min(_JSONL_REVERSE_CHUNK_BYTES, position)
                position -= read_size
                handle.seek(position)
                pending = handle.read(read_size) + pending
                lines = pending.split(b"\n")
                pending = lines[0]
                for raw_line in reversed(lines[1:]):
                    decoded = _decode_jsonl_raw_line(raw_line, skip_line=skip_line)
                    if decoded is _SKIPPED_JSONL_LINE:
                        continue
                    yield_now()
                    yield t.cast("JSONValue", decoded)
            if pending.strip():
                decoded = _decode_jsonl_raw_line(pending, skip_line=skip_line)
                if decoded is not _SKIPPED_JSONL_LINE:
                    yield_now()
                    yield t.cast("JSONValue", decoded)
    except OSError:
        return


_SKIPPED_JSONL_LINE = object()


def _decode_jsonl_raw_line(
    raw_line: bytes,
    *,
    skip_line: RawJsonlSkipLine | None = None,
) -> JSONValue | object:
    """Decode one raw JSONL line, or return a sentinel for skipped/invalid lines."""
    if not raw_line.strip():
        return _SKIPPED_JSONL_LINE
    line = raw_line.decode("utf-8", errors="replace")
    if skip_line is not None and skip_line(line):
        return _SKIPPED_JSONL_LINE
    stripped = line.strip()
    if not stripped:
        return _SKIPPED_JSONL_LINE
    try:
        parsed = _loads(stripped)
    except json.JSONDecodeError:
        return _SKIPPED_JSONL_LINE
    if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
        return t.cast("JSONValue", parsed)
    return _SKIPPED_JSONL_LINE


def _iter_jsonl_with_raw_prefix_skip(
    path: pathlib.Path,
    skip_line: RawJsonlSkipLine,
    *,
    full_line_skip: RawJsonlSkipLine | None = None,
) -> cabc.Iterator[JSONValue]:
    """Yield decoded JSON objects while skipping matched raw prefixes.

    ``skip_line`` sees only the line prefix and gates the chunked discard
    path; ``full_line_skip`` sees the fully accumulated line before JSON
    decode.
    """
    try:
        with path.open("rb") as handle:
            yield_now = _PeriodicYield()
            while True:
                prefix = handle.readline(_JSONL_PREFIX_BYTES)
                if not prefix:
                    break
                if not prefix.strip():
                    continue
                yield_now()
                prefix_text = prefix.decode("utf-8", errors="replace")
                if skip_line(prefix_text):
                    _discard_rest_of_line(handle, prefix)
                    continue
                raw_line = bytearray(prefix)
                while raw_line and not raw_line.endswith(b"\n"):
                    chunk = handle.readline(_JSONL_SKIP_CHUNK_BYTES)
                    if not chunk:
                        break
                    raw_line.extend(chunk)
                    yield_now()
                full_text = raw_line.decode("utf-8", errors="replace")
                if full_line_skip is not None and full_line_skip(full_text):
                    continue
                stripped = full_text.strip()
                if not stripped:
                    continue
                try:
                    parsed = _loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
                    yield t.cast("JSONValue", parsed)
    except OSError:
        return


def _iter_jsonl_with_raw_line_skip(
    path: pathlib.Path,
    skip_line: RawJsonlSkipLine,
) -> cabc.Iterator[JSONValue]:
    """Yield decoded JSON objects while skipping matched full raw lines."""
    try:
        with path.open("rb") as handle:
            yield_now = _PeriodicYield()
            for raw_line in handle:
                if not raw_line.strip():
                    continue
                yield_now()
                line = raw_line.decode("utf-8", errors="replace")
                if skip_line(line):
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    parsed = _loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
                    yield t.cast("JSONValue", parsed)
    except OSError:
        return


def _combine_raw_skip_lines(
    first: RawJsonlSkipLine | None,
    second: RawJsonlSkipLine | None,
) -> RawJsonlSkipLine | None:
    """Return a raw-line predicate that skips when either predicate skips."""
    if first is None:
        return second
    if second is None:
        return first

    def skip_line(raw_line: str) -> bool:
        return first(raw_line) or second(raw_line)

    return skip_line


_CODEX_SESSION_META_MARKER = '"type":"session_meta"'
"""Space-stripped prefix marker for the Codex session header line."""


_PI_SESSION_HEADER_MARKER = '"type":"session"'
"""Space-stripped prefix marker for the pi session header line."""


def _read_first_matching_jsonl_record(
    path: pathlib.Path,
    marker: str,
    *,
    accept_record: cabc.Callable[[dict[str, object]], bool],
) -> dict[str, object] | None:
    """Decode the first prefix-marked JSONL mapping accepted by a predicate.

    Scans forward using the raw-skip reader's bounded prefix and checks
    ``marker`` within the first 512 decoded characters after removing ASCII
    spaces. Unrelated oversized lines are discarded incrementally; a marked
    candidate is materialized completely so JSON decoding and
    ``accept_record`` can establish its semantic identity.

    Parameters
    ----------
    path : pathlib.Path
        JSONL file to scan.
    marker : str
        Space-stripped record marker expected in the bounded prefix.
    accept_record : collections.abc.Callable
        Semantic validator for decoded mappings. Malformed and rejected
        matching records are skipped so callers can locate the first valid
        record.

    Returns
    -------
    dict[str, object] or None
        First matching accepted record, or ``None`` when none exists.
    """
    try:
        with path.open("rb") as handle:
            while True:
                prefix = handle.readline(_JSONL_PREFIX_BYTES)
                if not prefix:
                    return None
                if not prefix.strip():
                    continue
                prefix_text = prefix.decode("utf-8", errors="replace")
                if marker not in prefix_text[:512].replace(" ", ""):
                    if not prefix.endswith(b"\n"):
                        _discard_rest_of_line(handle, prefix)
                    continue
                raw_line = bytearray(prefix)
                while raw_line and not raw_line.endswith(b"\n"):
                    chunk = handle.readline(_JSONL_SKIP_CHUNK_BYTES)
                    if not chunk:
                        break
                    raw_line.extend(chunk)
                try:
                    parsed = _loads(raw_line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    record = t.cast("dict[str, object]", parsed)
                    if accept_record(record):
                        return record
    except OSError:
        return None


def _keep_jsonl_header_lines(
    skip_line: RawJsonlSkipLine,
    marker: str,
) -> RawJsonlSkipLine:
    """Wrap a raw skip predicate so header lines are always decoded.

    Stateful session parsers learn canonical metadata (session id, model,
    cwd) from a header line that rarely contains the search term, so a raw
    text prefilter must never drop it.

    >>> keep = _keep_jsonl_header_lines(lambda _line: True, '"type":"session_meta"')
    >>> keep('{"type": "session_meta", "payload": {}}')
    False
    >>> keep('{"type": "response_item"}')
    True
    """

    def wrapped(raw_line: str) -> bool:
        if marker in raw_line[:512].replace(" ", ""):
            return False
        return skip_line(raw_line)

    return wrapped


def _discard_rest_of_line(handle: t.BinaryIO, prefix: bytes) -> None:
    """Discard the unread remainder of the current physical line.

    Each ``readline`` releases the GIL for the underlying read, so the
    discard loop stays cooperative without an explicit per-chunk yield.
    """
    chunk = prefix
    while chunk and not chunk.endswith(b"\n"):
        chunk = handle.readline(_JSONL_SKIP_CHUNK_BYTES)


def _is_codex_function_call_output_line(line: str) -> bool:
    """Return whether a Codex JSONL line is a tool output record.

    Tests the two distinctive quoted value tokens directly on the line
    prefix. JSON never reformats string contents, so this matches the same
    lines as a space-stripped key check — verified against the live store —
    without allocating a normalized copy of every scanned line.
    """
    prefix = line[:512]
    return '"response_item"' in prefix and '"function_call_output"' in prefix


def decode_sqlite_value(value: object) -> str | None:
    """Decode a SQLite value into UTF-8 text if possible."""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return None


def parse_embedded_json(text: str) -> JSONValue | None:
    """Parse a JSON-encoded string, returning ``None`` when unavailable."""
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        parsed = _loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
        return t.cast("JSONValue", parsed)
    return None


def file_mtime_ns(path: pathlib.Path) -> int:
    """Return a cached modification time for a path."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _file_size(path: pathlib.Path) -> int:
    """Return file size in bytes, falling back to zero on stat failure."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def isoformat_from_mtime_ns(mtime_ns: int) -> str | None:
    """Convert a nanosecond ``mtime`` to an ISO-8601 UTC timestamp.

    Used as a timestamp fallback for stores whose records carry no native
    timestamp — most notably Cursor CLI agent transcripts.
    """
    if mtime_ns <= 0:
        return None
    return (
        datetime.datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=datetime.UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def as_optional_str(value: object) -> str | None:
    """Return a stripped string when possible."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def select_backends() -> BackendSelection:
    """Return the best available subprocess helpers."""
    return BackendSelection(
        find_tool=which_first(("fd", "fdfind")),
        grep_tool=which_first(("rg", "ag")),
        json_tool=which_first(("jq", "jaq")),
    )


def which_first(names: tuple[str, ...]) -> str | None:
    """Return the first executable available on ``PATH``."""
    for name in names:
        found = shutil.which(name)
        if found is not None:
            return found
    return None


def run_readonly_command(
    command: list[str],
    *,
    control: SearchControl | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell and capture text output."""
    started_at = time.perf_counter()
    if control is None:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        _record_readonly_command_profile(command, started_at, completed)
        return completed
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.05)
        except subprocess.TimeoutExpired:
            if control.answer_now_requested():
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                completed = subprocess.CompletedProcess(
                    command,
                    process.returncode,
                    stdout,
                    stderr,
                )
                _record_readonly_command_profile(command, started_at, completed)
                return completed
            continue
        completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        _record_readonly_command_profile(command, started_at, completed)
        return completed


def _record_readonly_command_profile(
    command: list[str],
    started_at: float,
    completed: subprocess.CompletedProcess[str],
) -> None:
    """Record optional engine profiling metadata for a completed subprocess."""
    if "agentgrep._engine.profiling" not in sys.modules:
        return
    from agentgrep._engine.profiling import record_subprocess_run

    record_subprocess_run(
        command,
        duration_seconds=time.perf_counter() - started_at,
        completed=completed,
    )


def _record_engine_profile_sample(
    name: str,
    duration_seconds: float,
    **attributes: JSONScalar,
) -> None:
    """Record an optional engine profile sample when profiling is active."""
    if "agentgrep._engine.profiling" not in sys.modules:
        return
    from agentgrep._engine.profiling import current_engine_profiler

    profiler = current_engine_profiler()
    if profiler is None:
        return
    profiler.record(name, duration_seconds, **attributes)


def list_files_matching(
    root: pathlib.Path,
    glob_pattern: str,
    fd_program: str | None,
) -> list[pathlib.Path]:
    """List files under ``root`` that match a glob."""
    if not root.exists():
        return []
    if "/" in glob_pattern or "\\" in glob_pattern:
        return sorted(path for path in root.glob(glob_pattern) if path.is_file())
    if fd_program is not None:
        command = [
            fd_program,
            "-H",
            "-I",
            "-t",
            "f",
            "--glob",
            glob_pattern,
            str(root),
        ]
        completed = run_readonly_command(command)
        if completed.returncode == 0:
            return [pathlib.Path(line) for line in completed.stdout.splitlines() if line.strip()]
    return sorted(path for path in root.rglob(glob_pattern) if path.is_file())
