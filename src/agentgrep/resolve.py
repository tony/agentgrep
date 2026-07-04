"""Re-resolve a bookmarked record back to a live store record.

The one shared re-resolver behind ``bookmark show`` (and the deferred MCP/TUI
surfaces). agentgrep is read-only over the stores; this only *reads* the one
source a bookmark points at and matches by the stable content id, so a store
that is rewritten in place (advancing only its mtime) still resolves — the
content id excludes the mtime-derived timestamp.
"""

from __future__ import annotations

import typing as t

from agentgrep._text import format_display_path
from agentgrep.identity import record_content_id
from agentgrep.records import AGENT_CHOICES

if t.TYPE_CHECKING:
    import pathlib

    from agentgrep.records import AgentName, SearchRecord

__all__ = ["resolve_bookmarked_record"]


def resolve_bookmarked_record(
    home: pathlib.Path,
    *,
    agent: str,
    adapter_id: str,
    path: str,
    content_id: str,
) -> SearchRecord | None:
    """Return the live record a bookmark points at, or ``None`` if it is gone.

    Discovers the single source identified by ``agent`` + ``adapter_id`` +
    display ``path``, then returns the first record in it whose content id
    matches. Because the id is mtime-immune, this survives an in-place store
    rewrite.

    Parameters
    ----------
    home : pathlib.Path
        The user home whose stores are searched.
    agent : str
        The bookmark's agent tag; an unknown value resolves to ``None``.
    adapter_id : str
        The adapter that produced the record.
    path : str
        The home-collapsed display path of the source.
    content_id : str
        The stable content id to match within that one source.

    Returns
    -------
    SearchRecord or None
        The live record, or ``None`` when the source or record is gone.
    """
    if agent not in AGENT_CHOICES:
        return None

    from agentgrep.adapters import iter_source_records
    from agentgrep.discovery import discover_sources
    from agentgrep.readers import select_backends

    sources = discover_sources(
        home,
        t.cast("tuple[AgentName, ...]", (agent,)),
        select_backends(),
        include_non_default=True,
        version_detail="none",
    )
    for source in sources:
        if source.adapter_id != adapter_id or format_display_path(source.path) != path:
            continue
        for record in iter_source_records(source):
            if record_content_id(record) == content_id:
                return record
    return None
