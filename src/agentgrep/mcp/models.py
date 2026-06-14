"""Pydantic models for ``agentgrep`` MCP tool inputs and outputs."""

from __future__ import annotations

import typing as t

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from agentgrep.mcp._library import (
    SERVER_VERSION,
    AgentSelector,
    FindRecordLike,
    SearchRecordLike,
    SearchScopeName,
    SourceHandleLike,
    agentgrep,
)


class AgentGrepModel(BaseModel):
    """Base model for MCP payloads."""

    model_config: t.ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class SearchRecordModel(AgentGrepModel):
    """Normalized search result payload."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    ref: str
    kind: t.Literal["prompt", "history"]
    agent: t.Literal[
        "codex",
        "claude",
        "cursor-cli",
        "cursor-ide",
        "gemini",
        "antigravity-cli",
        "antigravity-ide",
        "grok",
        "pi",
        "opencode",
    ]
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
        from agentgrep.mcp import refs

        payload = agentgrep.serialize_search_record(record)
        payload["ref"] = refs.make_search_ref(record)
        return cls.model_validate(payload)


class FindRecordModel(AgentGrepModel):
    """Normalized find result payload."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    ref: str
    kind: t.Literal["find"]
    agent: t.Literal[
        "codex",
        "claude",
        "cursor-cli",
        "cursor-ide",
        "gemini",
        "antigravity-cli",
        "antigravity-ide",
        "grok",
        "pi",
        "opencode",
    ]
    store: str
    adapter_id: str
    path: str
    path_kind: t.Literal["history_file", "session_file", "sqlite_db", "store_file"]
    metadata: dict[str, t.Any] = Field(default_factory=dict)

    @classmethod
    def from_record(cls, record: FindRecordLike) -> FindRecordModel:
        """Build a typed result from an ``agentgrep`` find record."""
        from agentgrep.mcp import refs

        payload = agentgrep.serialize_find_record(record)
        payload["ref"] = refs.make_find_ref(record)
        return cls.model_validate(payload)


class SourceVersionDetectionModel(AgentGrepModel):
    """Detected version metadata for one discovered source."""

    app_version: str | None = None
    data_version: str | None = None
    strategy: t.Literal[
        "version_check",
        "embedded_metadata",
        "shape_inference",
        "catalog_observation",
    ]
    confidence: t.Literal["high", "medium", "low"]
    evidence: str


class SourceRecordModel(AgentGrepModel):
    """Discovered source summary payload."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    agent: t.Literal[
        "codex",
        "claude",
        "cursor-cli",
        "cursor-ide",
        "gemini",
        "antigravity-cli",
        "antigravity-ide",
        "grok",
        "pi",
        "opencode",
    ]
    store: str
    adapter_id: str
    path: str
    path_kind: t.Literal["history_file", "session_file", "sqlite_db", "store_file"]
    source_kind: t.Literal["json", "jsonl", "sqlite", "text", "opaque"]
    coverage: t.Literal["default_search", "inspectable", "catalog_only", "private"]
    searchable: bool
    search_by_default: bool
    searchable_reason: str
    inspectable: bool
    version_detection: SourceVersionDetectionModel | None = None
    search_root: str | None = None
    mtime_ns: int

    @classmethod
    def from_source(cls, source: SourceHandleLike) -> SourceRecordModel:
        """Build a typed result from a discovered source."""
        payload = agentgrep.serialize_source_handle(source)
        coverage = str(payload["coverage"])
        search_by_default = coverage == "default_search"
        inspectable = coverage in {"default_search", "inspectable"}
        if search_by_default:
            searchable_reason = "searched by default"
        elif inspectable:
            searchable_reason = "inspectable only; not searched by default"
        else:
            searchable_reason = "catalog only; not searched by default"
        payload["searchable"] = search_by_default
        payload["search_by_default"] = search_by_default
        payload["searchable_reason"] = searchable_reason
        payload["inspectable"] = inspectable
        return cls.model_validate(payload)


class ResultStatsModel(AgentGrepModel):
    """Counters collected while building one MCP result page.

    ``searched`` is tool-relative: search reports records examined, while
    find reports sources examined.
    """

    sources: int
    searched: int
    matched: int
    emitted: int


class PageInfoModel(AgentGrepModel):
    """Pagination metadata for a result page."""

    limit: int | None = None
    count: int
    next_cursor: str | None = None


class RunStatusModel(AgentGrepModel):
    """Search or find completion state."""

    state: t.Literal["complete", "bounded", "truncated", "cancelled", "approximate", "failed"]
    reason: str | None = None


class DiagnosticModel(AgentGrepModel):
    """Machine-readable result diagnostic."""

    code: str
    message: str


class SearchRequestModel(AgentGrepModel):
    """Validated search request payload."""

    terms: list[str]
    agent: AgentSelector
    scope: SearchScopeName
    case_sensitive: bool
    limit: int | None = None
    cursor: str | None = None


class SearchToolResponse(AgentGrepModel):
    """Structured response for the MCP search tool."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    request: SearchRequestModel
    stats: ResultStatsModel
    page: PageInfoModel
    status: RunStatusModel
    diagnostics: list[DiagnosticModel] = Field(default_factory=list)
    results: list[SearchRecordModel]


class FindRequestModel(AgentGrepModel):
    """Validated find request payload."""

    pattern: str | None = None
    agent: AgentSelector
    limit: int | None = None
    cursor: str | None = None


class FindToolResponse(AgentGrepModel):
    """Structured response for the MCP find tool."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    request: FindRequestModel
    stats: ResultStatsModel
    page: PageInfoModel
    status: RunStatusModel
    diagnostics: list[DiagnosticModel] = Field(default_factory=list)
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
    agents: list[
        t.Literal[
            "codex",
            "claude",
            "cursor-cli",
            "cursor-ide",
            "gemini",
            "antigravity-cli",
            "antigravity-ide",
            "grok",
            "pi",
            "opencode",
        ]
    ]
    search_scopes: list[SearchScopeName]
    adapters: list[str]
    tools: list[str]
    resources: list[str]
    prompts: list[str]
    backends: BackendAvailabilityModel


SourceListAdapter = TypeAdapter(list[SourceRecordModel])


class StoreDescriptorModel(AgentGrepModel):
    """Catalog descriptor for one on-disk agent store."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    kind: t.Literal["store"] = "store"
    agent: t.Literal[
        "codex",
        "claude",
        "cursor-cli",
        "cursor-ide",
        "gemini",
        "antigravity-cli",
        "antigravity-ide",
        "grok",
        "pi",
        "opencode",
    ]
    store_id: str
    role: str
    format: str
    path_pattern: str
    env_overrides: list[str] = Field(default_factory=list)
    platform_variants: dict[str, str] = Field(default_factory=dict)
    coverage: str
    version_strategies: list[str] = Field(default_factory=list)
    observed_version: str | None = None
    observed_at: str | None = None
    upstream_ref: str | None = None
    schema_notes: str | None = None
    sample_record: str | None = None
    search_by_default: bool | None = None
    search_notes: str | None = None
    distinguishes_from: list[str] = Field(default_factory=list)


class ListStoresRequest(AgentGrepModel):
    """Validated list-stores request payload."""

    agent: AgentSelector = "all"
    role_filter: str | None = None
    search_default_only: bool = False


class ListStoresResponse(AgentGrepModel):
    """Structured response for the MCP list_stores tool."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    stores: list[StoreDescriptorModel]
    total: int


class GetStoreDescriptorRequest(AgentGrepModel):
    """Validated get-store-descriptor request payload."""

    store_id: str = Field(
        min_length=1,
        description="Store id (e.g. 'claude.projects.session').",
    )


class ListSourcesRequest(AgentGrepModel):
    """Validated list-sources request payload."""

    agent: AgentSelector = "all"
    path_kind_filter: (
        t.Literal["history_file", "session_file", "sqlite_db", "store_file"] | None
    ) = None
    source_kind_filter: t.Literal["json", "jsonl", "sqlite", "text", "opaque"] | None = None
    coverage_filter: (
        t.Literal["default_search", "inspectable", "catalog_only", "private"] | None
    ) = None
    include_non_default: bool = False
    limit: int | None = Field(default=None, ge=1)


class ListSourcesResponse(AgentGrepModel):
    """Structured response for the MCP list_sources tool."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    sources: list[SourceRecordModel]
    total: int


class FilterSourcesRequest(AgentGrepModel):
    """Validated filter-sources request payload."""

    pattern: str | None = Field(default=None, min_length=1)
    agent: AgentSelector = "all"
    limit: int | None = Field(default=50, ge=1)
    cursor: str | None = None


class DiscoverySummaryRequest(AgentGrepModel):
    """Validated summarize-discovery request payload."""

    agent: AgentSelector = "all"


class DiscoverySummaryResponse(AgentGrepModel):
    """Aggregate counts of discovered sources."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    total_sources: int
    sources_by_agent: dict[str, int]
    sources_by_format: dict[str, int]
    sources_by_kind: dict[str, int]


class ValidateQueryRequest(AgentGrepModel):
    """Validated validate-query request payload."""

    terms: list[str] = Field(min_length=1)
    case_sensitive: bool = False
    sample_text: str


class ValidateQueryResponse(AgentGrepModel):
    """Result of a dry-run query validation."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    matches: bool
    regex_valid: bool
    error_message: str | None = None


class RecentSessionsRequest(AgentGrepModel):
    """Validated recent-sessions request payload."""

    agent: AgentSelector = "all"
    hours: int = Field(default=24, ge=1, le=24 * 30)
    limit: int | None = Field(default=10, ge=1)


class RecentSessionsResponse(AgentGrepModel):
    """Recently modified sources."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    cutoff_iso: str
    sources: list[SourceRecordModel]


class InspectSampleRequest(AgentGrepModel):
    """Validated inspect-record-sample request payload."""

    adapter_id: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    sample_size: int = Field(default=1, ge=1, le=20)


class InspectResultRequest(AgentGrepModel):
    """Validated inspect-result request payload."""

    ref: str = Field(min_length=1)
    sample_size: int = Field(default=1, ge=1, le=20)


class InspectSampleResponse(AgentGrepModel):
    """Sample records read from one source."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    adapter_id: str
    sample_count: int
    records: list[SearchRecordModel]
    error_message: str | None = None


class InspectResultResponse(AgentGrepModel):
    """Records read through an opaque result ref."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    ref: str
    sample_count: int
    records: list[SearchRecordModel]
    error_message: str | None = None
