"""Bookmark-domain MCP tools (read-only).

Read the records the user pinned in agentgrep's own bookmark store and
re-resolve one back to its live source record. Writing bookmarks over MCP is a
planned follow-up; these read-only tools keep the server's read-only posture.
"""

from __future__ import annotations

import asyncio
import pathlib
import typing as t

from pydantic import Field

from agentgrep.mcp._library import READONLY_TAGS, SearchRecordLike
from agentgrep.mcp.models import (
    BookmarkListResponse,
    BookmarkModel,
    BookmarkShowRequest,
    BookmarkShowResponse,
    SearchRecordModel,
)

if t.TYPE_CHECKING:
    from fastmcp import FastMCP

    from agentgrep.bookmarks import BookmarkEntry


def _to_model(entry: BookmarkEntry) -> BookmarkModel:
    """Adapt a stored bookmark to its MCP model."""
    return BookmarkModel(
        id=entry.short,
        content_id=entry.id,
        agent=entry.agent,
        store=entry.store,
        kind=entry.kind,
        title=entry.title,
        timestamp=entry.timestamp,
        session=entry.session,
        snippet=entry.snippet,
        note=entry.note,
        tags=list(entry.tags),
    )


def _bookmark_list_sync(limit: int | None) -> BookmarkListResponse:
    """Load the bookmark set newest-first (blocking)."""
    from agentgrep import bookmarks

    if bookmarks.bookmarks_disabled():
        return BookmarkListResponse(count=0, bookmarks=[])
    entries = bookmarks.load_bookmarks(
        bookmarks.bookmarks_path(pathlib.Path.home()),
        limit=limit,
    )
    return BookmarkListResponse(
        count=len(entries), bookmarks=[_to_model(entry) for entry in entries]
    )


def _bookmark_show_sync(request: BookmarkShowRequest) -> BookmarkShowResponse:
    """Resolve a bookmark by id prefix and re-read its live record (blocking)."""
    from agentgrep import bookmarks, resolve

    if bookmarks.bookmarks_disabled():
        return BookmarkShowResponse(error_message="bookmarks are disabled")
    home = pathlib.Path.home()
    match = bookmarks.find_by_prefix(
        bookmarks.load_bookmarks(bookmarks.bookmarks_path(home)),
        request.id_prefix,
    )
    if match.entry is None:
        if match.ambiguous:
            shorts = ", ".join(entry.short for entry in match.ambiguous)
            return BookmarkShowResponse(error_message=f"ambiguous id prefix; matches {shorts}")
        return BookmarkShowResponse(error_message="no bookmark matches that id prefix")
    entry = match.entry
    live = resolve.resolve_bookmarked_record(
        home,
        agent=entry.agent,
        adapter_id=entry.adapter_id,
        path=entry.path,
        content_id=entry.id,
    )
    resolved = (
        SearchRecordModel.from_record(t.cast("SearchRecordLike", live))
        if live is not None
        else None
    )
    return BookmarkShowResponse(bookmark=_to_model(entry), resolved=resolved)


def register(mcp: FastMCP) -> None:
    """Register read-only bookmark tools."""

    @mcp.tool(
        name="bookmark_list",
        tags=READONLY_TAGS | {"bookmark"},
        description="List the records pinned in agentgrep's bookmark store, newest-first.",
    )
    async def bookmark_list_tool(
        limit: t.Annotated[
            int | None,
            Field(default=None, ge=1, description="Maximum number of bookmarks to return."),
        ] = None,
    ) -> BookmarkListResponse:
        return await asyncio.to_thread(_bookmark_list_sync, limit)

    _ = bookmark_list_tool

    @mcp.tool(
        name="bookmark_show",
        tags=READONLY_TAGS | {"bookmark"},
        description="Resolve one bookmark by its id prefix and re-read the live record it pins.",
    )
    async def bookmark_show_tool(
        id_prefix: t.Annotated[
            str,
            Field(min_length=1, description="A bookmark id prefix (git-style unique prefix)."),
        ],
    ) -> BookmarkShowResponse:
        request = BookmarkShowRequest(id_prefix=id_prefix)
        return await asyncio.to_thread(_bookmark_show_sync, request)

    _ = bookmark_show_tool
