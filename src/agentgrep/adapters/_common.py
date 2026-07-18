"""Shared private helpers used across the per-agent parser modules.

Origin construction, path/remote heuristics, and unix-timestamp
conversion shared by two or more agent families.
"""

from __future__ import annotations

import datetime
import re

from agentgrep.origin import (
    is_path_like_text,
)
from agentgrep.readers import (
    as_optional_str,
)
from agentgrep.records import (
    RecordOrigin,
    SourceHandle,
)


def _record_origin(
    *,
    cwd: str | None = None,
    repo: str | None = None,
    worktree: str | None = None,
    branch: str | None = None,
    remote: str | None = None,
    cwd_hash: str | None = None,
    fallback: RecordOrigin | None = None,
) -> RecordOrigin | None:
    """Build a non-empty origin, inheriting omitted values from ``fallback``."""
    origin = RecordOrigin(
        cwd=cwd or (fallback.cwd if fallback is not None else None),
        repo=repo or (fallback.repo if fallback is not None else None),
        worktree=worktree or (fallback.worktree if fallback is not None else None),
        branch=branch or (fallback.branch if fallback is not None else None),
        remote=remote or (fallback.remote if fallback is not None else None),
        cwd_hash=cwd_hash or (fallback.cwd_hash if fallback is not None else None),
    )
    return None if origin.is_empty() else origin


_SCP_REMOTE_RE = re.compile(r"^[^@/\s:]+@[^:/\s]+:.+")


def _remote_like_str(text: str) -> bool:
    return "://" in text or _SCP_REMOTE_RE.match(text) is not None


def _path_like_str(value: object) -> str | None:
    """Accept a mapping value as an origin path only when it looks like one.

    Store blobs reuse key names like ``workspace`` or ``branch`` for
    non-filesystem values (workspace UUIDs, UI state); a bare token
    without a separator or home prefix must not become an origin path.
    """
    text = as_optional_str(value)
    if text is None or not is_path_like_text(text) or _remote_like_str(text):
        return None
    return text


def _unix_to_isoformat(value: object) -> str | None:
    """Convert a unix-seconds integer to an ISO-8601 UTC timestamp.

    Examples
    --------
    >>> _unix_to_isoformat(1700000000)
    '2023-11-14T22:13:20Z'
    >>> _unix_to_isoformat(0) is None
    True
    >>> _unix_to_isoformat(float("nan")) is None
    True
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return (
            datetime.datetime.fromtimestamp(value, tz=datetime.UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except ValueError, OSError, OverflowError:
        return None


def _unix_millis_to_isoformat(value: object) -> str | None:
    """Convert a unix-milliseconds timestamp to ISO-8601 UTC.

    Examples
    --------
    >>> _unix_millis_to_isoformat(1700000000000)
    '2023-11-14T22:13:20Z'
    >>> _unix_millis_to_isoformat(0) is None
    True
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return (
            datetime.datetime.fromtimestamp(value / 1000, tz=datetime.UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except ValueError, OSError, OverflowError:
        return None


def _discovered_origin(source: SourceHandle) -> RecordOrigin | None:
    """Return the origin discovery already recovered for one source.

    Some stores keep the working directory outside the file the adapter opens —
    in a sibling ``workspace.json``, or in a directory name that has to be
    decoded against the filesystem. Discovery resolves those and parks the
    result on :attr:`~agentgrep.records.SourceHandle.origin_summary`; reading it
    back here is what puts the value on the records, without a second file read
    or a second decode.
    """
    summary = source.origin_summary
    if summary is None or not summary.origins:
        return None
    return summary.origins[0]
