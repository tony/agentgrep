(backend-cursor)=

# Cursor

Two distinct surfaces, both catalogued and searched:

- **Cursor CLI agent** (`cursor-agent`): main and sub-agent
  transcripts at `~/.cursor/projects/<id>/agent-transcripts/`.
- **Cursor IDE**: `state.vscdb` SQLite at platform-specific locations.

## Stores

```{storage:agent} cursor
```

## Record schemas

### cursor.cli.transcripts

Anthropic-style JSONL: `role`, `message.content[]` with
`text`/`tool_use`/`tool_result` content blocks. No native per-turn
timestamp — agentgrep infers from the file's mtime.

Sub-agent dispatches nest below a session's `subagents/` directory and
share the same JSONL record shape. agentgrep reports them as the
distinct runtime store `cursor.cli_subagents` so nested sub-agent files
do not collapse into `cursor.cli_transcripts`.

### cursor.ai_tracking

SQLite with `conversation_summaries` table: `conversationId`,
`title`, `tldr`, `overview`, `summaryBullets`, `model`, `mode`,
`updatedAt`.

### cursor.ide.state_vscdb

Platform-specific SQLite (`state.vscdb`). Keys in
`ItemTable`/`cursorDiskKV` containing `chat`/`composer`/`prompt`/
`history` tokens hold conversation JSON.

| Platform | Path |
|----------|------|
| Linux | `~/.config/Cursor/User/globalStorage/state.vscdb` |
| macOS | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` |
| Windows | `%APPDATA%/Cursor/User/globalStorage/state.vscdb` |
