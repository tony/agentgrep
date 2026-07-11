(backend-cursor-ide)=

# Cursor IDE

The Cursor desktop application, modelled as its own backend
(`cursor-ide`) separate from {doc}`the terminal CLI <cursor-cli>`. It
stores chat history in VS Code-style `state.vscdb` SQLite databases
under the platform user-data directory.

## Stores

```{storage:agent} cursor-ide
```

## Record schemas

### Global state database

{storage:storeref}`cursor-ide.state_vscdb` is the platform-specific global SQLite
database (`state.vscdb`). agentgrep reads known prompt and chat keys in
`ItemTable`/`cursorDiskKV`; it does not scan arbitrary state values.
Global records may not carry project origin because the database is
shared across workspaces.

| Platform | Path |
|----------|------|
| Linux | `~/.config/Cursor/User/globalStorage/state.vscdb` |
| macOS | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` |
| Windows | `%APPDATA%/Cursor/User/globalStorage/state.vscdb` |

### Workspace state databases

{storage:storeref}`cursor-ide.workspace_state` covers one `state.vscdb` per opened
project under `workspaceStorage/<hash>/`. These share the global store's
`ItemTable` shape; the `aiService.prompts` key holds that workspace's typed
prompt history. agentgrep enumerates them through the platform
`workspaceStorage` directory and parses them with the same adapter as the
global store.

## Project context

| Store | `model` | `cwd` | `branch` |
|-------|---------|-------|----------|
| {storage:storeref}`cursor-ide.state_vscdb` | `composerData` `modelConfig.modelName`, `bubbleId` `modelInfo.modelName` | `composerData` `gitWorktree.worktreePath` | `composerData` `gitWorktree.branchName` |
| {storage:storeref}`cursor-ide.workspace_state` | same composer keys | sibling `workspace.json` folder URI | `composerData` `gitWorktree.branchName` |

Cursor keeps the interesting metadata in `cursorDiskKV`, not in the
prompt keys: `composerData:<uuid>` is the session document, carrying the
model it ran under and a `gitWorktree` block with the worktree path and
branch name, and `bubbleId:<uuid>:<uuid>` is one turn of that session,
naming its own model. Both are read, and both are
{ref}`lossless <backend-cwd-tiers>` — Cursor writes the path, so `--cwd`,
`cwd:`, and `branch:` answer with the real values. A turn that ran in its
own worktree keeps its own origin rather than inheriting the session's.

Per-workspace stores also contribute `origin.cwd_hash` from the
`workspaceStorage/<hash>` directory, and agentgrep resolves the sibling
`workspace.json` folder URI to `origin.cwd`. That pair lets `--cwd`,
`cwd:`, MCP `cwd`, and `cwd_hash:` target one Cursor workspace without
opening unrelated workspace databases. The workspace `cwd` is a fact
about the database, not a promise about every record in it, so a
composer bubble that names a different worktree still wins for its own
record.

Global `state.vscdb` records stay conservative when no composer origin is
known. They remain searchable by text, agent, scope, and other non-origin
fields, but they do not satisfy a hard current-project filter unless the
stored record itself carries project metadata.

```{note}
The `composerData` and `bubbleId` key paths are verified against a Cursor
backup, not yet against a live install. Every read is guarded, so a
schema change degrades to an absent field rather than a wrong one.
```

## Cross-host discovery on WSL

Cursor is a VS Code fork, so when its UI runs on a Windows host and edits
a project inside WSL, the IDE chat is written client-side on Windows under
`/mnt/c/Users/<user>/AppData/Roaming/Cursor/User`, not inside the distro.
On WSL, agentgrep detects this and also probes the Windows users mount so
both the global and per-workspace `state.vscdb` databases are searchable
from Linux. `AGENTGREP_WSL_USERS_ROOT` overrides the mount root (default
`/mnt/c/Users`) for non-default drive letters. See
{doc}`../dev/adr/0009-cross-host-discovery` for the design.
