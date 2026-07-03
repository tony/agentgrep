"""pi store descriptors for the agentgrep catalogue."""

from __future__ import annotations

from agentgrep.store_catalog._common import _PI_OBSERVED_AT
from agentgrep.stores import (
    DiscoverySpec,
    StoreCoverage,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
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
            "content-blocks; assistant turns carry `model`/`provider`; a "
            "`bashExecution` role has no `content` and carries its shell "
            "`command`/`output` instead; error/aborted assistant turns carry a "
            "diagnostic `errorMessage` string in place of `content`); "
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
        path_pattern="${HOME}/.pi/context-mode/sessions/<project_hash>.db",
        observed_version="pi v0.79.9 (observed 2026-06-21)",
        observed_at=_PI_OBSERVED_AT,
        schema_notes=(
            "Per-project context-mode SQLite database, rooted at "
            "`~/.pi/context-mode/sessions/<project_hash>.db` (outside the agent "
            "dir; the stem is `sha256(project_dir)[:16]`, so it is a hashed "
            "`cwd` grouping holding multiple sessions, each row carrying its "
            "own `session_id`). The `session_events` table holds events "
            "(`type` = role/intent/decision/tool_call/file_read/"
            "blocker_resolved/data) with a JSON `data` payload, emitted as "
            "inspectable records; sibling `session_meta`/`session_resume`/"
            "`tool_calls` tables exist but only `session_events` is parsed."
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
