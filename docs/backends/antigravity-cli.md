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

### Implicit protobuf artifacts

{storage:storeref}`antigravity-cli.implicit` files at
`implicit/<conversation_uuid>.pb` are protobuf transcript artifacts without a
published schema. They are inspectable only and share the same best-effort
protobuf text extraction path as conversation databases.
