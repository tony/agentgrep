"""gemini store descriptors for the agentgrep catalogue."""

from __future__ import annotations

from agentgrep.store_catalog._common import _GEMINI_OBSERVED_AT
from agentgrep.stores import (
    DiscoverySpec,
    StoreCoverage,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
    VersionDetectionStrategy,
)

_GEMINI_STORES: tuple[StoreDescriptor, ...] = (
    StoreDescriptor(
        agent="gemini",
        store_id="gemini.tmp.chats",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${GEMINI_CLI_HOME or ${HOME}/.gemini}/tmp/<project_hash>/chats/"
            "session-<timestamp><id>.jsonl"
        ),
        env_overrides=("GEMINI_CLI_HOME",),
        observed_version="gemini-cli v0.47.0 stable",
        observed_at=_GEMINI_OBSERVED_AT,
        upstream_ref=(
            "github.com/google-gemini/gemini-cli@927170fc/"
            "packages/core/src/services/chatRecordingTypes.ts#L82"
        ),
        schema_notes=(
            "JSONL with mixed record types. Line 1 is a SessionMetadataRecord "
            "(`sessionId`, `projectHash`, `startTime`, `lastUpdated`, `kind`). "
            "Subsequent lines are `MessageRecord` turns (`id`, `timestamp`, "
            "`type`, `content`, optional "
            "`toolCalls`/`thoughts`/`tokens`/`model`/`displayContent`) "
            "interleaved with `MetadataUpdateRecord` updates (`{$set: ...}`). "
            "`content` is the searched (expanded) form; the occasional "
            "`displayContent` key is the UI-echo variant. "
            "Upstream types also declare `RewindRecord` and `PartialMetadataRecord` "
            "plus `type` values `info`/`error`/`warning`. The CLI does write "
            "`info`/`error` system records, but agentgrep surfaces only the `user` "
            "and `gemini` conversation turns and skips the system records. "
            "Adapter `store` field uses the underscore-flattened form "
            "``gemini.tmp_chats``."
        ),
        sample_record='{"id":"...","timestamp":"2026-05-17T...","type":"user","content":[{"text":"<redacted>"}]}',
        search_by_default=True,
        search_notes=(
            "Parsed by agentgrep via `parse_gemini_chat_file` "
            "(`gemini.tmp_chats_jsonl.v1`). When a `gemini`-typed record's "
            "`content` is empty, the assistant's prose is drawn from "
            "`thoughts[*].subject`/`description` and the tool-call context "
            "from `toolCalls[*].name`/`description` — concatenated into one "
            "SearchRecord per turn."
        ),
        discovery=(
            DiscoverySpec(
                store="gemini.tmp_chats",
                adapter_id="gemini.tmp_chats_jsonl.v1",
                path_kind="session_file",
                source_kind="jsonl",
                home_subpath=("tmp",),
                glob="session-*.jsonl",
            ),
        ),
    ),
    StoreDescriptor(
        agent="gemini",
        store_id="gemini.tmp.checkpoints",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.JSON_OBJECT,
        path_pattern=(
            "${GEMINI_CLI_HOME or ${HOME}/.gemini}/tmp/<project_hash>/chats/checkpoint-<tag>.json"
        ),
        env_overrides=("GEMINI_CLI_HOME",),
        observed_version="gemini-cli v0.47.0 stable",
        observed_at=_GEMINI_OBSERVED_AT,
        upstream_ref=(
            "github.com/google-gemini/gemini-cli@927170fc/packages/core/src/core/logger.ts#L29"
        ),
        schema_notes=(
            "Single-file conversation snapshot written by the `/chat save` command. "
            "JSON object `{ history: Content[]; authType?: AuthType }` where each "
            "`Content` is `{role: 'user'|'model', parts: [...]}`."
        ),
        distinguishes_from=("gemini.tmp.chats",),
        search_notes="User-named snapshots vs. continuous transcript.",
    ),
    StoreDescriptor(
        agent="gemini",
        store_id="gemini.tmp.logs",
        role=StoreRole.PROMPT_HISTORY,
        format=StoreFormat.JSON_ARRAY,
        path_pattern="${GEMINI_CLI_HOME or ${HOME}/.gemini}/tmp/<project_hash>/logs.json",
        env_overrides=("GEMINI_CLI_HOME",),
        observed_version="gemini-cli v0.47.0 stable",
        observed_at=_GEMINI_OBSERVED_AT,
        upstream_ref=(
            "github.com/google-gemini/gemini-cli@927170fc/packages/core/src/core/logger.ts#L21"
        ),
        schema_notes=(
            "JSON array of `LogEntry { sessionId, messageId, timestamp, type, "
            "message }` — user-prompt audit log. Adapter `store` field uses the "
            "underscore-flattened form ``gemini.tmp_logs``."
        ),
        sample_record='[{"sessionId":"...","messageId":0,"timestamp":"...","type":"user","message":"<redacted>"}]',
        search_by_default=True,
        search_notes=(
            "Parsed by agentgrep via `parse_gemini_logs_file` (`gemini.tmp_logs_json.v1`)."
        ),
        discovery=(
            DiscoverySpec(
                store="gemini.tmp_logs",
                adapter_id="gemini.tmp_logs_json.v1",
                path_kind="history_file",
                source_kind="json",
                home_subpath=("tmp",),
                glob="logs.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="gemini",
        store_id="gemini.tmp.chats_legacy",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.JSON_OBJECT,
        path_pattern=(
            "${GEMINI_CLI_HOME or ${HOME}/.gemini}/tmp/<project_hash>/chats/"
            "session-<timestamp><id>.json"
        ),
        env_overrides=("GEMINI_CLI_HOME",),
        observed_version="gemini-cli v0.47.0 stable",
        observed_at=_GEMINI_OBSERVED_AT,
        upstream_ref=(
            "github.com/google-gemini/gemini-cli@927170fc/"
            "packages/core/src/services/chatRecordingService.ts#L1041"
        ),
        schema_notes=(
            "Pre-Feb 2026 single-file session format. JSON object with "
            "top-level `sessionId`, `projectHash`, `startTime`, `lastUpdated`, "
            "and a `messages` array carrying the same per-turn fields as the "
            "current JSONL format. Upstream still reads this shape via the "
            "`isLegacyRecord` discriminator. Adapter `store` field uses the "
            "underscore-flattened form ``gemini.tmp_chats_legacy``."
        ),
        sample_record=(
            '{"sessionId":"...","projectHash":"...","startTime":"...",'
            '"messages":[{"id":"...","timestamp":"...","type":"user",'
            '"content":[{"text":"<redacted>"}]}]}'
        ),
        distinguishes_from=("gemini.tmp.chats",),
        search_by_default=True,
        search_notes=(
            "Parsed by agentgrep via `parse_gemini_chat_legacy_file` "
            "(`gemini.tmp_chats_legacy_json.v1`). Covers sessions whose "
            "files predate the JSONL migration; upstream still handles them."
        ),
        discovery=(
            DiscoverySpec(
                store="gemini.tmp_chats_legacy",
                adapter_id="gemini.tmp_chats_legacy_json.v1",
                path_kind="session_file",
                source_kind="json",
                home_subpath=("tmp",),
                glob="session-*.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="gemini",
        store_id="gemini.skills",
        role=StoreRole.APP_STATE,
        format=StoreFormat.MARKDOWN_FRONTMATTER,
        path_pattern="${GEMINI_CLI_HOME or ${HOME}/.gemini}/skills/",
        env_overrides=("GEMINI_CLI_HOME",),
        observed_version="gemini-cli v0.47.0 stable",
        observed_at=_GEMINI_OBSERVED_AT,
        schema_notes="Skill definitions; not chat.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="gemini",
        store_id="gemini.settings",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${GEMINI_CLI_HOME or ${HOME}/.gemini}/settings.json",
        env_overrides=("GEMINI_CLI_HOME",),
        observed_version="gemini-cli v0.47.0 stable",
        observed_at=_GEMINI_OBSERVED_AT,
        schema_notes="Configuration; not chat.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="gemini",
        store_id="gemini.memory",
        role=StoreRole.PERSISTENT_MEMORY,
        format=StoreFormat.TEXT,
        path_pattern="${GEMINI_CLI_HOME or ${HOME}/.gemini}/GEMINI.md",
        env_overrides=("GEMINI_CLI_HOME",),
        observed_version="gemini-cli v0.47.0 stable",
        observed_at=_GEMINI_OBSERVED_AT,
        schema_notes=(
            "Global user-authored context/memory Markdown injected into Gemini "
            "CLI sessions — the Gemini analogue of Claude's CLAUDE.md. Standing "
            "instructions, not chat; inspectable opt-in rather than searched by "
            "default."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="gemini.memory",
                adapter_id="gemini.memory_text.v1",
                data_version="gemini.memory.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                files=("GEMINI.md",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="gemini",
        store_id="gemini.tool_outputs",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern=(
            "${GEMINI_CLI_HOME or ${HOME}/.gemini}/tmp/<project_hash>/"
            "tool-outputs/session-<id>/<name>.txt"
        ),
        env_overrides=("GEMINI_CLI_HOME",),
        observed_version="gemini-cli v0.47.0 stable",
        observed_at=_GEMINI_OBSERVED_AT,
        schema_notes=(
            "Per-tool-call output text (run_shell_command / read_file / "
            "update_topic results) under `tmp/<hash>/tool-outputs/session-<id>/`. "
            "Tool output rather than user prompts (may echo file or command "
            "content), so inspectable opt-in, not searched by default."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="gemini.tool_outputs",
                adapter_id="gemini.tool_outputs_text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("tmp",),
                glob="*.txt",
                path_parts_required=("tool-outputs",),
            ),
        ),
    ),
)
