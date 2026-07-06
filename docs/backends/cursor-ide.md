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

Per-workspace Cursor stores contribute `origin.cwd_hash` from the
`workspaceStorage/<hash>` directory. When the sibling `workspace.json`
records a folder URI, agentgrep resolves it to `origin.cwd` too. That
lets `--cwd`, `cwd:`, MCP `cwd`, and `cwd_hash:` target one Cursor
workspace without opening unrelated workspace databases.

Global `state.vscdb` records stay conservative when no workspace origin
is known. They remain searchable by text, agent, scope, and other
non-origin fields, but they do not satisfy a hard current-project
filter unless the stored record itself carries project metadata.

## Cross-host discovery on WSL

Cursor is a VS Code fork, so when its UI runs on a Windows host and edits
a project inside WSL, the IDE chat is written client-side on Windows under
`/mnt/c/Users/<user>/AppData/Roaming/Cursor/User`, not inside the distro.
On WSL, agentgrep detects this and also probes the Windows users mount so
both the global and per-workspace `state.vscdb` databases are searchable
from Linux. `AGENTGREP_WSL_USERS_ROOT` overrides the mount root (default
`/mnt/c/Users`) for non-default drive letters. See
{doc}`../dev/adr/0009-cross-host-discovery` for the design.
