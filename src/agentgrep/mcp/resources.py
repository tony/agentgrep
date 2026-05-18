"""Static and templated MCP resources for ``agentgrep``."""

from __future__ import annotations

import json
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
from agentgrep.store_catalog import CATALOG
from agentgrep.stores import StoreFormat, StoreRole

if t.TYPE_CHECKING:
    from fastmcp import FastMCP

#: One-line descriptions for each :class:`StoreRole` value. Kept here rather
#: than on the enum so the wording can be tuned for MCP consumers without
#: touching the library surface.
_ROLE_DESCRIPTIONS: dict[str, str] = {
    "primary_chat": "Primary conversation transcript for an agent.",
    "supplementary_chat": "Secondary chat (e.g. composer, side panel).",
    "prompt_history": "User-issued prompt history outside a session log.",
    "persistent_memory": "Cross-session memory or notes the agent retains.",
    "plan": "Plan-style step list the agent generated or maintains.",
    "todo": "Task list or todo store driven by the agent.",
    "app_state": "Application state (settings, UI, caches that aren't chat).",
    "cache": "Throwaway caches; usually not search-by-default.",
    "source_tree": "Source-tree snapshot or workspace index.",
    "unknown": "Role not yet classified.",
}

#: One-line descriptions for each :class:`StoreFormat` value.
_FORMAT_DESCRIPTIONS: dict[str, str] = {
    "jsonl": "JSON Lines: one object per line.",
    "json_array": "Single JSON array of records.",
    "json_object": "Single JSON object holding records at known keys.",
    "sqlite": "SQLite database opened read-only.",
    "md_frontmatter": "Markdown with YAML/JSON frontmatter blocks.",
    "protobuf": "Binary protobuf payload.",
    "opaque": "Format not parsed by agentgrep.",
}


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
            "agentgrep://catalog",
            "agentgrep://store-roles",
            "agentgrep://store-formats",
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

    @mcp.resource(
        "agentgrep://catalog",
        name="agentgrep_catalog",
        description="Full StoreCatalog: every known store with role, format, and notes.",
        mime_type="application/json",
        tags=READONLY_TAGS | {"catalog"},
        annotations=RESOURCE_ANNOTATIONS,
    )
    def catalog_resource() -> str:
        return CATALOG.model_dump_json(indent=2)

    _ = catalog_resource

    @mcp.resource(
        "agentgrep://store-roles",
        name="agentgrep_store_roles",
        description="StoreRole enum members with one-line descriptions.",
        mime_type="application/json",
        tags=READONLY_TAGS | {"catalog"},
        annotations=RESOURCE_ANNOTATIONS,
    )
    def store_roles_resource() -> str:
        rows = [
            {"name": role.name, "value": role.value, "description": _ROLE_DESCRIPTIONS[role.value]}
            for role in StoreRole
        ]
        return json.dumps(rows, indent=2)

    _ = store_roles_resource

    @mcp.resource(
        "agentgrep://store-formats",
        name="agentgrep_store_formats",
        description="StoreFormat enum members with one-line descriptions.",
        mime_type="application/json",
        tags=READONLY_TAGS | {"catalog"},
        annotations=RESOURCE_ANNOTATIONS,
    )
    def store_formats_resource() -> str:
        rows = [
            {
                "name": fmt.name,
                "value": fmt.value,
                "description": _FORMAT_DESCRIPTIONS[fmt.value],
            }
            for fmt in StoreFormat
        ]
        return json.dumps(rows, indent=2)

    _ = store_formats_resource
