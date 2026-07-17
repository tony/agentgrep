"""Persistent search-input history (the top search box only).

agentgrep is read-only over the agent *stores* it searches; this module is its
one piece of self-written state — a small JSONL file of the queries the user
typed into the search box, kept under the XDG state dir. It never reads or
writes any searched store, so the "read-only over stores" contract holds.

The layer is deliberately Textual-free (only the stdlib) so it stays
unit-testable offline. The on-disk format is one JSON object per line::

    {"text": "agent:codex refactor", "ts": 1750000000, "scope": "prompts"}

``scope`` is recorded but unused today; it reserves the field for a future
scope filter so adding it needs no format migration.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import pathlib
import tempfile
import time
import typing as t

__all__ = [
    "DISK_CAP",
    "DISPLAY_LIMIT",
    "QUERY_TEXT_MAX_CHARS",
    "HistoryEntry",
    "append_query",
    "history_disabled",
    "history_path",
    "load_history",
]

DISPLAY_LIMIT = 200
"""Maximum number of (deduplicated) rows the recall modal shows."""

QUERY_TEXT_MAX_CHARS = 4096
"""Maximum query characters admitted to persistence and recall."""

DISK_CAP = 1000
"""Soft ceiling: a load that finds more raw lines rewrites to the last N."""


class HistoryEntry(t.NamedTuple):
    """One recalled search query: the raw text, its unix ts, and launch scope."""

    text: str
    ts: float
    scope: str = ""


def history_disabled() -> bool:
    """Return whether ``AGENTGREP_NO_HISTORY`` opts the user out of history.

    Truthy for any value other than the empty string and the usual falsey
    spellings, so ``AGENTGREP_NO_HISTORY=1`` (or ``=true``) disables both the
    read and the write paths.
    """
    return os.environ.get("AGENTGREP_NO_HISTORY", "") not in ("", "0", "false", "False", "no", "No")


def history_path(home: pathlib.Path) -> pathlib.Path:
    """Resolve the history file path under the XDG state dir.

    Mirrors the stdlib history-file convention (an env override first, then a
    home fallback): ``$XDG_STATE_HOME/agentgrep/history.jsonl`` when the env is
    set, else ``<home>/.local/state/agentgrep/history.jsonl``.
    """
    override = os.environ.get("XDG_STATE_HOME")
    root = pathlib.Path(override) if override else home / ".local" / "state"
    return root / "agentgrep" / "history.jsonl"


def append_query(
    path: pathlib.Path,
    text: str,
    *,
    scope: str = "",
    now: float | None = None,
    dedup_last: str = "",
) -> bool:
    """Append one query to the history file; return whether it was recorded.

    Skips empty/whitespace-only text and a consecutive duplicate of the last
    recorded query (``dedup_last``). The file is created private (``0o600`` —
    it holds the user's typed queries) and all filesystem errors are swallowed
    so a read-only or unwritable home never breaks the session.
    """
    stripped = text.strip()[:QUERY_TEXT_MAX_CHARS]
    bounded_last = dedup_last.strip()[:QUERY_TEXT_MAX_CHARS]
    if not stripped or stripped == bounded_last:
        return False
    line = json.dumps(
        {"text": stripped, "ts": time.time() if now is None else now, "scope": scope},
        ensure_ascii=False,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            path.parent.chmod(0o700)
        # ``os.open`` with mode 0o600 sets the file private at creation (an
        # existing file keeps its mode); O_APPEND keeps concurrent appends sane.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


def load_history(path: pathlib.Path, *, limit: int = DISPLAY_LIMIT) -> list[HistoryEntry]:
    """Load history newest-first, deduplicated by text, capped to ``limit``.

    Tolerant of a missing file and of corrupt/foreign lines (each is skipped,
    never fatal). When the file has grown past :data:`DISK_CAP` raw lines it is
    atomically rewritten down to the most recent ``DISK_CAP`` before parsing.
    Deduplication keeps the newest occurrence of each distinct query.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    lines = [line for line in raw if line.strip()]
    if len(lines) > DISK_CAP:
        lines = lines[-DISK_CAP:]
        _trim(path, lines)
    parsed: list[HistoryEntry] = []
    for line in lines:
        entry = _parse_line(line)
        if entry is not None:
            parsed.append(entry)
    seen: set[str] = set()
    result: list[HistoryEntry] = []
    # Order by submit-time ts, not physical write order: concurrent history
    # workers (exclusive=False) can append out of order. Ties (equal or legacy
    # whole-second ts) fall back to file order with the later line newest,
    # matching the previous reversed() behaviour.
    order = sorted(range(len(parsed)), key=lambda i: (parsed[i].ts, i), reverse=True)
    for i in order:
        entry = parsed[i]
        if entry.text in seen:
            continue
        seen.add(entry.text)
        result.append(entry)
        if len(result) >= limit:
            break
    return result


def _parse_line(line: str) -> HistoryEntry | None:
    """Parse one JSONL row into a :class:`HistoryEntry`, or ``None`` if invalid."""
    try:
        obj = json.loads(line)
    except ValueError, TypeError:
        return None
    if not isinstance(obj, dict):
        return None
    text = obj.get("text")
    if not isinstance(text, str) or not text:
        return None
    text = text[:QUERY_TEXT_MAX_CHARS]
    raw_ts = obj.get("ts", 0)
    ts = 0.0
    if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool):
        try:
            ts = float(raw_ts)
        except ValueError, OverflowError:
            ts = 0.0
        if not math.isfinite(ts):
            # NaN/±inf sort pathologically against real timestamps; drop to 0.
            ts = 0.0
    raw_scope = obj.get("scope", "")
    scope = raw_scope if isinstance(raw_scope, str) else ""
    return HistoryEntry(text=text, ts=ts, scope=scope)


def _trim(path: pathlib.Path, keep_lines: list[str]) -> None:
    """Atomically rewrite ``path`` to ``keep_lines`` (best effort, never raises)."""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".history-", suffix=".tmp")
    except OSError:
        return
    tmp_path = pathlib.Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(keep_lines) + "\n")
        tmp_path.replace(path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
