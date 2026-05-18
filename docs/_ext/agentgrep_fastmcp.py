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
from agentgrep.mcp.models import (
    DiscoverySummaryResponse,
    InspectSampleResponse,
    ListSourcesResponse,
    ListStoresResponse,
    RecentSessionsResponse,
    StoreDescriptorModel,
    ValidateQueryResponse,
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


async def list_stores(
    agent: t.Annotated[
        str,
        Field(
            default="all",
            description="Filter to one agent or 'all' for every catalog entry.",
            examples=["all", "claude", "cursor"],
        ),
    ] = "all",
    role_filter: t.Annotated[
        str | None,
        Field(
            default=None,
            description="Filter to one StoreRole value (e.g. 'primary_chat').",
            examples=["primary_chat", "prompt_history"],
        ),
    ] = None,
    search_default_only: t.Annotated[
        bool,
        Field(
            default=False,
            description="Return only stores that are searched by default.",
        ),
    ] = False,
) -> ListStoresResponse:
    """List on-disk agent stores from the agentgrep catalog."""
    raise NotImplementedError(DOCS_ONLY_MESSAGE)


t.cast(t.Any, list_stores).__fastmcp__ = types.SimpleNamespace(
    name="list_stores",
    title="List Stores",
    tags=READONLY_TAGS | {"catalog"},
    annotations=None,
)


async def get_store_descriptor(
    store_id: t.Annotated[
        str,
        Field(
            min_length=1,
            description="Store id (e.g. 'claude.projects.session').",
            examples=["claude.projects.session", "codex.history"],
        ),
    ],
) -> StoreDescriptorModel:
    """Return the catalog descriptor for a single store by id."""
    raise NotImplementedError(DOCS_ONLY_MESSAGE)


t.cast(t.Any, get_store_descriptor).__fastmcp__ = types.SimpleNamespace(
    name="get_store_descriptor",
    title="Get Store Descriptor",
    tags=READONLY_TAGS | {"catalog"},
    annotations=None,
)


async def inspect_record_sample(
    adapter_id: t.Annotated[
        str,
        Field(
            min_length=1,
            description="Adapter id (e.g. 'claude.projects_jsonl.v1').",
            examples=["claude.projects_jsonl.v1", "codex.history_json.v1"],
        ),
    ],
    source_path: t.Annotated[
        str,
        Field(
            min_length=1,
            description="Absolute path to the source file.",
        ),
    ],
    sample_size: t.Annotated[
        int,
        Field(
            default=1,
            ge=1,
            le=20,
            description="Number of records to return (1-20).",
        ),
    ] = 1,
) -> InspectSampleResponse:
    """Read the first N records from one adapter+path for schema inspection."""
    raise NotImplementedError(DOCS_ONLY_MESSAGE)


t.cast(t.Any, inspect_record_sample).__fastmcp__ = types.SimpleNamespace(
    name="inspect_record_sample",
    title="Inspect Record Sample",
    tags=READONLY_TAGS | {"catalog"},
    annotations=None,
)


async def list_sources(
    agent: t.Annotated[
        AgentSelector,
        Field(description="Limit discovery to one agent or scan every agent."),
    ] = "all",
    path_kind_filter: t.Annotated[
        t.Literal["history_file", "session_file", "sqlite_db"] | None,
        Field(default=None, description="Filter by path kind."),
    ] = None,
    source_kind_filter: t.Annotated[
        t.Literal["json", "jsonl", "sqlite"] | None,
        Field(default=None, description="Filter by on-disk source kind."),
    ] = None,
    limit: t.Annotated[
        int | None,
        Field(default=None, ge=1, description="Maximum number of sources to return."),
    ] = None,
) -> ListSourcesResponse:
    """List discovered sources with structured path-kind/source-kind filters."""
    raise NotImplementedError(DOCS_ONLY_MESSAGE)


t.cast(t.Any, list_sources).__fastmcp__ = types.SimpleNamespace(
    name="list_sources",
    title="List Sources",
    tags=READONLY_TAGS | {"discovery"},
    annotations=None,
)


async def filter_sources(
    pattern: t.Annotated[
        str,
        Field(
            min_length=1,
            description="Required substring pattern.",
            examples=["state", ".jsonl"],
        ),
    ],
    agent: t.Annotated[
        AgentSelector,
        Field(description="Limit discovery to one agent or scan every agent."),
    ] = "all",
    limit: t.Annotated[
        int | None,
        Field(default=50, ge=1, description="Maximum number of sources to return."),
    ] = 50,
) -> FindToolResponse:
    """Filter discovered sources by required substring pattern."""
    raise NotImplementedError(DOCS_ONLY_MESSAGE)


t.cast(t.Any, filter_sources).__fastmcp__ = types.SimpleNamespace(
    name="filter_sources",
    title="Filter Sources",
    tags=READONLY_TAGS | {"discovery"},
    annotations=None,
)


async def summarize_discovery(
    agent: t.Annotated[
        AgentSelector,
        Field(description="Limit discovery to one agent or scan every agent."),
    ] = "all",
) -> DiscoverySummaryResponse:
    """Aggregate counts of discovered sources by agent, format, and kind."""
    raise NotImplementedError(DOCS_ONLY_MESSAGE)


t.cast(t.Any, summarize_discovery).__fastmcp__ = types.SimpleNamespace(
    name="summarize_discovery",
    title="Summarize Discovery",
    tags=READONLY_TAGS | {"discovery"},
    annotations=None,
)


async def validate_query(
    terms: t.Annotated[
        list[str],
        Field(
            min_length=1,
            description="One or more literal or regex search terms.",
            examples=[["alpha"], ["foo.*bar"]],
        ),
    ],
    sample_text: t.Annotated[
        str,
        Field(description="Sample text to test the query against."),
    ],
    regex: t.Annotated[
        bool,
        Field(description="Treat terms as regular expressions."),
    ] = False,
    case_sensitive: t.Annotated[
        bool,
        Field(description="Perform case-sensitive matching."),
    ] = False,
    any_term: t.Annotated[
        bool,
        Field(description="Match any term instead of requiring all terms."),
    ] = False,
) -> ValidateQueryResponse:
    """Dry-run a query against sample text without searching files."""
    raise NotImplementedError(DOCS_ONLY_MESSAGE)


t.cast(t.Any, validate_query).__fastmcp__ = types.SimpleNamespace(
    name="validate_query",
    title="Validate Query",
    tags=READONLY_TAGS | {"diagnostic"},
    annotations=None,
)


async def recent_sessions(
    agent: t.Annotated[
        AgentSelector,
        Field(description="Limit discovery to one agent or scan every agent."),
    ] = "all",
    hours: t.Annotated[
        int,
        Field(
            default=24,
            ge=1,
            le=24 * 30,
            description="Look back this many hours (max 30 days).",
            examples=[1, 24, 168],
        ),
    ] = 24,
    limit: t.Annotated[
        int | None,
        Field(
            default=10,
            ge=1,
            description="Maximum number of sources to return.",
        ),
    ] = 10,
) -> RecentSessionsResponse:
    """Return sources modified in the last N hours, newest-first."""
    raise NotImplementedError(DOCS_ONLY_MESSAGE)


t.cast(t.Any, recent_sessions).__fastmcp__ = types.SimpleNamespace(
    name="recent_sessions",
    title="Recent Sessions",
    tags=READONLY_TAGS | {"search"},
    annotations=None,
)
