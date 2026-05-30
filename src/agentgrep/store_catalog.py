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
)

OBSERVED_AT = datetime.date(2026, 5, 17)
_GROK_OBSERVED_AT = datetime.date(2026, 5, 25)
_CLAUDE_HISTORY_OBSERVED_AT = datetime.date(2026, 5, 29)


def gemini_project_hash(project_root: pathlib.Path) -> str:
    """Reproduce Gemini CLI's project-hash derivation.

    Mirrors the ``getProjectHash`` helper at
    ``packages/core/src/utils/paths.ts:318-320`` in
    ``github.com/google-gemini/gemini-cli`` (HEAD ``77e65c0d``):

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
        observed_version="claude-code v2.1.157",
        observed_at=_CLAUDE_HISTORY_OBSERVED_AT,
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
        search_notes=(
            "User-prompt audit log used by Claude Code prompt history; project "
            "transcripts remain the full conversation replay."
        ),
        discovery=(
            DiscoverySpec(
                store="claude.history",
                adapter_id="claude.history_jsonl.v1",
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
        observed_version="claude-code v2.1.143",
        observed_at=OBSERVED_AT,
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
        discovery=(
            DiscoverySpec(
                store="claude.projects",
                adapter_id="claude.projects_jsonl.v1",
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
        observed_version="claude-code v2.1.143",
        observed_at=OBSERVED_AT,
        schema_notes=(
            "Same JSONL line format as the parent session. Each file is one sub-agent "
            "dispatch from the Task tool."
        ),
        distinguishes_from=("claude.projects.session",),
        search_by_default=True,
        search_notes=(
            "Sub-agent transcripts are conversation content; de-duplicate with the "
            "parent session by `uuid`."
        ),
        discovery=(
            DiscoverySpec(
                store="claude.projects_subagents",
                adapter_id="claude.projects_jsonl.v1",
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
        observed_version="claude-code v2.1.143",
        observed_at=OBSERVED_AT,
        schema_notes=(
            "Markdown files with YAML frontmatter; the auto-memory feature. Each file "
            "holds one fact/feedback/project/reference memory."
        ),
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.tasks",
        role=StoreRole.TODO,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/tasks/<task_list>/<task_id>.json",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.143",
        observed_at=OBSERVED_AT,
        schema_notes=(
            "Claude Code task JSON with `id`, `subject`, `description`, `status`, "
            "`blocks`, `blockedBy`, and optional `activeForm` / metadata fields."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.tasks",
                adapter_id="claude.tasks_json.v1",
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
        observed_version="claude-code v2.1.143",
        observed_at=OBSERVED_AT,
        schema_notes="Persistent todo lists keyed by agent UUID.",
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.sessions",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/sessions/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.143",
        observed_at=OBSERVED_AT,
        schema_notes="Shell environment snapshots; rarely contains conversation text.",
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.store_db",
        role=StoreRole.APP_STATE,
        format=StoreFormat.SQLITE,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/__store.db",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.143",
        observed_at=OBSERVED_AT,
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
        observed_version="claude-code v2.1.157",
        observed_at=_CLAUDE_HISTORY_OBSERVED_AT,
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
        store_id="claude.projects.session_memory",
        role=StoreRole.PERSISTENT_MEMORY,
        format=StoreFormat.TEXT,
        path_pattern=(
            "${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/projects/<encoded_project>/"
            "<session_uuid>/session-memory/summary.md"
        ),
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.157",
        observed_at=_CLAUDE_HISTORY_OBSERVED_AT,
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
        observed_version="claude-code v2.1.157",
        observed_at=_CLAUDE_HISTORY_OBSERVED_AT,
        schema_notes=(
            "User configuration, local overrides, keybindings, policy limits, "
            "and update/auth cache state. Documented for inventory, not searched "
            "as prompt history."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="claude.settings",
                adapter_id="claude.settings_json.v1",
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
        store_id="claude.file_history",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/file-history/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.157",
        observed_at=_CLAUDE_HISTORY_OBSERVED_AT,
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
        observed_version="claude-code v2.1.157",
        observed_at=_CLAUDE_HISTORY_OBSERVED_AT,
        schema_notes="Shell integration snapshots; runtime state rather than prompt history.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.image_cache",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/image-cache/<session>/<image>",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.157",
        observed_at=_CLAUDE_HISTORY_OBSERVED_AT,
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
        observed_version="claude-code v2.1.157",
        observed_at=_CLAUDE_HISTORY_OBSERVED_AT,
        schema_notes="Context-mode counters and local app state, sometimes backed by SQLite.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.security_state",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/security/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.157",
        observed_at=_CLAUDE_HISTORY_OBSERVED_AT,
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
        observed_version="claude-code v2.1.157",
        observed_at=_CLAUDE_HISTORY_OBSERVED_AT,
        schema_notes="User and managed skill instructions loaded into Claude Code behavior.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.teams",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.TEXT,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/teams/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.157",
        observed_at=_CLAUDE_HISTORY_OBSERVED_AT,
        schema_notes="Team-provided instruction bundles under the Claude config root.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.session_env",
        role=StoreRole.APP_STATE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/session-env/",
        env_overrides=("CLAUDE_CONFIG_DIR",),
        observed_version="claude-code v2.1.157",
        observed_at=_CLAUDE_HISTORY_OBSERVED_AT,
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
        observed_version="claude-code v2.1.157",
        observed_at=_CLAUDE_HISTORY_OBSERVED_AT,
        schema_notes="IDE integration state and bridge metadata.",
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
        observed_version="claude-code v2.1.143",
        observed_at=OBSERVED_AT,
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
        observed_version="claude-code v2.1.143",
        observed_at=OBSERVED_AT,
        schema_notes="Plugin binaries and metadata; not chat content.",
        search_by_default=False,
    ),
)


_CURSOR_STORES: tuple[StoreDescriptor, ...] = (
    StoreDescriptor(
        agent="cursor",
        store_id="cursor.cli.transcripts",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${HOME}/.cursor/projects/<id>/agent-transcripts/<session_uuid>/<session_uuid>.jsonl"
        ),
        observed_version="cursor-agent (version not surfaced publicly)",
        observed_at=OBSERVED_AT,
        upstream_ref="cursor.com/docs/cli/overview",
        schema_notes=(
            "JSONL Anthropic-style: `role`, `message.content[]` with "
            "`text`/`tool_use`/`tool_result`. No native timestamp — agentgrep "
            "infers from the file's mtime. Tool outputs sometimes `[REDACTED]` "
            "in older `cursor-agent` versions. Adapter `store` field uses the "
            "underscore-flattened form ``cursor.cli_transcripts``."
        ),
        sample_record=(
            '{"role":"user","message":{"content":'
            '[{"type":"text","text":"<user_query>...</user_query>"}]}}'
        ),
        distinguishes_from=("cursor.ide.state_vscdb",),
        search_by_default=True,
        search_notes=(
            "Parsed by agentgrep via `parse_cursor_cli_transcript` (`cursor.cli_jsonl.v1`)."
        ),
        discovery=(
            DiscoverySpec(
                store="cursor.cli_transcripts",
                adapter_id="cursor.cli_jsonl.v1",
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
        agent="cursor",
        store_id="cursor.cli.subagent_transcripts",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${HOME}/.cursor/projects/<id>/agent-transcripts/<session_uuid>/subagents/<agent>.jsonl"
        ),
        observed_version="cursor-agent 2026.05.28-a70ca7c",
        observed_at=datetime.date(2026, 5, 29),
        schema_notes=(
            "Same JSONL Anthropic-style shape as `cursor.cli.transcripts`, nested "
            "under a session's `subagents/` directory."
        ),
        distinguishes_from=("cursor.cli.transcripts",),
        search_by_default=True,
        search_notes="Subagent transcript files are conversation content, not primary sessions.",
        discovery=(
            DiscoverySpec(
                store="cursor.cli_subagents",
                adapter_id="cursor.cli_jsonl.v1",
                path_kind="session_file",
                source_kind="jsonl",
                home_subpath=(".cursor", "projects"),
                glob="*.jsonl",
                path_parts_required=("agent-transcripts", "subagents"),
            ),
        ),
    ),
    StoreDescriptor(
        agent="cursor",
        store_id="cursor.cli.repo_meta",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${HOME}/.cursor/projects/<id>/repo.json",
        observed_version="cursor-agent (version not surfaced publicly)",
        observed_at=OBSERVED_AT,
        schema_notes="Project tree/manifest metadata.",
    ),
    StoreDescriptor(
        agent="cursor",
        store_id="cursor.cli.tools",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern=(
            "${HOME}/.cursor/projects/<id>/{mcps/*/SERVER_METADATA.json,"
            "tools/*.json,mcp-approvals.json}"
        ),
        observed_version="cursor-agent (version not surfaced publicly)",
        observed_at=OBSERVED_AT,
        schema_notes="MCP tool registry and approval records.",
    ),
    StoreDescriptor(
        agent="cursor",
        store_id="cursor.cli.terminals",
        role=StoreRole.APP_STATE,
        format=StoreFormat.OPAQUE,
        path_pattern="${HOME}/.cursor/projects/<id>/terminals/",
        observed_version="cursor-agent (version not surfaced publicly)",
        observed_at=OBSERVED_AT,
        schema_notes="Terminal output logs.",
    ),
    StoreDescriptor(
        agent="cursor",
        store_id="cursor.cli.canvases",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${HOME}/.cursor/projects/<id>/canvases/",
        observed_version="cursor-agent (version not surfaced publicly)",
        observed_at=OBSERVED_AT,
        schema_notes="Cursor canvas state.",
    ),
    StoreDescriptor(
        agent="cursor",
        store_id="cursor.cli.plans",
        role=StoreRole.PLAN,
        format=StoreFormat.MARKDOWN_FRONTMATTER,
        path_pattern="${HOME}/.cursor/plans/*.plan.md",
        observed_version="cursor-agent (version not surfaced publicly)",
        observed_at=OBSERVED_AT,
        schema_notes=("YAML frontmatter (name, overview, todos[], isProject) plus markdown body."),
    ),
    StoreDescriptor(
        agent="cursor",
        store_id="cursor.cli.state",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${HOME}/.cursor/agent-cli-state.json",
        observed_version="cursor-agent (version not surfaced publicly)",
        observed_at=OBSERVED_AT,
        schema_notes="UI tip-shown flags and legacy-cleanup markers.",
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="cursor",
        store_id="cursor.cli.worktrees",
        role=StoreRole.SOURCE_TREE,
        format=StoreFormat.OPAQUE,
        path_pattern="${HOME}/.cursor/worktrees/",
        observed_version="cursor-agent (version not surfaced publicly)",
        observed_at=OBSERVED_AT,
        schema_notes=(
            "Full git worktrees used as code context by the CLI agent. Not chat — "
            "catalogued so future adapter PRs do not index source code as history."
        ),
        search_by_default=False,
        search_notes="Source trees, not transcripts; would drown real hits.",
    ),
    StoreDescriptor(
        agent="cursor",
        store_id="cursor.ai_tracking",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.SQLITE,
        path_pattern="${HOME}/.cursor/ai-tracking/ai-code-tracking.db",
        observed_version="cursor-agent (version not surfaced publicly)",
        observed_at=OBSERVED_AT,
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
                store="cursor.ai_tracking",
                adapter_id="cursor.ai_tracking_sqlite.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                home_subpath=(".cursor", "ai-tracking"),
                files=("ai-code-tracking.db",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="cursor",
        store_id="cursor.ide.state_vscdb",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.SQLITE,
        path_pattern="${HOME}/.config/Cursor/User/globalStorage/state.vscdb",
        platform_variants={
            "darwin": "${HOME}/Library/Application Support/Cursor/User/globalStorage/state.vscdb",
            "win32": "%APPDATA%/Cursor/User/globalStorage/state.vscdb",
        },
        observed_version="Cursor IDE (current observed paths)",
        observed_at=OBSERVED_AT,
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
        distinguishes_from=("cursor.cli.transcripts",),
        search_notes=(
            "Cursor IDE store, parsed by the current `cursor.state_vscdb_modern.v1` "
            "adapter. Not the same as the Cursor CLI agent transcripts."
        ),
        search_by_default=True,
        discovery=(
            DiscoverySpec(
                store="cursor.state",
                adapter_id="cursor.state_vscdb_modern.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                platform_paths=(
                    "~/.config/Cursor/User/globalStorage/state.vscdb",
                    "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb",
                    "~/AppData/Roaming/Cursor/User/globalStorage/state.vscdb",
                ),
            ),
            DiscoverySpec(
                store="cursor.state",
                adapter_id="cursor.state_vscdb_legacy.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                home_subpath=(".cursor",),
                glob="state.vscdb",
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
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        upstream_ref=("github.com/openai/codex@4c89772/codex-rs/message-history/src/lib.rs#L54"),
        schema_notes=(
            "`HistoryEntry { session_id: String, ts: u64 (unix seconds), text: "
            "String }` — one record per user prompt, append-only across all threads."
        ),
        sample_record='{"session_id":"...","ts":1747509826,"text":"<redacted>"}',
        distinguishes_from=("codex.sessions",),
        search_by_default=True,
        discovery=(
            DiscoverySpec(
                store="codex.history",
                adapter_id="codex.history_json.v1",
                path_kind="history_file",
                source_kind="json",
                files=("history.json",),
            ),
            DiscoverySpec(
                store="codex.history",
                adapter_id="codex.history_jsonl.v1",
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
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        upstream_ref=("github.com/openai/codex@4c89772/codex-rs/protocol/src/protocol.rs#L2783"),
        schema_notes=(
            "JSONL `RolloutItem` tagged enum (`type` + `payload`): "
            "`session_meta` | `response_item` | `compacted` | `turn_context` | "
            "`event_msg`. First line is a `SessionMetaLine` with `id`, `timestamp`, "
            "`cwd`, `cli_version`, optional `git` info."
        ),
        sample_record='{"type":"response_item","payload":{"role":"user","content":"<redacted>"}}',
        distinguishes_from=("codex.history",),
        search_by_default=True,
        search_notes=(
            "Full per-thread transcript with tool calls; `codex.history` is the "
            "user-prompts-only audit log."
        ),
        discovery=(
            DiscoverySpec(
                store="codex.sessions",
                adapter_id="codex.sessions_jsonl.v1",
                path_kind="session_file",
                source_kind="jsonl",
                home_subpath=("sessions",),
                glob="*.jsonl",
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
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        upstream_ref="github.com/openai/codex@4c89772/codex-rs/rollout/src/session_index.rs",
        schema_notes=(
            "Append-only JSONL session index with `id`, `thread_name`, and "
            "`updated_at`. It summarizes sessions but does not replace the full "
            "`sessions/**/*.jsonl` transcripts."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.session_index",
                adapter_id="codex.session_index_jsonl.v1",
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
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        upstream_ref="github.com/openai/codex@4c89772/codex-rs/state/src/lib.rs#L70",
        schema_notes=(
            "Codex state DB; schema managed via migrations. Observed prompt-bearing "
            "columns include `threads.first_user_message`, `threads.preview`, "
            "`threads.title`, and `agent_jobs.instruction`."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.state_db",
                adapter_id="codex.state_sqlite.v1",
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
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        upstream_ref="github.com/openai/codex@4c89772/codex-rs/state/src/lib.rs#L71",
        schema_notes=(
            "Codex logs DB (`LOGS_DB_FILENAME` in `codex-rs/state/src/lib.rs`). "
            "The `_N.sqlite` files at the Codex root (`logs_2.sqlite`, "
            "`state_5.sqlite`) belong to the Codex CLI, not Cursor."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.logs_db",
                adapter_id="codex.logs_sqlite.v1",
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
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        schema_notes=(
            "SQLite memory pipeline state. Observed tables include `stage1_outputs` "
            "with raw memory, rollout summary, slug, usage, and selection columns."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.memories_db",
                adapter_id="codex.memories_sqlite.v1",
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
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        schema_notes=(
            "SQLite goal tracker with `thread_goals(objective, status, "
            "token_budget, tokens_used, time_used_seconds, created_at_ms, "
            "updated_at_ms)`."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.goals_db",
                adapter_id="codex.goals_sqlite.v1",
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
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        upstream_ref="github.com/openai/codex@4c89772/codex-rs/external-agent-sessions/src/ledger.rs",
        schema_notes=(
            "Import ledger with `records` carrying source path, content hash, "
            "imported thread id, and import timestamp."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="codex.external_agent_imports",
                adapter_id="codex.external_imports_json.v1",
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
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
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
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        schema_notes=(
            "Persistent memory workspace: `MEMORY.md`, `memory_summary.md`, "
            "`raw_memories.md`, rollout summaries, and skill memory folders."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.config",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/{config.toml,*.toml}",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        schema_notes=(
            "Codex TOML configuration, including optional `sqlite_home`. "
            "It can affect discovery roots but is not prompt history."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.auth",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/auth.json",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        schema_notes="Authentication state. Documented but never enumerated or searched.",
        coverage=StoreCoverage.PRIVATE,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.model_cache",
        role=StoreRole.CACHE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/models_cache.json",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        schema_notes="Cached model metadata: client version, ETag, fetch time, and models.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.internal_storage",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/internal_storage.json",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        schema_notes="Small app flags and migration markers.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.plugins",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.OPAQUE,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/plugins/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        schema_notes="Installed plugin bundles, commands, skills, and cached metadata.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.skills",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.TEXT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/skills/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        schema_notes="User-level Codex skill instructions.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.rules",
        role=StoreRole.INSTRUCTION,
        format=StoreFormat.TEXT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/rules/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        schema_notes="User-defined rule files that can affect agent behavior.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.policy",
        role=StoreRole.APP_STATE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/policy/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
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
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        schema_notes="Runtime cache, temporary files, and SQLite sidecar directories.",
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
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        schema_notes=(
            "Runtime log files. Search the structured logs DB only by explicit inspection."
        ),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.process_manager",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/process_manager/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        schema_notes="Process-manager state for background jobs.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.shell_snapshots",
        role=StoreRole.APP_STATE,
        format=StoreFormat.OPAQUE,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/shell_snapshots/",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        schema_notes="Shell/runtime snapshots, not prompt history.",
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
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
        observed_version="gemini-cli v0.42.0 stable; types from v0.44.0-nightly @77e65c0d",
        observed_at=OBSERVED_AT,
        upstream_ref=(
            "github.com/google-gemini/gemini-cli@77e65c0d/"
            "packages/core/src/services/chatRecordingTypes.ts#L12"
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
        observed_version="gemini-cli v0.42.0 stable; types from v0.44.0-nightly @77e65c0d",
        observed_at=OBSERVED_AT,
        upstream_ref=(
            "github.com/google-gemini/gemini-cli@77e65c0d/packages/core/src/core/logger.ts#L29"
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
        observed_version="gemini-cli v0.42.0 stable; types from v0.44.0-nightly @77e65c0d",
        observed_at=OBSERVED_AT,
        upstream_ref=(
            "github.com/google-gemini/gemini-cli@77e65c0d/packages/core/src/core/logger.ts#L15"
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
        observed_version="gemini-cli v0.42.0 stable; types from v0.44.0-nightly @77e65c0d",
        observed_at=OBSERVED_AT,
        upstream_ref=(
            "github.com/google-gemini/gemini-cli@77e65c0d/"
            "packages/core/src/services/chatRecordingService.ts#L941"
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
        observed_version="gemini-cli v0.42.0 stable",
        observed_at=OBSERVED_AT,
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
        observed_version="gemini-cli v0.42.0 stable",
        observed_at=OBSERVED_AT,
        schema_notes="Configuration; not chat.",
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
        observed_version="grok-cli v0.1.219 (observed 2026-05-25)",
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
        observed_version="grok-cli v0.1.219 (observed 2026-05-25)",
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
        observed_version="grok-cli v0.1.219 (observed 2026-05-25)",
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
        store_id="grok.sessions.events",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSONL,
        path_pattern=(
            "${GROK_HOME or ${HOME}/.grok}/sessions/"
            "<url_encoded_project>/<session_uuid>/events.jsonl"
        ),
        env_overrides=("GROK_HOME",),
        observed_version="grok-cli v0.1.219 (observed 2026-05-25)",
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
        observed_version="grok-cli v0.1.219 (observed 2026-05-25)",
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
        observed_version="grok-cli v0.1.219 (observed 2026-05-25)",
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
        observed_version="grok-cli v0.1.219 (observed 2026-05-25)",
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
        observed_version="grok-cli v0.1.219 (observed 2026-05-25)",
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
        observed_version="grok-cli v0.1.219 (observed 2026-05-25)",
        observed_at=_GROK_OBSERVED_AT,
        schema_notes="TOML configuration file.",
        search_by_default=False,
    ),
)


CATALOG = StoreCatalog(
    catalog_version=5,
    captured_at=_CLAUDE_HISTORY_OBSERVED_AT,
    stores=(
        *_CLAUDE_STORES,
        *_CURSOR_STORES,
        *_CODEX_STORES,
        *_GEMINI_STORES,
        *_GROK_STORES,
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
