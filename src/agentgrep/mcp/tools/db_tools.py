"""Read-only MCP tools for the persistent DB index."""

from __future__ import annotations

import asyncio
import pathlib
import sqlite3
import typing as t

from pydantic import Field

from agentgrep.mcp._library import READONLY_TAGS
from agentgrep.mcp.models import DbStatusModel

if t.TYPE_CHECKING:
    from fastmcp import FastMCP


def _selected_db_path(db_path: str | None) -> pathlib.Path:
    """Return the selected agentgrep db path without creating it."""
    from agentgrep.db import default_db_path

    if db_path is not None:
        return pathlib.Path(db_path).expanduser()
    return default_db_path()


def _db_status_sync(db_path: str | None) -> DbStatusModel:
    """Return DB status, using zero counts when no DB exists."""
    from agentgrep.db import DbRuntime

    path = _selected_db_path(db_path)
    if not path.exists():
        return DbStatusModel(
            db_path=str(path),
            db_schema_version=0,
            sources=0,
            records=0,
        )
    try:
        with DbRuntime.open_readonly(path) as runtime:
            status = runtime.status()
    except sqlite3.DatabaseError:
        return DbStatusModel(
            db_path=str(path),
            db_schema_version=0,
            sources=0,
            records=0,
        )
    return DbStatusModel(
        db_path=str(status.db_path),
        db_schema_version=status.schema_version,
        sources=status.sources,
        records=status.records,
    )


def register(mcp: FastMCP) -> None:
    """Register read-only DB index tools."""

    @mcp.tool(
        name="db_status",
        tags=READONLY_TAGS | {"db"},
        description="Return row counts for the persistent DB index.",
    )
    async def db_status_tool(
        db_path: t.Annotated[
            str | None,
            Field(default=None, description="Optional agentgrep db path."),
        ] = None,
    ) -> DbStatusModel:
        return await asyncio.to_thread(_db_status_sync, db_path)

    _ = db_status_tool
