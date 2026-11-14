"""Read cached HTTP responses from a Chromium blockfile disk cache.

Chromium-based apps on Windows (ChatGPT desktop among them) keep their HTTP
cache in the *blockfile* format: an ``index`` file whose hash table points at
256-byte entry records in ``data_1``, each naming the blocks or external
``f_<n>`` files that hold its key, response headers (stream 0), and body
(stream 1). This module reads that layout directly, without Chromium and
without taking the cache's lock, the way :mod:`agentgrep._chromium_storage`
reads LevelDB. Only the entries a caller accepts by URL have their streams
read, and a damaged entry is skipped without losing the others.

The layout follows Chromium's ``net/disk_cache/blockfile/disk_format.h``,
``disk_format_base.h``, and ``addr.h`` (tag 130.0.6723.116).
"""

from __future__ import annotations

import contextlib
import dataclasses
import gzip
import struct
import typing as t
import zlib

if t.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

__all__ = ("CachedResponse", "decode_body", "read_blockfile_cache")

_INDEX_MAGIC = 0xC103CAC3
_INDEX_HEADER_SIZE = 368
"""``IndexHeader``: 256 bytes of counters and padding, then the 112-byte ``LruData``."""
_DEFAULT_TABLE_LEN = 0x10000
_BLOCK_HEADER_SIZE = 8192
_ENTRY_SIZE = 256
_BLOCK_SIZES = {2: 256, 3: 1024, 4: 4096}
"""Block size per address file type: ``BLOCK_256``, ``BLOCK_1K``, ``BLOCK_4K``."""
_ENTRY = struct.Struct("<II4xiii8xiI4i4II16xI")
"""``EntryStore`` up to its key: hash, next, (rankings), reuse and refetch
counts, state, (creation time), key length, long key, four stream sizes, four
stream addresses, flags, (padding), self hash."""
_KEY_OFFSET = 96
_MAX_ENTRIES = 1 << 20
"""Ceiling on entries visited, so a corrupt chain cannot loop forever."""


@dataclasses.dataclass(frozen=True, slots=True)
class CachedResponse:
    """One cached HTTP response: its URL, content encoding, and decoded body."""

    url: str
    content_encoding: str
    body: bytes


class _CacheFiles:
    """Open block and external files on demand, read by offset, close together."""

    def __init__(self, directory: pathlib.Path, stack: contextlib.ExitStack) -> None:
        self._directory = directory
        self._stack = stack
        self._handles: dict[str, t.BinaryIO] = {}

    def read(self, name: str, offset: int, size: int) -> bytes:
        handle = self._handles.get(name)
        if handle is None:
            handle = self._stack.enter_context((self._directory / name).open("rb"))
            self._handles[name] = handle
        handle.seek(offset)
        data = handle.read(size)
        if len(data) != size:
            msg = f"{name}: short read at {offset}"
            raise ValueError(msg)
        return data

    def read_address(self, address: int, size: int) -> bytes:
        """Return ``size`` bytes stored at cache address ``address``."""
        if not address & 0x80000000:
            msg = "uninitialized cache address"
            raise ValueError(msg)
        file_type = (address & 0x70000000) >> 28
        if file_type == 0:
            return self.read(f"f_{address & 0x0FFFFFFF:06x}", 0, size)
        block_size = _BLOCK_SIZES.get(file_type)
        if block_size is None:
            msg = f"unsupported cache file type {file_type}"
            raise ValueError(msg)
        blocks = ((address & 0x03000000) >> 24) + 1
        if size > blocks * block_size:
            msg = "stream larger than its blocks"
            raise ValueError(msg)
        start = address & 0x0000FFFF
        name = f"data_{(address & 0x00FF0000) >> 16}"
        return self.read(name, _BLOCK_HEADER_SIZE + start * block_size, size)


def _content_encoding(headers: bytes) -> str:
    """Return the ``content-encoding`` of a pickled response's raw header block."""
    lowered = headers.lower()
    marker = lowered.find(b"\x00content-encoding:")
    if marker < 0:
        return ""
    start = marker + len(b"\x00content-encoding:")
    end = lowered.find(b"\x00", start)
    return lowered[start : end if end >= 0 else len(lowered)].strip().decode("latin-1")


def decode_body(body: bytes, content_encoding: str) -> bytes:
    """Undo a response's ``content-encoding``; raise ``ValueError`` when it cannot.

    Examples
    --------
    >>> import gzip
    >>> from agentgrep._chromium_cache import decode_body
    >>> decode_body(gzip.compress(b'{"items": []}'), "gzip")
    b'{"items": []}'
    >>> decode_body(b"plain", "")
    b'plain'
    """
    try:
        if content_encoding in {"", "identity"}:
            return body
        if content_encoding == "gzip":
            return gzip.decompress(body)
        if content_encoding == "deflate":
            return zlib.decompress(body)
        if content_encoding == "br":
            import brotli  # a C extension, loaded only when a cached body needs it

            return t.cast("bytes", brotli.decompress(body))
    except Exception as exc:
        msg = f"cannot decode {content_encoding} body"
        raise ValueError(msg) from exc
    msg = f"unsupported content-encoding {content_encoding!r}"
    raise ValueError(msg)


def read_blockfile_cache(
    directory: pathlib.Path,
    keep_url: cabc.Callable[[str], bool],
) -> list[CachedResponse]:
    """Return the cached responses whose URL ``keep_url`` accepts, bodies decoded.

    An entry's key ends with its URL, after a ``1/0/`` prefix and, for a
    partitioned cache, ``_dk_<site> <site> ``. Entries whose URL is refused are
    never read past their key, and an entry that is damaged, truncated, or
    encoded in a way this reader cannot undo is skipped.

    Parameters
    ----------
    directory : pathlib.Path
        The cache directory holding ``index`` and the ``data_<n>`` files.
    keep_url : Callable[[str], bool]
        Whether to read an entry's streams, given its URL.

    Returns
    -------
    list[CachedResponse]
        Accepted responses, in index order.

    Raises
    ------
    ValueError
        If ``index`` is not a blockfile cache index.
    """
    index = (directory / "index").read_bytes()
    if len(index) < _INDEX_HEADER_SIZE or struct.unpack_from("<I", index)[0] != _INDEX_MAGIC:
        msg = f"{directory}: not a blockfile cache index"
        raise ValueError(msg)
    table_len = struct.unpack_from("<i", index, 28)[0] or _DEFAULT_TABLE_LEN
    available = (len(index) - _INDEX_HEADER_SIZE) // 4
    heads = struct.unpack_from(f"<{min(table_len, available)}I", index, _INDEX_HEADER_SIZE)
    responses: list[CachedResponse] = []
    visited: set[int] = set()
    with contextlib.ExitStack() as stack:
        files = _CacheFiles(directory, stack)
        for head in heads:
            address = head
            while address and address not in visited and len(visited) < _MAX_ENTRIES:
                visited.add(address)
                try:
                    address, response = _read_entry(files, address, keep_url)
                except OSError, ValueError, struct.error:
                    break
                if response is not None:
                    responses.append(response)
    return responses


def _read_entry(
    files: _CacheFiles,
    address: int,
    keep_url: cabc.Callable[[str], bool],
) -> tuple[int, CachedResponse | None]:
    """Read one entry record; return the next address in its chain and its response."""
    record = files.read_address(address, _ENTRY_SIZE)
    fields = _ENTRY.unpack_from(record)
    next_address, key_len, long_key = fields[1], fields[5], fields[6]
    sizes, addresses = fields[7:11], fields[11:15]
    if key_len <= 0:
        return next_address, None
    if long_key:
        key = files.read_address(long_key, key_len)
    else:
        key = record[_KEY_OFFSET : _KEY_OFFSET + key_len]
    text = key.decode("utf-8", "replace").rsplit(" ", 1)[-1]
    scheme = text.find("://")
    # Keys start with a ``1/0/`` prefix, and partitioned ones with ``_dk_<site> <site> ``.
    url = text[text.rfind("/", 0, max(scheme, 0)) + 1 :] if scheme > 0 else text
    if not keep_url(url):
        return next_address, None
    try:
        headers = files.read_address(addresses[0], sizes[0]) if sizes[0] > 0 else b""
        body = files.read_address(addresses[1], sizes[1]) if sizes[1] > 0 else b""
        encoding = _content_encoding(headers)
        decoded = decode_body(body, encoding)
    except OSError, ValueError:
        return next_address, None
    return next_address, CachedResponse(url=url, content_encoding=encoding, body=decoded)
