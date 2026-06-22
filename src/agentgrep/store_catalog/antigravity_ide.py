"""antigravity_ide store descriptors for the agentgrep catalogue (ADR 0010)."""

from __future__ import annotations

from agentgrep.store_catalog._common import _ANTIGRAVITY_OBSERVED_AT
from agentgrep.stores import (
    DiscoverySpec,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
)

_ANTIGRAVITY_IDE_STORES: tuple[StoreDescriptor, ...] = (
    StoreDescriptor(
        agent="antigravity-ide",
        store_id="antigravity-ide.conversations",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.PROTOBUF,
        path_pattern="${HOME}/.gemini/antigravity/conversations/<conversation_uuid>.pb",
        observed_version="Google Antigravity IDE (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes=(
            "Per-conversation protobuf artifacts without a published schema. "
            "Inspectable via the generic protobuf text extractor."
        ),
        sample_record="<protobuf with readable prompt/history strings>",
        search_by_default=False,
        search_notes="Inspectable only; not searched by default.",
        discovery=(
            DiscoverySpec(
                store="antigravity-ide.conversations",
                adapter_id="antigravity_ide.conversations_protobuf.v1",
                data_version="antigravity_ide.conversations_protobuf.v1",
                path_kind="session_file",
                source_kind="opaque",
                home_subpath=("conversations",),
                glob="*.pb",
            ),
        ),
    ),
    StoreDescriptor(
        agent="antigravity-ide",
        store_id="antigravity-ide.implicit",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.PROTOBUF,
        path_pattern="${HOME}/.gemini/antigravity/implicit/<conversation_uuid>.pb",
        observed_version="Google Antigravity IDE (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes=(
            "Implicit protobuf conversation artifacts without a published "
            "schema. Inspectable via the generic protobuf text extractor."
        ),
        distinguishes_from=("antigravity-ide.conversations",),
        search_by_default=False,
        search_notes="Inspectable only; not searched by default.",
        discovery=(
            DiscoverySpec(
                store="antigravity-ide.implicit",
                adapter_id="antigravity_ide.implicit_protobuf.v1",
                data_version="antigravity_ide.implicit_protobuf.v1",
                path_kind="session_file",
                source_kind="opaque",
                home_subpath=("implicit",),
                glob="*.pb",
            ),
        ),
    ),
    StoreDescriptor(
        agent="antigravity-ide",
        store_id="antigravity-ide.brain",
        role=StoreRole.PLAN,
        format=StoreFormat.TEXT,
        path_pattern="${HOME}/.gemini/antigravity/brain/**/*.md",
        observed_version="Google Antigravity IDE (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes="Markdown planning and memory artifacts, not prompt recall.",
        search_by_default=False,
        search_notes="Inspectable only; not searched by default.",
        discovery=(
            DiscoverySpec(
                store="antigravity-ide.brain",
                adapter_id="antigravity_ide.brain_text.v1",
                data_version="antigravity_ide.brain_text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("brain",),
                glob="**/*.md",
            ),
        ),
    ),
    StoreDescriptor(
        agent="antigravity-ide",
        store_id="antigravity-ide.brain_resolved",
        role=StoreRole.PLAN,
        format=StoreFormat.TEXT,
        path_pattern="${HOME}/.gemini/antigravity/brain/<uuid>/task.md.resolved",
        observed_version="Google Antigravity IDE (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes=(
            "Expanded task Markdown (`task.md.resolved` plus numbered "
            "`.resolved.0..N` snapshots) that the `**/*.md` brain glob cannot "
            "reach because of the `.resolved` suffix. Readable plan text, "
            "inspectable opt-in."
        ),
        distinguishes_from=("antigravity-ide.brain",),
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="antigravity-ide.brain_resolved",
                adapter_id="antigravity_ide.brain_resolved_text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("brain",),
                glob="**/task.md.resolved*",
            ),
        ),
    ),
    StoreDescriptor(
        agent="antigravity-ide",
        store_id="antigravity-ide.skills",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.MARKDOWN_FRONTMATTER,
        path_pattern="${HOME}/.gemini/antigravity/skills/**/*.md",
        observed_version="Google Antigravity IDE (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes="Markdown skill definitions and instructions, not conversation history.",
        search_by_default=False,
        search_notes="Inspectable only; not searched by default.",
        discovery=(
            DiscoverySpec(
                store="antigravity-ide.skills",
                adapter_id="antigravity_ide.skills_text.v1",
                data_version="antigravity_ide.skills_text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("skills",),
                glob="**/*.md",
            ),
        ),
    ),
    StoreDescriptor(
        agent="antigravity-ide",
        store_id="antigravity-ide.user_settings",
        role=StoreRole.APP_STATE,
        format=StoreFormat.PROTOBUF,
        path_pattern="${HOME}/.gemini/antigravity/user_settings.pb",
        observed_version="Google Antigravity IDE (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes="Protobuf user settings. Configuration, not chat content.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="antigravity-ide",
        store_id="antigravity-ide.mcp_config",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${HOME}/.gemini/antigravity/mcp_config.json",
        observed_version="Google Antigravity IDE (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes="MCP server configuration. Configuration, not chat content.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="antigravity-ide",
        store_id="antigravity-ide.server",
        role=StoreRole.SOURCE_TREE,
        format=StoreFormat.OPAQUE,
        path_pattern="${HOME}/.antigravity-server/",
        observed_version="Google Antigravity IDE (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes="Local IDE server state and binaries. Not conversation history.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="antigravity-ide",
        store_id="antigravity-ide.cache",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern="${HOME}/.cache/antigravity/staging/",
        observed_version="Google Antigravity IDE (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes="Staging cache files. Cache state, not conversation history.",
        search_by_default=False,
    ),
)
