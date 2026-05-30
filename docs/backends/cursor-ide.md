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
