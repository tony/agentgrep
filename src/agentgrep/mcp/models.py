"""Pydantic models for ``agentgrep`` MCP tool inputs and outputs."""

from __future__ import annotations

import typing as t

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from agentgrep.mcp._library import (
    SERVER_VERSION,
    AgentSelector,
    FindRecordLike,
    SearchRecordLike,
    SearchTypeName,
    SourceHandleLike,
    agentgrep,
)


class AgentGrepModel(BaseModel):
    """Base model for MCP payloads."""

    model_config: t.ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class SearchRecordModel(AgentGrepModel):
    """Normalized search result payload."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    kind: t.Literal["prompt", "history"]
    agent: t.Literal["codex", "claude", "cursor", "gemini"]
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
        """Build a typed result from an ``agentgrep`` search record."""
        return cls.model_validate(agentgrep.serialize_search_record(record))


class FindRecordModel(AgentGrepModel):
    """Normalized find result payload."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    kind: t.Literal["find"]
    agent: t.Literal["codex", "claude", "cursor", "gemini"]
    store: str
    adapter_id: str
    path: str
    path_kind: t.Literal["history_file", "session_file", "sqlite_db"]
    metadata: dict[str, t.Any] = Field(default_factory=dict)

    @classmethod
    def from_record(cls, record: FindRecordLike) -> FindRecordModel:
        """Build a typed result from an ``agentgrep`` find record."""
        return cls.model_validate(agentgrep.serialize_find_record(record))


class SourceRecordModel(AgentGrepModel):
    """Discovered source summary payload."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    agent: t.Literal["codex", "claude", "cursor", "gemini"]
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
        return cls.model_validate(agentgrep.serialize_source_handle(source))


class SearchToolQuery(AgentGrepModel):
    """Echo of normalized search tool inputs."""

    terms: list[str]
    agent: AgentSelector
    search_type: SearchTypeName
    any_term: bool
    regex: bool
    case_sensitive: bool
    limit: int | None = None


class SearchToolResponse(AgentGrepModel):
    """Structured response for the MCP search tool."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    query: SearchToolQuery
    results: list[SearchRecordModel]


class FindToolQuery(AgentGrepModel):
    """Echo of normalized find tool inputs."""

    pattern: str | None = None
    agent: AgentSelector
    limit: int | None = None


class FindToolResponse(AgentGrepModel):
    """Structured response for the MCP find tool."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    query: FindToolQuery
    results: list[FindRecordModel]


class BackendAvailabilityModel(AgentGrepModel):
    """Selected read-only subprocess backends."""

    find_tool: str | None = None
    grep_tool: str | None = None
    json_tool: str | None = None


class CapabilitiesModel(AgentGrepModel):
    """Static MCP capability summary."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    name: str = "agentgrep"
    version: str = SERVER_VERSION
    read_only: bool = True
    agents: list[t.Literal["codex", "claude", "cursor", "gemini"]]
    search_types: list[SearchTypeName]
    adapters: list[str]
    tools: list[str]
    resources: list[str]
    prompts: list[str]
    backends: BackendAvailabilityModel


SourceListAdapter = TypeAdapter(list[SourceRecordModel])


class SearchRequestModel(AgentGrepModel):
    """Validated search request payload."""

    terms: list[str]
    agent: AgentSelector
    search_type: SearchTypeName
    any_term: bool
    regex: bool
    case_sensitive: bool
    limit: int | None = None


class FindRequestModel(AgentGrepModel):
    """Validated find request payload."""

    pattern: str | None = None
    agent: AgentSelector
    limit: int | None = None
