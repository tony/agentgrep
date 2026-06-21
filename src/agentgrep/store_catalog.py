"""Concrete catalogue of agentgrep's known stores.

The catalogue declares *where* each store lives and *what* its records look
like. Whether agentgrep searches a store by default is a separate decision —
each :class:`~agentgrep.stores.StoreDescriptor` carries a
``search_by_default`` field that the per-agent discover functions consult.
``None`` means the decision is deliberately deferred; see the per-store
``search_notes``.

Every entry stamps an ``observed_version`` and ``observed_at`` so future
readers can tell whether the schema notes are still current. When upstream
renames a path or changes a key, bump the entry's stamps and the catalogue's
``catalog_version``.
"""

from __future__ import annotations

import datetime
import hashlib
import pathlib

from agentgrep.stores import (
    DiscoverySpec,
    StoreCatalog,
    StoreCoverage,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
    VersionDetectionStrategy,
)

OBSERVED_AT = datetime.date(2026, 5, 17)
_GROK_OBSERVED_AT = datetime.date(2026, 6, 21)
_CLAUDE_OBSERVED_AT = datetime.date(2026, 6, 21)
_CURSOR_IDE_OBSERVED_AT = datetime.date(2026, 6, 21)
_PI_OBSERVED_AT = datetime.date(2026, 6, 21)
_OPENCODE_OBSERVED_AT = datetime.date(2026, 6, 21)
_ANTIGRAVITY_OBSERVED_AT = datetime.date(2026, 6, 21)
_GEMINI_OBSERVED_AT = datetime.date(2026, 6, 21)
_CURSOR_CLI_OBSERVED_AT = datetime.date(2026, 6, 21)
_CODEX_OBSERVED_AT = datetime.date(2026, 6, 21)


def gemini_project_hash(project_root: pathlib.Path) -> str:
    """Reproduce Gemini CLI's project-hash derivation.

    Mirrors the ``getProjectHash`` helper at
    ``packages/core/src/utils/paths.ts:187-189`` in
    ``github.com/google-gemini/gemini-cli`` (HEAD ``927170fc``):

    .. code-block:: typescript

       export function getProjectHash(projectRoot: string): string {
         return crypto.createHash('sha256').update(projectRoot).digest('hex');
       }

    Parameters
    ----------
    project_root : pathlib.Path
        Absolute project root path.

    Returns
    -------
    str
        Lower-case hex SHA-256 of the absolute path string.

    Examples
    --------
    >>> gemini_project_hash(pathlib.Path("/example"))
    '99d0533064c83d0483dc07145a0aa887cb104311dac8cc2ca57843c6723a5b69'
    """
    return hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()


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


_CURSOR_CLI_STORES: tuple[StoreDescriptor, ...] = (
    StoreDescriptor(
        agent="cursor-cli",
        store_id="cursor-cli.transcripts",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${HOME}/.cursor/projects/<id>/agent-transcripts/<session_uuid>/<session_uuid>.jsonl"
        ),
        observed_version="cursor-agent 2026.06.19-653a7fb",
        observed_at=_CURSOR_CLI_OBSERVED_AT,
        upstream_ref="cursor.com/docs/cli/overview",
        schema_notes=(
            "JSONL Anthropic-style: `role`, `message.content[]` with "
            "`text`/`tool_use`/`tool_result`. No native timestamp — agentgrep "
            "infers from the file's mtime. Tool outputs sometimes `[REDACTED]` "
            "in older `cursor-agent` versions."
        ),
        sample_record=(
            '{"role":"user","message":{"content":'
            '[{"type":"text","text":"<user_query>...</user_query>"}]}}'
        ),
        distinguishes_from=("cursor-ide.state_vscdb", "cursor-cli.chats"),
        search_by_default=True,
        search_notes=(
            "Parsed by agentgrep via `parse_cursor_cli_transcript` "
            "(`cursor_cli.transcripts_jsonl.v1`)."
        ),
        discovery=(
            DiscoverySpec(
                store="cursor-cli.transcripts",
                adapter_id="cursor_cli.transcripts_jsonl.v1",
                path_kind="session_file",
                source_kind="jsonl",
                home_subpath=(".cursor", "projects"),
                glob="*.jsonl",
                path_parts_required=("agent-transcripts",),
                path_parts_excluded=("subagents",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="cursor-cli",
        store_id="cursor-cli.subagent_transcripts",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${HOME}/.cursor/projects/<id>/agent-transcripts/<session_uuid>/subagents/<agent>.jsonl"
        ),
        observed_version="cursor-agent 2026.06.19-653a7fb",
        observed_at=_CURSOR_CLI_OBSERVED_AT,
        schema_notes=(
            "Same JSONL Anthropic-style shape as `cursor-cli.transcripts`, nested "
            "under a session's `subagents/` directory."
        ),
        distinguishes_from=("cursor-cli.transcripts",),
        search_by_default=True,
        search_notes="Subagent transcript files are conversation content, not primary sessions.",
        discovery=(
            DiscoverySpec(
                store="cursor-cli.subagent_transcripts",
                adapter_id="cursor_cli.transcripts_jsonl.v1",
                path_kind="session_file",
                source_kind="jsonl",
                home_subpath=(".cursor", "projects"),
                glob="*.jsonl",
                path_parts_required=("agent-transcripts", "subagents"),
            ),
        ),
    ),
    StoreDescriptor(
        agent="cursor-cli",
        store_id="cursor-cli.repo_meta",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${HOME}/.cursor/projects/<id>/repo.json",
        observed_version="cursor-agent 2026.06.19-653a7fb",
        observed_at=_CURSOR_CLI_OBSERVED_AT,
        schema_notes="Project tree/manifest metadata.",
    ),
    StoreDescriptor(
        agent="cursor-cli",
        store_id="cursor-cli.tools",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern=(
            "${HOME}/.cursor/projects/<id>/{mcps/*/SERVER_METADATA.json,"
            "tools/*.json,mcp-approvals.json}"
        ),
        observed_version="cursor-agent 2026.06.19-653a7fb",
        observed_at=_CURSOR_CLI_OBSERVED_AT,
        schema_notes="MCP tool registry and approval records.",
    ),
    StoreDescriptor(
        agent="cursor-cli",
        store_id="cursor-cli.terminals",
        role=StoreRole.APP_STATE,
        format=StoreFormat.OPAQUE,
        path_pattern="${HOME}/.cursor/projects/<id>/terminals/",
        observed_version="cursor-agent 2026.06.19-653a7fb",
        observed_at=_CURSOR_CLI_OBSERVED_AT,
        schema_notes="Terminal output logs.",
    ),
    StoreDescriptor(
        agent="cursor-cli",
        store_id="cursor-cli.canvases",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${HOME}/.cursor/projects/<id>/canvases/",
        observed_version="cursor-agent 2026.06.19-653a7fb",
        observed_at=_CURSOR_CLI_OBSERVED_AT,
        schema_notes="Cursor canvas state.",
    ),
    StoreDescriptor(
        agent="cursor-cli",
        store_id="cursor-cli.plans",
        role=StoreRole.PLAN,
        format=StoreFormat.MARKDOWN_FRONTMATTER,
        path_pattern="${HOME}/.cursor/plans/*.plan.md",
        observed_version="cursor-agent 2026.06.19-653a7fb",
        observed_at=_CURSOR_CLI_OBSERVED_AT,
        schema_notes=("YAML frontmatter (name, overview, todos[], isProject) plus markdown body."),
    ),
    StoreDescriptor(
        agent="cursor-cli",
        store_id="cursor-cli.state",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${HOME}/.cursor/agent-cli-state.json",
        observed_version="cursor-agent 2026.06.19-653a7fb",
        observed_at=_CURSOR_CLI_OBSERVED_AT,
        schema_notes="UI tip-shown flags and legacy-cleanup markers.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="cursor-cli",
        store_id="cursor-cli.worktrees",
        role=StoreRole.SOURCE_TREE,
        format=StoreFormat.OPAQUE,
        path_pattern="${HOME}/.cursor/worktrees/",
        observed_version="cursor-agent 2026.06.19-653a7fb",
        observed_at=_CURSOR_CLI_OBSERVED_AT,
        schema_notes=(
            "Full git worktrees used as code context by the CLI agent. Not chat — "
            "catalogued so future adapter PRs do not index source code as history."
        ),
        search_by_default=False,
        search_notes="Source trees, not transcripts; would drown real hits.",
    ),
    StoreDescriptor(
        agent="cursor-cli",
        store_id="cursor-cli.ai_tracking",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.SQLITE,
        path_pattern="${HOME}/.cursor/ai-tracking/ai-code-tracking.db",
        observed_version="cursor-agent 2026.06.19-653a7fb",
        observed_at=_CURSOR_CLI_OBSERVED_AT,
        schema_notes=(
            "SQLite with `conversation_summaries(conversationId, title, tldr, "
            "overview, summaryBullets, model, mode, updatedAt)` — title and prose "
            "summaries of CLI agent chats, not raw transcripts. Some installs have "
            "the table empty even when the CLI agent runs — the tracker may be "
            "disabled or unused; agentgrep tolerates that silently."
        ),
        search_by_default=True,
        discovery=(
            DiscoverySpec(
                store="cursor-cli.ai_tracking",
                adapter_id="cursor_cli.ai_tracking_sqlite.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                home_subpath=(".cursor", "ai-tracking"),
                files=("ai-code-tracking.db",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="cursor-cli",
        store_id="cursor-cli.prompt_history",
        role=StoreRole.PROMPT_HISTORY,
        format=StoreFormat.JSON_ARRAY,
        path_pattern="${HOME}/.config/cursor/prompt_history.json",
        observed_version="cursor-agent 2026.06.19-653a7fb",
        observed_at=_CURSOR_CLI_OBSERVED_AT,
        schema_notes=(
            "Flat JSON array of strings — one entry per prompt typed into "
            "`cursor-agent`, oldest first. The CLI's up-arrow recall buffer; "
            "no per-entry timestamps, so agentgrep stamps records with the "
            "file mtime. Lives under the lowercase `~/.config/cursor` home, "
            "separate from the `~/.cursor` transcript tree."
        ),
        sample_record='["continue", "run the tests", "<redacted>"]',
        distinguishes_from=("cursor-cli.transcripts", "cursor-cli.chats"),
        search_by_default=True,
        search_notes=(
            "Cursor's prompt-history store, parity with `claude.history` / "
            "`codex.history` / `grok.prompt_history`. Parsed via "
            "`parse_cursor_prompt_history` (`cursor_cli.prompt_history_json.v1`)."
        ),
        discovery=(
            DiscoverySpec(
                store="cursor-cli.prompt_history",
                adapter_id="cursor_cli.prompt_history_json.v1",
                path_kind="history_file",
                source_kind="json",
                home_subpath=(".config", "cursor"),
                files=("prompt_history.json",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="cursor-cli",
        store_id="cursor-cli.chats",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.SQLITE,
        path_pattern="${HOME}/.config/cursor/chats/<project_hash>/<session_uuid>/store.db",
        observed_version="cursor-agent 2026.06.19-653a7fb",
        observed_at=_CURSOR_CLI_OBSERVED_AT,
        version_strategies=(VersionDetectionStrategy.CATALOG_OBSERVATION,),
        upstream_ref="agentgrep.parse_cursor_cli_chats_db / iter_protobuf_text_fields",
        schema_notes=(
            "Per-session SQLite with `meta(key, value)` and `blobs(id, data)` "
            "tables. `meta` holds session metadata (`agentId`, "
            "`latestRootBlobId`); `blobs` holds content-addressed protobuf "
            "messages forming a Merkle graph from the root blob. Cursor "
            "publishes no schema, so agentgrep walks the protobuf wire format "
            "generically and surfaces readable UTF-8 runs — best-effort and "
            "date-versioned, not an official format."
        ),
        distinguishes_from=("cursor-cli.transcripts",),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        search_notes=(
            "Opt-in (inspectable), not searched by default: the protobuf "
            "extraction is best-effort and overlaps the cleaner "
            "`cursor-cli.transcripts` JSONL. Parsed via "
            "`parse_cursor_cli_chats_db` (`cursor_cli.chats_protobuf.v1`) when "
            "the store is explicitly included."
        ),
        discovery=(
            DiscoverySpec(
                store="cursor-cli.chats",
                adapter_id="cursor_cli.chats_protobuf.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                home_subpath=(".config", "cursor", "chats"),
                glob="*/*/store.db",
            ),
        ),
    ),
    StoreDescriptor(
        agent="cursor-cli",
        store_id="cursor-cli.skills",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.TEXT,
        path_pattern="${HOME}/.cursor/{skills,skills-cursor}/<skill>/SKILL.md",
        observed_version="cursor-agent 2026.06.19-653a7fb",
        observed_at=_CURSOR_CLI_OBSERVED_AT,
        schema_notes=(
            "Skill definitions installed for cursor-agent — `SKILL.md` files "
            "with YAML frontmatter under `~/.cursor/skills/` (user) and "
            "`~/.cursor/skills-cursor/` (built-in). Instruction content that "
            "steers future sessions; inspectable, parity with claude.skills."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="cursor-cli.skills",
                adapter_id="cursor_cli.skills_text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=(".cursor",),
                glob="SKILL.md",
            ),
        ),
    ),
    StoreDescriptor(
        agent="cursor-cli",
        store_id="cursor-cli.uploads",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.TEXT,
        path_pattern="${HOME}/.cursor/projects/<id>/uploads/<name>.md",
        observed_version="cursor-agent 2026.06.19-653a7fb",
        observed_at=_CURSOR_CLI_OBSERVED_AT,
        schema_notes=(
            "User-uploaded Markdown attachments the user fed the agent as "
            "conversation input (plan/reference extracts). Inspectable opt-in "
            "supplementary content, not searched by default."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="cursor-cli.uploads",
                adapter_id="cursor_cli.uploads_text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=(".cursor", "projects"),
                glob="*.md",
                path_parts_required=("uploads",),
            ),
        ),
    ),
)

_CURSOR_IDE_STORES: tuple[StoreDescriptor, ...] = (
    StoreDescriptor(
        agent="cursor-ide",
        store_id="cursor-ide.state_vscdb",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.SQLITE,
        path_pattern="${HOME}/.config/Cursor/User/globalStorage/state.vscdb",
        platform_variants={
            "darwin": "${HOME}/Library/Application Support/Cursor/User/globalStorage/state.vscdb",
            "win32": "%APPDATA%/Cursor/User/globalStorage/state.vscdb",
        },
        observed_version="Cursor IDE (current observed paths)",
        observed_at=_CURSOR_IDE_OBSERVED_AT,
        upstream_ref=("agentgrep.parse_cursor_state_db / CURSOR_STATE_TOKENS"),
        schema_notes=(
            "Cursor IDE chat storage; keys in `ItemTable`/`cursorDiskKV` containing "
            "`chat`/`composer`/`prompt`/`history` tokens hold conversation JSON. "
            "Cursor does not publish a formal schema — agentgrep's parser is the "
            "reference implementation."
        ),
        sample_record=(
            "ItemTable row: key='workbench.panel.aichat.view...prompts', "
            'value=\'{"prompts":[{"text":"<redacted>","commandType":1}]}\''
        ),
        distinguishes_from=("cursor-cli.transcripts", "cursor-ide.workspace_state"),
        search_notes=(
            "Cursor IDE store, parsed by the current `cursor_ide.state_vscdb_modern.v1` "
            "adapter. Not the same as the Cursor CLI agent transcripts."
        ),
        search_by_default=True,
        discovery=(
            DiscoverySpec(
                store="cursor-ide.state_vscdb",
                adapter_id="cursor_ide.state_vscdb_modern.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                platform_paths=(
                    "~/.config/Cursor/User/globalStorage/state.vscdb",
                    "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb",
                    "~/AppData/Roaming/Cursor/User/globalStorage/state.vscdb",
                ),
            ),
            DiscoverySpec(
                store="cursor-ide.state_vscdb",
                adapter_id="cursor_ide.state_vscdb_legacy.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                home_subpath=(".cursor",),
                files=("state.vscdb",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="cursor-ide",
        store_id="cursor-ide.workspace_state",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.SQLITE,
        path_pattern="${HOME}/.config/Cursor/User/workspaceStorage/<hash>/state.vscdb",
        platform_variants={
            "darwin": (
                "${HOME}/Library/Application Support/Cursor/User/workspaceStorage/"
                "<hash>/state.vscdb"
            ),
            "win32": "%APPDATA%/Cursor/User/workspaceStorage/<hash>/state.vscdb",
        },
        observed_version="Cursor IDE (current observed paths)",
        observed_at=_CURSOR_IDE_OBSERVED_AT,
        upstream_ref=("agentgrep.parse_cursor_state_db / CURSOR_STATE_TOKENS"),
        schema_notes=(
            "Per-workspace `state.vscdb`, one per opened project under "
            "`workspaceStorage/<hash>/`. Same `ItemTable` shape as the global "
            "store; the `aiService.prompts` key holds that workspace's prompt "
            "history. Reuses the `cursor_ide.state_vscdb_modern.v1` adapter."
        ),
        distinguishes_from=("cursor-ide.state_vscdb",),
        search_notes=(
            "Per-workspace IDE history, complementing the global "
            "`cursor-ide.state_vscdb`. Parsed by the shared "
            "`cursor_ide.state_vscdb_modern.v1` adapter."
        ),
        search_by_default=True,
        discovery=(
            DiscoverySpec(
                store="cursor-ide.workspace_state",
                adapter_id="cursor_ide.state_vscdb_modern.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                glob="*/state.vscdb",
                root_key="ide_workspace",
            ),
        ),
    ),
)


_CODEX_STORES: tuple[StoreDescriptor, ...] = (
    StoreDescriptor(
        agent="codex",
        store_id="codex.history",
        role=StoreRole.PROMPT_HISTORY,
        format=StoreFormat.JSONL,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/history.jsonl",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        upstream_ref=("github.com/openai/codex@3fb81667/codex-rs/message-history/src/lib.rs#L56"),
        schema_notes=(
            "`HistoryEntry { session_id: String, ts: u64 (unix seconds), text: "
            "String }` — one record per user prompt, append-only across all threads."
        ),
        sample_record='{"session_id":"...","ts":1747509826,"text":"<redacted>"}',
        distinguishes_from=("codex.sessions",),
        search_by_default=True,
        version_strategies=(
            VersionDetectionStrategy.VERSION_CHECK,
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.history",
                adapter_id="codex.history_json.v1",
                data_version="codex.history_json.legacy",
                path_kind="history_file",
                source_kind="json",
                files=("history.json",),
            ),
            DiscoverySpec(
                store="codex.history",
                adapter_id="codex.history_jsonl.v1",
                data_version="codex.history_jsonl.current",
                path_kind="history_file",
                source_kind="jsonl",
                files=("history.jsonl",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.sessions",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${CODEX_HOME or ${HOME}/.codex}/sessions/YYYY/MM/DD/"
            "rollout-YYYY-MM-DDThh-mm-ss-<uuid>.jsonl"
        ),
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        upstream_ref=("github.com/openai/codex@3fb81667/codex-rs/protocol/src/protocol.rs#L2929"),
        schema_notes=(
            "JSONL `RolloutItem` tagged enum (`type` + `payload`): "
            "`session_meta` | `response_item` | `compacted` | `turn_context` | "
            "`event_msg`. First line is a `SessionMetaLine` with `id`, `timestamp`, "
            "`cwd`, `cli_version`, optional `git` info. Older root-level "
            "`sessions/rollout-*.json` files are JSON objects with `session` "
            "metadata and an `items` array of message-like records."
        ),
        sample_record='{"type":"response_item","payload":{"role":"user","content":"<redacted>"}}',
        distinguishes_from=("codex.history",),
        search_by_default=True,
        search_notes=(
            "Full per-thread transcript with tool calls; `codex.history` is the "
            "user-prompts-only audit log."
        ),
        version_strategies=(
            VersionDetectionStrategy.EMBEDDED_METADATA,
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.sessions",
                adapter_id="codex.sessions_jsonl.v1",
                data_version="codex.sessions.rollout.v1",
                path_kind="session_file",
                source_kind="jsonl",
                home_subpath=("sessions",),
                glob="*.jsonl",
            ),
            DiscoverySpec(
                store="codex.sessions",
                adapter_id="codex.sessions_legacy_json.v1",
                data_version="codex.sessions.legacy_json.v1",
                path_kind="session_file",
                source_kind="json",
                home_subpath=("sessions",),
                glob="rollout-*.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.session_index",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSONL,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/session_index.jsonl",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        upstream_ref="github.com/openai/codex@3fb81667/codex-rs/rollout/src/session_index.rs",
        schema_notes=(
            "Append-only JSONL session index with `id`, `thread_name`, and "
            "`updated_at`. It summarizes sessions but does not replace the full "
            "`sessions/**/*.jsonl` transcripts."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.session_index",
                adapter_id="codex.session_index_jsonl.v1",
                data_version="codex.session_index.jsonl.v1",
                path_kind="store_file",
                source_kind="jsonl",
                files=("session_index.jsonl",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.state_db",
        role=StoreRole.APP_STATE,
        format=StoreFormat.SQLITE,
        path_pattern="${CODEX_SQLITE_HOME or ${CODEX_HOME or ${HOME}/.codex}}/state_5.sqlite",
        env_overrides=("CODEX_HOME", "CODEX_SQLITE_HOME"),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        upstream_ref="github.com/openai/codex@3fb81667/codex-rs/state/src/lib.rs#L95",
        schema_notes=(
            "Codex state DB; schema managed via migrations. Observed prompt-bearing "
            "columns include `threads.first_user_message`, `threads.preview`, "
            "`threads.title`, and `agent_jobs.instruction`."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.state_db",
                adapter_id="codex.state_sqlite.v1",
                data_version="codex.state.sqlite.v5",
                path_kind="sqlite_db",
                source_kind="sqlite",
                root_key="codex_sqlite",
                files=("state_5.sqlite",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.logs_db",
        role=StoreRole.APP_STATE,
        format=StoreFormat.SQLITE,
        path_pattern="${CODEX_SQLITE_HOME or ${CODEX_HOME or ${HOME}/.codex}}/logs_2.sqlite",
        env_overrides=("CODEX_HOME", "CODEX_SQLITE_HOME"),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        upstream_ref="github.com/openai/codex@3fb81667/codex-rs/state/src/lib.rs#L92",
        schema_notes=(
            "Codex logs DB (`LOGS_DB_FILENAME` in `codex-rs/state/src/lib.rs`). "
            "The `_N.sqlite` files at the Codex root (`logs_2.sqlite`, "
            "`state_5.sqlite`) belong to the Codex CLI, not Cursor."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.logs_db",
                adapter_id="codex.logs_sqlite.v1",
                data_version="codex.logs.sqlite.v2",
                path_kind="sqlite_db",
                source_kind="sqlite",
                root_key="codex_sqlite",
                files=("logs_2.sqlite",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.memories_db",
        role=StoreRole.PERSISTENT_MEMORY,
        format=StoreFormat.SQLITE,
        path_pattern="${CODEX_SQLITE_HOME or ${CODEX_HOME or ${HOME}/.codex}}/memories_1.sqlite",
        env_overrides=("CODEX_HOME", "CODEX_SQLITE_HOME"),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes=(
            "SQLite memory pipeline state. Observed tables include `stage1_outputs` "
            "with raw memory, rollout summary, slug, usage, and selection columns."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.memories_db",
                adapter_id="codex.memories_sqlite.v1",
                data_version="codex.memories.sqlite.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                root_key="codex_sqlite",
                files=("memories_1.sqlite",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.goals_db",
        role=StoreRole.PLAN,
        format=StoreFormat.SQLITE,
        path_pattern="${CODEX_SQLITE_HOME or ${CODEX_HOME or ${HOME}/.codex}}/goals_1.sqlite",
        env_overrides=("CODEX_HOME", "CODEX_SQLITE_HOME"),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes=(
            "SQLite goal tracker with `thread_goals(objective, status, "
            "token_budget, tokens_used, time_used_seconds, created_at_ms, "
            "updated_at_ms)`."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.goals_db",
                adapter_id="codex.goals_sqlite.v1",
                data_version="codex.goals.sqlite.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                root_key="codex_sqlite",
                files=("goals_1.sqlite",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.external_agent_imports",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/external_agent_session_imports.json",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        upstream_ref="github.com/openai/codex@3fb81667/codex-rs/external-agent-sessions/src/ledger.rs",
        schema_notes=(
            "Import ledger with `records` carrying source path, content hash, "
            "imported thread id, and import timestamp."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.external_agent_imports",
                adapter_id="codex.external_imports_json.v1",
                data_version="codex.external_imports.json.v1",
                path_kind="store_file",
                source_kind="json",
                files=("external_agent_session_imports.json",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.instructions",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.TEXT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/instructions.md",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="User-level Codex instructions loaded into new sessions.",
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.instructions",
                adapter_id="codex.instructions_text.v1",
                path_kind="store_file",
                source_kind="text",
                files=("instructions.md",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.memories",
        role=StoreRole.PERSISTENT_MEMORY,
        format=StoreFormat.MARKDOWN_FRONTMATTER,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/memories/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes=(
            "Persistent memory workspace: `MEMORY.md`, `memory_summary.md`, "
            "`raw_memories.md`, rollout summaries, and skill memory folders."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.memories",
                adapter_id="codex.memories_text.v1",
                data_version="codex.memories.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("memories",),
                glob="*.md",
                path_parts_excluded=(".git",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.config",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/{config.toml,*.toml}",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes=(
            "Codex TOML configuration, including optional `sqlite_home`. "
            "It can affect discovery roots but is not prompt history."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.config",
                adapter_id="codex.config_toml.v1",
                data_version="codex.config.toml.v1",
                path_kind="store_file",
                source_kind="text",
                files=("config.toml",),
            ),
            DiscoverySpec(
                store="codex.config",
                adapter_id="codex.config_toml.v1",
                data_version="codex.managed_config.toml.v1",
                path_kind="store_file",
                source_kind="text",
                files=("managed_config.toml",),
            ),
            DiscoverySpec(
                store="codex.config",
                adapter_id="codex.config_toml.v1",
                data_version="codex.environments.toml.v1",
                path_kind="store_file",
                source_kind="text",
                files=("environments.toml",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.config_backups",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/{config.toml.bak*,config.toml.backup-*}",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Historical config backups. Catalogued separately from active config.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.config_backups",
                adapter_id="codex.config_backup_toml.v1",
                data_version="codex.config_backup.toml.v1",
                path_kind="store_file",
                source_kind="text",
                glob="config.toml.bak*",
            ),
            DiscoverySpec(
                store="codex.config_backups",
                adapter_id="codex.config_backup_toml.v1",
                data_version="codex.config_backup.toml.v1",
                path_kind="store_file",
                source_kind="text",
                glob="config.toml.backup-*",
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.auth",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/auth.json",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Authentication state. Documented but never enumerated or searched.",
        coverage=StoreCoverage.PRIVATE,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.installation_id",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/installation_id",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Stable local installation identifier. Documented but never enumerated.",
        coverage=StoreCoverage.PRIVATE,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.secrets",
        role=StoreRole.APP_STATE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/secrets/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Local secret store. Documented but never enumerated or searched.",
        coverage=StoreCoverage.PRIVATE,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.env",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/.env",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Environment file for local runtime configuration. Private inventory only.",
        coverage=StoreCoverage.PRIVATE,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.update_check",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/update-check.json",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Update-check cache metadata; not prompt history.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.update_check",
                adapter_id="codex.app_state_json_summary.v1",
                data_version="codex.update_check.json.v1",
                path_kind="store_file",
                source_kind="json",
                files=("update-check.json",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.version_file",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/version.json",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Local app-version cache used as an inventory hint.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.version_file",
                adapter_id="codex.app_state_json_summary.v1",
                data_version="codex.version_file.json.v1",
                path_kind="store_file",
                source_kind="json",
                files=("version.json",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.personality_migration",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/.personality_migration",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Migration marker for Codex personality defaults; not prompt history.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.personality_migration",
                adapter_id="codex.file_metadata_summary.v1",
                data_version="codex.personality_migration.marker.v1",
                path_kind="store_file",
                source_kind="text",
                files=(".personality_migration",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.model_cache",
        role=StoreRole.CACHE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/models_cache.json",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Cached model metadata: client version, ETag, fetch time, and models.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.model_cache",
                adapter_id="codex.app_state_json_summary.v1",
                data_version="codex.model_cache.json.v1",
                path_kind="store_file",
                source_kind="json",
                files=("models_cache.json",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.internal_storage",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/internal_storage.json",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Small app flags and migration markers.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.internal_storage",
                adapter_id="codex.app_state_json_summary.v1",
                data_version="codex.internal_storage.json.v1",
                path_kind="store_file",
                source_kind="json",
                files=("internal_storage.json",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.plugins",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.OPAQUE,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/plugins/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Installed plugin bundles, commands, skills, and cached metadata.",
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.plugins",
                adapter_id="codex.plugin_manifest_json.v1",
                data_version="codex.plugin_manifest.json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("plugins",),
                glob="plugin.json",
                path_parts_excluded=(".git",),
            ),
            DiscoverySpec(
                store="codex.plugins",
                adapter_id="codex.plugin_instruction_text.v1",
                data_version="codex.plugin_instruction.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("plugins",),
                glob="*.md",
                path_parts_required=("commands",),
                path_parts_excluded=(".git",),
            ),
            DiscoverySpec(
                store="codex.plugins",
                adapter_id="codex.plugin_instruction_text.v1",
                data_version="codex.plugin_instruction.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("plugins",),
                glob="*.md",
                path_parts_required=("agents",),
                path_parts_excluded=(".git",),
            ),
            DiscoverySpec(
                store="codex.plugins",
                adapter_id="codex.plugin_instruction_text.v1",
                data_version="codex.plugin_instruction.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("plugins",),
                glob="*.md",
                path_parts_required=("skills",),
                path_parts_excluded=(".git",),
            ),
            DiscoverySpec(
                store="codex.plugins",
                adapter_id="codex.plugin_instruction_text.v1",
                data_version="codex.plugin_instruction.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("plugins",),
                glob="*.md",
                path_parts_required=("custom-skills",),
                path_parts_excluded=(".git",),
            ),
            DiscoverySpec(
                store="codex.plugins",
                adapter_id="codex.plugin_hooks_json.v1",
                data_version="codex.plugin_hooks.json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("plugins",),
                glob="hooks.json",
                path_parts_required=("hooks",),
                path_parts_excluded=(".git",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.plugin_marketplace",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/plugins/**/marketplace.json",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes=(
            "Plugin marketplace metadata for installed or project-local plugin roots. "
            "Summarized structurally, not searched as prompt history."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.plugin_marketplace",
                adapter_id="codex.plugin_marketplace_json.v1",
                data_version="codex.plugin_marketplace.json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("plugins",),
                glob="marketplace.json",
                path_parts_excluded=(".git",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.skills",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.TEXT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/skills/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="User-level Codex skill instructions.",
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.skills",
                adapter_id="codex.skills_text.v1",
                data_version="codex.skills.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("skills",),
                glob="*.md",
                path_parts_excluded=(".git",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.rules",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.TEXT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/rules/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="User-defined rule files that can affect agent behavior.",
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.rules",
                adapter_id="codex.rules_text.v1",
                data_version="codex.rules.text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("rules",),
                glob="*.rules",
                path_parts_excluded=(".git",),
            ),
            DiscoverySpec(
                store="codex.rules",
                adapter_id="codex.rules_text.v1",
                data_version="codex.rules.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("rules",),
                glob="*.md",
                path_parts_excluded=(".git",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.hooks",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern=(
            "${CODEX_HOME or ${HOME}/.codex}/hooks.json and "
            "${HOME}/<known_project_root>/.codex/hooks.json"
        ),
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes=(
            "Hook configuration JSON. User/project hook shape is summarized without "
            "raw command values."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.hooks",
                adapter_id="codex.hooks_json.v1",
                data_version="codex.hooks.json.v1",
                path_kind="store_file",
                source_kind="json",
                files=("hooks.json",),
            ),
            DiscoverySpec(
                store="codex.hooks",
                adapter_id="codex.hooks_json.v1",
                data_version="codex.project_hooks.json.v1",
                path_kind="store_file",
                source_kind="json",
                root_key="codex_project",
                home_subpath=(".codex",),
                files=("hooks.json",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.project_config",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern="${HOME}/<known_project_root>/.codex/config.toml",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes=(
            "Project-local Codex configuration discovered only from roots already "
            "referenced by local Codex sessions."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.project_config",
                adapter_id="codex.project_config_toml.v1",
                data_version="codex.project_config.toml.v1",
                path_kind="store_file",
                source_kind="text",
                root_key="codex_project",
                home_subpath=(".codex",),
                files=("config.toml",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.project_skills",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.TEXT,
        path_pattern="${HOME}/<known_project_root>/.codex/skills/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes=(
            "Project-local Codex skill Markdown from known project roots. Kept "
            "non-default because project instructions are broader than chat history."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        version_strategies=(
            VersionDetectionStrategy.SHAPE_INFERENCE,
            VersionDetectionStrategy.CATALOG_OBSERVATION,
        ),
        discovery=(
            DiscoverySpec(
                store="codex.project_skills",
                adapter_id="codex.project_skill_text.v1",
                data_version="codex.project_skills.markdown.v1",
                path_kind="store_file",
                source_kind="text",
                root_key="codex_project",
                home_subpath=(".codex", "skills"),
                glob="*.md",
                path_parts_excluded=(".git",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.policy",
        role=StoreRole.APP_STATE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/policy/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Policy state and downloaded policy metadata.",
        coverage=StoreCoverage.PRIVATE,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.runtime_cache",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/{cache,tmp,.tmp,sqlite}/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Runtime cache, temporary files, and SQLite sidecar directories.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.arg0_runtime",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/tmp/arg0/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Arg0 runtime coordination state, summarized without raw values.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.arg0_runtime",
                adapter_id="codex.app_state_json_summary.v1",
                data_version="codex.arg0_runtime.json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("tmp", "arg0"),
                glob="*.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.sqlite_sidecars",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CODEX_SQLITE_HOME or ${CODEX_HOME or ${HOME}/.codex}}/*.sqlite-{wal,shm}",
        env_overrides=("CODEX_HOME", "CODEX_SQLITE_HOME"),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="SQLite WAL/SHM sidecars for Codex databases.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.log_files",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/log/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes=(
            "Runtime log files. Search the structured logs DB only by explicit inspection."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.log_files",
                adapter_id="codex.file_metadata_summary.v1",
                data_version="codex.log_file.text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("log",),
                glob="*",
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.process_manager",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/process_manager/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Process-manager state for background jobs.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.process_manager",
                adapter_id="codex.app_state_json_summary.v1",
                data_version="codex.process_manager.json.v1",
                path_kind="store_file",
                source_kind="json",
                home_subpath=("process_manager",),
                glob="*.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.shell_snapshots",
        role=StoreRole.APP_STATE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/shell_snapshots/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@3fb81667 (2026-06-21)",
        observed_at=_CODEX_OBSERVED_AT,
        schema_notes="Shell/runtime snapshots, not prompt history.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.shell_snapshots",
                adapter_id="codex.file_metadata_summary.v1",
                data_version="codex.shell_snapshot.text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=("shell_snapshots",),
                glob="*.sh",
            ),
        ),
    ),
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
            "`type`, `content`, optional `toolCalls`/`thoughts`/`tokens`/`model`) "
            "interleaved with `MetadataUpdateRecord` updates (`{$set: ...}`). "
            "Upstream types also declare `RewindRecord` and `PartialMetadataRecord` "
            "plus `type` values `info`/`error`/`warning` — these are valid in the "
            "schema but do not appear in observed real-world session files; only "
            "`user` and `gemini` `type` values were seen in v1 adapter sampling. "
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
            "`idx`, `step_type`, `status`, `metadata`, `task_details`, "
            "`step_payload` protobuf blobs, and `step_format`; companion "
            "metadata tables hold protobuf blobs. No public schema is "
            "available, so agentgrep extracts readable protobuf strings "
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
            "`.system_generated/logs/`. Each line is a step record "
            "(`type`, `source`, `status`, `created_at`, `content`); string "
            "`content` carries the user/assistant/tool turns. This is the "
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
        path_pattern="${GROK_HOME or ${HOME}/.grok}/memory/MEMORY.md",
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.2.59 (observed 2026-06-21)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes="Persistent memory in Markdown; managed by Grok's memory system.",
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
)


_PI_STORES: tuple[StoreDescriptor, ...] = (
    StoreDescriptor(
        agent="pi",
        store_id="pi.sessions",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${PI_CODING_AGENT_DIR or ${HOME}/.pi/agent}/sessions/"
            "--<encoded_cwd>--/<ts>_<session_uuid>.jsonl"
        ),
        env_overrides=("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR"),
        observed_version="pi v0.79.9 (observed 2026-06-21)",
        observed_at=_PI_OBSERVED_AT,
        upstream_ref=(
            "github.com/earendil-works/pi@v0.79.9/packages/coding-agent/"
            "src/core/session-manager.ts#L24-L31"
        ),
        schema_notes=(
            "Append-only JSONL transcript, one file per session, grouped by "
            "working directory (`--<encoded_cwd>--`, leading slash stripped, "
            '`/ \\ :` -> `-`). Line 1 is a `type:"session"` header (`id`, '
            "`timestamp`, `cwd`; `version` is 3 and may be absent in v1 files). "
            "Each later line is a SessionEntry tagged union sharing "
            "`id`/`parentId`/`timestamp`: `message` wraps an LLM message "
            "(`role` user/assistant/toolResult, `content` string or "
            "content-blocks; assistant turns carry `model`/`provider`); "
            "`compaction`/`branch_summary` carry a `summary`; `session_info` "
            "carries a user-set `name`. No separate prompt-history log or "
            "SQLite index exists."
        ),
        sample_record=(
            '{"type":"message","id":"...","parentId":"...",'
            '"timestamp":"2026-05-30T18:23:54.003Z","message":{"role":"user",'
            '"content":[{"type":"text","text":"<redacted>"}],'
            '"timestamp":1780165434002}}'
        ),
        search_by_default=True,
        search_notes=(
            "The sole searchable pi store. User turns surface as prompts and "
            "assistant/tool turns as history via the shared role->kind mapping; "
            "compaction/branch summaries and session names are included as "
            "history text."
        ),
        discovery=(
            DiscoverySpec(
                store="pi.sessions",
                adapter_id="pi.sessions_jsonl.v1",
                path_kind="session_file",
                source_kind="jsonl",
                root_key="default",
                home_subpath=("sessions",),
                glob="*.jsonl",
            ),
            DiscoverySpec(
                store="pi.sessions",
                adapter_id="pi.sessions_jsonl.v1",
                path_kind="session_file",
                source_kind="jsonl",
                root_key="pi_session",
                glob="*.jsonl",
            ),
        ),
    ),
    StoreDescriptor(
        agent="pi",
        store_id="pi.context_mode_db",
        role=StoreRole.APP_STATE,
        format=StoreFormat.SQLITE,
        path_pattern="${HOME}/.pi/context-mode/sessions/<session_id>.db",
        observed_version="pi v0.79.9 (observed 2026-06-21)",
        observed_at=_PI_OBSERVED_AT,
        schema_notes=(
            "Per-session context-mode SQLite database, rooted at "
            "`~/.pi/context-mode/sessions/<16-hex>.db` (outside the agent dir "
            "and keyed by a 16-hex session id, unlike `pi.sessions`' "
            "`--<cwd>--` grouping). The `session_events` table holds events "
            "(`type` = role/intent/decision/tool_call/file_read/"
            "blocker_resolved) with a JSON `data` payload, emitted as "
            "inspectable records."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="pi.context_mode_db",
                adapter_id="pi.context_mode_sqlite.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                root_key="pi_context_mode",
                home_subpath=("sessions",),
                glob="*.db",
            ),
        ),
    ),
    StoreDescriptor(
        agent="pi",
        store_id="pi.settings",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${PI_CODING_AGENT_DIR or ${HOME}/.pi/agent}/settings.json",
        env_overrides=("PI_CODING_AGENT_DIR",),
        observed_version="pi v0.79.9 (observed 2026-06-21)",
        observed_at=_PI_OBSERVED_AT,
        schema_notes=(
            "User preferences: selected models, themes, installed extension "
            "`packages`, and assorted UI/agent settings. Configuration, not "
            "chat content."
        ),
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="pi",
        store_id="pi.auth",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${PI_CODING_AGENT_DIR or ${HOME}/.pi/agent}/auth.json",
        env_overrides=("PI_CODING_AGENT_DIR",),
        observed_version="pi v0.79.9 (observed 2026-06-21)",
        observed_at=_PI_OBSERVED_AT,
        schema_notes="Provider API credentials. Documented but never enumerated.",
        coverage=StoreCoverage.PRIVATE,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="pi",
        store_id="pi.models",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${PI_CODING_AGENT_DIR or ${HOME}/.pi/agent}/models.json",
        env_overrides=("PI_CODING_AGENT_DIR",),
        observed_version="pi v0.79.9 (observed 2026-06-21)",
        observed_at=_PI_OBSERVED_AT,
        schema_notes=(
            "Custom model definitions and provider overrides. Created only "
            "when the user adds custom models."
        ),
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="pi",
        store_id="pi.themes",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${PI_CODING_AGENT_DIR or ${HOME}/.pi/agent}/themes/<theme>.json",
        env_overrides=("PI_CODING_AGENT_DIR",),
        observed_version="pi v0.79.9 (observed 2026-06-21)",
        observed_at=_PI_OBSERVED_AT,
        schema_notes="User-defined TUI colour schemes. Created only when the user adds themes.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="pi",
        store_id="pi.tools",
        role=StoreRole.APP_STATE,
        format=StoreFormat.OPAQUE,
        path_pattern="${PI_CODING_AGENT_DIR or ${HOME}/.pi/agent}/tools/<tool>",
        env_overrides=("PI_CODING_AGENT_DIR",),
        observed_version="pi v0.79.9 (observed 2026-06-21)",
        observed_at=_PI_OBSERVED_AT,
        schema_notes="Directory of user-authored custom tool scripts. Created on demand.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="pi",
        store_id="pi.bin",
        role=StoreRole.APP_STATE,
        format=StoreFormat.OPAQUE,
        path_pattern="${PI_CODING_AGENT_DIR or ${HOME}/.pi/agent}/bin/<binary>",
        env_overrides=("PI_CODING_AGENT_DIR",),
        observed_version="pi v0.79.9 (observed 2026-06-21)",
        observed_at=_PI_OBSERVED_AT,
        schema_notes="Managed binaries (e.g. `fd`, `rg`) pi downloads for its own use.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="pi",
        store_id="pi.prompts",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.MARKDOWN_FRONTMATTER,
        path_pattern="${PI_CODING_AGENT_DIR or ${HOME}/.pi/agent}/prompts/<prompt>.md",
        env_overrides=("PI_CODING_AGENT_DIR",),
        observed_version="pi v0.79.9 (observed 2026-06-21)",
        observed_at=_PI_OBSERVED_AT,
        schema_notes=(
            "User-authored Markdown prompt templates, not conversation history. Created on demand."
        ),
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="pi",
        store_id="pi.debug_log",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern="${PI_CODING_AGENT_DIR or ${HOME}/.pi/agent}/pi-debug.log",
        env_overrides=("PI_CODING_AGENT_DIR",),
        observed_version="pi v0.79.9 (observed 2026-06-21)",
        observed_at=_PI_OBSERVED_AT,
        schema_notes="Runtime diagnostics log. Written only when debug logging is enabled.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="pi",
        store_id="pi.extensions_npm",
        role=StoreRole.APP_STATE,
        format=StoreFormat.OPAQUE,
        path_pattern="${PI_CODING_AGENT_DIR or ${HOME}/.pi/agent}/npm/",
        env_overrides=("PI_CODING_AGENT_DIR",),
        observed_version="pi v0.79.9 (observed 2026-06-21)",
        observed_at=_PI_OBSERVED_AT,
        schema_notes=(
            "Managed npm extension install root: `package.json`, "
            "`package-lock.json`, and `node_modules/`. Declared via the "
            "`packages` array in pi.settings."
        ),
        search_by_default=False,
    ),
)


_OPENCODE_STORES: tuple[StoreDescriptor, ...] = (
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.db",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.SQLITE,
        path_pattern="${XDG_DATA_HOME or ${HOME}/.local/share}/opencode/opencode.db",
        env_overrides=("XDG_DATA_HOME", "OPENCODE_DB"),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        upstream_ref=(
            "github.com/anomalyco/opencode/blob/v1.17.9/packages/core/src/session/sql.ts#L23-L82"
        ),
        schema_notes=(
            "SQLite store (Drizzle). Tables `session` (id, project_id, "
            "`directory` = working dir, title, version, time_created/updated, "
            "model, cost, tokens_*), `message` (id, session_id FK, `data` JSON "
            "with role user/assistant, modelID/providerID, time, path.cwd), and "
            "`part` (id, message_id FK, session_id, `data` JSON with type + "
            "payload). Searchable text lives in `part.data`: type `text`/"
            "`reasoning` -> `text`, `subtask` -> `prompt`. A conversation turn "
            "is reconstructed by joining part -> message -> session. Channel "
            "installs use `opencode-<channel>.db`; `OPENCODE_DB` overrides the "
            "path (also `:memory:`/absolute). The same file also carries the "
            "unreleased v2 event-sourced tables `session_input`, "
            "`session_message`, `event`/`event_sequence`, and `todo`; in stable "
            "installs these are empty beta state, the canonical transcript "
            "staying in `session`/`message`/`part`, so they are not searched. "
            "Secret-bearing `account`, `account_state`, `control_account`, and "
            "`credential` tables are present but never enumerated — the adapter "
            "reads only text-bearing `part` rows."
        ),
        sample_record=(
            'part.data: {"type":"text","text":"<redacted>",'
            '"time":{"start":1779999665000,"end":1779999666000}}'
        ),
        search_by_default=True,
        search_notes=(
            "The sole searchable OpenCode store. kind is derived from the "
            "joined message role (user -> prompt, else history)."
        ),
        discovery=(
            DiscoverySpec(
                store="opencode.db",
                adapter_id="opencode.db_sqlite.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                root_key="default",
                files=("opencode.db",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.storage_legacy",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.JSON_OBJECT,
        path_pattern=(
            "${XDG_DATA_HOME or ${HOME}/.local/share}/opencode/storage/"
            "{session,message,part}/**/*.json"
        ),
        env_overrides=("XDG_DATA_HOME",),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        upstream_ref=(
            "github.com/anomalyco/opencode/blob/v1.17.9/packages/opencode/"
            "src/storage/storage.ts#L189-L230"
        ),
        schema_notes=(
            "Pre-migration on-disk layout: one JSON file per session/message/"
            "part. A startup migration folds these into opencode.db; migrated "
            "installs keep only an empty `storage/session_diff/` and a "
            "`storage/migration` marker. Documentary — relevant only to older, "
            "un-migrated installs."
        ),
        distinguishes_from=("opencode.db",),
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.config",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${XDG_CONFIG_HOME or ${HOME}/.config}/opencode/opencode.json",
        env_overrides=("XDG_CONFIG_HOME", "OPENCODE_CONFIG_DIR"),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        schema_notes=(
            "Application config (`opencode.json`/`opencode.jsonc`): providers, "
            "agents, plugins, commands, UI settings. Configuration, not chat."
        ),
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.auth",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${XDG_DATA_HOME or ${HOME}/.local/share}/opencode/auth.json",
        env_overrides=("XDG_DATA_HOME",),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        schema_notes="Provider API keys and OAuth tokens. Documented but never enumerated.",
        coverage=StoreCoverage.PRIVATE,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.snapshots",
        role=StoreRole.SOURCE_TREE,
        format=StoreFormat.OPAQUE,
        path_pattern="${XDG_DATA_HOME or ${HOME}/.local/share}/opencode/snapshot/",
        env_overrides=("XDG_DATA_HOME",),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        schema_notes="Per-project git repositories holding session file snapshots.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.repos",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern="${XDG_DATA_HOME or ${HOME}/.local/share}/opencode/repos/",
        env_overrides=("XDG_DATA_HOME",),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        schema_notes="Cache of cloned git repositories referenced during sessions.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.logs",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern="${XDG_DATA_HOME or ${HOME}/.local/share}/opencode/log/",
        env_overrides=("XDG_DATA_HOME",),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        schema_notes="Timestamped application logs. Diagnostics, not chat content.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="opencode",
        store_id="opencode.tool_output",
        role=StoreRole.CACHE,
        format=StoreFormat.TEXT,
        path_pattern="${XDG_DATA_HOME or ${HOME}/.local/share}/opencode/tool-output/",
        env_overrides=("XDG_DATA_HOME",),
        observed_version="opencode v1.17.9 (observed 2026-06-21)",
        observed_at=_OPENCODE_OBSERVED_AT,
        schema_notes="Overflow storage for large tool output that exceeds inline limits.",
        search_by_default=False,
    ),
)


CATALOG = StoreCatalog(
    catalog_version=24,
    captured_at=_ANTIGRAVITY_OBSERVED_AT,
    stores=(
        *_CLAUDE_STORES,
        *_CURSOR_CLI_STORES,
        *_CURSOR_IDE_STORES,
        *_CODEX_STORES,
        *_GEMINI_STORES,
        *_ANTIGRAVITY_CLI_STORES,
        *_ANTIGRAVITY_IDE_STORES,
        *_GROK_STORES,
        *_PI_STORES,
        *_OPENCODE_STORES,
    ),
)
"""The canonical agentgrep store catalogue.

This is the single source of truth for *where* agent data lives on disk and
*what shape* its records take. Adapters consume :class:`CATALOG`; the
catalogue itself does not depend on any adapter code.
"""


__all__ = (
    "CATALOG",
    "OBSERVED_AT",
    "gemini_project_hash",
)
