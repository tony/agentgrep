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

### cursor-cli.prompt_history

`~/.config/cursor/prompt_history.json` is a flat JSON array of
strings — one entry per prompt typed into `cursor-agent`, oldest
first. This is the CLI's up-arrow recall buffer and gives Cursor the
same prompt-history store the Claude, Codex, and Grok backends expose.
There are no per-entry timestamps, so records share the file's mtime.

### cursor-cli.chats

`~/.config/cursor/chats/<project_hash>/<session_uuid>/store.db` is a
per-session SQLite database with a `meta` table (`agentId`,
`latestRootBlobId`) and a `blobs` table of content-addressed protobuf
messages forming a graph from the root blob. Cursor publishes no
schema, so agentgrep walks the protobuf wire format generically and
surfaces the readable UTF-8 runs it finds — a best-effort, date-versioned
adapter. Because the extraction is noisier than and overlaps the JSONL
transcripts, the store is **inspectable** (opt-in) rather than searched
by default; include it explicitly to parse it.
