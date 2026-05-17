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
    StoreCatalog,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
)

OBSERVED_AT = datetime.date(2026, 5, 17)


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
        store_id="claude.projects.session",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.JSONL,
        path_pattern="${HOME}/.claude/projects/<encoded_project>/<session_uuid>.jsonl",
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
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.projects.subagent",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.JSONL,
        path_pattern="${HOME}/.claude/projects/<encoded_project>/<session_uuid>/subagents/<agent>.jsonl",
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
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.projects.memory",
        role=StoreRole.PERSISTENT_MEMORY,
        format=StoreFormat.MARKDOWN_FRONTMATTER,
        path_pattern="${HOME}/.claude/projects/<encoded_project>/memory/*.md",
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
        path_pattern="${HOME}/.claude/tasks/<uuid>/",
        observed_version="claude-code v2.1.143",
        observed_at=OBSERVED_AT,
        schema_notes="Per-conversation task lists written by the TodoWrite tool.",
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.todos",
        role=StoreRole.TODO,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${HOME}/.claude/todos/*.json",
        observed_version="claude-code v2.1.143",
        observed_at=OBSERVED_AT,
        schema_notes="Persistent todo lists keyed by agent UUID.",
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.sessions",
        role=StoreRole.APP_STATE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern="${HOME}/.claude/sessions/",
        observed_version="claude-code v2.1.143",
        observed_at=OBSERVED_AT,
        schema_notes="Shell environment snapshots; rarely contains conversation text.",
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.store_db",
        role=StoreRole.APP_STATE,
        format=StoreFormat.SQLITE,
        path_pattern="${HOME}/.claude/__store.db",
        observed_version="claude-code v2.1.143",
        observed_at=OBSERVED_AT,
        schema_notes="SQLite app state. Schema not yet documented.",
    ),
    StoreDescriptor(
        agent="claude",
        store_id="claude.paste_cache",
        role=StoreRole.CACHE,
        format=StoreFormat.OPAQUE,
        path_pattern="${HOME}/.claude/paste-cache/",
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
        path_pattern="${HOME}/.claude/plugins/cache/",
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
            "`text`/`tool_use`/`tool_result`/`bubbleId`. No native timestamp — infer "
            "from session-directory mtime. Tool outputs sometimes `[REDACTED]` in "
            "older `cursor-agent` versions."
        ),
        sample_record=(
            '{"role":"user","message":{"content":'
            '[{"type":"text","text":"<user_query>...</user_query>"}]}}'
        ),
        distinguishes_from=("cursor.ide.state_vscdb",),
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
        role=StoreRole.APP_STATE,
        format=StoreFormat.SQLITE,
        path_pattern="${HOME}/.cursor/ai-tracking/ai-code-tracking.db",
        observed_version="cursor-agent (version not surfaced publicly)",
        observed_at=OBSERVED_AT,
        schema_notes=(
            "SQLite with `conversation_summaries(conversationId, title, tldr, "
            "overview, summaryBullets, model, mode, updatedAt)`. Some installs have "
            "the table empty even when the CLI agent runs — the tracker may be "
            "disabled or unused; agentgrep tolerates that silently."
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
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.state_db",
        role=StoreRole.APP_STATE,
        format=StoreFormat.SQLITE,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/state_5.sqlite",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        upstream_ref="github.com/openai/codex@4c89772/codex-rs/state/src/lib.rs#L70",
        schema_notes="Codex state DB; schema managed via migrations.",
    ),
    StoreDescriptor(
        agent="codex",
        store_id="codex.logs_db",
        role=StoreRole.APP_STATE,
        format=StoreFormat.SQLITE,
        path_pattern="${CODEX_HOME or ${HOME}/.codex}/logs_2.sqlite",
        env_overrides=("CODEX_HOME",),
        observed_version="github.com/openai/codex@4c89772 (2026-05-16)",
        observed_at=OBSERVED_AT,
        upstream_ref="github.com/openai/codex@4c89772/codex-rs/state/src/lib.rs#L71",
        schema_notes=(
            "Codex logs DB (`LOGS_DB_FILENAME`). Note: the `_N.sqlite` files belong "
            "to codex, not Cursor — earlier exploration mis-attributed them."
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
        schema_notes="Persistent memory notes.",
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
            "JSONL with mixed record types: `MessageRecord` (`id`, `timestamp`, "
            "`type` ∈ user/info/error/warning/gemini, `content`: PartListUnion, "
            "optional `toolCalls`, `thoughts`, `tokens`, `model`); `RewindRecord` "
            "(`{$rewindTo}`); `MetadataUpdateRecord` (`{$set: ...}`); "
            "`PartialMetadataRecord` (initial session metadata)."
        ),
        sample_record='{"id":"...","timestamp":"2026-05-17T...","type":"user","content":[{"text":"<redacted>"}]}',
        search_by_default=True,
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
            "message }` — user-prompt audit log."
        ),
        sample_record='[{"sessionId":"...","messageId":0,"timestamp":"...","type":"user","message":"<redacted>"}]',
    ),
    StoreDescriptor(
        agent="gemini",
        store_id="gemini.history",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.JSONL,
        path_pattern="${GEMINI_CLI_HOME or ${HOME}/.gemini}/history/<timestamp>/",
        env_overrides=("GEMINI_CLI_HOME",),
        observed_version="gemini-cli v0.42.0 stable; types from v0.44.0-nightly @77e65c0d",
        observed_at=OBSERVED_AT,
        schema_notes=(
            "Archive subset of `tmp/` after retention prune. Same record schema as "
            "`gemini.tmp.chats`. Defaults: `minRetention=1d`, `maxAge=30d`, "
            "`maxCount` unbounded — see "
            "`packages/cli/src/utils/sessionCleanup.ts:23`."
        ),
        distinguishes_from=("gemini.tmp.chats",),
        search_notes="De-duplicate against `gemini.tmp.chats` by `sessionId`.",
    ),
    StoreDescriptor(
        agent="gemini",
        store_id="gemini.antigravity.brain",
        role=StoreRole.PLAN,
        format=StoreFormat.MARKDOWN_FRONTMATTER,
        path_pattern="${GEMINI_CLI_HOME or ${HOME}/.gemini}/antigravity/brain/<uuid>/task.md*",
        env_overrides=("GEMINI_CLI_HOME",),
        observed_version="gemini-cli v0.42.0 stable",
        observed_at=OBSERVED_AT,
        schema_notes="Persistent task/plan storage. Markdown, not chat.",
    ),
    StoreDescriptor(
        agent="gemini",
        store_id="gemini.antigravity.conversations",
        role=StoreRole.SUPPLEMENTARY_CHAT,
        format=StoreFormat.PROTOBUF,
        path_pattern="${GEMINI_CLI_HOME or ${HOME}/.gemini}/antigravity/conversations/*.pb",
        env_overrides=("GEMINI_CLI_HOME",),
        observed_version="gemini-cli v0.42.0 stable",
        observed_at=OBSERVED_AT,
        schema_notes=(
            "Protobuf conversation blobs. No `.proto` definitions in the public "
            "gemini-cli repo as of HEAD `77e65c0d` — schema is opaque; defer adapter "
            "until upstream publishes definitions or the format is reverse-engineered."
        ),
        search_by_default=False,
        search_notes="Opaque protobuf without public schema.",
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


CATALOG = StoreCatalog(
    catalog_version=1,
    captured_at=OBSERVED_AT,
    stores=(*_CLAUDE_STORES, *_CURSOR_STORES, *_CODEX_STORES, *_GEMINI_STORES),
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
