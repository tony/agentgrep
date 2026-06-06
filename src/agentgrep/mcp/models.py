"""Pydantic models for ``agentgrep`` MCP tool inputs and outputs."""

from __future__ import annotations

import typing as t

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from agentgrep._query_gate import UNREGISTERED_FIELD_PREDICATE_CODE
from agentgrep.mcp._library import (
    SERVER_VERSION,
    AgentName,
    AgentSelector,
    CatalogAgentSelector,
    FindRecordLike,
    SearchEffortName,
    SearchRecordLike,
    SearchScopeName,
    SourceHandleLike,
    agentgrep,
)

if t.TYPE_CHECKING:
    from agentgrep._query_gate import UnregisteredFieldToken
    from agentgrep.results import NextAction, RunCoverage, RunDiagnostic, RunSummary


class AgentGrepModel(BaseModel):
    """Base model for MCP payloads."""

    model_config: t.ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class RecordOriginModel(AgentGrepModel):
    """Project/workspace origin attached to a search result."""

    cwd: str | None = None
    repo: str | None = None
    worktree: str | None = None
    branch: str | None = None
    remote: str | None = None
    cwd_hash: str | None = None


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
        "windsurf",
        "vscode",
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
    origin: RecordOriginModel | None = None
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
        "windsurf",
        "vscode",
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
        "windsurf",
        "vscode",
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
    store_role: str
    required_effort: t.Literal["prompt", "exhaustive"] | None
    version_detection: SourceVersionDetectionModel | None = None
    search_root: str | None = None
    mtime_ns: int

    @classmethod
    def from_source(cls, source: SourceHandleLike) -> SourceRecordModel:
        """Build a typed result from a discovered source."""
        payload = agentgrep.serialize_source_handle(source)
        coverage = str(payload["coverage"])
        role = agentgrep.store_role_for_record(source.store, source.adapter_id)
        store_role = "unknown" if role is None else str(role)
        searchable = coverage in {"default_search", "inspectable"}
        search_by_default = coverage == "default_search"
        inspectable = coverage != "private"
        required_effort: t.Literal["prompt", "exhaustive"] | None
        if not searchable:
            required_effort = None
            if coverage == "catalog_only":
                searchable_reason = "not searchable; available for explicit inspection"
            else:
                searchable_reason = "private catalog entry; not discovered or searchable"
        elif store_role == "prompt_history":
            required_effort = "prompt"
            searchable_reason = "searched by fast prompt effort"
        elif store_role in {"primary_chat", "supplementary_chat"}:
            required_effort = "exhaustive"
            searchable_reason = (
                "targeted effort may select this conversation store; exhaustive "
                "effort guarantees it is eligible for direct search"
            )
        else:
            required_effort = "exhaustive"
            searchable_reason = (
                "search requires exhaustive effort and a scope that admits this store role"
            )
        payload["searchable"] = searchable
        payload["search_by_default"] = search_by_default
        payload["searchable_reason"] = searchable_reason
        payload["inspectable"] = inspectable
        payload["store_role"] = store_role
        payload["required_effort"] = required_effort
        return cls.model_validate(payload)


class DbStatusModel(AgentGrepModel):
    """DB index status payload."""

    schema_version: str = agentgrep.SCHEMA_VERSION
    db_path: str
    db_schema_version: int
    sources: int
    records: int


class ResultStatsModel(AgentGrepModel):
    """Counters collected while building one MCP result page.

    ``searched`` is tool-relative: search reports records examined, while
    find reports sources examined.
    """

    sources: int
    searched: int
    matched: int
    emitted: int


class SearchEffortModel(AgentGrepModel):
    """Requested and successfully completed search effort."""

    requested: t.Literal["prompt", "targeted", "exhaustive"]
    completed: t.Literal["prompt", "targeted", "exhaustive"] | None

    @classmethod
    def from_summary(cls, summary: RunSummary) -> SearchEffortModel:
        """Adapt engine effort without changing its semantics."""
        return cls(
            requested=summary.requested_effort,
            completed=summary.completed_effort,
        )


class NormalizedSearchRequestModel(AgentGrepModel):
    """Engine-normalized search semantics, excluding adapter page mechanics."""

    terms: list[str]
    scope: SearchScopeName
    scope_provenance: t.Literal["inferred", "explicit"]
    effort: t.Literal["prompt", "targeted", "exhaustive"]
    agents: list[AgentName]
    conversation_limit: int | None
    dedupe: bool
    case_sensitive: bool
    order: t.Literal["newest", "relevance", "scan"]
    match_surface: t.Literal["haystack", "text"]

    @classmethod
    def from_summary(
        cls,
        summary: RunSummary,
    ) -> NormalizedSearchRequestModel:
        """Adapt normalized engine intent without exposing page overfetch."""
        request = summary.request
        return cls(
            terms=list(request.terms),
            scope=request.scope,
            scope_provenance=request.scope_provenance,
            effort=request.effort,
            agents=t.cast("list[AgentName]", list(request.agents)),
            conversation_limit=request.conversation_limit,
            dedupe=request.dedupe,
            case_sensitive=request.case_sensitive,
            order=t.cast(
                "t.Literal['newest', 'relevance', 'scan']",
                request.order,
            ),
            match_surface=request.match_surface,
        )


class SearchCoverageModel(AgentGrepModel):
    """Engine-owned source, record, and conversation coverage."""

    sources_discovered: int
    sources_eligible: int
    sources_planned: int
    sources_attempted: int
    sources_completed: int
    sources_bounded: int
    sources_skipped: int
    sources_unsupported: int
    sources_failed: int
    sources_cancelled: int
    records_seen: int
    matches_seen: int
    conversations_eligible: int
    conversations_selected: int
    conversations_completed: int
    source_stop_reasons: list[str]

    @classmethod
    def from_coverage(cls, coverage: RunCoverage) -> SearchCoverageModel:
        """Adapt dependency-light coverage to the MCP schema."""
        return cls(
            sources_discovered=coverage.sources_discovered,
            sources_eligible=coverage.sources_eligible,
            sources_planned=coverage.sources_planned,
            sources_attempted=coverage.sources_attempted,
            sources_completed=coverage.sources_completed,
            sources_bounded=coverage.sources_bounded,
            sources_skipped=coverage.sources_skipped,
            sources_unsupported=coverage.sources_unsupported,
            sources_failed=coverage.sources_failed,
            sources_cancelled=coverage.sources_cancelled,
            records_seen=coverage.records_seen,
            matches_seen=coverage.matches_seen,
            conversations_eligible=coverage.conversations_eligible,
            conversations_selected=coverage.conversations_selected,
            conversations_completed=coverage.conversations_completed,
            source_stop_reasons=list(coverage.source_stop_reasons),
        )


class PageInfoModel(AgentGrepModel):
    """Pagination metadata for a result page."""

    limit: int | None
    count: int
    next_cursor: str | None


class SearchPageModel(AgentGrepModel):
    """Bounded result-window metadata for one cursorless search."""

    limit: int | None
    count: int


class RunStatusModel(AgentGrepModel):
    """Search or find completion state."""

    state: t.Literal["complete", "bounded", "truncated", "cancelled", "approximate", "failed"]
    reason: str | None
    conditions: list[str]

    @classmethod
    def from_summary(cls, summary: RunSummary) -> RunStatusModel:
        """Adapt engine status without inferring completion."""
        return cls(
            state=summary.status.state,
            reason=summary.status.reason,
            conditions=list(summary.status.conditions),
        )


class DiagnosticModel(AgentGrepModel):
    """Machine-readable result diagnostic."""

    code: str
    message: str
    severity: t.Literal["info", "warning", "error"]

    @classmethod
    def from_diagnostic(cls, diagnostic: RunDiagnostic) -> DiagnosticModel:
        """Adapt one privacy-safe engine diagnostic."""
        return cls(
            code=diagnostic.code,
            message=diagnostic.message,
            severity=diagnostic.severity,
        )

    @classmethod
    def from_query_diagnostic(cls, diagnostic: UnregisteredFieldToken) -> DiagnosticModel:
        """Adapt one non-fatal query-language diagnostic (unregistered field predicate)."""
        return cls(
            code=UNREGISTERED_FIELD_PREDICATE_CODE,
            message=diagnostic.message,
            severity="warning",
        )


class SearchRequestPatchModel(AgentGrepModel):
    """Bounded request changes for one engine-authored next action."""

    effort: SearchEffortName | None
    scope: SearchScopeName | None
    conversation_limit: int | None


class NextActionModel(AgentGrepModel):
    """One engine-authored related-search action."""

    action_id: str
    kind: str
    label: str
    reason: str
    patch: SearchRequestPatchModel
    requires_confirmation: bool

    @classmethod
    def from_action(cls, action: NextAction) -> NextActionModel:
        """Adapt one engine action without expanding its patch."""
        return cls(
            action_id=action.action_id,
            kind=action.kind,
            label=action.label,
            reason=action.reason,
            patch=SearchRequestPatchModel(
                effort=action.patch.effort,
                scope=action.patch.scope,
                conversation_limit=action.patch.conversation_limit,
            ),
            requires_confirmation=action.requires_confirmation,
        )


class SearchRequestModel(AgentGrepModel):
    """Validated search request payload."""

    terms: list[str]
    agent: AgentSelector
    scope: SearchScopeName
    case_sensitive: bool
    effort: SearchEffortName | None = None
    conversation_limit: int | None = None
    limit: int | None = None
    cwd: str | None = None
    repo: str | None = None
    branch: str | None = None
    scope_provenance: t.Literal["inferred", "explicit"] = "inferred"


class SearchToolResponse(AgentGrepModel):
    """Structured response for the MCP search tool."""

    schema_version: str
    request: NormalizedSearchRequestModel
    effort: SearchEffortModel
    outcome: t.Literal[
        "matches",
        "no_prompt_match",
        "no_candidate_conversation",
        "no_selected_conversation_match",
        "no_exhaustive_match",
        "undetermined",
    ]
    coverage: SearchCoverageModel
    stats: ResultStatsModel
    page: SearchPageModel
    status: RunStatusModel
    diagnostics: list[DiagnosticModel]
    next_actions: list[NextActionModel]
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
            "windsurf",
            "vscode",
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
        "windsurf",
        "vscode",
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

    agent: CatalogAgentSelector = "all"
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


VALIDATE_QUERY_INPUT_ERROR = "provide terms, query, or both"


class ValidateQueryRequest(AgentGrepModel):
    """Validated validate-query request payload.

    Supply ``terms`` to dry-run literal/regex matching against
    ``sample_text``, ``query`` to validate query-language syntax, or both.
    """

    terms: list[str] | None = None
    query: str | None = None
    case_sensitive: bool = False
    sample_text: str = ""

    @model_validator(mode="after")
    def _require_terms_or_query(self) -> ValidateQueryRequest:
        """Require at least one of ``terms`` or ``query``."""
        if not self.terms and self.query is None:
            raise ValueError(VALIDATE_QUERY_INPUT_ERROR)
        return self


class ValidateQueryResponse(AgentGrepModel):
    """Result of a dry-run query validation.

    ``matches`` / ``regex_valid`` describe the literal/regex dry-run over
    ``terms``; ``query_valid`` describes query-language parse + compile and
    is ``None`` when no ``query`` was supplied.
    """

    schema_version: str = agentgrep.SCHEMA_VERSION
    matches: bool
    regex_valid: bool
    query_valid: bool | None = None
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
