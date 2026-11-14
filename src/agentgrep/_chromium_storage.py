r"""Read Chromium's LevelDB-backed web storage without a native dependency.

Electron apps such as ChatGPT desktop keep their web storage in LevelDB
directories (``Local Storage/leveldb``). This module reads them in pure Python:
Snappy decompression, LevelDB write-ahead logs (``*.log``) and sorted tables
(``*.ldb``), and Chromium's Local Storage key and value encoding.

It never opens a directory through a LevelDB library, so it takes no lock and
writes nothing. A store the running app is writing may yield a slightly older
snapshot: a record cut off at the end of a log is skipped, and a damaged write
batch or table block is skipped on its own, keeping the rest of the file.

Examples
--------
>>> from agentgrep._chromium_storage import snappy_decompress
>>> snappy_decompress(b"\x05\x10hello")
b'hello'
"""

from __future__ import annotations

import pathlib
import typing as t

if t.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("local_storage_values", "read_leveldb", "snappy_decompress")

_LOG_BLOCK_SIZE = 32768
_LOG_HEADER_SIZE = 7
_LOG_FULL, _LOG_FIRST, _LOG_MIDDLE, _LOG_LAST = 1, 2, 3, 4
_TABLE_MAGIC = 0xDB4775248B80FB57
_TABLE_FOOTER_SIZE = 48
_VALUE_TYPE_PUT = 1
_BLOCK_NO_COMPRESSION, _BLOCK_SNAPPY = 0, 1

_Entry = tuple[bytes, bytes | None, int]
"""A LevelDB key, its value (``None`` for a deletion), and its sequence number."""


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a little-endian base-128 varint at ``pos``; return it and the next offset."""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            msg = "varint runs past the end of the data"
            raise ValueError(msg)
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            msg = "varint longer than 64 bits"
            raise ValueError(msg)


def snappy_decompress(data: bytes) -> bytes:
    r"""Decompress one raw Snappy block, as LevelDB stores compressed table blocks.

    Parameters
    ----------
    data : bytes
        A Snappy block: the uncompressed length as a varint, then literal and
        copy elements.

    Returns
    -------
    bytes
        The uncompressed bytes.

    Raises
    ------
    ValueError
        If the block is malformed or decompresses to the wrong length.

    Examples
    --------
    A literal ``ab`` followed by a 4-byte copy at offset 2:

    >>> snappy_decompress(b"\x06\x04ab\x01\x02")
    b'ababab'

    A truncated block raises :class:`ValueError`:

    >>> snappy_decompress(b"")
    Traceback (most recent call last):
    ...
    ValueError: varint runs past the end of the data
    """
    try:
        return _snappy_decompress(data)
    except IndexError as exc:
        msg = "snappy block is truncated"
        raise ValueError(msg) from exc


def _snappy_decompress(data: bytes) -> bytes:
    """Decompress a Snappy block; :func:`snappy_decompress` maps truncation to ``ValueError``."""
    length, pos = _varint(data, 0)
    out = bytearray()
    end = len(data)
    while pos < end:
        tag = data[pos]
        pos += 1
        kind = tag & 0x03
        if kind == 0:
            size = tag >> 2
            if size >= 60:
                extra = size - 59
                size = int.from_bytes(data[pos : pos + extra], "little")
                pos += extra
            size += 1
            out += data[pos : pos + size]
            pos += size
            continue
        if kind == 1:
            size = ((tag >> 2) & 0x07) + 4
            offset = ((tag >> 5) << 8) | data[pos]
            pos += 1
        elif kind == 2:
            size = (tag >> 2) + 1
            offset = int.from_bytes(data[pos : pos + 2], "little")
            pos += 2
        else:
            size = (tag >> 2) + 1
            offset = int.from_bytes(data[pos : pos + 4], "little")
            pos += 4
        if offset == 0 or offset > len(out):
            msg = "snappy copy points outside the output"
            raise ValueError(msg)
        start = len(out) - offset
        while size > 0:
            chunk = out[start : start + min(size, offset)]
            out += chunk
            start += len(chunk)
            size -= len(chunk)
    if len(out) != length:
        msg = "snappy block decompressed to the wrong length"
        raise ValueError(msg)
    return bytes(out)


def _log_payloads(raw: bytes) -> cabc.Iterator[bytes]:
    """Yield the write batches in a LevelDB log, reassembling fragmented records."""
    pos = 0
    pending = bytearray()
    in_record = False
    while pos + _LOG_HEADER_SIZE <= len(raw):
        left_in_block = _LOG_BLOCK_SIZE - pos % _LOG_BLOCK_SIZE
        if left_in_block < _LOG_HEADER_SIZE:
            pos += left_in_block
            continue
        length = int.from_bytes(raw[pos + 4 : pos + 6], "little")
        record_type = raw[pos + 6]
        start = pos + _LOG_HEADER_SIZE
        end = start + length
        if record_type == 0:
            pos += left_in_block
            continue
        if end > len(raw):
            return
        pos = end
        data = raw[start:end]
        if record_type == _LOG_FULL:
            in_record = False
            yield data
        elif record_type == _LOG_FIRST:
            pending[:] = data
            in_record = True
        elif record_type == _LOG_MIDDLE and in_record:
            pending += data
        elif record_type == _LOG_LAST and in_record:
            pending += data
            in_record = False
            yield bytes(pending)
        else:
            in_record = False


def _batch_entries(payload: bytes) -> cabc.Iterator[_Entry]:
    """Yield the puts and deletions in one LevelDB write batch."""
    if len(payload) < 12:
        return
    sequence = int.from_bytes(payload[:8], "little")
    count = int.from_bytes(payload[8:12], "little")
    pos = 12
    for index in range(count):
        tag = payload[pos]
        pos += 1
        key_length, pos = _varint(payload, pos)
        key = payload[pos : pos + key_length]
        pos += key_length
        if tag == _VALUE_TYPE_PUT:
            value_length, pos = _varint(payload, pos)
            yield key, payload[pos : pos + value_length], sequence + index
            pos += value_length
        elif tag == 0:
            yield key, None, sequence + index
        else:
            return


def _read_block(raw: bytes, offset: int, size: int) -> bytes:
    """Return one table block's contents, decompressed when the block is Snappy."""
    block = raw[offset : offset + size]
    compression = raw[offset + size]
    if compression == _BLOCK_NO_COMPRESSION:
        return block
    if compression == _BLOCK_SNAPPY:
        return snappy_decompress(block)
    msg = f"unsupported table block compression {compression}"
    raise ValueError(msg)


def _block_entries(block: bytes) -> cabc.Iterator[tuple[bytes, bytes]]:
    """Yield the prefix-compressed key/value entries of one table block."""
    restarts = int.from_bytes(block[-4:], "little")
    limit = len(block) - 4 - 4 * restarts
    pos = 0
    key = b""
    while pos < limit:
        shared, pos = _varint(block, pos)
        unshared, pos = _varint(block, pos)
        value_length, pos = _varint(block, pos)
        key = key[:shared] + block[pos : pos + unshared]
        pos += unshared
        yield key, block[pos : pos + value_length]
        pos += value_length


def _table_entries(raw: bytes) -> cabc.Iterator[_Entry]:
    """Yield the entries of one LevelDB sorted table, with internal keys split."""
    if len(raw) < _TABLE_FOOTER_SIZE or int.from_bytes(raw[-8:], "little") != _TABLE_MAGIC:
        return
    footer = raw[-_TABLE_FOOTER_SIZE:]
    _metaindex_offset, pos = _varint(footer, 0)
    _metaindex_size, pos = _varint(footer, pos)
    index_offset, pos = _varint(footer, pos)
    index_size, _ = _varint(footer, pos)
    for _last_key, handle in _block_entries(_read_block(raw, index_offset, index_size)):
        try:
            block_offset, pos = _varint(handle, 0)
            block_size, _ = _varint(handle, pos)
            entries = list(_block_entries(_read_block(raw, block_offset, block_size)))
        except ValueError, IndexError:
            continue
        for internal_key, value in entries:
            if len(internal_key) < 8:
                continue
            trailer = int.from_bytes(internal_key[-8:], "little")
            is_put = trailer & 0xFF == _VALUE_TYPE_PUT
            yield internal_key[:-8], value if is_put else None, trailer >> 8


def read_leveldb(directory: pathlib.Path, *, key_prefix: bytes = b"") -> dict[bytes, bytes]:
    """Return the live key/value pairs of a LevelDB directory, read-only.

    Every ``*.log`` and ``*.ldb`` file is read and the newest entry per key
    (by sequence number) wins; deleted keys are left out. Only keys starting
    with ``key_prefix`` are kept, so memory holds just the entries asked for.

    Parameters
    ----------
    directory : pathlib.Path
        A LevelDB directory, such as Chromium's ``Local Storage/leveldb``.
    key_prefix : bytes
        Keep only keys with this prefix; empty keeps every key.

    Returns
    -------
    dict[bytes, bytes]
        Live keys and their values; empty when the directory cannot be listed.

    Examples
    --------
    >>> import pathlib, tempfile
    >>> from agentgrep._chromium_storage import read_leveldb
    >>> read_leveldb(pathlib.Path(tempfile.mkdtemp()))
    {}
    """
    latest: dict[bytes, tuple[int, bytes | None]] = {}
    try:
        paths = sorted(directory.iterdir())
    except OSError:
        return {}
    for path in paths:
        if path.suffix == ".log":
            reader = _log_entries
        elif path.suffix in {".ldb", ".sst"}:
            reader = _table_entries
        else:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        try:
            for key, value, sequence in reader(raw):
                if not key.startswith(key_prefix):
                    continue
                current = latest.get(key)
                if current is None or sequence >= current[0]:
                    latest[key] = (sequence, value)
        except ValueError, IndexError:
            continue
    return {key: value for key, (_, value) in latest.items() if value is not None}


def _log_entries(raw: bytes) -> cabc.Iterator[_Entry]:
    """Yield the entries of each intact write batch in a LevelDB log.

    A batch is applied whole or not at all, as LevelDB applies it: one that
    does not parse is skipped and the batches after it are still read.
    """
    for payload in _log_payloads(raw):
        try:
            entries = list(_batch_entries(payload))
        except ValueError, IndexError:
            continue
        yield from entries


def _decode_storage_string(raw: bytes) -> str | None:
    """Decode a Chromium Local Storage string: ``0x01`` + Latin-1, or ``0x00`` + UTF-16LE."""
    if not raw:
        return None
    if raw[0] == 1:
        return raw[1:].decode("latin-1")
    if raw[0] == 0 and len(raw) % 2 == 1:
        return raw[1:].decode("utf-16-le", errors="replace")
    return None


def local_storage_values(
    directory: pathlib.Path,
    origin: str,
    keep: cabc.Callable[[str], bool],
) -> dict[str, str]:
    r"""Return one origin's Local Storage values whose key ``keep`` accepts.

    Chromium stores each item under ``_<origin>\x00<key>``; keys and values are
    encoded strings. Only accepted keys are decoded, so a caller that reads one
    entry never touches the origin's other values (session data, secrets).

    Parameters
    ----------
    directory : pathlib.Path
        The ``Local Storage/leveldb`` directory.
    origin : str
        Serialized origin, such as ``https://chatgpt.com``.
    keep : Callable[[str], bool]
        Returns true for the item keys to decode.

    Returns
    -------
    dict[str, str]
        Accepted item keys and their values.

    Examples
    --------
    >>> import pathlib, tempfile
    >>> from agentgrep._chromium_storage import local_storage_values
    >>> local_storage_values(pathlib.Path(tempfile.mkdtemp()), "https://chatgpt.com", bool)
    {}
    """
    prefix = b"_" + origin.encode() + b"\x00"
    values: dict[str, str] = {}
    for raw_key, raw_value in read_leveldb(directory, key_prefix=prefix).items():
        key = _decode_storage_string(raw_key[len(prefix) :])
        if key is None or not keep(key):
            continue
        value = _decode_storage_string(raw_value)
        if value is not None:
            values[key] = value
    return values
