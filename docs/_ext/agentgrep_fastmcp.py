"""Docs-only FastMCP registration shim for agentgrep tools.

The runtime server registers its tools inside ``build_mcp_server()`` so the
live FastMCP instance stays self-contained. The Sphinx FastMCP tool collector
documents module-level ``register(server)`` hooks, so this module mirrors the
public tool signatures without changing runtime behavior.
"""

from __future__ import annotations

import types
import typing as t

from pydantic import Field

from agentgrep.mcp import (
    AgentSelector,
    FindToolResponse,
    SearchToolResponse,
    SearchTypeName,
)

READONLY_TAGS = {"readonly", "agentgrep"}
DOCS_ONLY_MESSAGE = "Documentation signature only."


async def search(
    terms: t.Annotated[
        list[str],
        Field(
            min_length=1,
            description="One or more literal or regex search terms.",
        ),
    ],
    agent: t.Annotated[
        AgentSelector,
        Field(description="Limit search to one agent or search all agents."),
    ] = "all",
    search_type: t.Annotated[
        SearchTypeName,
        Field(description="Search prompts, history, or both."),
    ] = "prompts",
    any_term: t.Annotated[
        bool,
        Field(description="Match any term instead of requiring all terms."),
    ] = False,
    regex: t.Annotated[
        bool,
        Field(description="Treat search terms as regular expressions."),
    ] = False,
    case_sensitive: t.Annotated[
        bool,
        Field(description="Perform case-sensitive matching."),
    ] = False,
    limit: t.Annotated[
        int | None,
        Field(
            default=20,
            ge=1,
            description="Maximum number of search results to return.",
        ),
    ] = 20,
) -> SearchToolResponse:
    """Search normalized prompts or history across local agent stores."""
    raise NotImplementedError(DOCS_ONLY_MESSAGE)


t.cast(t.Any, search).__fastmcp__ = types.SimpleNamespace(
    name="search",
    title="Search",
    tags=READONLY_TAGS | {"search"},
    annotations=None,
)


async def find(
    pattern: t.Annotated[
        str | None,
        Field(
            default=None,
            description="Optional substring filter against discovered paths and adapters.",
        ),
    ] = None,
    agent: t.Annotated[
        AgentSelector,
        Field(description="Limit discovery to one agent or search all agents."),
    ] = "all",
    limit: t.Annotated[
        int | None,
        Field(
            default=50,
            ge=1,
            description="Maximum number of discovered sources to return.",
        ),
    ] = 50,
) -> FindToolResponse:
    """Find known agent stores, session files, and SQLite databases."""
    raise NotImplementedError(DOCS_ONLY_MESSAGE)


t.cast(t.Any, find).__fastmcp__ = types.SimpleNamespace(
    name="find",
    title="Find",
    tags=READONLY_TAGS | {"discovery"},
    annotations=None,
)
