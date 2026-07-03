(backend-antigravity-cli)=

# Antigravity CLI

Base path: `~/.gemini/antigravity-cli` (no observed env override).

`observed_version`: `agy v1.0.10` (observed 2026-06-21).

Antigravity CLI is a separate backend from Gemini CLI even though both
store data under `~/.gemini`. The CLI prompt recall log is plain JSONL and
is searched by default. Full transcript artifacts are SQLite databases with
protobuf blobs, so agentgrep keeps them inspectable only.

## Stores

```{storage:agent} antigravity-cli
```

## Record schemas

### antigravity-cli.history

`history.jsonl` is a prompt recall log. Each line carries `display`
(prompt text), `timestamp` (Unix milliseconds), `workspace`, optional
`type`, and optional `conversationId`. agentgrep emits these rows as
prompt records with `role="user"`.

### antigravity-cli.conversations

`conversations/<conversation_uuid>.db` is one SQLite database per
conversation. The observed `steps` table stores protobuf data in
`step_payload`; companion metadata tables also use protobuf blobs. There is
no published schema, so agentgrep extracts readable protobuf strings
best-effort and exposes the store only when non-default inventory sources
are requested.

### antigravity-cli.transcript

`brain/<conversation_uuid>/.system_generated/logs/transcript_full.jsonl`
is a readable JSONL log of the conversation. Each line is a step record
with a universal `step_index` plus `type`, `source`, `status`,
`created_at`. A string `content` holds user/assistant turns; some lines
add readable payload in `thinking`/`tool_calls` (which may co-occur with
`content`, not only replace it); and some lines (e.g.
`CONVERSATION_HISTORY`) carry none of the three. This is the readable
counterpart to
the opaque protobuf `antigravity-cli.conversations` and reaches text the
brain Markdown glob cannot. agentgrep discovers the untruncated
`transcript_full.jsonl` (skipping the `transcript.jsonl` sibling) and
exposes it as an inspectable store.

### antigravity-cli.implicit

`implicit/<conversation_uuid>.pb` files are protobuf transcript artifacts
without a published schema. They are inspectable only and share the same
best-effort protobuf text extraction path as conversation databases.
