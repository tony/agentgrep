"""Durable, privacy-minimal bookmarks for canonical record identities."""

from __future__ import annotations

import builtins
import contextlib
import dataclasses
import datetime
import fcntl
import json
import os
import pathlib
import re
import stat
import tempfile
import typing as t

from agentgrep.identity import record_identity
from agentgrep.records import SearchRecord

__all__ = (
    "BookmarkCapacityError",
    "BookmarkEntry",
    "BookmarkError",
    "BookmarkFormatError",
    "BookmarkMutation",
    "BookmarkMutationAction",
    "BookmarkScope",
    "BookmarkStore",
    "BookmarkValidationError",
    "bookmark_entry_for_record",
)

type BookmarkScope = t.Literal["record", "thread", "content"]
type BookmarkMutationAction = t.Literal["added", "removed", "unchanged"]

_SCHEMA_VERSION = 1
_DEFAULT_CAPACITY = 200
_ID_PATTERN = re.compile(r"ag[crt]1:[0-9a-v]{26}")
_CREATED_AT_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})"
)
_PREFIX_SCOPE: dict[str, BookmarkScope] = {
    "agc1:": "content",
    "agr1:": "record",
    "agt1:": "thread",
}


class BookmarkError(Exception):
    """Base class for path-free bookmark failures."""


class BookmarkValidationError(BookmarkError, ValueError):
    """Raised when a bookmark value violates the public schema."""


class BookmarkFormatError(BookmarkError):
    """Raised when persisted bookmark data cannot be trusted."""


class BookmarkCapacityError(BookmarkError):
    """Raised when adding an entry would exceed the configured capacity."""


@dataclasses.dataclass(frozen=True, slots=True)
class BookmarkEntry:
    """One persisted canonical bookmark."""

    target_id: str
    scope: BookmarkScope
    content_id: str | None
    created_at: str

    def __post_init__(self) -> None:
        """Validate the complete immutable entry."""
        _validate_entry(self)


@dataclasses.dataclass(frozen=True, slots=True)
class BookmarkMutation:
    """Result of one idempotent bookmark mutation."""

    action: BookmarkMutationAction
    entry: BookmarkEntry | None


def _default_path() -> pathlib.Path:
    """Return the XDG bookmark snapshot path."""
    data_home = os.environ.get("XDG_DATA_HOME")
    root = pathlib.Path(data_home) if data_home else pathlib.Path.home() / ".local" / "share"
    return root / "agentgrep" / "bookmarks.json"


def _created_at_now() -> str:
    """Return the current UTC time in a stable RFC 3339 form."""
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _scope_for_target(target_id: object) -> BookmarkScope:
    """Validate ``target_id`` and return the scope encoded by its prefix."""
    if not isinstance(target_id, str) or _ID_PATTERN.fullmatch(target_id) is None:
        msg = "bookmark target must be a complete canonical agc1:, agr1:, or agt1: ID"
        raise BookmarkValidationError(msg)
    scope = _PREFIX_SCOPE.get(target_id[:5])
    if scope is None:  # pragma: no cover - the full-match check fixes the prefix set
        msg = "bookmark target has an unsupported canonical ID prefix"
        raise BookmarkValidationError(msg)
    return scope


def _validate_content_id(content_id: object) -> str:
    """Validate and return an exact content ID."""
    if (
        not isinstance(content_id, str)
        or _ID_PATTERN.fullmatch(content_id) is None
        or not content_id.startswith("agc1:")
    ):
        msg = "record bookmarks require a complete agc1: content ID"
        raise BookmarkValidationError(msg)
    return content_id


def _validate_created_at(created_at: object) -> str:
    """Validate and return one timezone-qualified RFC 3339 timestamp."""
    if not isinstance(created_at, str) or _CREATED_AT_PATTERN.fullmatch(created_at) is None:
        msg = "bookmark creation time must be a timezone-qualified RFC 3339 timestamp"
        raise BookmarkValidationError(msg)
    try:
        parsed = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        msg = "bookmark creation time must be a valid RFC 3339 timestamp"
        raise BookmarkValidationError(msg) from exc
    if parsed.utcoffset() is None:  # pragma: no cover - the pattern requires an offset
        msg = "bookmark creation time must include a timezone"
        raise BookmarkValidationError(msg)
    return created_at


def _validate_entry(entry: BookmarkEntry) -> None:
    """Validate target, scope, content validation ID, and creation time."""
    inferred_scope = _scope_for_target(entry.target_id)
    if entry.scope not in _PREFIX_SCOPE.values() or entry.scope != inferred_scope:
        msg = "bookmark scope does not match the target ID prefix"
        raise BookmarkValidationError(msg)
    if entry.scope == "record":
        _validate_content_id(entry.content_id)
    elif entry.content_id is not None:
        msg = "only record bookmarks accept a content validation ID"
        raise BookmarkValidationError(msg)
    _validate_created_at(entry.created_at)


def _entry_payload(entry: BookmarkEntry) -> dict[str, object]:
    """Return the persisted mapping for one entry."""
    return {
        "target_id": entry.target_id,
        "scope": entry.scope,
        "content_id": entry.content_id,
        "created_at": entry.created_at,
    }


def _snapshot_bytes(entries: t.Sequence[BookmarkEntry]) -> bytes:
    """Encode one canonical compact bookmark snapshot."""
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "entries": [_entry_payload(entry) for entry in entries],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8", "surrogatepass")
    return encoded + b"\n"


def _entry_from_payload(value: object) -> BookmarkEntry:
    """Validate and decode one persisted entry mapping."""
    if not isinstance(value, dict) or set(value) != {
        "content_id",
        "created_at",
        "scope",
        "target_id",
    }:
        msg = "bookmark data is corrupt"
        raise BookmarkFormatError(msg)
    entry_payload = t.cast("dict[str, object]", value)
    try:
        return BookmarkEntry(
            target_id=t.cast("str", entry_payload["target_id"]),
            scope=t.cast("BookmarkScope", entry_payload["scope"]),
            content_id=t.cast("str | None", entry_payload["content_id"]),
            created_at=t.cast("str", entry_payload["created_at"]),
        )
    except BookmarkValidationError as exc:
        msg = "bookmark data is corrupt"
        raise BookmarkFormatError(msg) from exc


def _decode_snapshot(raw: bytes, *, capacity: int) -> list[BookmarkEntry]:
    """Decode and validate an entire snapshot before returning any entry."""
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        msg = "bookmark data is corrupt"
        raise BookmarkFormatError(msg) from exc
    if not isinstance(payload, dict) or set(payload) != {"entries", "schema_version"}:
        msg = "bookmark data is corrupt"
        raise BookmarkFormatError(msg)
    schema_version = payload["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != _SCHEMA_VERSION
    ):
        msg = "bookmark schema is not supported"
        raise BookmarkFormatError(msg)
    raw_entries = payload["entries"]
    if not isinstance(raw_entries, list) or len(raw_entries) > capacity:
        msg = "bookmark data is corrupt"
        raise BookmarkFormatError(msg)
    entries = [_entry_from_payload(value) for value in raw_entries]
    if len({entry.target_id for entry in entries}) != len(entries):
        msg = "bookmark data is corrupt"
        raise BookmarkFormatError(msg)
    return entries


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte to ``fd``, including after short writes."""
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            msg = "bookmark snapshot write made no progress"
            raise OSError(msg)
        remaining = remaining[written:]


def _read_all(fd: int) -> bytes:
    """Read every byte from an open snapshot descriptor."""
    data = bytearray()
    while chunk := os.read(fd, 64 * 1024):
        data.extend(chunk)
    return bytes(data)


def _call_without_storage_details[**P, R](
    operation: t.Callable[P, R],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """Translate filesystem failures without retaining sensitive details."""
    try:
        result = operation(*args, **kwargs)
    except OSError:
        pass
    else:
        return result
    msg = "bookmark storage operation failed"
    raise BookmarkError(msg)


def _fsync_directory(path: pathlib.Path) -> None:
    """Synchronize directory metadata after atomic replacement."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _exclusive_lock(path: pathlib.Path) -> t.Iterator[None]:
    """Hold one private sidecar lock for a complete store operation."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class BookmarkStore:
    """Bounded transactional storage for canonical bookmarks.

    Parameters
    ----------
    path
        Snapshot path. ``None`` selects the XDG data directory.
    capacity
        Maximum number of entries. The default is 200.
    """

    __slots__ = ("_owns_directory", "capacity", "path")

    def __init__(
        self,
        path: pathlib.Path | None = None,
        *,
        capacity: int = _DEFAULT_CAPACITY,
    ) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            msg = "bookmark capacity must be a positive integer"
            raise ValueError(msg)
        self._owns_directory = path is None
        self.path = path if path is not None else _default_path()
        self.capacity = capacity

    @property
    def _lock_path(self) -> pathlib.Path:
        """Return the stable sidecar lock path."""
        return self.path.parent / "bookmarks.lock"

    def _ensure_directory(self) -> None:
        """Create the private application directory when needed."""
        if self._owns_directory:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.path.parent.chmod(0o700)
            return
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:
            return
        self.path.parent.chmod(0o700)

    def _read_unlocked(self) -> builtins.list[BookmarkEntry]:
        """Read and validate the snapshot while the caller holds the lock."""
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return []
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                msg = "bookmark snapshot is not a regular file"
                raise IsADirectoryError(msg)
            os.fchmod(fd, 0o600)
            raw = _read_all(fd)
        finally:
            os.close(fd)
        return _decode_snapshot(raw, capacity=self.capacity)

    def _write_unlocked(self, entries: t.Sequence[BookmarkEntry]) -> None:
        """Durably replace the snapshot while the caller holds the lock."""
        data = _snapshot_bytes(entries)
        fd, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=".bookmarks-",
            suffix=".tmp",
        )
        temporary_path = pathlib.Path(temporary_name)
        fd_open = True
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, data)
            os.fsync(fd)
            os.close(fd)
            fd_open = False
            temporary_path.replace(self.path)
            _fsync_directory(self.path.parent)
        finally:
            if fd_open:
                os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                temporary_path.unlink()

    def _list(self) -> builtins.list[BookmarkEntry]:
        """List entries without translating filesystem failures."""
        self._ensure_directory()
        with _exclusive_lock(self._lock_path):
            return self._read_unlocked()

    def list(self) -> builtins.list[BookmarkEntry]:
        """Return a validated copy of the stored entries."""
        return _call_without_storage_details(self._list)

    def _add(self, entry: BookmarkEntry) -> BookmarkMutation:
        """Add a validated entry without translating filesystem failures."""
        self._ensure_directory()
        with _exclusive_lock(self._lock_path):
            entries = self._read_unlocked()
            existing = next(
                (item for item in entries if item.target_id == entry.target_id),
                None,
            )
            if existing is not None:
                if existing.content_id != entry.content_id:
                    msg = "bookmark target already has a different content validation ID"
                    raise BookmarkValidationError(msg)
                return BookmarkMutation("unchanged", existing)
            if len(entries) >= self.capacity:
                msg = f"bookmark capacity reached ({self.capacity} entries)"
                raise BookmarkCapacityError(msg)
            entries.append(entry)
            self._write_unlocked(entries)
            return BookmarkMutation("added", entry)

    def add(
        self,
        target_id: str,
        *,
        content_id: str | None = None,
        created_at: str | None = None,
    ) -> BookmarkMutation:
        """Add one bookmark idempotently.

        Parameters
        ----------
        target_id
            Complete canonical target ID.
        content_id
            Required ``agc1:`` validation ID for an ``agr1:`` target.
        created_at
            Creation time to persist. ``None`` uses the current UTC time.

        Returns
        -------
        BookmarkMutation
            ``added`` for a new target or ``unchanged`` for an existing one.
        """
        scope = _scope_for_target(target_id)
        entry = BookmarkEntry(
            target_id=target_id,
            scope=scope,
            content_id=content_id,
            created_at=created_at if created_at is not None else _created_at_now(),
        )
        return _call_without_storage_details(self._add, entry)

    def _remove(self, target_id: str) -> BookmarkMutation:
        """Remove a validated target without translating filesystem failures."""
        self._ensure_directory()
        with _exclusive_lock(self._lock_path):
            entries = self._read_unlocked()
            existing = next((item for item in entries if item.target_id == target_id), None)
            if existing is not None:
                remaining = [item for item in entries if item.target_id != target_id]
                self._write_unlocked(remaining)
                return BookmarkMutation("removed", existing)
            return BookmarkMutation("unchanged", None)

    def remove(self, target_id: str) -> BookmarkMutation:
        """Remove one bookmark idempotently."""
        _scope_for_target(target_id)
        return _call_without_storage_details(self._remove, target_id)

    def _toggle(self, entry: BookmarkEntry) -> BookmarkMutation:
        """Toggle a validated entry without translating filesystem failures."""
        self._ensure_directory()
        with _exclusive_lock(self._lock_path):
            entries = self._read_unlocked()
            existing = next(
                (item for item in entries if item.target_id == entry.target_id),
                None,
            )
            if existing is not None:
                remaining = [item for item in entries if item.target_id != entry.target_id]
                self._write_unlocked(remaining)
                return BookmarkMutation("removed", existing)
            if len(entries) >= self.capacity:
                msg = f"bookmark capacity reached ({self.capacity} entries)"
                raise BookmarkCapacityError(msg)
            entries.append(entry)
            self._write_unlocked(entries)
            return BookmarkMutation("added", entry)

    def toggle(self, entry: BookmarkEntry) -> BookmarkMutation:
        """Remove an existing target or add the supplied entry."""
        _validate_entry(entry)
        return _call_without_storage_details(self._toggle, entry)


def bookmark_entry_for_record(
    record: SearchRecord,
    *,
    scope: BookmarkScope = "record",
    created_at: str | None = None,
) -> BookmarkEntry:
    """Build one canonical bookmark from a normalized search record.

    Parameters
    ----------
    record
        Record whose prepared canonical identity supplies the target.
    scope
        Identity scope to bookmark.
    created_at
        Creation time to persist. ``None`` uses the current UTC time.

    Returns
    -------
    BookmarkEntry
        Validated immutable bookmark entry.
    """
    prepared = record_identity(record)
    timestamp = created_at if created_at is not None else _created_at_now()
    if scope == "record":
        if prepared.record_id is None:
            msg = "record has no canonical record identity"
            raise BookmarkValidationError(msg)
        return BookmarkEntry(prepared.record_id, scope, prepared.content_id, timestamp)
    if scope == "thread":
        if prepared.thread_id is None:
            msg = "record has no canonical thread identity"
            raise BookmarkValidationError(msg)
        return BookmarkEntry(prepared.thread_id, scope, None, timestamp)
    if scope == "content":
        return BookmarkEntry(prepared.content_id, scope, None, timestamp)
    msg = "bookmark scope must be record, thread, or content"
    raise BookmarkValidationError(msg)
