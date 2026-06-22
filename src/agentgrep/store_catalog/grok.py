"""grok store descriptors for the agentgrep catalogue (ADR 0010)."""

from __future__ import annotations

from agentgrep.store_catalog._common import _GROK_OBSERVED_AT
from agentgrep.stores import (
    DiscoverySpec,
    StoreCoverage,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
)

_GROK_STORES: tuple[StoreDescriptor, ...] = (
    StoreDescriptor(
        agent="grok",
        store_id="grok.prompt_history",
        role=StoreRole.PROMPT_HISTORY,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${GROK_HOME or ${HOME}/.grok}/sessions/<url_encoded_project>/prompt_history.jsonl"
        ),
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes=(
            "JSONL per-project user-prompt audit log. Keys: `timestamp` "
            "(ISO-8601 nanosecond), `session_id` (UUIDv7), `prompt` (text), "
            "`is_bash` (bool)."
        ),
        sample_record=(
            '{"timestamp":"2026-05-25T10:00:00.000000000Z",'
            '"session_id":"...","prompt":"<redacted>","is_bash":false}'
        ),
        search_by_default=True,
        discovery=(
            DiscoverySpec(
                store="grok.prompt_history",
                adapter_id="grok.prompt_history_jsonl.v1",
                path_kind="history_file",
                source_kind="jsonl",
                home_subpath=("sessions",),
                glob="prompt_history.jsonl",
            ),
        ),
    ),
    StoreDescriptor(
        agent="grok",
        store_id="grok.sessions",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${GROK_HOME or ${HOME}/.grok}/sessions/"
            "<url_encoded_project>/<session_uuid>/chat_history.jsonl"
        ),
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes=(
            "JSONL full session transcripts. `type` field discriminates "
            "system/user/assistant/tool_use/tool_result. `content` is text "
            "or content-blocks array. Includes tool calls and usage stats."
        ),
        sample_record=(
            '{"type":"user","content":"<redacted>","timestamp":"2026-05-25T10:00:01.000000000Z"}'
        ),
        distinguishes_from=("grok.prompt_history",),
        search_by_default=True,
        search_notes=(
            "Full per-session transcript with tool calls; "
            "`grok.prompt_history` is the user-prompts-only audit log."
        ),
        discovery=(
            DiscoverySpec(
                store="grok.sessions",
                adapter_id="grok.sessions_jsonl.v1",
                path_kind="session_file",
                source_kind="jsonl",
                home_subpath=("sessions",),
                glob="chat_history.jsonl",
            ),
        ),
    ),
    StoreDescriptor(
        agent="grok",
        store_id="grok.session_search",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.SQLITE,
        path_pattern="${GROK_HOME or ${HOME}/.grok}/sessions/session_search.sqlite",
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes=(
            "SQLite with FTS5. Table `session_docs`: session_id, cwd, "
            "updated_at (unix seconds), title (generated), content "
            "(full-text index), content_hash. Schema version 3."
        ),
        search_by_default=True,
        search_notes=(
            "Pre-indexed session titles and content for fast lookup. "
            "De-duplicate against `grok.sessions` by session_id."
        ),
        discovery=(
            DiscoverySpec(
                store="grok.session_search",
                adapter_id="grok.session_search_sqlite.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                home_subpath=("sessions",),
                files=("session_search.sqlite",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="grok",
        store_id="grok.subagents",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.JSON_OBJECT,
        path_pattern=(
            "${GROK_HOME or ${HOME}/.grok}/sessions/<url_encoded_project>/"
            "<session_uuid>/subagents/<subagent_uuid>/meta.json"
        ),
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes=(
            "Per-subagent dispatch record. One JSON object per delegated "
            "subagent: `prompt` (the delegated instruction), `description`, "
            "`subagent_type`, `tool_calls`, `turns`, and parent/child session "
            "linkage. The subagent's own turns are not persisted separately, so "
            "this `prompt` is the only searchable record of the delegation."
        ),
        sample_record=(
            '{"subagent_id":"...","parent_session_id":"...",'
            '"subagent_type":"...","description":"<redacted>",'
            '"prompt":"<redacted>","tool_calls":[]}'
        ),
        distinguishes_from=("grok.sessions",),
        search_by_default=True,
        search_notes=(
            "Subagent dispatch prompts are conversation content with no sibling "
            "transcript; parity with claude.projects.subagent and "
            "cursor-cli.subagent_transcripts."
        ),
        discovery=(
            DiscoverySpec(
                store="grok.subagents",
                adapter_id="grok.subagents_json.v1",
                path_kind="session_file",
                source_kind="json",
                home_subpath=("sessions",),
                glob="meta.json",
                path_parts_required=("subagents",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="grok",
        store_id="grok.sessions.events",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${GROK_HOME or ${HOME}/.grok}/sessions/"
            "<url_encoded_project>/<session_uuid>/events.jsonl"
        ),
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes=(
            "Per-session event stream with turn-level lifecycle events: "
            "turn_started, loop_started, phase_changed, tool_started, "
            "tool_finished. Schema version 1.0."
        ),
    ),
    StoreDescriptor(
        agent="grok",
        store_id="grok.sessions.summary",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern=(
            "${GROK_HOME or ${HOME}/.grok}/sessions/"
            "<url_encoded_project>/<session_uuid>/summary.json"
        ),
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes=(
            "Per-session summary: id, cwd, session_summary, created_at, "
            "updated_at, num_messages, current_model_id, git metadata, "
            "generated_title, agent_name."
        ),
    ),
    StoreDescriptor(
        agent="grok",
        store_id="grok.memory",
        role=StoreRole.PERSISTENT_MEMORY,
        format=StoreFormat.MARKDOWN_FRONTMATTER,
        path_pattern="${GROK_HOME or ${HOME}/.grok}/memory/**/MEMORY.md",
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes=(
            "Persistent memory Markdown managed by Grok's memory system. Covers "
            "the flat `memory/MEMORY.md` and the per-project "
            "`memory/<project_hash>/MEMORY.md` subtree; the companion "
            "`index.sqlite` FTS index of the same content is not separately "
            "enumerated. Inspectable opt-in."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="grok.memory",
                adapter_id="grok.memory_text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("memory",),
                glob="**/MEMORY.md",
            ),
        ),
    ),
    StoreDescriptor(
        agent="grok",
        store_id="grok.logs",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSONL,
        path_pattern="${GROK_HOME or ${HOME}/.grok}/logs/unified.jsonl",
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes=(
            "Structured application logs: ts, src, pid, lvl, msg, ctx. "
            "Debugging diagnostics, not chat content."
        ),
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="grok",
        store_id="grok.worktrees_db",
        role=StoreRole.APP_STATE,
        format=StoreFormat.SQLITE,
        path_pattern="${GROK_HOME or ${HOME}/.grok}/worktrees.db",
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes="SQLite database tracking git worktrees created by Grok.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="grok",
        store_id="grok.config",
        role=StoreRole.APP_STATE,
        format=StoreFormat.OPAQUE,
        path_pattern="${GROK_HOME or ${HOME}/.grok}/config.toml",
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes="TOML configuration file.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="grok",
        store_id="grok.plans",
        role=StoreRole.PLAN,
        format=StoreFormat.MARKDOWN_FRONTMATTER,
        path_pattern=(
            "${GROK_HOME or ${HOME}/.grok}/sessions/<url_encoded_project>/<session_uuid>/plan.md"
        ),
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes=(
            "Per-session plan-mode Markdown — the agent's working plan for the "
            "session. Inspectable, parity with claude.plans and "
            "cursor-cli.plans; not searched by default."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="grok.plans",
                adapter_id="grok.plans_text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("sessions",),
                glob="plan.md",
            ),
        ),
    ),
    StoreDescriptor(
        agent="grok",
        store_id="grok.sessions.system_prompt",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern=(
            "${GROK_HOME or ${HOME}/.grok}/sessions/<url_encoded_project>/"
            "<session_uuid>/system_prompt.txt"
        ),
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes=(
            "The rendered system prompt for the session (agent instructions "
            "plus injected context). Agent-side boilerplate, largely shared "
            "across sessions; documented for inventory, not searched."
        ),
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="grok",
        store_id="grok.sessions.prompt_context",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern=(
            "${GROK_HOME or ${HOME}/.grok}/sessions/<url_encoded_project>/"
            "<session_uuid>/prompt_context.json"
        ),
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes=(
            "Session prompt-context metadata: working_directory, "
            "agents_md_files, persona_summaries, os_name, current_date, "
            "prompt_mode, audience, version. Configuration, not chat."
        ),
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="grok",
        store_id="grok.sessions.hunk_records",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${GROK_HOME or ${HOME}/.grok}/sessions/<url_encoded_project>/"
            "<session_uuid>/hunk_records.jsonl"
        ),
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes=(
            "Edit-attribution JSONL (filePath, hunkStart/End, linesAdded/"
            "Removed, authorType, promptIndex, eventType). Code-change "
            "telemetry, no prompt payload; documented, not searched."
        ),
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="grok",
        store_id="grok.sessions.updates",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${GROK_HOME or ${HOME}/.grok}/sessions/<url_encoded_project>/"
            "<session_uuid>/updates.jsonl"
        ),
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes=(
            "ACP-style session/update notification stream (method, "
            "params.sessionId, update payloads). Protocol traffic, not chat; "
            "documented, not searched."
        ),
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="grok",
        store_id="grok.sessions.terminal",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern=(
            "${GROK_HOME or ${HOME}/.grok}/sessions/<url_encoded_project>/"
            "<session_uuid>/terminal/call-<id>.log"
        ),
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes=(
            "Per-tool-call terminal stdout/stderr logs (thousands per active "
            "project). Tool output, not chat, and high-volume; documented for "
            "inventory and deliberately not searched."
        ),
        search_by_default=False,
    ),
)
