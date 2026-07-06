"""Serialization helpers for project-origin metadata."""

from __future__ import annotations

import pathlib
import re
import urllib.parse

from agentgrep._text import format_display_path
from agentgrep.origin import LEGACY_ORIGIN_METADATA_KEYS, is_path_like_text
from agentgrep.records import RecordOrigin, RecordOriginPayload

__all__ = [
    "serialize_record_metadata",
    "serialize_record_origin",
]

_LEGACY_ORIGIN_PATH_METADATA_KEYS = LEGACY_ORIGIN_METADATA_KEYS - frozenset(
    {"branch", "cwd_hash", "gitBranch", "project_hash", "projectHash"},
)


def serialize_record_origin(origin: RecordOrigin | None) -> RecordOriginPayload | None:
    """Serialize project-origin metadata with display-safe paths."""
    if origin is None or origin.is_empty():
        return None
    payload: RecordOriginPayload = {}
    if origin.cwd:
        payload["cwd"] = _display_path_text(origin.cwd)
    if origin.repo:
        payload["repo"] = _display_path_text(origin.repo)
    if origin.worktree:
        payload["worktree"] = _display_path_text(origin.worktree)
    if origin.branch:
        payload["branch"] = origin.branch
    if origin.remote:
        remote = _safe_remote_text(origin.remote)
        if remote:
            payload["remote"] = remote
    if origin.cwd_hash:
        payload["cwd_hash"] = origin.cwd_hash
    return payload or None


def serialize_record_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Return metadata with legacy path-like origin values redacted for display."""
    payload: dict[str, object] = {}
    for key, value in metadata.items():
        is_legacy_path = (
            key in _LEGACY_ORIGIN_PATH_METADATA_KEYS
            and isinstance(value, str)
            and is_path_like_text(value)
        )
        if is_legacy_path:
            payload[key] = _display_path_text(value)
        else:
            payload[key] = value
    return payload


def _display_path_text(value: str) -> str:
    return format_display_path(pathlib.Path(value), directory=True)


# @ is excluded from host and path so credential-shaped remotes fall
# through to the URL parse and are omitted instead of emitted.
_SCP_REMOTE_RE = re.compile(r"^[^@/\s:]+@(?P<host>[^:/\s@]+):(?P<path>[^@\s]+)$")
_SAFE_REMOTE_SCHEMES = frozenset({"git", "http", "https", "ssh"})


def _safe_remote_text(value: str) -> str | None:
    remote = value.strip()
    if not remote:
        return None
    scp_match = _SCP_REMOTE_RE.match(remote)
    if scp_match is not None:
        return f"ssh://{scp_match.group('host')}/{scp_match.group('path').lstrip('/')}"
    parsed = urllib.parse.urlsplit(remote)
    if parsed.scheme not in _SAFE_REMOTE_SCHEMES or not parsed.netloc:
        return None
    hostname = parsed.hostname
    if hostname is None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = _remote_netloc(hostname, port)
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _remote_netloc(hostname: str, port: int | None) -> str:
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if port is not None:
        return f"{hostname}:{port}"
    return hostname
