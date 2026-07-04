"""Persistent, user-curated bookmarks of matched records.

agentgrep is read-only over the agent stores it searches; like
:mod:`agentgrep.ui._history` this module is its self-written state — a small
JSONL file of records the user pinned by their :mod:`agentgrep.identity`
content id, kept under the XDG data dir. It never reads or writes any searched
store, so the read-only-over-stores contract holds.

The layer is deliberately stdlib-only (Textual-free, pydantic-free) so it stays
unit-testable offline and drives the headless CLI, the MCP tools, and the TUI
from one place. The on-disk format is one JSON object per line.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import pathlib
import tempfile
import time
import typing as t

from agentgrep.identity import resolve_short_prefix, short_id

__all__ = [
    "BookmarkEntry",
    "PrefixResolution",
    "add_bookmark",
    "bookmarks_disabled",
    "bookmarks_path",
    "find_by_prefix",
    "load_bookmarks",
    "remove_bookmark",
]


@dataclasses.dataclass(frozen=True, slots=True)
class BookmarkEntry:
    """One pinned record, keyed by its stable :mod:`agentgrep.identity` id."""

    id: str
    agent: str
    store: str
    adapter_id: str
    path: str
    kind: str = "record"
    title: str | None = None
    timestamp: str | None = None
    session: str | None = None
    snippet: str = ""
    note: str = ""
    tags: tuple[str, ...] = ()
    created_at: float = 0.0

    @property
    def short(self) -> str:
        """Return the git-style short handle for this bookmark's content id."""
        return short_id(self.id)


class PrefixResolution(t.NamedTuple):
    """The outcome of resolving a short-id prefix against a bookmark set."""

    entry: BookmarkEntry | None
    ambiguous: tuple[BookmarkEntry, ...]


def bookmarks_disabled() -> bool:
    """Return whether ``AGENTGREP_NO_BOOKMARKS`` opts the user out.

    Truthy for any value other than the empty string and the usual falsey
    spellings, so ``AGENTGREP_NO_BOOKMARKS=1`` disables both read and write.
    """
    return os.environ.get("AGENTGREP_NO_BOOKMARKS", "") not in (
        "",
        "0",
        "false",
        "False",
        "no",
        "No",
    )


def bookmarks_path(home: pathlib.Path) -> pathlib.Path:
    """Resolve the bookmarks file under the XDG data dir.

    ``$XDG_DATA_HOME/agentgrep/bookmarks.jsonl`` when the env is set, else
    ``<home>/.local/share/agentgrep/bookmarks.jsonl``. Bookmarks are curated,
    durable user data (XDG *data*), distinct from the ephemeral search-input
    history kept under XDG *state*.
    """
    override = os.environ.get("XDG_DATA_HOME")
    root = pathlib.Path(override) if override else home / ".local" / "share"
    return root / "agentgrep" / "bookmarks.jsonl"


def _entry_to_row(entry: BookmarkEntry) -> dict[str, object]:
    """Serialize a bookmark to its JSON row."""
    return {
        "id": entry.id,
        "agent": entry.agent,
        "store": entry.store,
        "adapter_id": entry.adapter_id,
        "path": entry.path,
        "kind": entry.kind,
        "title": entry.title,
        "timestamp": entry.timestamp,
        "session": entry.session,
        "snippet": entry.snippet,
        "note": entry.note,
        "tags": list(entry.tags),
        "created_at": entry.created_at,
    }


def _opt_str(obj: dict[str, object], key: str) -> str | None:
    value = obj.get(key)
    return value if isinstance(value, str) else None


def _row_to_entry(obj: object) -> BookmarkEntry | None:
    """Parse one JSONL row into a :class:`BookmarkEntry`, or ``None`` if invalid."""
    if not isinstance(obj, dict):
        return None
    data = t.cast("dict[str, object]", obj)
    id_ = data.get("id")
    adapter_id = data.get("adapter_id")
    if not isinstance(id_, str) or not id_ or not isinstance(adapter_id, str) or not adapter_id:
        return None
    raw_tags = data.get("tags", [])
    tags = tuple(x for x in raw_tags if isinstance(x, str)) if isinstance(raw_tags, list) else ()
    raw_created = data.get("created_at", 0)
    created_at = (
        float(raw_created)
        if isinstance(raw_created, (int, float)) and not isinstance(raw_created, bool)
        else 0.0
    )
    return BookmarkEntry(
        id=id_,
        agent=_opt_str(data, "agent") or "",
        store=_opt_str(data, "store") or "",
        adapter_id=adapter_id,
        path=_opt_str(data, "path") or "",
        kind=_opt_str(data, "kind") or "record",
        title=_opt_str(data, "title"),
        timestamp=_opt_str(data, "timestamp"),
        session=_opt_str(data, "session"),
        snippet=_opt_str(data, "snippet") or "",
        note=_opt_str(data, "note") or "",
        tags=tags,
        created_at=created_at,
    )


def load_bookmarks(path: pathlib.Path, *, limit: int | None = None) -> list[BookmarkEntry]:
    """Load bookmarks newest-first, deduplicated by id, tolerant of bad lines.

    A missing file yields ``[]``; a corrupt or foreign line is skipped rather
    than fatal. When an id appears twice (a re-add) the newest row wins.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    by_id: dict[str, BookmarkEntry] = {}
    for line in raw:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except ValueError:
            continue
        entry = _row_to_entry(obj)
        if entry is not None:
            by_id[entry.id] = entry
    result = sorted(by_id.values(), key=lambda entry: (entry.created_at, entry.id), reverse=True)
    if limit is not None:
        result = result[:limit]
    return result


def add_bookmark(path: pathlib.Path, entry: BookmarkEntry, *, now: float | None = None) -> bool:
    """Append ``entry`` unless its id is already bookmarked; return if it was added.

    The file is created private (``0o600`` — it names the user's own records)
    and every filesystem error is swallowed so a read-only home never breaks a
    search session.
    """
    if entry.id in {existing.id for existing in load_bookmarks(path)}:
        return False
    stamped = (
        entry
        if entry.created_at
        else dataclasses.replace(entry, created_at=time.time() if now is None else now)
    )
    line = json.dumps(_entry_to_row(stamped), ensure_ascii=False)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            path.parent.chmod(0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


def find_by_prefix(entries: t.Sequence[BookmarkEntry], prefix: str) -> PrefixResolution:
    """Resolve a short-id ``prefix`` against ``entries``, git-style.

    Examples
    --------
    >>> entry = BookmarkEntry(id="a" * 64, agent="codex", store="s", adapter_id="x", path="~/x")
    >>> find_by_prefix([entry], entry.short[:4]).entry is entry
    True
    >>> find_by_prefix([entry], "zzzz").entry is None
    True
    """
    by_short: dict[str, BookmarkEntry] = {}
    for entry in entries:
        by_short.setdefault(entry.short, entry)
    resolution = resolve_short_prefix(prefix, by_short.keys())
    if resolution.status == "unique" and resolution.match is not None:
        return PrefixResolution(by_short[resolution.match], ())
    if resolution.status == "ambiguous":
        return PrefixResolution(
            None, tuple(by_short[candidate] for candidate in resolution.candidates)
        )
    return PrefixResolution(None, ())


def _rewrite(path: pathlib.Path, entries: t.Sequence[BookmarkEntry]) -> None:
    """Atomically rewrite ``path`` to ``entries`` (best effort, never raises)."""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".bookmarks-", suffix=".tmp")
    except OSError:
        return
    tmp_path = pathlib.Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(_entry_to_row(entry), ensure_ascii=False) + "\n")
        tmp_path.replace(path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp_path.unlink()


def remove_bookmark(path: pathlib.Path, id_prefix: str) -> BookmarkEntry | None:
    """Remove the bookmark uniquely matching ``id_prefix``; return it or ``None``.

    Returns ``None`` when the prefix matches nothing or is ambiguous, leaving
    the file untouched in both cases.
    """
    entries = load_bookmarks(path)
    match = find_by_prefix(entries, id_prefix)
    if match.entry is None:
        return None
    _rewrite(path, [entry for entry in entries if entry.id != match.entry.id])
    return match.entry
