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
    CatalogAgentSelector,
    ExportRecordsResponse,
    FindToolResponse,
    SearchScopeName,
    SearchToolResponse,
)
from agentgrep.mcp.models import (
    DiscoverySummaryResponse,
    InspectResultResponse,
    InspectSampleResponse,
    ListSourcesResponse,
    ListStoresResponse,
    RecentSessionsResponse,
    StoreDescriptorModel,
    ValidateQueryResponse,
)
from agentgrep.mcp.refs import MAX_RECORD_REF_CHARS
from agentgrep.query.help import query_language_summary

READONLY_TAGS = {"readonly", "agentgrep"}
DOCS_ONLY_MESSAGE = "Documentation signature only."


async def search(
    terms: t.Annotated[
        list[str] | None,
        Field(
            default=None,
            description=f"Search terms. {query_language_summary()}",
        ),
    ] = None,
    agent: t.Annotated[
        AgentSelector,
        Field(description="Limit search to one agent or search all agents."),
    ] = "all",
    scope: t.Annotated[
        SearchScopeName,
        Field(description="Search prompts, conversations, or both."),
    ] = "prompts",
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
    cursor: t.Annotated[
        str | None,
        Field(
            default=None,
            description="Opaque page cursor returned by a previous search response.",
        ),
    ] = None,
    cwd: t.Annotated[
        str | None,
        Field(
            default=None,
            description="Only return records whose recorded cwd matches this path.",
        ),
    ] = None,
    repo: t.Annotated[
        str | None,
        Field(
            default=None,
            description="Only return records whose recorded repository root matches this path.",
        ),
    ] = None,
    branch: t.Annotated[
        str | None,
        Field(
            default=None,
            description="Only return records whose recorded git branch matches this name.",
        ),
    ] = None,
) -> SearchToolResponse:
    """Search normalized prompts by default; opt into conversations with scope.

    Terms accept agentgrep's query language (field predicates, booleans,
    phrases, and wildcards); see agentgrep://query-language.
    """
    raise NotImplementedError(DOCS_ONLY_MESSAGE)


t.cast(t.Any, search).__fastmcp__ = types.SimpleNamespace(
    name="search",
    title="Search",
    tags=READONLY_TAGS | {"search"},
    annotations=None,
)


async def export_records(
    refs: t.Annotated[
        list[
            t.Annotated[
                str,
                Field(min_length=1, max_length=MAX_RECORD_REF_CHARS),
            ]
        ],
        Field(
            min_length=1,
            max_length=20,
            description="One to 20 opaque refs returned by search.",
        ),
    ],
    format: t.Annotated[  # noqa: A002 - public MCP argument name.
        t.Literal["ndjson", "markdown"],
        Field(description="Inline artifact format."),
    ] = "ndjson",
    selection: t.Annotated[
        t.Literal["records", "thread"],
        Field(description="Export flat records or one observed thread."),
    ] = "records",
    include_bodies: t.Annotated[
        bool,
        Field(description="Include prompt and history text in the artifact."),
    ] = False,
) -> ExportRecordsResponse:
    """Return selected search refs as one bounded inline artifact."""
    raise NotImplementedError(DOCS_ONLY_MESSAGE)


t.cast(t.Any, export_records).__fastmcp__ = types.SimpleNamespace(
    name="export_records",
    title="Export Records",
    tags=READONLY_TAGS | {"export"},
    annotations=types.SimpleNamespace(
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
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
    cursor: t.Annotated[
        str | None,
        Field(
            default=None,
            description="Opaque page cursor returned by a previous find response.",
        ),
    ] = None,
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
        CatalogAgentSelector,
        Field(
            default="all",
            description=("Filter to one catalog agent, including catalog-only agents, or 'all'."),
            examples=["all", "claude", "windsurf"],
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
            description="Path returned by list_sources; '~' home prefixes are accepted.",
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


async def inspect_result(
    ref: t.Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_RECORD_REF_CHARS,
            description="Opaque ref from a search or find result.",
        ),
    ],
    sample_size: t.Annotated[
        int,
        Field(
            default=1,
            ge=1,
            le=20,
            description="Number of source records to return for find refs (1-20).",
        ),
    ] = 1,
) -> InspectResultResponse:
    """Inspect records behind an opaque search/find result ref."""
    raise NotImplementedError(DOCS_ONLY_MESSAGE)


t.cast(t.Any, inspect_result).__fastmcp__ = types.SimpleNamespace(
    name="inspect_result",
    title="Inspect Result",
    tags=READONLY_TAGS | {"search", "discovery"},
    annotations=None,
)


async def list_sources(
    agent: t.Annotated[
        AgentSelector,
        Field(description="Limit discovery to one agent or scan every agent."),
    ] = "all",
    path_kind_filter: t.Annotated[
        t.Literal["history_file", "session_file", "sqlite_db", "store_file"] | None,
        Field(default=None, description="Filter by path kind."),
    ] = None,
    source_kind_filter: t.Annotated[
        t.Literal["json", "jsonl", "sqlite", "text", "opaque"] | None,
        Field(default=None, description="Filter by on-disk source kind."),
    ] = None,
    coverage_filter: t.Annotated[
        t.Literal["default_search", "inspectable", "catalog_only", "private"] | None,
        Field(default=None, description="Filter by coverage level."),
    ] = None,
    include_non_default: t.Annotated[
        bool,
        Field(
            default=False,
            description="Include non-default inventory sources when true.",
        ),
    ] = False,
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
        str | None,
        Field(
            default=None,
            min_length=1,
            description="Required substring pattern unless cursor is provided.",
            examples=["state", ".jsonl"],
        ),
    ] = None,
    agent: t.Annotated[
        AgentSelector,
        Field(description="Limit discovery to one agent or scan every agent."),
    ] = "all",
    limit: t.Annotated[
        int | None,
        Field(default=50, ge=1, description="Maximum number of sources to return."),
    ] = 50,
    cursor: t.Annotated[
        str | None,
        Field(
            default=None,
            description="Opaque page cursor returned by a previous filter_sources response.",
        ),
    ] = None,
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
        list[str] | None,
        Field(
            default=None,
            description="Literal/regex terms to test against sample_text.",
        ),
    ] = None,
    query: t.Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Query-language string to parse and compile; reports "
                "query_valid and any parse/compile error."
            ),
        ),
    ] = None,
    sample_text: t.Annotated[
        str,
        Field(description="Sample text to test terms against."),
    ] = "",
    case_sensitive: t.Annotated[
        bool,
        Field(description="Perform case-sensitive matching."),
    ] = False,
) -> ValidateQueryResponse:
    """Dry-run terms against sample text and/or validate query-language syntax.

    Supported syntax includes field predicates, booleans, and phrases; no
    files are searched.
    """
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
