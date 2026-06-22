"""windsurf store descriptors for the agentgrep catalogue (ADR 0010)."""

from __future__ import annotations

from agentgrep.store_catalog._common import _WINDSURF_OBSERVED_AT
from agentgrep.stores import (
    StoreCoverage,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
)

_WINDSURF_STORES: tuple[StoreDescriptor, ...] = (
    StoreDescriptor(
        agent="windsurf",
        store_id="windsurf.cascade",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.PROTOBUF,
        path_pattern="${HOME}/.codeium/windsurf/cascade/<session_uuid>.pb",
        observed_version="Windsurf Cascade (observed 2026-06-21)",
        observed_at=_WINDSURF_OBSERVED_AT,
        schema_notes=(
            "Per-session Cascade conversation transcript as opaque binary "
            "(`cascade/<uuid>.pb`, often multi-megabyte). The observed payloads "
            "are high-entropy with no extractable UTF-8 runs and are not "
            "gzip/zlib — they appear encrypted or custom-encoded, so agentgrep "
            "cannot read them. Documented location only; Windsurf is "
            "unsupported. The top-level `~/.codeium/cascade/` directory mirrors "
            "this for the non-Windsurf Codeium install."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="windsurf",
        store_id="windsurf.implicit",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.PROTOBUF,
        path_pattern="${HOME}/.codeium/windsurf/implicit/<uuid>.pb",
        observed_version="Windsurf Cascade (observed 2026-06-21)",
        observed_at=_WINDSURF_OBSERVED_AT,
        schema_notes=(
            "Implicit/background Cascade context-capture records as opaque, "
            "apparently-encrypted binary. Documented location only; unsupported."
        ),
        distinguishes_from=("windsurf.cascade",),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="windsurf",
        store_id="windsurf.chat_state",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.PROTOBUF,
        path_pattern="${HOME}/.codeium/windsurf/chat_state/<name>.pb",
        observed_version="Windsurf Cascade (observed 2026-06-21)",
        observed_at=_WINDSURF_OBSERVED_AT,
        schema_notes=(
            "Per-file chat state for legacy Codeium chat, opaque "
            "apparently-encrypted binary keyed by source file path. Documented "
            "location only; unsupported."
        ),
        distinguishes_from=("windsurf.cascade",),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="windsurf",
        store_id="windsurf.memories",
        role=StoreRole.PERSISTENT_MEMORY,
        format=StoreFormat.PROTOBUF,
        path_pattern="${HOME}/.codeium/windsurf/memories/<uuid>.pb",
        observed_version="Windsurf Cascade (observed 2026-06-21)",
        observed_at=_WINDSURF_OBSERVED_AT,
        schema_notes=(
            "Cascade memory entries as opaque, apparently-encrypted binary, one "
            "per uuid. Documented location only; unsupported. The companion "
            "`memories/global_rules.md` is readable Markdown (see "
            "`windsurf.global_rules`)."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="windsurf",
        store_id="windsurf.brain",
        role=StoreRole.PLAN,
        format=StoreFormat.MARKDOWN_FRONTMATTER,
        path_pattern="${HOME}/.codeium/windsurf/brain/<uuid>/plan.md",
        observed_version="Windsurf Cascade (observed 2026-06-21)",
        observed_at=_WINDSURF_OBSERVED_AT,
        schema_notes=(
            "Cascade agent-authored implementation plans as Markdown "
            "(`brain/<uuid>/plan.md`). Readable, but documented location only "
            "because Windsurf's conversation transcripts are encrypted and the "
            "agent is unsupported; the companion `plan_metadata.pbtxt` is "
            "protobuf-text metadata."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="windsurf",
        store_id="windsurf.global_rules",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.TEXT,
        path_pattern="${HOME}/.codeium/windsurf/memories/global_rules.md",
        observed_version="Windsurf Cascade (observed 2026-06-21)",
        observed_at=_WINDSURF_OBSERVED_AT,
        schema_notes=(
            "User-authored global rules Markdown injected into Cascade "
            "sessions — the Windsurf analogue of Claude's CLAUDE.md. Readable, "
            "but documented location only because the agent is unsupported."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
)
