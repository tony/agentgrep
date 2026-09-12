(backend-antigravity-cli)=

# Antigravity CLI

Antigravity CLI contributes two distinct surfaces: a searchable prompt recall
log and inspectable conversation artifacts. agentgrep searches the prompt log
by default. Targeted effort can resolve a prompt `conversationId` to its
readable transcript or corresponding conversation database. The protobuf-backed
database remains best-effort because its schema is not public.

Base path: `~/.gemini/antigravity-cli` (no observed env override).

`observed_version`: `agy v1.2.1` (observed 2026-09-11).

Antigravity CLI is a separate backend from Gemini CLI even though both
store data under `~/.gemini`.

## Stores

```{storage:agent} antigravity-cli
```

## Record schemas

### Prompt recall log

{storage:storeref}`antigravity-cli.history` is a prompt recall log in `history.jsonl`.
Each line carries `display` (prompt text), `timestamp` (Unix milliseconds),
`workspace`, optional `type`, and optional `conversationId`. agentgrep emits
these rows as prompt records with `role="user"`.

### Conversation databases

{storage:storeref}`antigravity-cli.conversations` is one SQLite database per
conversation at `conversations/<conversation_uuid>.db`. The observed `steps`
table stores protobuf data in `step_payload`; companion metadata tables also
use protobuf blobs. There is no published schema, so agentgrep extracts
readable protobuf strings best-effort and exposes the store only when
non-default inventory sources are requested or a targeted prompt locator
selects that exact conversation.

The `steps` blobs carry the transcript text and no model; the model is one
table over, in the `gen_metadata` protobuf Struct (with `executor_metadata`
as the fallback). agentgrep reads it once per database and applies it to
every record that database yields. A database predating those tables still
yields its step records, without a model.

### Readable transcripts

{storage:storeref}`antigravity-cli.transcript` is a readable JSONL conversation log at
`brain/<conversation_uuid>/.system_generated/logs/transcript_full.jsonl`. Each
line is a step record with a universal `step_index` plus `type`, `source`,
`status`, `created_at`. agentgrep surfaces the string `content`
(user/assistant turns); lines without `content` — `thinking`/`tool_calls`-only
lines (e.g. `PLANNER_RESPONSE`) and payload-less lines (e.g.
`CONVERSATION_HISTORY`) — yield no record. This is the readable counterpart to
the opaque protobuf {storage:storeref}`antigravity-cli.conversations` and reaches text
the brain Markdown glob cannot. agentgrep discovers the untruncated
`transcript_full.jsonl` (skipping the `transcript.jsonl` sibling) and exposes
it as an inspectable store.

### Skills

{storage:storeref}`antigravity-cli.skills` covers
`~/.gemini/config/skills/<name>/SKILL.md`, the directory agy's binary names
as where it loads skills from; it did not exist when observed. Installed
plugins bring their own skills under `~/.gemini/config/plugins/<plugin>/`,
catalogued as {storage:storeref}`antigravity-cli.plugins`. The
`skills/<skill>/SKILL.md` definitions seen under the agy home were placed by
`gh skill install` and are catalogued apart as
{storage:storeref}`antigravity-cli.gh_skills`, since agy never names that
directory. None of them is searched: skills are instructions, not history.

### Configuration directory

agy keeps its configuration in `~/.gemini/config/`: `config.json`, the
`import_manifest.json` imports list, and `mcp_config.json` with its MCP
servers, catalogued as {storage:storeref}`antigravity-cli.config`,
{storage:storeref}`antigravity-cli.import_manifest`, and
{storage:storeref}`antigravity-cli.mcp_config`. Under the agy home, `mcp/<server>/`
caches each MCP server's tool definitions, catalogued as
{storage:storeref}`antigravity-cli.mcp_tools`. None is searched.

### Implicit artifacts (encrypted, unsupported)

{storage:storeref}`antigravity-cli.implicit` files at
`implicit/<conversation_uuid>.pb` are high-entropy bytes with no extractable
UTF-8 runs and no protobuf field framing: they are encrypted or custom-encoded,
so agentgrep cannot read them. The store is catalogued for storage inventory
only and is never searched.

The encryption is on the loose `.pb` file, not on protobuf — the protobuf blobs
inside {storage:storeref}`antigravity-cli.conversations`' SQLite rows still
decode.

## Project context

| Store | `model` | `cwd` | `branch` |
|-------|---------|-------|----------|
| {storage:storeref}`antigravity-cli.history` | — | each line's `workspace` | — |
| {storage:storeref}`antigravity-cli.conversations` | `gen_metadata`, else `executor_metadata` | — | — |

The prompt recall log writes the workspace path literally, so its records
are {ref}`lossless <backend-cwd-tiers>` and answer `--cwd` and `cwd:`. The
conversation databases record no working directory at all, so they stay
out of an origin filter no matter which scope you search at.

The model Antigravity records is a coarse family (`gemini-pro-agent`)
rather than a version-pinned slug, so `model:` groups and filters
Antigravity conversations without telling you the exact build.

## Changes by version

Each entry brackets a change between the observation that first saw it and
the last one that did not; see {ref}`storage-observations`.

### 1.2.1

Seen in 1.2.1 (2026-09-11); absent in 1.1.11 (2026-08-08).

- `skills/<skill>/SKILL.md` appears, catalogued as
  {storage:storeref}`antigravity-cli.gh_skills`.
- `~/.gemini/config/import_manifest.json`, an `imports` list, appears in the
  shared `~/.gemini/config/` directory. agy's binary names the file and Gemini
  CLI's does not. It is catalogued as
  {storage:storeref}`antigravity-cli.import_manifest`.
