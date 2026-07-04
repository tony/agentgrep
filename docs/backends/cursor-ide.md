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

### cursor-ide.state_vscdb

Platform-specific SQLite (`state.vscdb`). Keys in
`ItemTable`/`cursorDiskKV` containing `chat`/`composer`/`prompt`/
`history` tokens hold conversation JSON.

| Platform | Path |
|----------|------|
| Linux | `~/.config/Cursor/User/globalStorage/state.vscdb` |
| macOS | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` |
| Windows | `%APPDATA%/Cursor/User/globalStorage/state.vscdb` |

### cursor-ide.workspace_state

The IDE also writes one `state.vscdb` per opened project under
`workspaceStorage/<hash>/`. These share the global store's `ItemTable`
shape; the `aiService.prompts` key holds that workspace's typed prompt
history. agentgrep enumerates them through the platform
`workspaceStorage` directory and parses them with the same adapter as
the global store.

## Cross-host discovery on WSL

Cursor is a VS Code fork, so when its UI runs on a Windows host and edits
a project inside WSL, the IDE chat is written client-side on Windows under
`/mnt/c/Users/<user>/AppData/Roaming/Cursor/User`, not inside the distro.
On WSL, agentgrep detects this and also probes the Windows users mount so
both the global and per-workspace `state.vscdb` databases are searchable
from Linux. `AGENTGREP_WSL_USERS_ROOT` overrides the mount root (default
`/mnt/c/Users`) for non-default drive letters. See
{doc}`../dev/adr/0009-cross-host-discovery` for the design.
