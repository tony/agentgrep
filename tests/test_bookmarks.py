# ruff: noqa: D103
"""Functional tests for durable bookmark persistence."""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import stat
import typing as t

import pytest

import agentgrep.bookmarks as bookmarks
from agentgrep.bookmarks import (
    BookmarkCapacityError,
    BookmarkEntry,
    BookmarkError,
    BookmarkFormatError,
    BookmarkMutation,
    BookmarkStore,
    BookmarkValidationError,
    bookmark_entry_for_record,
)
from agentgrep.identity import record_identity
from agentgrep.records import RecordPosition, SearchRecord

CONTENT_ID = "agc1:00000000000000000000000000"
OTHER_CONTENT_ID = "agc1:11111111111111111111111111"
RECORD_ID = "agr1:22222222222222222222222222"
THREAD_ID = "agt1:33333333333333333333333333"
OTHER_THREAD_ID = "agt1:44444444444444444444444444"
CREATED_AT = "2026-07-12T12:00:00Z"


def _mode(path: pathlib.Path) -> int:
    """Return only the permission bits for ``path``."""
    return stat.S_IMODE(path.stat().st_mode)


def _record(tmp_path: pathlib.Path) -> SearchRecord:
    """Build one record with all three canonical identities."""
    return SearchRecord(
        kind="prompt",
        agent="codex",
        store="codex.sessions",
        adapter_id="codex.sessions_jsonl.v1",
        path=tmp_path / "private" / "rollout.jsonl",
        text="private prompt body",
        role="user",
        session_id="abc",
        identity_namespace="codex.session",
        position=RecordPosition(native_id="msg-1"),
    )


def _call_store_method(store: BookmarkStore, method: str) -> object:
    """Call one public storage method with valid bookmark input."""
    if method == "list":
        return store.list()
    if method == "add":
        return store.add(THREAD_ID, created_at=CREATED_AT)
    if method == "remove":
        return store.remove(THREAD_ID)
    if method == "toggle":
        return store.toggle(BookmarkEntry(THREAD_ID, "thread", None, CREATED_AT))
    msg = f"unsupported test method: {method}"
    raise AssertionError(msg)


def _assert_path_free_storage_error(error: BookmarkError, path: pathlib.Path) -> None:
    """Assert a storage error exposes neither a path nor a chained cause."""
    sensitive = str(path)
    assert type(error) is BookmarkError
    assert sensitive not in str(error)
    assert sensitive not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_default_path_uses_xdg_data_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    store = BookmarkStore()

    assert store.path == data_home / "agentgrep" / "bookmarks.json"


def test_default_path_falls_back_to_local_share(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    store = BookmarkStore()

    assert store.path == tmp_path / ".local" / "share" / "agentgrep" / "bookmarks.json"


def test_bookmark_values_are_immutable() -> None:
    entry = BookmarkEntry(RECORD_ID, "record", CONTENT_ID, CREATED_AT)
    mutation = BookmarkMutation("added", entry)

    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.target_id = THREAD_ID  # ty: ignore[invalid-assignment]
    with pytest.raises(dataclasses.FrozenInstanceError):
        mutation.action = "removed"  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    ("target_id", "scope", "content_id"),
    [
        ("agr1:short", "record", CONTENT_ID),
        ("agr1:zzzzzzzzzzzzzzzzzzzzzzzzzz", "record", CONTENT_ID),
        (RECORD_ID.upper(), "record", CONTENT_ID),
        (RECORD_ID, "thread", None),
        (THREAD_ID, "content", None),
        (CONTENT_ID, "record", CONTENT_ID),
        (RECORD_ID, "unknown", CONTENT_ID),
        (RECORD_ID, "record", None),
        (RECORD_ID, "record", THREAD_ID),
        (THREAD_ID, "thread", CONTENT_ID),
        (CONTENT_ID, "content", CONTENT_ID),
    ],
)
def test_entry_rejects_invalid_identifier_scope_combinations(
    target_id: str,
    scope: str,
    content_id: str | None,
) -> None:
    with pytest.raises(BookmarkValidationError):
        BookmarkEntry(
            target_id=target_id,
            scope=t.cast("bookmarks.BookmarkScope", scope),
            content_id=content_id,
            created_at=CREATED_AT,
        )


@pytest.mark.parametrize(
    "created_at",
    ["", "not-a-time", "2026-07-12", "2026-07-12T12:00:00", "2026-99-12T12:00:00Z"],
)
def test_entry_rejects_invalid_creation_time(created_at: str) -> None:
    with pytest.raises(BookmarkValidationError):
        BookmarkEntry(CONTENT_ID, "content", None, created_at)


def test_store_rejects_invalid_capacity(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="capacity"):
        BookmarkStore(tmp_path / "bookmarks.json", capacity=0)


def test_round_trip_uses_canonical_versioned_schema(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "state" / "bookmarks.json"
    store = BookmarkStore(path)

    mutation = store.add(RECORD_ID, content_id=CONTENT_ID, created_at=CREATED_AT)

    entry = BookmarkEntry(RECORD_ID, "record", CONTENT_ID, CREATED_AT)
    assert mutation == BookmarkMutation("added", entry)
    assert store.list() == [entry]
    assert path.read_bytes() == (
        b'{"entries":[{"content_id":"agc1:00000000000000000000000000",'
        b'"created_at":"2026-07-12T12:00:00Z","scope":"record",'
        b'"target_id":"agr1:22222222222222222222222222"}],"schema_version":1}\n'
    )


def test_add_and_remove_are_idempotent(tmp_path: pathlib.Path) -> None:
    store = BookmarkStore(tmp_path / "bookmarks.json")
    entry = BookmarkEntry(THREAD_ID, "thread", None, CREATED_AT)

    first_add = store.add(THREAD_ID, created_at=CREATED_AT)
    first_bytes = store.path.read_bytes()
    second_add = store.add(THREAD_ID, created_at="2026-07-12T12:01:00Z")

    assert first_add == BookmarkMutation("added", entry)
    assert second_add == BookmarkMutation("unchanged", entry)
    assert store.path.read_bytes() == first_bytes
    assert store.remove(THREAD_ID) == BookmarkMutation("removed", entry)
    assert store.remove(THREAD_ID) == BookmarkMutation("unchanged", None)
    assert store.list() == []


def test_toggle_adds_then_removes_one_entry(tmp_path: pathlib.Path) -> None:
    store = BookmarkStore(tmp_path / "bookmarks.json")
    entry = BookmarkEntry(CONTENT_ID, "content", None, CREATED_AT)

    assert store.toggle(entry) == BookmarkMutation("added", entry)
    assert store.list() == [entry]
    assert store.toggle(entry) == BookmarkMutation("removed", entry)
    assert store.list() == []


def test_capacity_error_does_not_evict_or_rewrite(tmp_path: pathlib.Path) -> None:
    store = BookmarkStore(tmp_path / "bookmarks.json", capacity=1)
    first = BookmarkEntry(THREAD_ID, "thread", None, CREATED_AT)
    assert store.add(THREAD_ID, created_at=CREATED_AT).entry == first
    original = store.path.read_bytes()

    with pytest.raises(BookmarkCapacityError, match=r"200|capacity|full"):
        store.add(OTHER_THREAD_ID, created_at=CREATED_AT)

    assert store.list() == [first]
    assert store.path.read_bytes() == original


def test_store_creates_private_directory_snapshot_and_lock(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "private" / "bookmarks.json"
    store = BookmarkStore(path)

    store.add(CONTENT_ID, created_at=CREATED_AT)

    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600
    assert _mode(path.parent / "bookmarks.lock") == 0o600


@pytest.mark.skipif(
    getattr(os, "O_NOFOLLOW", 0) == 0,
    reason="platform does not expose O_NOFOLLOW",
)
def test_lock_symlink_is_refused_without_touching_target(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bookmarks.json"
    lock_path = path.parent / "bookmarks.lock"
    target = tmp_path / "lock-target"
    target.write_bytes(b"keep this lock target unchanged")
    target.chmod(0o640)
    lock_path.symlink_to(target)
    original_stat = target.stat()
    flock_calls: list[int] = []

    def tracking_flock(fd: int, operation: int) -> None:
        flock_calls.append(operation)

    monkeypatch.setattr(bookmarks.fcntl, "flock", tracking_flock)

    with pytest.raises(BookmarkError) as exc_info:
        BookmarkStore(path).list()

    _assert_path_free_storage_error(exc_info.value, lock_path)
    assert flock_calls == []
    assert lock_path.is_symlink()
    assert target.read_bytes() == b"keep this lock target unchanged"
    assert stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE(original_stat.st_mode)


def test_lock_descriptor_must_be_a_regular_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bookmarks.json"
    lock_path = path.parent / "bookmarks.lock"
    read_fd, write_fd = os.pipe()
    real_open = bookmarks.os.open
    flock_calls: list[int] = []

    def open_pipe_for_lock(
        candidate: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
    ) -> int:
        if pathlib.Path(candidate) == lock_path:
            return os.dup(read_fd)
        return real_open(candidate, flags, mode)

    def tracking_flock(fd: int, operation: int) -> None:
        flock_calls.append(operation)

    monkeypatch.setattr(bookmarks.os, "open", open_pipe_for_lock)
    monkeypatch.setattr(bookmarks.fcntl, "flock", tracking_flock)
    try:
        with pytest.raises(BookmarkError) as exc_info:
            BookmarkStore(path).list()
    finally:
        os.close(read_fd)
        os.close(write_fd)

    _assert_path_free_storage_error(exc_info.value, lock_path)
    assert flock_calls == []


@pytest.mark.parametrize("method", ["list", "add"], ids=["list", "idempotent-add"])
def test_existing_snapshot_mode_is_repaired_under_lock(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """flock/fchmod instrumentation is needed to prove repair ordering."""
    path = tmp_path / "bookmarks.json"
    store = BookmarkStore(path)
    entry = BookmarkEntry(THREAD_ID, "thread", None, CREATED_AT)
    store.add(THREAD_ID, created_at=CREATED_AT)
    path.chmod(0o644)
    snapshot_stat = path.stat()
    real_fchmod = bookmarks.os.fchmod
    real_flock = bookmarks.fcntl.flock
    lock_held = False
    snapshot_repairs = 0

    def tracking_flock(fd: int, operation: int) -> None:
        nonlocal lock_held
        if operation & bookmarks.fcntl.LOCK_UN:
            assert lock_held
            real_flock(fd, operation)
            lock_held = False
            return
        real_flock(fd, operation)
        assert not lock_held
        lock_held = True

    def tracking_fchmod(fd: int, mode: int) -> None:
        nonlocal snapshot_repairs
        descriptor_stat = os.fstat(fd)
        if (
            descriptor_stat.st_dev == snapshot_stat.st_dev
            and descriptor_stat.st_ino == snapshot_stat.st_ino
        ):
            assert lock_held
            assert mode == 0o600
            snapshot_repairs += 1
        real_fchmod(fd, mode)

    monkeypatch.setattr(bookmarks.fcntl, "flock", tracking_flock)
    monkeypatch.setattr(bookmarks.os, "fchmod", tracking_fchmod)

    result = _call_store_method(store, method)

    if method == "list":
        assert result == [entry]
    else:
        assert result == BookmarkMutation("unchanged", entry)
    assert snapshot_repairs == 1
    assert _mode(path) == 0o600


def test_existing_explicit_parent_permissions_are_preserved(tmp_path: pathlib.Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o755)

    assert BookmarkStore(parent / "bookmarks.json").list() == []

    assert _mode(parent) == 0o755


def test_existing_default_app_directory_is_repaired(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_home = tmp_path / "xdg-data"
    app_directory = data_home / "agentgrep"
    app_directory.mkdir(parents=True)
    app_directory.chmod(0o755)
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    assert BookmarkStore().list() == []

    assert _mode(app_directory) == 0o700


@pytest.mark.parametrize("method", ["list", "add", "remove", "toggle"])
def test_public_store_methods_hide_is_a_directory_failures(
    tmp_path: pathlib.Path,
    method: str,
) -> None:
    path = tmp_path / "bookmarks.json"
    path.mkdir()

    with pytest.raises(BookmarkError) as exc_info:
        _call_store_method(BookmarkStore(path), method)

    _assert_path_free_storage_error(exc_info.value, path)


@pytest.mark.parametrize("method", ["list", "add", "remove", "toggle"])
def test_public_store_methods_hide_permission_failures(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """Monkeypatching is required because elevated CI can bypass mode denial."""
    path = tmp_path / "private" / "bookmarks.json"

    def deny_directory(_store: BookmarkStore) -> None:
        message = f"permission denied: {path}"
        raise PermissionError(message)

    monkeypatch.setattr(BookmarkStore, "_ensure_directory", deny_directory)

    with pytest.raises(BookmarkError) as exc_info:
        _call_store_method(BookmarkStore(path), method)

    _assert_path_free_storage_error(exc_info.value, path)


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json\n",
        b'{"entries":[],"schema_version":2}\n',
        b'{"entries":[],"extra":true,"schema_version":1}\n',
        b'{"entries":"not-a-list","schema_version":1}\n',
        (
            b'{"entries":[{"content_id":null,'
            b'"created_at":"2026-07-12T12:00:00Z","scope":"thread",'
            b'"target_id":"agt1:33333333333333333333333333"},'
            b'{"content_id":null,"created_at":"","scope":"thread",'
            b'"target_id":"agt1:44444444444444444444444444"}],'
            b'"schema_version":1}\n'
        ),
    ],
    ids=["invalid-json", "unknown-schema", "unknown-field", "entries-type", "later-entry"],
)
def test_list_refuses_corrupt_or_unknown_snapshots(
    tmp_path: pathlib.Path,
    raw: bytes,
) -> None:
    path = tmp_path / "bookmarks.json"
    path.write_bytes(raw)

    with pytest.raises(BookmarkFormatError, match="bookmark") as exc_info:
        BookmarkStore(path).list()

    assert str(path) not in str(exc_info.value)
    assert "private prompt body" not in str(exc_info.value)


def test_snapshot_rejects_duplicate_targets(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "bookmarks.json"
    entry = {
        "content_id": None,
        "created_at": CREATED_AT,
        "scope": "thread",
        "target_id": THREAD_ID,
    }
    path.write_text(
        json.dumps({"entries": [entry, entry], "schema_version": 1}),
        encoding="utf-8",
    )

    with pytest.raises(BookmarkFormatError, match="bookmark"):
        BookmarkStore(path).list()


def test_short_writes_are_completed(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = BookmarkStore(tmp_path / "bookmarks.json")
    real_write = bookmarks.os.write
    write_sizes: list[int] = []

    def short_write(fd: int, data: bytes | bytearray | memoryview) -> int:
        requested = len(data)
        write_sizes.append(requested)
        return real_write(fd, data[: max(1, requested // 2)])

    monkeypatch.setattr(bookmarks.os, "write", short_write)

    store.add(RECORD_ID, content_id=CONTENT_ID, created_at=CREATED_AT)

    assert len(write_sizes) > 1
    assert store.list() == [BookmarkEntry(RECORD_ID, "record", CONTENT_ID, CREATED_AT)]


def test_one_lock_covers_read_modify_replace_transaction(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = BookmarkStore(tmp_path / "bookmarks.json")
    store.add(THREAD_ID, created_at=CREATED_AT)
    real_flock = bookmarks.fcntl.flock
    real_loads = bookmarks.json.loads
    real_replace = bookmarks.pathlib.Path.replace
    lock_held = False
    acquisitions = 0
    observed: list[str] = []

    def tracking_flock(fd: int, operation: int) -> None:
        nonlocal acquisitions, lock_held
        if operation & bookmarks.fcntl.LOCK_UN:
            assert lock_held
            real_flock(fd, operation)
            lock_held = False
            observed.append("unlock")
            return
        real_flock(fd, operation)
        assert not lock_held
        lock_held = True
        acquisitions += 1
        observed.append("lock")

    def tracking_loads(data: str | bytes | bytearray) -> object:
        assert lock_held
        observed.append("read")
        return real_loads(data)

    def tracking_replace(
        source: pathlib.Path,
        target: os.PathLike[str] | str,
    ) -> pathlib.Path:
        assert lock_held
        observed.append("replace")
        return real_replace(source, target)

    monkeypatch.setattr(bookmarks.fcntl, "flock", tracking_flock)
    monkeypatch.setattr(bookmarks.json, "loads", tracking_loads)
    monkeypatch.setattr(bookmarks.pathlib.Path, "replace", tracking_replace)

    store.add(OTHER_THREAD_ID, created_at=CREATED_AT)

    assert acquisitions == 1
    assert observed == ["lock", "read", "replace", "unlock"]


def test_write_fsyncs_file_replaces_atomically_then_fsyncs_parent(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state" / "bookmarks.json"
    store = BookmarkStore(path)
    real_fsync = bookmarks.os.fsync
    real_replace = bookmarks.pathlib.Path.replace
    events: list[tuple[str, object]] = []

    def tracking_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        kind = "directory" if stat.S_ISDIR(mode) else "file"
        events.append(("fsync", kind))
        real_fsync(fd)

    def tracking_replace(
        source: pathlib.Path,
        target: os.PathLike[str] | str,
    ) -> pathlib.Path:
        source_path = source
        target_path = pathlib.Path(target)
        assert source_path.parent == path.parent
        assert target_path == path
        assert source_path != path
        events.append(("replace", target_path.name))
        return real_replace(source, target)

    monkeypatch.setattr(bookmarks.os, "fsync", tracking_fsync)
    monkeypatch.setattr(bookmarks.pathlib.Path, "replace", tracking_replace)

    store.add(CONTENT_ID, created_at=CREATED_AT)

    assert events == [
        ("fsync", "file"),
        ("replace", "bookmarks.json"),
        ("fsync", "directory"),
    ]


def test_failed_atomic_replace_preserves_snapshot_and_cleans_temp(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bookmarks.json"
    store = BookmarkStore(path)
    store.add(THREAD_ID, created_at=CREATED_AT)
    original = path.read_bytes()

    def fail_replace(
        _source: pathlib.Path,
        _target: os.PathLike[str] | str,
    ) -> pathlib.Path:
        message = f"replace failed for {path}"
        raise OSError(message)

    monkeypatch.setattr(bookmarks.pathlib.Path, "replace", fail_replace)

    with pytest.raises(BookmarkError) as exc_info:
        store.add(OTHER_THREAD_ID, created_at=CREATED_AT)

    _assert_path_free_storage_error(exc_info.value, path)
    assert path.read_bytes() == original
    assert [item for item in tmp_path.iterdir() if item.suffix == ".tmp"] == []


def test_bookmark_entry_for_record_builds_each_scope(tmp_path: pathlib.Path) -> None:
    record = _record(tmp_path)
    prepared = record_identity(record)
    assert prepared.record_id is not None
    assert prepared.thread_id is not None

    assert bookmark_entry_for_record(
        record,
        scope="record",
        created_at=CREATED_AT,
    ) == BookmarkEntry(prepared.record_id, "record", prepared.content_id, CREATED_AT)
    assert bookmark_entry_for_record(
        record,
        scope="thread",
        created_at=CREATED_AT,
    ) == BookmarkEntry(prepared.thread_id, "thread", None, CREATED_AT)
    assert bookmark_entry_for_record(
        record,
        scope="content",
        created_at=CREATED_AT,
    ) == BookmarkEntry(prepared.content_id, "content", None, CREATED_AT)


def test_bookmark_entry_for_record_rejects_missing_record_identity(
    tmp_path: pathlib.Path,
) -> None:
    record = dataclasses.replace(_record(tmp_path), position=None)

    with pytest.raises(BookmarkValidationError, match="record"):
        bookmark_entry_for_record(record, scope="record")


def test_bookmark_entry_for_record_rejects_missing_thread_identity(
    tmp_path: pathlib.Path,
) -> None:
    record = dataclasses.replace(
        _record(tmp_path),
        identity_namespace=None,
        session_id=None,
    )

    with pytest.raises(BookmarkValidationError, match="thread"):
        bookmark_entry_for_record(record, scope="thread")


def test_snapshot_contains_no_record_body_or_path(tmp_path: pathlib.Path) -> None:
    record = _record(tmp_path)
    entry = bookmark_entry_for_record(record, scope="record", created_at=CREATED_AT)
    store = BookmarkStore(tmp_path / "state" / "bookmarks.json")

    store.toggle(entry)

    persisted = store.path.read_text(encoding="utf-8")
    assert record.text not in persisted
    assert str(record.path) not in persisted
    assert record.session_id is not None
    assert record.session_id not in persisted
