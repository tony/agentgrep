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

_CATALOG_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
)


def _catalog_uuid_path_token(source: SourceHandle) -> str | None:
    """Return a UUID only from the source family's documented path suffix."""
    path = source.path
    token: str | None = None
    match (source.agent, source.store, source.adapter_id):
        case (
            "cursor-cli",
            "cursor-cli.transcripts",
            "cursor_cli.transcripts_jsonl.v1",
        ) if (
            len(path.parents) >= 2
            and path.parents[1].name == "agent-transcripts"
            and path.name == f"{path.parent.name}.jsonl"
        ):
            token = path.parent.name
        case (
            "cursor-cli",
            "cursor-cli.chats",
            "cursor_cli.chats_protobuf.v1",
        ) if len(path.parents) >= 3 and path.parents[2].name == "chats" and path.name == "store.db":
            token = path.parent.name
        case (
            "antigravity-cli",
            "antigravity-cli.transcript",
            "antigravity_cli.transcript_jsonl.v1",
        ) if (
            len(path.parents) >= 4
            and path.parents[0].name == "logs"
            and path.parents[1].name == ".system_generated"
            and path.parents[3].name == "brain"
            and path.name == "transcript_full.jsonl"
        ):
            token = path.parents[2].name
        case (
            "antigravity-cli",
            "antigravity-cli.conversations",
            "antigravity_cli.conversations_sqlite_protobuf.v1",
        ) if path.parent.name == "conversations" and path.suffix == ".db":
            token = path.stem
        case (
            "antigravity-cli",
            "antigravity-cli.implicit",
            "antigravity_cli.implicit_protobuf.v1",
        ) if path.parent.name == "implicit" and path.suffix == ".pb":
            token = path.stem
        case (
            "antigravity-ide",
            "antigravity-ide.conversations",
            "antigravity_ide.conversations_protobuf.v1",
        ) if path.parent.name == "conversations" and path.suffix == ".pb":
            token = path.stem
        case (
            "antigravity-ide",
            "antigravity-ide.implicit",
            "antigravity_ide.implicit_protobuf.v1",
        ) if path.parent.name == "implicit" and path.suffix == ".pb":
            token = path.stem
    return token if token is not None and _CATALOG_UUID_RE.fullmatch(token) else None


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
