#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# dependencies = ["fastmcp>=3.0.0", "pydantic>=2.11.3"]
# ///
"""FastMCP server exposing ``agentex`` search and discovery.

Examples
--------
Run the MCP server over stdio:

```console
$ uv run py/agentex_mcp.py
```

Use the FastMCP config:

```console
$ uv run fastmcp run py/agentex.fastmcp.json
```
"""

from __future__ import annotations

import asyncio
import importlib
import pathlib
import typing as t

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

AgentSelector = t.Literal["codex", "claude", "cursor", "all"]
SearchTypeName = t.Literal["prompts", "history", "all"]

SERVER_VERSION = "0.1.0"
KNOWN_ADAPTERS: tuple[str, ...] = (
    "codex.history_json.v1",
    "codex.sessions_jsonl.v1",
    "claude.projects_jsonl.v1",
    "cursor.ai_tracking_sqlite.v1",
    "cursor.state_vscdb_legacy.v1",
    "cursor.state_vscdb_modern.v1",
)
READONLY_TAGS = {"readonly", "agentex"}
RESOURCE_ANNOTATIONS = {"readOnlyHint": True, "idempotentHint": True}


class SearchRecordLike(t.Protocol):
    """Structural type for shared ``agentex`` search records."""

    kind: str
    agent: str
    store: str
    adapter_id: str
    path: pathlib.Path
    text: str
    title: str | None
    role: str | None
    timestamp: str | None
    model: str | None
    session_id: str | None
    conversation_id: str | None
    metadata: dict[str, object]


class FindRecordLike(t.Protocol):
    """Structural type for shared ``agentex`` find records."""

    kind: str
    agent: str
    store: str
    adapter_id: str
    path: pathlib.Path
    path_kind: str
    metadata: dict[str, object]


class SourceHandleLike(t.Protocol):
    """Structural type for discovered ``agentex`` sources."""

    agent: str
    store: str
    adapter_id: str
    path: pathlib.Path
    path_kind: str
    source_kind: str
    search_root: pathlib.Path | None
    mtime_ns: int


class SearchQueryFactory(t.Protocol):
    """Factory protocol for ``agentex.SearchQuery``."""

    def __call__(  # noqa: D102
        self,
        *,
        terms: tuple[str, ...],
        search_type: str,
        any_term: bool,
        regex: bool,
        case_sensitive: bool,
        agents: tuple[str, ...],
        limit: int | None,
    ) -> object: ...


class BackendSelectionLike(t.Protocol):
    """Structural type for subprocess backend selection."""

    find_tool: str | None
    grep_tool: str | None
    json_tool: str | None


class AgentexModule(t.Protocol):
    """Structural type for the imported ``agentex`` module."""

    SCHEMA_VERSION: str
    SearchQuery: SearchQueryFactory

    def parse_agents(self, values: list[str]) -> tuple[str, ...]: ...  # noqa: D102

    def select_backends(self) -> BackendSelectionLike: ...  # noqa: D102

    def discover_sources(  # noqa: D102
        self,
        home: pathlib.Path,
        agents: tuple[str, ...],
        backends: BackendSelectionLike,
    ) -> list[SourceHandleLike]: ...

    def run_search_query(  # noqa: D102
        self,
        home: pathlib.Path,
        query: object,
        *,
        backends: BackendSelectionLike | None = None,
    ) -> list[SearchRecordLike]: ...

    def run_find_query(  # noqa: D102
        self,
        home: pathlib.Path,
        agents: tuple[str, ...],
        *,
        pattern: str | None,
        limit: int | None,
        backends: BackendSelectionLike | None = None,
    ) -> list[FindRecordLike]: ...

    def serialize_search_record(  # noqa: D102
        self,
        record: SearchRecordLike,
    ) -> dict[str, object]: ...

    def serialize_find_record(  # noqa: D102
        self,
        record: FindRecordLike,
    ) -> dict[str, object]: ...

    def serialize_source_handle(  # noqa: D102
        self,
        source: SourceHandleLike,
    ) -> dict[str, object]: ...


agentex = t.cast(
    "AgentexModule",
    t.cast("object", importlib.import_module("agentex")),
)


class AgentexModel(BaseModel):
    """Base model for MCP payloads."""

    model_config: t.ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class SearchRecordModel(AgentexModel):
    """Normalized search result payload."""

    schema_version: str = agentex.SCHEMA_VERSION
    kind: t.Literal["prompt", "history"]
    agent: t.Literal["codex", "claude", "cursor"]
    store: str
    adapter_id: str
    path: str
    text: str
    title: str | None = None
    role: str | None = None
    timestamp: str | None = None
    model: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    metadata: dict[str, t.Any] = Field(default_factory=dict)

    @classmethod
    def from_record(cls, record: SearchRecordLike) -> SearchRecordModel:
        """Build a typed result from an ``agentex`` search record."""
        return cls.model_validate(agentex.serialize_search_record(record))


class FindRecordModel(AgentexModel):
    """Normalized find result payload."""

    schema_version: str = agentex.SCHEMA_VERSION
    kind: t.Literal["find"]
    agent: t.Literal["codex", "claude", "cursor"]
    store: str
    adapter_id: str
    path: str
    path_kind: t.Literal["history_file", "session_file", "sqlite_db"]
    metadata: dict[str, t.Any] = Field(default_factory=dict)

    @classmethod
    def from_record(cls, record: FindRecordLike) -> FindRecordModel:
        """Build a typed result from an ``agentex`` find record."""
        return cls.model_validate(agentex.serialize_find_record(record))


class SourceRecordModel(AgentexModel):
    """Discovered source summary payload."""

    schema_version: str = agentex.SCHEMA_VERSION
    agent: t.Literal["codex", "claude", "cursor"]
    store: str
    adapter_id: str
    path: str
    path_kind: t.Literal["history_file", "session_file", "sqlite_db"]
    source_kind: t.Literal["json", "jsonl", "sqlite"]
    search_root: str | None = None
    mtime_ns: int

    @classmethod
    def from_source(cls, source: SourceHandleLike) -> SourceRecordModel:
        """Build a typed result from a discovered source."""
        return cls.model_validate(agentex.serialize_source_handle(source))


class SearchToolQuery(AgentexModel):
    """Echo of normalized search tool inputs."""

    terms: list[str]
    agent: AgentSelector
    search_type: SearchTypeName
    any_term: bool
    regex: bool
    case_sensitive: bool
    limit: int | None = None


class SearchToolResponse(AgentexModel):
    """Structured response for the MCP search tool."""

    schema_version: str = agentex.SCHEMA_VERSION
    query: SearchToolQuery
    results: list[SearchRecordModel]


class FindToolQuery(AgentexModel):
    """Echo of normalized find tool inputs."""

    pattern: str | None = None
    agent: AgentSelector
    limit: int | None = None


class FindToolResponse(AgentexModel):
    """Structured response for the MCP find tool."""

    schema_version: str = agentex.SCHEMA_VERSION
    query: FindToolQuery
    results: list[FindRecordModel]


class BackendAvailabilityModel(AgentexModel):
    """Selected read-only subprocess backends."""

    find_tool: str | None = None
    grep_tool: str | None = None
    json_tool: str | None = None


class CapabilitiesModel(AgentexModel):
    """Static MCP capability summary."""

    schema_version: str = agentex.SCHEMA_VERSION
    name: str = "agentex"
    version: str = SERVER_VERSION
    read_only: bool = True
    agents: list[t.Literal["codex", "claude", "cursor"]]
    search_types: list[SearchTypeName]
    adapters: list[str]
    tools: list[str]
    resources: list[str]
    prompts: list[str]
    backends: BackendAvailabilityModel


SourceListAdapter = TypeAdapter(list[SourceRecordModel])


def normalize_agent_selection(agent: AgentSelector) -> tuple[str, ...]:
    """Convert a single MCP agent selector into ``agentex`` agents."""
    values: list[str] = [] if agent == "all" else [agent]
    return agentex.parse_agents(values)


def list_source_models(agent: AgentSelector = "all") -> list[SourceRecordModel]:
    """Return discovered sources as typed MCP payloads."""
    backends = agentex.select_backends()
    sources = agentex.discover_sources(
        pathlib.Path.home(),
        normalize_agent_selection(agent),
        backends,
    )
    return [SourceRecordModel.from_source(source) for source in sources]


def build_capabilities() -> CapabilitiesModel:
    """Build a typed capability summary."""
    backends = agentex.select_backends()
    return CapabilitiesModel(
        agents=["codex", "claude", "cursor"],
        search_types=["prompts", "history", "all"],
        adapters=list(KNOWN_ADAPTERS),
        tools=["search", "find"],
        resources=[
            "agentex://capabilities",
            "agentex://sources",
            "agentex://sources/{agent}",
        ],
        prompts=["search_prompts", "search_history", "inspect_stores"],
        backends=BackendAvailabilityModel(
            find_tool=backends.find_tool,
            grep_tool=backends.grep_tool,
            json_tool=backends.json_tool,
        ),
    )


def _build_instructions() -> str:
    """Return server instructions for MCP clients."""
    return (
        "agentex is a read-only MCP server for local AI agent history search. "
        "Use `search` to retrieve full prompt/history matches and `find` to inspect "
        "discovered stores and session files. Search results are newest-first and "
        "duplicate prompts within the same session are collapsed. "
        "This server never mutates agent stores, never opens SQLite in write mode, "
        "and never executes arbitrary shell commands."
    )


class SearchRequestModel(AgentexModel):
    """Validated search request payload."""

    terms: list[str]
    agent: AgentSelector
    search_type: SearchTypeName
    any_term: bool
    regex: bool
    case_sensitive: bool
    limit: int | None = None


class FindRequestModel(AgentexModel):
    """Validated find request payload."""

    pattern: str | None = None
    agent: AgentSelector
    limit: int | None = None


def _search_sync(request: SearchRequestModel) -> SearchToolResponse:
    """Run the blocking search work and build a typed response."""
    query = agentex.SearchQuery(
        terms=tuple(request.terms),
        search_type=request.search_type,
        any_term=request.any_term,
        regex=request.regex,
        case_sensitive=request.case_sensitive,
        agents=normalize_agent_selection(request.agent),
        limit=request.limit,
    )
    records = agentex.run_search_query(pathlib.Path.home(), query)
    return SearchToolResponse(
        query=SearchToolQuery(
            terms=request.terms,
            agent=request.agent,
            search_type=request.search_type,
            any_term=request.any_term,
            regex=request.regex,
            case_sensitive=request.case_sensitive,
            limit=request.limit,
        ),
        results=[SearchRecordModel.from_record(record) for record in records],
    )


def _find_sync(request: FindRequestModel) -> FindToolResponse:
    """Run the blocking find work and build a typed response."""
    records = agentex.run_find_query(
        pathlib.Path.home(),
        normalize_agent_selection(request.agent),
        pattern=request.pattern,
        limit=request.limit,
    )
    return FindToolResponse(
        query=FindToolQuery(
            pattern=request.pattern,
            agent=request.agent,
            limit=request.limit,
        ),
        results=[FindRecordModel.from_record(record) for record in records],
    )


def _register_tools(mcp: FastMCP) -> None:
    """Register tool handlers on the server."""

    @mcp.tool(
        name="search",
        tags=READONLY_TAGS | {"search"},
        description="Search normalized prompts or history across local agent stores.",
    )
    async def search_tool(
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
        request = SearchRequestModel(
            terms=terms,
            agent=agent,
            search_type=search_type,
            any_term=any_term,
            regex=regex,
            case_sensitive=case_sensitive,
            limit=limit,
        )
        return await asyncio.to_thread(_search_sync, request)

    _ = search_tool

    @mcp.tool(
        name="find",
        tags=READONLY_TAGS | {"discovery"},
        description="Find known agent stores, session files, and SQLite databases.",
    )
    async def find_tool(
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
        request = FindRequestModel(pattern=pattern, agent=agent, limit=limit)
        return await asyncio.to_thread(_find_sync, request)

    _ = find_tool


def _register_resources(mcp: FastMCP) -> None:
    """Register static and templated resources."""

    @mcp.resource(
        "agentex://capabilities",
        name="agentex_capabilities",
        description="Read-only capability summary for the agentex MCP server.",
        mime_type="application/json",
        tags=READONLY_TAGS | {"capabilities"},
        annotations=RESOURCE_ANNOTATIONS,
    )
    def capabilities_resource() -> str:
        return build_capabilities().model_dump_json(indent=2)

    _ = capabilities_resource

    @mcp.resource(
        "agentex://sources",
        name="agentex_sources",
        description="All discovered read-only agent stores known to agentex.",
        mime_type="application/json",
        tags=READONLY_TAGS | {"discovery"},
        annotations=RESOURCE_ANNOTATIONS,
    )
    def sources_resource() -> str:
        return SourceListAdapter.dump_json(list_source_models()).decode("utf-8")

    _ = sources_resource

    @mcp.resource(
        "agentex://sources/{agent}",
        name="agentex_sources_by_agent",
        description="Discovered sources filtered to one agent.",
        mime_type="application/json",
        tags=READONLY_TAGS | {"discovery"},
        annotations=RESOURCE_ANNOTATIONS,
    )
    def sources_by_agent_resource(agent: str) -> str:
        selected_agent = t.cast("AgentSelector", agent)
        return SourceListAdapter.dump_json(list_source_models(selected_agent)).decode("utf-8")

    _ = sources_by_agent_resource


def _register_prompts(mcp: FastMCP) -> None:
    """Register prompt templates that guide MCP clients."""

    @mcp.prompt(
        name="search_prompts",
        description="Guide the client to search for matching user prompts.",
        tags={"search", "prompts", "readonly"},
    )
    def search_prompts_prompt(topic: str, agent: str = "all") -> str:
        return (
            "Use the `search` tool to find full user prompts about "
            f"{topic!r}. Search `prompts` only, keep newest-first ordering, "
            f"and limit the search to agent={agent!r} if requested."
        )

    _ = search_prompts_prompt

    @mcp.prompt(
        name="search_history",
        description="Guide the client to search assistant or command history records.",
        tags={"search", "history", "readonly"},
    )
    def search_history_prompt(topic: str, agent: str = "all") -> str:
        return (
            "Use the `search` tool to find matching history records about "
            f"{topic!r}. Search `history` only, and restrict to "
            f"agent={agent!r} when appropriate."
        )

    _ = search_history_prompt

    @mcp.prompt(
        name="inspect_stores",
        description="Guide the client to inspect discovered agent stores and session files.",
        tags={"discovery", "readonly"},
    )
    def inspect_stores_prompt(agent: str = "all", pattern: str = "") -> str:
        return (
            "Use the `find` tool to inspect discovered stores, session files, and "
            f"SQLite databases for agent={agent!r}. "
            f"Apply the pattern {pattern!r} when it is non-empty."
        )

    _ = inspect_stores_prompt


def build_mcp_server() -> FastMCP:
    """Build and return the FastMCP server instance."""
    mcp = FastMCP(
        name="agentex",
        version=SERVER_VERSION,
        instructions=_build_instructions(),
        on_duplicate="error",
    )
    _register_tools(mcp)
    _register_resources(mcp)
    _register_prompts(mcp)
    return mcp


def main() -> int:
    """Run the MCP server over stdio."""
    build_mcp_server().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
