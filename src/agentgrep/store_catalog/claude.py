"""claude store descriptors for the agentgrep catalogue."""

from __future__ import annotations

from agentgrep.store_catalog._common import _CLAUDE_OBSERVED_AT
from agentgrep.stores import (
    DiscoverySpec,
    StoreCoverage,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
    VersionDetectionStrategy,
)

_CLAUDE_STORES: tuple[StoreDescriptor, ...] = (
    StoreDescriptor(
        agent="claude",
        store_id="claude.history",
        role=StoreRole.PROMPT_HISTORY,
        format=StoreFormat.JSONL,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/history.jsonl",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes=(
            "Global prompt history JSONL. Each line carries `display`, "
            "`pastedContents`, `timestamp` (Unix milliseconds), `project`, and "
            "`sessionId`. Large text pastes may be content-addressed through "
            "`paste-cache/<contentHash>.txt`."
        ),
        sample_record=(
            '{"display":"<redacted>","pastedContents":{},'
            '"timestamp":1700000000000,"project":"<path>","sessionId":"..."}'
        ),
        distinguishes_from=("claude.projects.session",),
        search_by_default=True,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        search_notes=(
            "User-prompt audit log used by Claude Code prompt history; project "
            "transcripts remain the full conversation replay."
        ),
        discovery=(
            DiscoverySpec(
                store="claude.history",
                adapter_id="claude.history_jsonl.v1",
                data_version="claude.history_jsonl.log_entry.v1",
                path_kind="history_file",
                source_kind="jsonl",
                home_subpath=(),
                files=("history.jsonl",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.projects.session",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/projects/"
            "<encoded_project>/<session_uuid>.jsonl"
        ),
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        upstream_ref="code.claude.com/docs/en/changelog",
        schema_notes=(
            "JSONL; stream fragments grouped by `uuid`, dedup across `/resume`, skip "
            "`isCompactSummary: true`. Keys: `type`, `uuid`, `parentUuid`, `timestamp`, "
            "`sessionId`, `cwd`, `gitBranch`, `version`, `message.role`, "
            "`message.content[]` (`text`/`thinking`/`tool_use`/`tool_result`), "
            "`message.usage`."
        ),
        sample_record='{"type":"user","uuid":"...","timestamp":"2026-05-17T...","message":{"role":"user","content":[{"type":"text","text":"<redacted>"}]}}',
        search_by_default=True,
        version_strategies=(
            VersionDetectionStrategy.EMBEDDED_METADATA,
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="claude.projects",
                adapter_id="claude.projects_jsonl.v1",
                data_version="claude.projects_jsonl.message.v1",
                path_kind="session_file",
                source_kind="jsonl",
                home_subpath=("projects",),
                glob="*.jsonl",
                path_parts_excluded=("subagents",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.projects.subagent",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/projects/"
            "<encoded_project>/<session_uuid>/subagents/<agent>.jsonl"
        ),
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes=(
            "Same JSONL line format as the parent session. Each file is one sub-agent "
            "dispatch from the Task tool."
        ),
        distinguishes_from=("claude.projects.session",),
        search_by_default=True,
        version_strategies=(
            VersionDetectionStrategy.EMBEDDED_METADATA,
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        search_notes=(
            "Sub-agent transcripts are conversation content; de-duplicate with the "
            "parent session by `uuid`."
        ),
        discovery=(
            DiscoverySpec(
                store="claude.projects_subagents",
                adapter_id="claude.projects_jsonl.v1",
                data_version="claude.projects_jsonl.message.v1",
                path_kind="session_file",
                source_kind="jsonl",
                home_subpath=("projects",),
                glob="*.jsonl",
                path_parts_required=("subagents",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.projects.memory",
        role=StoreRole.PERSISTENT_MEMORY,
        format=StoreFormat.MARKDOWN_FRONTMATTER,
        path_pattern=(
            "${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/projects/<encoded_project>/memory/*.md"
        ),
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes=(
            "Markdown files with YAML frontmatter; the auto-memory feature. Each file "
            "holds one fact/feedback/project/reference memory."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="claude.projects.memory",
                adapter_id="claude.projects_memory_text.v1",
                data_version="claude.projects.memory.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("projects",),
                glob="**/memory/**/*.md",
                path_parts_excluded=(".git",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.memory_files",
        role=StoreRole.PERSISTENT_MEMORY,
        format=StoreFormat.TEXT,
        path_pattern=(
            "${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/CLAUDE.md and known "
            "project roots/{CLAUDE.md,.claude.md}"
        ),
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes=(
            "Persistent user and project memory Markdown loaded into Claude Code. "
            "Project-root files are discovered only from roots already referenced "
            "by local Claude project transcripts."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="claude.memory_files",
                adapter_id="claude.memory_text.v1",
                data_version="claude.memory.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                files=("CLAUDE.md",),
            ),
            DiscoverySpec(
                store="claude.memory_files",
                adapter_id="claude.memory_text.v1",
                data_version="claude.project_memory.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                root_key="claude_project",
                files=("CLAUDE.md", ".claude.md"),
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.tasks",
        role=StoreRole.TODO,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/tasks/<task_list>/<task_id>.json",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes=(
            "Claude Code task JSON with `id`, `subject`, `description`, `status`, "
            "`blocks`, `blockedBy`, and optional `activeForm` / metadata fields."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="claude.tasks",
                adapter_id="claude.tasks_json.v1",
                data_version="claude.tasks.json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("tasks",),
                glob="*.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.todos",
        role=StoreRole.TODO,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/todos/*.json",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Persistent todo lists keyed by agent UUID.",
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="claude.todos",
                adapter_id="claude.todos_json.v1",
                data_version="claude.todos.json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("todos",),
                glob="*.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.sessions",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/sessions/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Shell environment snapshots; rarely contains conversation text.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.sessions",
                adapter_id="claude.app_state_json_summary.v1",
                data_version="claude.sessions.json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("sessions",),
                glob="*.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.store_db",
        role=StoreRole.APP_STATE,
        format=StoreFormat.SQLITE,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/__store.db",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes=(
            "SQLite conversation/app state. Observed tables include "
            "`base_messages`, `user_messages`, `assistant_messages`, and "
            "`conversation_summaries`; message tables can duplicate JSONL "
            "transcript content, so this stays opt-in."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.store_db",
                adapter_id="claude.store_sqlite.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                files=("__store.db",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.plans",
        role=StoreRole.PLAN,
        format=StoreFormat.TEXT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/plans/*.md",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes=(
            "Markdown plans stored under the configured Claude Code root. "
            "Project settings may redirect plan creation, but this root is the "
            "global default observed in current installs."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.plans",
                adapter_id="claude.plans_text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("plans",),
                glob="*.md",
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.projects.workflows",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern=(
            "${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/projects/<encoded_project>/"
            "<session_uuid>/workflows/scripts/<name>.js"
        ),
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes=(
            "Orchestration-workflow driver scripts emitted by the Workflow "
            "tool, each carrying an embedded `meta = {name, description}` that "
            "names the workflow intent. Machine-generated driver code; "
            "inspectable opt-in, not searched by default."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.projects.workflows",
                adapter_id="claude.workflow_scripts_text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("projects",),
                glob="*.js",
                path_parts_required=("workflows", "scripts"),
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.usage_data",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/usage-data/facets/<session>.json",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes=(
            "Claude Code's own derived per-session reflection summaries. The "
            "readable natural-language fields are `brief_summary`, "
            "`underlying_goal`, and `friction_detail`; counts and category "
            "fields are metadata. Derived state, not transcript — inspectable "
            "opt-in, not searched by default."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.usage_data",
                adapter_id="claude.usage_facets_json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("usage-data", "facets"),
                glob="*.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.projects.session_memory",
        role=StoreRole.PERSISTENT_MEMORY,
        format=StoreFormat.TEXT,
        path_pattern=(
            "${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/projects/<encoded_project>/"
            "<session_uuid>/session-memory/summary.md"
        ),
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes=(
            "Per-session memory summary Markdown plus optional session-memory "
            "template/prompt configuration. It can contain prompt context but is "
            "not part of the default transcript search surface."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.projects_session_memory",
                adapter_id="claude.session_memory_text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("projects",),
                glob="summary.md",
                path_parts_required=("session-memory",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.settings",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/{settings*.json,keybindings.json}",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes=(
            "User configuration, local overrides, keybindings, policy limits, "
            "and update/auth cache state. Documented for inventory, not searched "
            "as prompt history."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="claude.settings",
                adapter_id="claude.settings_json.v1",
                data_version="claude.settings.json.v1",
                path_kind="store_file",
                source_kind="json",
                files=(
                    "settings.json",
                    "settings.local.json",
                    "keybindings.json",
                    "policy-limits.json",
                    "mcp-needs-auth-cache.json",
                ),
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.credentials",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/.credentials.json",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Authentication and account credentials. Documented but never enumerated.",
        coverage=StoreCoverage.PRIVATE,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.update_state",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/{.last-*,.last-update-result.json}",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Update and cleanup marker files; not prompt history.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.update_state",
                adapter_id="claude.app_state_json_summary.v1",
                data_version="claude.update_state.json.v1",
                path_kind="store_file",
                source_kind="json",
                files=(".last-update-result.json",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.stats_cache",
        role=StoreRole.CACHE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/stats-cache.json",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Cached usage and statistics state; not conversation content.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.stats_cache",
                adapter_id="claude.app_state_json_summary.v1",
                data_version="claude.stats_cache.json.v3",
                path_kind="store_file",
                source_kind="json",
                files=("stats-cache.json",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.debug_logs",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/debug/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Debug output and diagnostics. Catalogued separately from transcripts.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.debug_logs",
                adapter_id="claude.file_metadata_summary.v1",
                data_version="claude.debug_logs.text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("debug",),
                glob="*",
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.backups",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/backups/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Backup files retained by Claude Code; opaque inventory only.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.generic_cache",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/cache/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="General cache directory; not a prompt/history source.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.file_history",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/file-history/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes=(
            "Content-addressed file-history backups referenced by transcript "
            "snapshots. This can be large and is not conversation history."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.shell_snapshots",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/shell-snapshots/*.sh",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Shell integration snapshots; runtime state rather than prompt history.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.shell_snapshots",
                adapter_id="claude.file_metadata_summary.v1",
                data_version="claude.shell_snapshot.text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("shell-snapshots",),
                glob="*.sh",
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.image_cache",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/image-cache/<session>/<image>",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Base64/image payload cache for pasted image references.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.context_mode",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/context-mode/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Context-mode counters and local app state, sometimes backed by SQLite.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.context_mode",
                adapter_id="claude.app_state_json_summary.v1",
                data_version="claude.context_mode.json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("context-mode",),
                glob="*.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.security_state",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern=(
            "${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/{security/,security_warnings_state_*.json}"
        ),
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Security warning and repository baseline state.",
        coverage=StoreCoverage.PRIVATE,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.skills",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.TEXT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/skills/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="User and managed skill instructions loaded into Claude Code behavior.",
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="claude.skills",
                adapter_id="claude.skills_text.v1",
                data_version="claude.skills.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("skills",),
                glob="*.md",
                path_parts_excluded=(".git",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.commands",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.TEXT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/commands/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Legacy custom slash-command Markdown loaded through the skill loader.",
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="claude.commands",
                adapter_id="claude.commands_text.v1",
                data_version="claude.commands.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("commands",),
                glob="*.md",
                path_parts_excluded=(".git",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.project_instructions",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.TEXT,
        path_pattern="${HOME}/<known_project_root>/.claude/{commands,agents,skills}/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes=(
            "Project-local commands, agents, and skills. Roots are bounded to "
            "projects already observed in local Claude transcript metadata."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="claude.project_instructions",
                adapter_id="claude.project_instruction_text.v1",
                data_version="claude.project_commands.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                root_key="claude_project",
                home_subpath=(".claude", "commands"),
                glob="*.md",
                path_parts_excluded=(".git",),
            ),
            DiscoverySpec(
                store="claude.project_instructions",
                adapter_id="claude.project_instruction_text.v1",
                data_version="claude.project_agents.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                root_key="claude_project",
                home_subpath=(".claude", "agents"),
                glob="*.md",
                path_parts_excluded=(".git",),
            ),
            DiscoverySpec(
                store="claude.project_instructions",
                adapter_id="claude.project_instruction_text.v1",
                data_version="claude.project_skills.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                root_key="claude_project",
                home_subpath=(".claude", "skills"),
                glob="*.md",
                path_parts_excluded=(".git",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.teams",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/teams/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Team config JSON with member prompts and coordination metadata.",
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="claude.teams",
                adapter_id="claude.teams_json.v1",
                data_version="claude.teams.config_json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("teams",),
                glob="config.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.session_env",
        role=StoreRole.APP_STATE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/session-env/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Environment state captured for active or resumed sessions.",
        coverage=StoreCoverage.PRIVATE,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.ide",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/ide/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="IDE integration state and bridge metadata.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.ide",
                adapter_id="claude.app_state_json_summary.v1",
                data_version="claude.ide.json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("ide",),
                glob="*.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.chrome",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/chrome/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Claude-in-Chrome native-host and browser bridge state.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.chrome",
                adapter_id="claude.app_state_json_summary.v1",
                data_version="claude.chrome.json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("chrome",),
                glob="*.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.native_install",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/local/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Native installer and local binary-management state.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.native_install",
                adapter_id="claude.app_state_json_summary.v1",
                data_version="claude.native_install.json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("local",),
                glob="*.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.jobs",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/jobs/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Background job state and metadata, summarized without raw values.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.jobs",
                adapter_id="claude.app_state_json_summary.v1",
                data_version="claude.jobs.json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("jobs",),
                glob="*.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.uploads",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/uploads/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Attachment upload staging; documented as opaque cache inventory.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.paste_cache",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/paste-cache/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes="Transient clipboard staging.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.plugins_cache",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/plugins/cache/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.185",
        observed_at=_CLAUDE_OBSERVED_AT,
        schema_notes=(
            "Installed plugin bundles. Manifest, command, agent, skill, and hook "
            "instruction surfaces are inspectable; runtime/cache payloads remain opaque."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="claude.plugins_cache",
                adapter_id="claude.plugin_manifest_json.v1",
                data_version="claude.plugin_manifest.json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("plugins", "cache"),
                glob="plugin.json",
                path_parts_required=(".claude-plugin",),
                path_parts_excluded=(".git",),
            ),
            DiscoverySpec(
                store="claude.plugins_cache",
                adapter_id="claude.plugin_instruction_text.v1",
                data_version="claude.plugin_instruction.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("plugins", "cache"),
                glob="*.md",
                path_parts_required=("commands",),
                path_parts_excluded=(".git",),
            ),
            DiscoverySpec(
                store="claude.plugins_cache",
                adapter_id="claude.plugin_instruction_text.v1",
                data_version="claude.plugin_instruction.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("plugins", "cache"),
                glob="*.md",
                path_parts_required=("agents",),
                path_parts_excluded=(".git",),
            ),
            DiscoverySpec(
                store="claude.plugins_cache",
                adapter_id="claude.plugin_instruction_text.v1",
                data_version="claude.plugin_instruction.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("plugins", "cache"),
                glob="*.md",
                path_parts_required=("skills",),
                path_parts_excluded=(".git",),
            ),
            DiscoverySpec(
                store="claude.plugins_cache",
                adapter_id="claude.plugin_hooks_json.v1",
                data_version="claude.plugin_hooks.json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("plugins", "cache"),
                glob="hooks.json",
                path_parts_required=("hooks",),
                path_parts_excluded=(".git",),
            ),
        ),
    ),
)
