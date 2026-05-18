"""Static and templated MCP resources for ``agentgrep``."""

from __future__ import annotations

import pathlib
import typing as t

from agentgrep.mcp._library import (
    KNOWN_ADAPTERS,
    READONLY_TAGS,
    RESOURCE_ANNOTATIONS,
    AgentSelector,
    agentgrep,
    normalize_agent_selection,
)
from agentgrep.mcp.models import (
    BackendAvailabilityModel,
    CapabilitiesModel,
    SourceListAdapter,
    SourceRecordModel,
)

if t.TYPE_CHECKING:
    from fastmcp import FastMCP


def list_source_models(agent: AgentSelector = "all") -> list[SourceRecordModel]:
    """Return discovered sources as typed MCP payloads."""
    backends = agentgrep.select_backends()
    sources = agentgrep.discover_sources(
        pathlib.Path.home(),
        normalize_agent_selection(agent),
        backends,
    )
    return [SourceRecordModel.from_source(source) for source in sources]


def build_capabilities() -> CapabilitiesModel:
    """Build a typed capability summary."""
    backends = agentgrep.select_backends()
    return CapabilitiesModel(
        agents=list(agentgrep.AGENT_CHOICES),
        search_types=["prompts", "history", "all"],
        adapters=list(KNOWN_ADAPTERS),
        tools=[
            "search",
            "recent_sessions",
            "find",
            "list_sources",
            "filter_sources",
            "summarize_discovery",
            "list_stores",
            "get_store_descriptor",
            "inspect_record_sample",
            "validate_query",
        ],
        resources=[
            "agentgrep://capabilities",
            "agentgrep://sources",
            "agentgrep://sources/{agent}",
        ],
        prompts=["search_prompts", "search_history", "inspect_stores"],
        backends=BackendAvailabilityModel(
            find_tool=backends.find_tool,
            grep_tool=backends.grep_tool,
            json_tool=backends.json_tool,
        ),
    )


def register_resources(mcp: FastMCP) -> None:
    """Register every ``agentgrep`` resource on ``mcp``."""

    @mcp.resource(
        "agentgrep://capabilities",
        name="agentgrep_capabilities",
        description="Read-only capability summary for the agentgrep MCP server.",
        mime_type="application/json",
        tags=READONLY_TAGS | {"capabilities"},
        annotations=RESOURCE_ANNOTATIONS,
    )
    def capabilities_resource() -> str:
        return build_capabilities().model_dump_json(indent=2)

    _ = capabilities_resource

    @mcp.resource(
        "agentgrep://sources",
        name="agentgrep_sources",
        description="All discovered read-only agent stores known to agentgrep.",
        mime_type="application/json",
        tags=READONLY_TAGS | {"discovery"},
        annotations=RESOURCE_ANNOTATIONS,
    )
    def sources_resource() -> str:
        return SourceListAdapter.dump_json(list_source_models()).decode("utf-8")

    _ = sources_resource

    @mcp.resource(
        "agentgrep://sources/{agent}",
        name="agentgrep_sources_by_agent",
        description="Discovered sources filtered to one agent.",
        mime_type="application/json",
        tags=READONLY_TAGS | {"discovery"},
        annotations=RESOURCE_ANNOTATIONS,
    )
    def sources_by_agent_resource(agent: str) -> str:
        selected_agent = t.cast("AgentSelector", agent)
        return SourceListAdapter.dump_json(list_source_models(selected_agent)).decode("utf-8")

    _ = sources_by_agent_resource
