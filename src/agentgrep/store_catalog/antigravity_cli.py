"""antigravity_cli store descriptors for the agentgrep catalogue."""

from __future__ import annotations

from agentgrep.store_catalog._common import _ANTIGRAVITY_OBSERVED_AT
from agentgrep.stores import (
    DiscoverySpec,
    StoreCoverage,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
)

_ANTIGRAVITY_CLI_STORES: tuple[StoreDescriptor, ...] = (
    StoreDescriptor(
        agent="antigravity-cli",
        store_id="antigravity-cli.history",
        role=StoreRole.PROMPT_HISTORY,
        format=StoreFormat.JSONL,
        path_pattern="${HOME}/.gemini/antigravity-cli/history.jsonl",
        observed_version="agy v1.0.10 (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes=(
            "JSONL prompt recall log. Observed keys: `display` (prompt text), "
            "`timestamp` (Unix milliseconds), `workspace`, optional `type`, "
            "and optional `conversationId`."
        ),
        sample_record=(
            '{"display":"<redacted>","timestamp":1780142400000,'
            '"type":"prompt","workspace":"/repo","conversationId":"..."}'
        ),
        search_by_default=True,
        search_notes=(
            "Default-searchable user prompt history. Full transcript stores "
            "are protobuf and remain inspectable only."
        ),
        discovery=(
            DiscoverySpec(
                store="antigravity-cli.history",
                adapter_id="antigravity_cli.history_jsonl.v1",
                data_version="antigravity_cli.history_jsonl.v1",
                path_kind="history_file",
                source_kind="jsonl",
                files=("history.jsonl",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="antigravity-cli",
        store_id="antigravity-cli.conversations",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.SQLITE,
        path_pattern="${HOME}/.gemini/antigravity-cli/conversations/<conversation_uuid>.db",
        observed_version="agy v1.0.10 (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes=(
            "One SQLite database per conversation. Table `steps` contains "
            "`idx`, `step_type`, `status`, `has_subtrajectory`, `metadata`, "
            "`error_details`, `permissions`, `task_details`, `render_info`, "
            "`step_payload` (mostly protobuf blobs), and `step_format`; "
            "companion metadata tables hold protobuf blobs. No public schema "
            "is available, so agentgrep extracts readable protobuf strings "
            "best-effort."
        ),
        sample_record="steps(idx=1, step_payload=<protobuf>, step_format=1)",
        distinguishes_from=("antigravity-cli.history",),
        search_by_default=False,
        search_notes="Inspectable only; protobuf transcripts are not searched by default.",
        discovery=(
            DiscoverySpec(
                store="antigravity-cli.conversations",
                adapter_id="antigravity_cli.conversations_sqlite_protobuf.v1",
                data_version="antigravity_cli.conversations_sqlite_protobuf.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                home_subpath=("conversations",),
                glob="*.db",
            ),
        ),
    ),
    StoreDescriptor(
        agent="antigravity-cli",
        store_id="antigravity-cli.transcript",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${HOME}/.gemini/antigravity-cli/brain/<conversation_uuid>/"
            ".system_generated/logs/transcript_full.jsonl"
        ),
        observed_version="agy v1.0.10 (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes=(
            "Readable JSONL transcript log under a brain conversation's "
            "`.system_generated/logs/`. Each line is a step record with a "
            "universal `step_index` plus `type`, `source`, `status`, "
            "`created_at`. agentgrep surfaces the string `content` "
            "(user/assistant turns); lines without `content` — "
            "`thinking`/`tool_calls`-only lines (e.g. `PLANNER_RESPONSE`) and "
            "payload-less lines (e.g. `CONVERSATION_HISTORY`) — yield no "
            "record. This is the "
            "readable counterpart to the opaque protobuf "
            "`antigravity-cli.conversations` and reaches text the brain "
            "Markdown glob cannot. The truncated `transcript.jsonl` sibling is "
            "skipped in favour of `transcript_full.jsonl`."
        ),
        distinguishes_from=("antigravity-cli.conversations", "antigravity-cli.brain"),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="antigravity-cli.transcript",
                adapter_id="antigravity_cli.transcript_jsonl.v1",
                path_kind="session_file",
                source_kind="jsonl",
                home_subpath=("brain",),
                glob="**/transcript_full.jsonl",
            ),
        ),
    ),
    StoreDescriptor(
        agent="antigravity-cli",
        store_id="antigravity-cli.implicit",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.PROTOBUF,
        path_pattern="${HOME}/.gemini/antigravity-cli/implicit/<conversation_uuid>.pb",
        observed_version="agy v1.0.10 (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes=(
            "Protobuf conversation artifacts without a published schema. "
            "Inspectable via the generic protobuf text extractor."
        ),
        distinguishes_from=("antigravity-cli.conversations",),
        search_by_default=False,
        search_notes="Inspectable only; not searched by default.",
        discovery=(
            DiscoverySpec(
                store="antigravity-cli.implicit",
                adapter_id="antigravity_cli.implicit_protobuf.v1",
                data_version="antigravity_cli.implicit_protobuf.v1",
                path_kind="session_file",
                source_kind="opaque",
                home_subpath=("implicit",),
                glob="*.pb",
            ),
        ),
    ),
    StoreDescriptor(
        agent="antigravity-cli",
        store_id="antigravity-cli.brain",
        role=StoreRole.PLAN,
        format=StoreFormat.TEXT,
        path_pattern="${HOME}/.gemini/antigravity-cli/brain/**/*.md",
        observed_version="agy v1.0.10 (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes="Markdown planning and memory artifacts, not prompt recall.",
        search_by_default=False,
        search_notes="Inspectable only; not searched by default.",
        discovery=(
            DiscoverySpec(
                store="antigravity-cli.brain",
                adapter_id="antigravity_cli.brain_text.v1",
                data_version="antigravity_cli.brain_text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("brain",),
                glob="**/*.md",
            ),
        ),
    ),
    StoreDescriptor(
        agent="antigravity-cli",
        store_id="antigravity-cli.cache",
        role=StoreRole.CACHE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${HOME}/.gemini/antigravity-cli/cache/",
        observed_version="agy v1.0.10 (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes="Runtime cache files. Cache state, not conversation history.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="antigravity-cli",
        store_id="antigravity-cli.log",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern="${HOME}/.gemini/antigravity-cli/log/",
        observed_version="agy v1.0.10 (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes="Application logs. Diagnostics, not chat content.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="antigravity-cli",
        store_id="antigravity-cli.oauth",
        role=StoreRole.APP_STATE,
        format=StoreFormat.OPAQUE,
        path_pattern="${HOME}/.gemini/antigravity-cli/antigravity-oauth-token",
        observed_version="agy v1.0.10 (observed 2026-06-21)",
        observed_at=_ANTIGRAVITY_OBSERVED_AT,
        schema_notes="OAuth token material. Documented but never enumerated.",
        coverage=StoreCoverage.PRIVATE,
        search_by_default=False,
    ),
)
