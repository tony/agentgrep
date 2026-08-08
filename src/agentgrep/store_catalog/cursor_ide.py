"""cursor_ide store descriptors for the agentgrep catalogue."""

from __future__ import annotations

from agentgrep.store_catalog._common import _CURSOR_IDE_OBSERVED_AT
from agentgrep.stores import (
    DiscoverySpec,
    StoreCoverage,
    StoreDescriptor,
    StoreFormat,
    StoreRole,
    VersionDetectionStrategy,
)

_CURSOR_IDE_OBSERVED_VERSION = "Cursor IDE 3.15.6"
"""App version the Cursor IDE rows below were verified against.

The observation date lives in ``observed_at`` alone. Repeating it here
is how one row drifted to a date its own module constant disagreed with.
``observations/`` records the store shapes seen at this version.
"""


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
        observed_version=_CURSOR_IDE_OBSERVED_VERSION,
        observed_at=_CURSOR_IDE_OBSERVED_AT,
        upstream_ref=("agentgrep.parse_cursor_state_db / Cursor state key selectors"),
        schema_notes=(
            "Cursor IDE chat storage; known prompt/chat keys in "
            "`ItemTable`/`cursorDiskKV` hold conversation JSON. agentgrep does "
            "not scan arbitrary state values. Cursor does not publish a formal "
            "schema — agentgrep's parser is the reference implementation. On "
            "WSL the store is discovered under the Windows-host mount too (see "
            "ADR 0009)."
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
        observed_version=_CURSOR_IDE_OBSERVED_VERSION,
        observed_at=_CURSOR_IDE_OBSERVED_AT,
        upstream_ref=("agentgrep.parse_cursor_state_db / Cursor state key selectors"),
        schema_notes=(
            "Per-workspace `state.vscdb`, one per opened project under "
            "`workspaceStorage/<hash>/`. Same `ItemTable` shape as the global "
            "store; the `aiService.prompts` key holds that workspace's prompt "
            "history. The directory hash contributes `origin.cwd_hash`; sibling "
            "`workspace.json` can contribute `origin.cwd`. Reuses the "
            "`cursor_ide.state_vscdb_modern.v1` adapter."
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
    StoreDescriptor(
        agent="cursor-ide",
        store_id="cursor-ide.composer_headers",
        role=StoreRole.APP_STATE,
        format=StoreFormat.SQLITE,
        path_pattern="${HOME}/.config/Cursor/User/globalStorage/state.vscdb",
        platform_variants={
            "darwin": "${HOME}/Library/Application Support/Cursor/User/globalStorage/state.vscdb",
            "win32": "%APPDATA%/Cursor/User/globalStorage/state.vscdb",
        },
        observed_version=_CURSOR_IDE_OBSERVED_VERSION,
        observed_at=_CURSOR_IDE_OBSERVED_AT,
        schema_notes=(
            "A third table in the same `state.vscdb` as `ItemTable` and "
            "`cursorDiskKV`, holding one row per session: `composerHeaders("
            "composerId, workspaceId, createdAt, lastUpdatedAt, isArchived, "
            "isSubagent, recency, checkpointAt, value)`. The `value` JSON carries "
            "session identity and origin — `name`, `isWorktree`, `trackedGitRepos`, "
            "`agentLocation`, `workspaceIdentifier`, `referencedPlans`. Notably it "
            "lists sessions that have neither a `composerData:` nor a `bubbleId:` "
            "row, which no other store can see at all; the readers gate to "
            "`ItemTable` and `cursorDiskKV`, so nothing opens this table today."
        ),
        distinguishes_from=("cursor-ide.state_vscdb",),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        version_strategies=(VersionDetectionStrategy.SHAPE_INFERENCE,),
    ),
    StoreDescriptor(
        agent="cursor-ide",
        store_id="cursor-ide.conversation_search",
        role=StoreRole.CACHE,
        format=StoreFormat.SQLITE,
        path_pattern="${HOME}/.config/Cursor/User/globalStorage/conversation-search.db",
        platform_variants={
            "darwin": (
                "${HOME}/Library/Application Support/Cursor/User/globalStorage/"
                "conversation-search.db"
            ),
            "win32": "%APPDATA%/Cursor/User/globalStorage/conversation-search.db",
        },
        observed_version=_CURSOR_IDE_OBSERVED_VERSION,
        observed_at=_CURSOR_IDE_OBSERVED_AT,
        schema_notes=(
            "Cursor's own FTS5 index over conversation bodies, beside "
            "`state.vscdb`. `conversations(fts_rowid, source, scope, id, title, "
            "updated_at, is_archived, root_fingerprint, cache_fingerprint)` with "
            "`source` constrained to `local` or `cloud-cache`, plus a virtual "
            "`conversation_fts(title, body)` using unicode61. Derived data — the "
            "bodies also live in `cursorDiskKV` — so it is catalogued as a cache "
            "rather than a transcript, and searching it would duplicate hits."
        ),
        distinguishes_from=("cursor-ide.state_vscdb",),
        coverage=StoreCoverage.CATALOG_ONLY,
        search_by_default=False,
        version_strategies=(VersionDetectionStrategy.SHAPE_INFERENCE,),
    ),
)
