(backend-cursor-cli)=

# Cursor CLI

The `cursor-agent` terminal CLI, modelled as its own backend
(`cursor-cli`) separate from {doc}`the desktop IDE <cursor-ide>`. Its
data spans two home directories: the original `~/.cursor/` tree
(transcripts, plans, AI-tracking) and the newer lowercase
`~/.config/cursor/` tree (prompt history and chat `store.db` blobs).

## Stores

```{storage:agent} cursor-cli
```

## Record schemas

### cursor-cli.transcripts

Anthropic-style JSONL: `role`, `message.content[]` with
`text`/`tool_use`/`tool_result` content blocks. No native per-turn
timestamp — agentgrep infers from the file's mtime.

Sub-agent dispatches nest below a session's `subagents/` directory and
share the same JSONL record shape. agentgrep reports them as the
distinct store `cursor-cli.subagent_transcripts` so nested sub-agent
files do not collapse into `cursor-cli.transcripts`.

### cursor-cli.ai_tracking

SQLite with `conversation_summaries` table: `conversationId`,
`title`, `tldr`, `overview`, `summaryBullets`, `model`, `mode`,
`updatedAt`.
