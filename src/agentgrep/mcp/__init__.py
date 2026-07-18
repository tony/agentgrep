"""FastMCP server exposing ``agentgrep`` search and discovery.

Examples
--------
Run the MCP server over stdio:

```console
$ uv run agentgrep-mcp
```

Use the FastMCP config:

```console
$ uv run fastmcp run fastmcp.json
```
"""

from __future__ import annotations

from agentgrep.mcp._library import (
    KNOWN_ADAPTERS,
    READONLY_TAGS,
    RESOURCE_ANNOTATIONS,
    SERVER_VERSION,
    TOOL_ANNOTATIONS,
    AgentGrepModule,
    AgentName,
    AgentSelector,
    BackendSelectionLike,
    CatalogAgentSelector,
    FindRecordLike,
    SearchQueryFactory,
    SearchRecordLike,
    SearchScopeName,
    SourceHandleLike,
    agentgrep,
    normalize_agent_selection,
)
from agentgrep.mcp.models import (
    AgentGrepModel,
    BackendAvailabilityModel,
    CapabilitiesModel,
    DiagnosticModel,
    DiscoverySummaryRequest,
    DiscoverySummaryResponse,
    FilterSourcesRequest,
    FindRecordModel,
    FindRequestModel,
    FindToolResponse,
    GetStoreDescriptorRequest,
    InsightsSkillsRequest,
    InsightsSkillsResponse,
    InspectResultRequest,
    InspectResultResponse,
    InspectSampleRequest,
    InspectSampleResponse,
    ListSourcesRequest,
    ListSourcesResponse,
    ListStoresRequest,
    ListStoresResponse,
    PageInfoModel,
    RecentSessionsRequest,
    RecentSessionsResponse,
    RecordOriginModel,
    ResultStatsModel,
    RunStatusModel,
    SearchRecordModel,
    SearchRequestModel,
    SearchToolResponse,
    SourceListAdapter,
    SourceRecordModel,
    SourceVersionDetectionModel,
    StoreDescriptorModel,
    ValidateQueryRequest,
    ValidateQueryResponse,
)
from agentgrep.mcp.resources import build_capabilities, list_source_models
from agentgrep.mcp.server import build_mcp_server, main

__all__ = (
    "KNOWN_ADAPTERS",
    "READONLY_TAGS",
    "RESOURCE_ANNOTATIONS",
    "SERVER_VERSION",
    "TOOL_ANNOTATIONS",
    "AgentGrepModel",
    "AgentGrepModule",
    "AgentName",
    "AgentSelector",
    "BackendAvailabilityModel",
    "BackendSelectionLike",
    "CapabilitiesModel",
    "CatalogAgentSelector",
    "DiagnosticModel",
    "DiscoverySummaryRequest",
    "DiscoverySummaryResponse",
    "FilterSourcesRequest",
    "FindRecordLike",
    "FindRecordModel",
    "FindRequestModel",
    "FindToolResponse",
    "GetStoreDescriptorRequest",
    "InsightsSkillsRequest",
    "InsightsSkillsResponse",
    "InspectResultRequest",
    "InspectResultResponse",
    "InspectSampleRequest",
    "InspectSampleResponse",
    "ListSourcesRequest",
    "ListSourcesResponse",
    "ListStoresRequest",
    "ListStoresResponse",
    "PageInfoModel",
    "RecentSessionsRequest",
    "RecentSessionsResponse",
    "RecordOriginModel",
    "ResultStatsModel",
    "RunStatusModel",
    "SearchQueryFactory",
    "SearchRecordLike",
    "SearchRecordModel",
    "SearchRequestModel",
    "SearchScopeName",
    "SearchToolResponse",
    "SourceHandleLike",
    "SourceListAdapter",
    "SourceRecordModel",
    "SourceVersionDetectionModel",
    "StoreDescriptorModel",
    "ValidateQueryRequest",
    "ValidateQueryResponse",
    "agentgrep",
    "build_capabilities",
    "build_mcp_server",
    "list_source_models",
    "main",
    "normalize_agent_selection",
)
