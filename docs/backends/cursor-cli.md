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

### Transcript JSONL

{storage:storeref}`cursor-cli.transcripts` is Anthropic-style JSONL: `role`,
`message.content[]` with `text`/`tool_use` content blocks (tool outputs live
in the separate {storage:storeref}`cursor-cli.agent_tools` store, not inline). No
native per-turn timestamp — agentgrep infers from the file's mtime.

Sub-agent dispatches nest below a session's `subagents/` directory and
share the same JSONL record shape. agentgrep reports them as the distinct store
{storage:storeref}`cursor-cli.subagent_transcripts` so nested sub-agent files do not
collapse into {storage:storeref}`cursor-cli.transcripts`.

### AI-tracking summaries

{storage:storeref}`cursor-cli.ai_tracking` is a SQLite store with a
`conversation_summaries` table: `conversationId`, `title`, `tldr`,
`overview`, `summaryBullets`, `model`, `mode`, `updatedAt`.

### Prompt history

{storage:storeref}`cursor-cli.prompt_history` is a flat JSON array at
`~/.config/cursor/prompt_history.json` — one entry per prompt typed into
`cursor-agent`, oldest first. This is the CLI's up-arrow recall buffer and
gives Cursor the same prompt-history store the Claude, Codex, and Grok
backends expose. There are no per-entry timestamps, so records share the
file's mtime.

### Protobuf chat databases

{storage:storeref}`cursor-cli.chats` is a per-session SQLite database at
`~/.config/cursor/chats/<project_hash>/<session_uuid>/store.db`. Its `meta`
table holds a single row keyed `'0'` with hex-encoded JSON metadata
(`agentId`, `latestRootBlobId`, …), alongside a `blobs` table of
content-addressed protobuf messages forming a graph from the root blob.
Cursor publishes no
schema, so agentgrep walks the protobuf wire format generically and
surfaces the readable UTF-8 runs it finds — a best-effort, date-versioned
adapter. Because the extraction is noisier than and overlaps the JSONL
transcripts, the store is **inspectable** (opt-in) rather than searched
by default; include it explicitly to parse it.

### Skills

{storage:storeref}`cursor-cli.skills` covers `SKILL.md` definitions installed for
cursor-agent under `~/.cursor/skills/` (user) and
`~/.cursor/skills-cursor/` (built-in). Instruction content that steers future
sessions — inspectable (opt-in), parity with {storage:storeref}`claude.skills`.

### Uploads

{storage:storeref}`cursor-cli.uploads` covers Markdown attachments the user fed the
agent as conversation input, under `~/.cursor/projects/<id>/uploads/*.md`.
Inspectable (opt-in) supplementary content, not searched by default.

## Project context

| Store | `model` | `cwd` | `branch` |
|-------|---------|-------|----------|
| {storage:storeref}`cursor-cli.transcripts` | — | `projects/<name>/`, dash-decoded | — |
| {storage:storeref}`cursor-cli.subagent_transcripts` | — | `projects/<name>/`, dash-decoded | — |
| {storage:storeref}`cursor-cli.chats` | `meta` row's `lastUsedModel` | never — `cwd_hash` only, from `chats/<project_hash>/` | — |
| {storage:storeref}`cursor-cli.prompt_history` | — | — | — |

Nothing inside a Cursor CLI transcript says where the session ran. The
only record of it is the `projects/<name>/` directory the file sits
under, and that name is the working directory with every separator
replaced by `-` and nothing escaped — the
{ref}`lossy tier <backend-cwd-tiers>`. agentgrep reconstructs the name
against the filesystem and reports `origin.cwd` only when exactly one
reconstruction resolves to a directory that exists. When two do, or when
the project has since been moved or deleted, the record carries no `cwd`
at all rather than a plausible one: it drops out of a `--cwd` filter
instead of answering it with a path you never worked in.

The decode runs once per project name per discovery pass, and its memo
lives and dies with that pass, so a long-running TUI or MCP server never
answers from a directory layout that has since changed.

### The chat store's working directory is not recoverable

{storage:storeref}`cursor-cli.chats` sits under
`chats/<project_hash>/<session_uuid>/`, and that first segment is a
digest, so the store answers `cwd_hash:` and never `cwd:` — the
{ref}`digest tier <backend-cwd-tiers>`. The literal path does appear
inside the store, but only as unstructured bytes inside the protobuf
blobs, interleaved with unrelated file paths and with no key that
reliably yields it, so there is nothing to read it out of. The digest
comes from the path segment; it is never manufactured by hashing a `cwd`
recovered from the transcripts next door. Treat `origin.cwd` here as
asked and answered: it stays unset by design.

The same database's `meta` row holds hex-encoded JSON whose
`lastUsedModel` names the model the session ran on, so `model:` does
reach this store. Cursor's `default` sentinel is rejected rather than
reported as a slug.
