"""ChatGPT desktop on Windows: the cached conversation list becomes title records.

The app keeps its Local Storage in LevelDB. These tests write small LevelDB
directories by hand (write-ahead logs and sorted tables, plain and Snappy) so
the pure-Python reader and the title adapter run against the real byte formats
without the app or a LevelDB library.
"""

from __future__ import annotations

import json
import pathlib
import typing as t

import pytest

from agentgrep import wsl
from agentgrep._chromium_storage import local_storage_values
from agentgrep.adapters import iter_source_records
from agentgrep.discovery import CHATGPT_DESKTOP_PACKAGE, discover_sources
from agentgrep.records import BackendSelection

_NO_BACKENDS = BackendSelection(find_tool=None, grep_tool=None, json_tool=None)
_ORIGIN = b"_https://chatgpt.com\x00"
_HISTORY_KEY = "cache/user-acct/abc123/conversation-history"


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _storage_string(text: str, *, utf16: bool = False) -> bytes:
    return b"\x00" + text.encode("utf-16-le") if utf16 else b"\x01" + text.encode("latin-1")


def _history_value(items: list[dict[str, object]], *, utf16: bool = False) -> bytes:
    payload = {"timestamp": 1, "value": {"pages": [{"items": items, "total": len(items)}]}}
    return _storage_string(json.dumps(payload, ensure_ascii=False), utf16=utf16)


def _item(conversation_id: str, title: str, update_time: str) -> dict[str, object]:
    return {
        "id": conversation_id,
        "title": title,
        "create_time": "2026-09-01T00:00:00.000000Z",
        "update_time": update_time,
        "mapping": None,
    }


Put = tuple[bytes, bytes | None]
"""A Local Storage key and its value; ``None`` writes a deletion."""


def _batch(sequence: int, puts: list[Put], *, count: int | None = None) -> bytes:
    """Encode one LevelDB write batch; ``count`` overrides the header's entry count."""
    header = sequence.to_bytes(8, "little") + (len(puts) if count is None else count).to_bytes(
        4,
        "little",
    )
    body = bytearray(header)
    for key, value in puts:
        body += (b"\x01" if value is not None else b"\x00") + _varint(len(key)) + key
        if value is not None:
            body += _varint(len(value)) + value
    return bytes(body)


def _record(payload: bytes) -> bytes:
    """Frame one payload as a full LevelDB log record."""
    return b"\x00\x00\x00\x00" + len(payload).to_bytes(2, "little") + b"\x01" + payload


def _write_log(directory: pathlib.Path, name: str, sequence: int, puts: list[Put]) -> None:
    """Write one LevelDB log holding a single write batch."""
    (directory / name).write_bytes(_record(_batch(sequence, puts)))


def _snappy_literal(data: bytes) -> bytes:
    """Compress as one Snappy literal, the simplest valid Snappy block."""
    size = len(data) - 1
    tag = bytes([size << 2]) if size < 60 else bytes([61 << 2]) + size.to_bytes(2, "little")
    return _varint(len(data)) + tag + data


def _block(entries: list[tuple[bytes, bytes]]) -> bytes:
    body = b"".join(
        _varint(0) + _varint(len(key)) + _varint(len(value)) + key + value for key, value in entries
    )
    return body + (0).to_bytes(4, "little") + (1).to_bytes(4, "little")


def _write_table(
    directory: pathlib.Path,
    name: str,
    sequence: int,
    puts: list[Put],
    *,
    snappy: bool,
) -> None:
    """Write one LevelDB sorted table with a single data block."""
    entries = [
        (
            key
            + (((sequence + index) << 8) | (1 if value is not None else 0)).to_bytes(8, "little"),
            value or b"",
        )
        for index, (key, value) in enumerate(sorted(puts, key=lambda put: put[0]))
    ]
    data = _block(entries)
    stored = _snappy_literal(data) if snappy else data
    data_section = stored + (b"\x01" if snappy else b"\x00") + b"\x00" * 4
    index = _block([(entries[-1][0], _varint(0) + _varint(len(stored)))])
    index_offset = len(data_section)
    index_section = index + b"\x00" * 5
    meta = _block([])
    meta_offset = index_offset + len(index_section)
    handles = (
        _varint(meta_offset) + _varint(len(meta)) + _varint(index_offset) + _varint(len(index))
    )
    footer = handles.ljust(40, b"\x00") + (0xDB4775248B80FB57).to_bytes(8, "little")
    (directory / name).write_bytes(data_section + index_section + meta + b"\x00" * 5 + footer)


class ConversationListCase(t.NamedTuple):
    """One Local Storage layout and the titles the adapter must read from it."""

    test_id: str
    files: tuple[tuple[str, int, tuple[Put, ...]], ...]
    expected: frozenset[str]


_KEY = _ORIGIN + _storage_string(_HISTORY_KEY)
_SECRET = _ORIGIN + _storage_string("client-correlated-secret")
_OLD = _history_value([_item("c1", "Draft the widget plan", "2026-09-02T00:00:00.000000Z")])
_NEW = _history_value(
    [
        _item("c1", "Widget launch plan", "2026-09-05T00:00:00.000000Z"),
        _item("c2", "Résumé ✓ 日本語", "2026-09-04T00:00:00.000000Z"),
    ],
    utf16=True,
)

CONVERSATION_LIST_CASES: tuple[ConversationListCase, ...] = (
    ConversationListCase(
        test_id="write-ahead-log",
        files=(("000003.log", 1, ((_KEY, _OLD), (_SECRET, _storage_string("x")))),),
        expected=frozenset({"Draft the widget plan"}),
    ),
    ConversationListCase(
        test_id="snappy-table-newer-than-log",
        files=(
            ("000003.log", 1, ((_KEY, _OLD),)),
            ("000005.ldb", 7, ((_KEY, _NEW), (_SECRET, _storage_string("x")))),
        ),
        expected=frozenset({"Widget launch plan", "Résumé ✓ 日本語"}),
    ),
    ConversationListCase(
        test_id="plain-table-older-than-log",
        files=(
            ("000004.ldb", 1, ((_KEY, _NEW),)),
            ("000006.log", 9, ((_KEY, _OLD),)),
        ),
        expected=frozenset({"Draft the widget plan"}),
    ),
    ConversationListCase(
        test_id="mixed-time-formats",
        files=(
            (
                "000003.log",
                1,
                (
                    (
                        _KEY,
                        _history_value(
                            [{"id": "c1", "title": "Epoch title", "update_time": 1757000000}],
                        ),
                    ),
                    (
                        _ORIGIN + _storage_string(_HISTORY_KEY + "-without-projects"),
                        _history_value(
                            [_item("c1", "ISO title", "2025-09-04T15:33:20.900000Z")],
                        ),
                    ),
                ),
            ),
        ),
        expected=frozenset({"ISO title"}),
    ),
    ConversationListCase(
        test_id="deleted-list",
        files=(
            ("000003.log", 1, ((_KEY, _OLD),)),
            ("000007.log", 4, ((_KEY, None),)),
        ),
        expected=frozenset(),
    ),
)


@pytest.mark.parametrize(
    list(ConversationListCase._fields),
    CONVERSATION_LIST_CASES,
    ids=[case.test_id for case in CONVERSATION_LIST_CASES],
)
def test_conversation_list_becomes_title_records(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    test_id: str,
    files: tuple[tuple[str, int, tuple[Put, ...]], ...],
    expected: frozenset[str],
) -> None:
    """The newest snapshot of each conversation becomes one record; other keys stay unread."""
    users = tmp_path / "Users"
    profile = users / "windows-account-name"
    leveldb = (
        profile
        / f"AppData/Local/Packages/{CHATGPT_DESKTOP_PACKAGE}/LocalCache/Roaming/ChatGPT"
        / "Local Storage/leveldb"
    )
    leveldb.mkdir(parents=True)
    (leveldb / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")
    for name, sequence, puts in files:
        if name.endswith(".log"):
            _write_log(leveldb, name, sequence, list(puts))
        else:
            _write_table(leveldb, name, sequence, list(puts), snappy="snappy" in test_id)
    monkeypatch.setenv(wsl.USERS_ROOT_ENV, str(users))
    monkeypatch.setattr(wsl, "is_wsl", lambda proc_version=None: True)

    (source,) = discover_sources(tmp_path / "home", ("chatgpt",), _NO_BACKENDS)
    records = list(iter_source_records(source))
    assert {record.title for record in records} == expected
    assert all(record.session_id == record.conversation_id for record in records)
    assert local_storage_values(leveldb, "https://chatgpt.com", lambda key: True).keys() <= {
        _HISTORY_KEY,
        _HISTORY_KEY + "-without-projects",
        "client-correlated-secret",
    }
    assert set(
        local_storage_values(leveldb, "https://chatgpt.com", lambda key: key == _HISTORY_KEY),
    ) <= {_HISTORY_KEY}


class DamagedStoreCase(t.NamedTuple):
    """A LevelDB directory with one damaged part, and the entries that must survive it."""

    test_id: str
    files: tuple[tuple[str, bytes], ...]
    expected: dict[bytes, bytes]


def _snappy_flagged_table() -> bytes:
    """Build a table with a bad block (flagged Snappy, holding plain bytes) and a good one."""

    def internal(key: bytes, sequence: int) -> bytes:
        return key + ((sequence << 8) | 1).to_bytes(8, "little")

    bad = _block([(internal(b"t", 9), b"lost")])
    good = _block([(internal(b"u", 10), b"kept")])
    bad_section = bad + b"\x01" + b"\x00" * 4
    good_section = good + b"\x00" * 5
    index = _block(
        [
            (internal(b"t", 9), _varint(0) + _varint(len(bad))),
            (internal(b"u", 10), _varint(len(bad_section)) + _varint(len(good))),
        ],
    )
    index_offset = len(bad_section) + len(good_section)
    index_section = index + b"\x00" * 5
    meta = _block([])
    meta_offset = index_offset + len(index_section)
    meta_handle = _varint(meta_offset) + _varint(len(meta))
    handles = meta_handle + _varint(index_offset) + _varint(len(index))
    footer = handles.ljust(40, b"\x00") + (0xDB4775248B80FB57).to_bytes(8, "little")
    return bad_section + good_section + index_section + meta + b"\x00" * 5 + footer


DAMAGED_STORE_CASES: tuple[DamagedStoreCase, ...] = (
    DamagedStoreCase(
        test_id="over-counted-batch-skipped",
        files=(
            (
                "000003.log",
                _record(_batch(1, [(b"a", b"1")]))
                + _record(_batch(2, [(b"x", b"9")], count=3))
                + _record(_batch(5, [(b"b", b"2")])),
            ),
        ),
        expected={b"a": b"1", b"b": b"2"},
    ),
    DamagedStoreCase(
        test_id="bad-snappy-block-skipped",
        files=(
            ("000004.ldb", _snappy_flagged_table()),
            ("000005.log", _record(_batch(1, [(b"c", b"3")]))),
        ),
        expected={b"u": b"kept", b"c": b"3"},
    ),
)


@pytest.mark.parametrize(
    list(DamagedStoreCase._fields),
    DAMAGED_STORE_CASES,
    ids=[case.test_id for case in DAMAGED_STORE_CASES],
)
def test_damaged_store_keeps_intact_entries(
    tmp_path: pathlib.Path,
    test_id: str,
    files: tuple[tuple[str, bytes], ...],
    expected: dict[bytes, bytes],
) -> None:
    """A damaged batch or table block loses only itself, never the rest of the file."""
    from agentgrep._chromium_storage import read_leveldb

    for name, data in files:
        (tmp_path / name).write_bytes(data)
    assert read_leveldb(tmp_path) == expected
