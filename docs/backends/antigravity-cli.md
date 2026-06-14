(backend-antigravity-cli)=

# Antigravity CLI

Base path: `~/.gemini/antigravity-cli` (no observed env override).

`observed_version`: `agy v1.0.8` (observed 2026-06-14).

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

### antigravity-cli.implicit

`implicit/<conversation_uuid>.pb` files are protobuf transcript artifacts
without a published schema. They are inspectable only and share the same
best-effort protobuf text extraction path as conversation databases.
