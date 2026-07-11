(backend-antigravity-cli)=

# Antigravity CLI

Antigravity CLI contributes two distinct surfaces: a searchable prompt recall
log and inspectable conversation artifacts. agentgrep searches the prompt log
by default and keeps the protobuf-backed transcript databases opt-in because
their schema is not public.

Base path: `~/.gemini/antigravity-cli` (no observed env override).

`observed_version`: `agy v1.0.10` (observed 2026-06-21).

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
non-default inventory sources are requested.

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
