"""cursor_ide store descriptors for the agentgrep catalogue."""

from __future__ import annotations

from agentgrep.store_catalog._common import _CURSOR_IDE_OBSERVED_AT
from agentgrep.stores import (
    DiscoverySpec,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
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
        env_overrides=("AGENTGREP_WSL_USERS_ROOT",),
        observed_version="Cursor IDE (current observed paths)",
        observed_at=_CURSOR_IDE_OBSERVED_AT,
        upstream_ref=("agentgrep.parse_cursor_state_db / CURSOR_STATE_TOKENS"),
        schema_notes=(
            "Cursor IDE chat storage; keys in `ItemTable`/`cursorDiskKV` containing "
            "`chat`/`composer`/`prompt`/`history` tokens hold conversation JSON. "
            "Cursor does not publish a formal schema — agentgrep's parser is the "
            "reference implementation. On WSL the store is discovered under the "
            "Windows-host mount too (see ADR 0009)."
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
                root_key="ide_global",
                files=("state.vscdb",),
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
        env_overrides=("AGENTGREP_WSL_USERS_ROOT",),
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
