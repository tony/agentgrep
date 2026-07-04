"""vscode store descriptors for the agentgrep catalogue."""

from __future__ import annotations

from agentgrep.store_catalog._common import _VSCODE_OBSERVED_AT
from agentgrep.stores import (
    DiscoverySpec,
    StoreCoverage,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
)

_VSCODE_STORES: tuple[StoreDescriptor, ...] = (
    StoreDescriptor(
        agent="vscode",
        store_id="vscode.chat_sessions",
        role=StoreRole.PRIMARY_CHAT,
        format=StoreFormat.JSON_OBJECT,
        path_pattern=(
            "${HOME}/.config/Code/User/workspaceStorage/<hash>/chatSessions/<uuid>.jsonl"
        ),
        platform_variants={
            "darwin": (
                "${HOME}/Library/Application Support/Code/User/workspaceStorage/"
                "<hash>/chatSessions/<uuid>.jsonl"
            ),
            "win32": "%APPDATA%/Code/User/workspaceStorage/<hash>/chatSessions/<uuid>.jsonl",
        },
        env_overrides=("VSCODE_APPDATA", "AGENTGREP_WSL_USERS_ROOT"),
        observed_version="VS Code GitHub Copilot Chat (chatSessions v3)",
        observed_at=_VSCODE_OBSERVED_AT,
        upstream_ref="agentgrep.parse_vscode_chat_session",
        schema_notes=(
            "GitHub Copilot Chat transcript under a per-workspace "
            "`workspaceStorage/<hash>/chatSessions/` directory (windowless sessions "
            "live in `globalStorage/emptyWindowChatSessions/`). Current sessions are "
            "a `.jsonl` mutation log: the first `kind:0` line holds the whole session "
            "snapshot under `v`, then `kind:1` lines set a value at key-path `k` and "
            "`kind:2` lines replace the array at `k` from index `i` with `v` "
            "(truncate + append), rebuilding the `requests[]` "
            "list that older `.json` sessions store as one object. Either way "
            "`requests[]` is the turn list: the user prompt is `message.text`; the "
            "assistant reply is the response parts with no `kind` (bare "
            "`MarkdownString`), joined; `result.metadata.toolCallRounds[].toolCalls[]"
            ".name` names invoked tools; `timestamp` is epoch-ms. The sibling "
            "`workspace.json` `folder` URI resolves the project cwd, including "
            "`vscode-remote://wsl+<distro>/<path>` remotes. VS Code does not publish "
            "a formal schema — agentgrep's parser is the reference implementation."
        ),
        sample_record=(
            '{"kind":0,"v":{"sessionId":"...","requests":[{"message":{"text":"<redacted>"},'
            '"response":[{"value":"<redacted>"}],"timestamp":1779999665000}]}}'
        ),
        distinguishes_from=("vscode.inline_history", "cursor-ide.state_vscdb"),
        search_notes=(
            "The primary searchable VS Code store. Distinct from the Cursor IDE "
            "fork's `state.vscdb` chat and from the inline-edit prompt history."
        ),
        search_by_default=True,
        discovery=(
            DiscoverySpec(
                store="vscode.chat_sessions",
                adapter_id="vscode.chat_sessions_json.v1",
                path_kind="session_file",
                source_kind="jsonl",
                root_key="vscode_workspace",
                glob="*/chatSessions/*.jsonl",
            ),
            DiscoverySpec(
                store="vscode.chat_sessions",
                adapter_id="vscode.chat_sessions_json.v1",
                path_kind="session_file",
                source_kind="jsonl",
                root_key="vscode_global",
                glob="emptyWindowChatSessions/*.jsonl",
            ),
            DiscoverySpec(
                store="vscode.chat_sessions",
                adapter_id="vscode.chat_sessions_json.v1",
                path_kind="session_file",
                source_kind="json",
                root_key="vscode_workspace",
                glob="*/chatSessions/*.json",
            ),
            DiscoverySpec(
                store="vscode.chat_sessions",
                adapter_id="vscode.chat_sessions_json.v1",
                path_kind="session_file",
                source_kind="json",
                root_key="vscode_global",
                glob="emptyWindowChatSessions/*.json",
            ),
        ),
    ),
    StoreDescriptor(
        agent="vscode",
        store_id="vscode.inline_history",
        role=StoreRole.PROMPT_HISTORY,
        format=StoreFormat.SQLITE,
        path_pattern="${HOME}/.config/Code/User/globalStorage/state.vscdb",
        platform_variants={
            "darwin": ("${HOME}/Library/Application Support/Code/User/globalStorage/state.vscdb"),
            "win32": "%APPDATA%/Code/User/globalStorage/state.vscdb",
        },
        env_overrides=("VSCODE_APPDATA", "AGENTGREP_WSL_USERS_ROOT"),
        observed_version="VS Code GitHub Copilot Chat (inline-chat-history)",
        observed_at=_VSCODE_OBSERVED_AT,
        upstream_ref="agentgrep.parse_vscode_inline_history",
        schema_notes=(
            "The workbench `state.vscdb` `ItemTable` holds an `inline-chat-history` "
            "key: a JSON array of the user's Ctrl+I inline-edit prompts. agentgrep "
            "reads that key alone (token-filtered), so the `secret://...` auth keys "
            "in the same database are never enumerated (see ADR 0001)."
        ),
        sample_record='inline-chat-history -> ["<redacted prompt>", "<redacted prompt>"]',
        distinguishes_from=("vscode.chat_sessions",),
        search_notes=(
            "Inline-edit prompts only (no assistant text); complements the full "
            "`vscode.chat_sessions` transcripts."
        ),
        search_by_default=True,
        discovery=(
            DiscoverySpec(
                store="vscode.inline_history",
                adapter_id="vscode.inline_history_sqlite.v1",
                path_kind="sqlite_db",
                source_kind="sqlite",
                root_key="vscode_global",
                files=("state.vscdb",),
            ),
        ),
    ),
    StoreDescriptor(
        agent="vscode",
        store_id="vscode.editing_sessions",
        role=StoreRole.SOURCE_TREE,
        format=StoreFormat.JSON_OBJECT,
        path_pattern=(
            "${HOME}/.config/Code/User/workspaceStorage/<hash>/chatEditingSessions/<sessionId>/"
        ),
        env_overrides=("VSCODE_APPDATA", "AGENTGREP_WSL_USERS_ROOT"),
        observed_version="VS Code GitHub Copilot Chat (chatEditingSessions)",
        observed_at=_VSCODE_OBSERVED_AT,
        schema_notes=(
            "Per-chat working-set snapshots written when a Copilot Chat turn edits "
            "files: `chatEditingSessions/<sessionId>/state.json` plus a `contents/` "
            "tree of pre/post file states, keyed by the same session UUID as "
            "`chatSessions/`. A byproduct of the transcripts (often empty), not a "
            "prompt source — documented so future adapters do not mistake the edit "
            "snapshots for chat history."
        ),
        distinguishes_from=("vscode.chat_sessions",),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
    ),
    StoreDescriptor(
        agent="vscode",
        store_id="vscode.auth",
        role=StoreRole.APP_STATE,
        format=StoreFormat.SQLITE,
        path_pattern="${HOME}/.config/Code/User/globalStorage/state.vscdb",
        env_overrides=("VSCODE_APPDATA", "AGENTGREP_WSL_USERS_ROOT"),
        observed_version="VS Code GitHub Copilot Chat (state.vscdb secrets)",
        observed_at=_VSCODE_OBSERVED_AT,
        schema_notes=(
            "The same global `state.vscdb` holds `secret://...` keys with provider "
            "OAuth tokens and API keys alongside the searchable `inline-chat-history`. "
            "Documented but never enumerated: the inline-history adapter is "
            "token-filtered to its one key, so these auth keys are never read "
            "(see ADR 0001)."
        ),
        distinguishes_from=("vscode.inline_history",),
        coverage=StoreCoverage.PRIVATE,
        search_by_default=False,
    ),
)
