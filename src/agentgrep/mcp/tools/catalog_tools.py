"""Catalog-domain MCP tools."""

from __future__ import annotations

import asyncio
import pathlib
import typing as t

from fastmcp.exceptions import ToolError
from pydantic import Field

from agentgrep.mcp import refs
from agentgrep.mcp._library import (
    READONLY_TAGS,
    TOOL_ANNOTATIONS,
    AgentSelector,
    agentgrep,
)
from agentgrep.mcp.models import (
    GetStoreDescriptorRequest,
    InspectResultRequest,
    InspectResultResponse,
    InspectSampleRequest,
    InspectSampleResponse,
    ListStoresRequest,
    ListStoresResponse,
    SearchRecordModel,
    StoreDescriptorModel,
)
from agentgrep.store_catalog import CATALOG

if t.TYPE_CHECKING:
    from fastmcp import FastMCP


def _descriptor_to_model(descriptor: t.Any) -> StoreDescriptorModel:
    """Convert a library ``StoreDescriptor`` to the MCP model."""
    observed_at = descriptor.observed_at
    observed_at_iso = observed_at.isoformat() if observed_at is not None else None
    return StoreDescriptorModel(
        agent=descriptor.agent,
        store_id=descriptor.store_id,
        role=descriptor.role.value,
        format=descriptor.format.value,
        path_pattern=descriptor.path_pattern,
        env_overrides=list(descriptor.env_overrides),
        platform_variants=dict(descriptor.platform_variants),
        coverage=descriptor.coverage_level.value,
        version_strategies=[strategy.value for strategy in descriptor.version_strategies],
        observed_version=descriptor.observed_version,
        observed_at=observed_at_iso,
        upstream_ref=descriptor.upstream_ref,
        schema_notes=descriptor.schema_notes,
        sample_record=descriptor.sample_record,
        search_by_default=descriptor.search_by_default,
        search_notes=descriptor.search_notes,
        distinguishes_from=list(descriptor.distinguishes_from),
    )


def _list_stores_sync(request: ListStoresRequest) -> ListStoresResponse:
    """Build a filtered list of catalog descriptors."""
    selected: list[StoreDescriptorModel] = []
    for descriptor in CATALOG.stores:
        if request.agent != "all" and descriptor.agent != request.agent:
            continue
        if request.role_filter is not None and descriptor.role.value != request.role_filter:
            continue
        if request.search_default_only and not descriptor.search_by_default:
            continue
        selected.append(_descriptor_to_model(descriptor))
    return ListStoresResponse(stores=selected, total=len(selected))


def _get_store_descriptor_sync(request: GetStoreDescriptorRequest) -> StoreDescriptorModel:
    """Look up one store descriptor by ``store_id``."""
    try:
        descriptor = CATALOG.by_id(request.store_id)
    except KeyError as exc:
        msg = f"unknown store_id: {request.store_id!r}"
        raise ToolError(msg) from exc
    return _descriptor_to_model(descriptor)


def _inspect_record_sample_sync(request: InspectSampleRequest) -> InspectSampleResponse:
    """Yield the first ``sample_size`` records from a matching source."""
    backends = agentgrep.select_backends()
    sources = agentgrep.discover_sources(
        pathlib.Path.home(),
        agentgrep.AGENT_CHOICES,
        backends,
        include_non_default=True,
        version_detail="none",
    )
    requested = pathlib.Path(request.source_path).expanduser().resolve()
    target = next(
        (
            source
            for source in sources
            if source.adapter_id == request.adapter_id
            and pathlib.Path(source.path).resolve() == requested
        ),
        None,
    )
    if target is None:
        return InspectSampleResponse(
            adapter_id=request.adapter_id,
            sample_count=0,
            records=[],
            error_message="source not found",
        )
    try:
        records: list[SearchRecordModel] = []
        for record in agentgrep.iter_source_records(target):
            records.append(SearchRecordModel.from_record(record))
            if len(records) >= request.sample_size:
                break
    except Exception as exc:
        return InspectSampleResponse(
            adapter_id=request.adapter_id,
            sample_count=0,
            records=[],
            error_message=f"{type(exc).__name__}: {exc}",
        )
    return InspectSampleResponse(
        adapter_id=request.adapter_id,
        sample_count=len(records),
        records=records,
    )


def _inspect_result_sync(request: InspectResultRequest) -> InspectResultResponse:
    """Resolve an opaque result ref and return source records."""
    home = pathlib.Path.home()
    try:
        parsed = refs.parse_record_ref(request.ref, home=home)
    except refs.McpTokenError as exc:
        return InspectResultResponse(
            ref=request.ref,
            sample_count=0,
            records=[],
            error_message=f"invalid ref: {exc}",
        )
    backends = agentgrep.select_backends()
    sources = agentgrep.discover_sources(
        home,
        agentgrep.AGENT_CHOICES,
        backends,
        include_non_default=True,
        version_detail="none",
    )
    target = next(
        (
            source
            for source in sources
            if source.adapter_id == parsed.adapter_id
            and pathlib.Path(source.path).resolve() == parsed.path.resolve()
        ),
        None,
    )
    if target is None:
        return InspectResultResponse(
            ref=request.ref,
            sample_count=0,
            records=[],
            error_message="source not found",
        )
    try:
        records: list[SearchRecordModel] = []
        for record in agentgrep.iter_source_records(target):
            if parsed.kind == "search" and (
                refs.search_record_fingerprint(record) != parsed.fingerprint
            ):
                continue
            records.append(SearchRecordModel.from_record(record))
            if parsed.kind == "search" or len(records) >= request.sample_size:
                break
    except Exception as exc:
        return InspectResultResponse(
            ref=request.ref,
            sample_count=0,
            records=[],
            error_message=f"{type(exc).__name__}: {exc}",
        )
    if not records:
        return InspectResultResponse(
            ref=request.ref,
            sample_count=0,
            records=[],
            error_message="record not found",
        )
    return InspectResultResponse(
        ref=request.ref,
        sample_count=len(records),
        records=records,
    )


def register(mcp: FastMCP) -> None:
    """Register catalog-domain tools."""

    @mcp.tool(
        name="list_stores",
        tags=READONLY_TAGS | {"catalog"},
        annotations=TOOL_ANNOTATIONS,
        description="List on-disk agent stores from the agentgrep catalog.",
    )
    async def list_stores_tool(
        agent: t.Annotated[
            AgentSelector,
            Field(
                default="all",
                description="Filter to one agent or 'all' for every catalog entry.",
            ),
        ] = "all",
        role_filter: t.Annotated[
            str | None,
            Field(
                default=None,
                description="Filter to one StoreRole value (e.g. 'primary_chat').",
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
        request = ListStoresRequest(
            agent=t.cast("t.Any", agent),
            role_filter=role_filter,
            search_default_only=search_default_only,
        )
        return await asyncio.to_thread(_list_stores_sync, request)

    _ = list_stores_tool

    @mcp.tool(
        name="get_store_descriptor",
        tags=READONLY_TAGS | {"catalog"},
        annotations=TOOL_ANNOTATIONS,
        description="Return the catalog descriptor for a single store by id.",
    )
    async def get_store_descriptor_tool(
        store_id: t.Annotated[
            str,
            Field(
                min_length=1,
                description="Store id (e.g. 'claude.projects.session').",
            ),
        ],
    ) -> StoreDescriptorModel:
        request = GetStoreDescriptorRequest(store_id=store_id)
        return await asyncio.to_thread(_get_store_descriptor_sync, request)

    _ = get_store_descriptor_tool

    @mcp.tool(
        name="inspect_record_sample",
        tags=READONLY_TAGS | {"catalog"},
        annotations=TOOL_ANNOTATIONS,
        description="Read the first N records from one adapter+path for schema inspection.",
    )
    async def inspect_record_sample_tool(
        adapter_id: t.Annotated[
            str,
            Field(
                min_length=1,
                description="Adapter id (e.g. 'claude.projects_jsonl.v1').",
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
        request = InspectSampleRequest(
            adapter_id=adapter_id,
            source_path=source_path,
            sample_size=sample_size,
        )
        return await asyncio.to_thread(_inspect_record_sample_sync, request)

    _ = inspect_record_sample_tool

    @mcp.tool(
        name="inspect_result",
        tags=READONLY_TAGS | {"search", "discovery"},
        annotations=TOOL_ANNOTATIONS,
        description="Inspect records behind an opaque search/find result ref.",
    )
    async def inspect_result_tool(
        ref: t.Annotated[
            str,
            Field(min_length=1, description="Opaque ref from a search or find result."),
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
        request = InspectResultRequest(ref=ref, sample_size=sample_size)
        return await asyncio.to_thread(_inspect_result_sync, request)

    _ = inspect_result_tool
