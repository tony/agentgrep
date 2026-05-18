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
    AgentGrepModule,
    AgentName,
    AgentSelector,
    BackendSelectionLike,
    FindRecordLike,
    SearchQueryFactory,
    SearchRecordLike,
    SearchTypeName,
    SourceHandleLike,
    agentgrep,
    normalize_agent_selection,
)
from agentgrep.mcp.models import (
    AgentGrepModel,
    BackendAvailabilityModel,
    CapabilitiesModel,
    FindRecordModel,
    FindRequestModel,
    FindToolQuery,
    FindToolResponse,
    SearchRecordModel,
    SearchRequestModel,
    SearchToolQuery,
    SearchToolResponse,
    SourceListAdapter,
    SourceRecordModel,
)
from agentgrep.mcp.resources import build_capabilities, list_source_models
from agentgrep.mcp.server import build_mcp_server, main

__all__ = (
    "KNOWN_ADAPTERS",
    "READONLY_TAGS",
    "RESOURCE_ANNOTATIONS",
    "SERVER_VERSION",
    "AgentGrepModel",
    "AgentGrepModule",
    "AgentName",
    "AgentSelector",
    "BackendAvailabilityModel",
    "BackendSelectionLike",
    "CapabilitiesModel",
    "FindRecordLike",
    "FindRecordModel",
    "FindRequestModel",
    "FindToolQuery",
    "FindToolResponse",
    "SearchQueryFactory",
    "SearchRecordLike",
    "SearchRecordModel",
    "SearchRequestModel",
    "SearchToolQuery",
    "SearchToolResponse",
    "SearchTypeName",
    "SourceHandleLike",
    "SourceListAdapter",
    "SourceRecordModel",
    "agentgrep",
    "build_capabilities",
    "build_mcp_server",
    "list_source_models",
    "main",
    "normalize_agent_selection",
)
