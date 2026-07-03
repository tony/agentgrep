"""cursor_cli store descriptors for the agentgrep catalogue."""

from __future__ import annotations

from agentgrep.store_catalog._common import _CURSOR_CLI_OBSERVED_AT
from agentgrep.stores import (
    DiscoverySpec,
    StoreCoverage,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
    VersionDetectionStrategy,
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
            "tables. `meta` holds a single row keyed `'0'` whose value is "
            "hex-encoded JSON nesting the session metadata (`agentId`, "
            "`latestRootBlobId`, …); `blobs` holds content-addressed protobuf "
            "messages (64-char sha256 ids) forming a Merkle graph from the root "
            "blob. Cursor publishes no schema, so agentgrep walks the protobuf "
            "wire format generically and surfaces readable UTF-8 runs — "
            "best-effort and date-versioned, not an official format."
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
    StoreDescriptor(
        agent="cursor-cli",
        store_id="cursor-cli.agent_tools",
        role=StoreRole.APP_STATE,
        format=StoreFormat.TEXT,
        path_pattern="${HOME}/.cursor/projects/<id>/agent-tools/<name>.txt",
        observed_version="cursor-agent 2026.06.19-653a7fb",
        observed_at=_CURSOR_CLI_OBSERVED_AT,
        schema_notes=(
            "Captured tool-result payloads written per project under "
            "`agent-tools/*.txt`, distinct from the `tools/*.json` registry. "
            "Tool output rather than chat; inspectable opt-in, not searched by "
            "default."
        ),
        coverage=StoreCoverage.INSPECTABLE,
        search_by_default=False,
        discovery=(
            DiscoverySpec(
                store="cursor-cli.agent_tools",
                adapter_id="cursor_cli.agent_tools_text.v1",
                path_kind="store_file",
                source_kind="text",
                home_subpath=(".cursor", "projects"),
                glob="*.txt",
                path_parts_required=("agent-tools",),
            ),
        ),
    ),
)
