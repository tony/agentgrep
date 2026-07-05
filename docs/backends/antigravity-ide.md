(backend-antigravity-ide)=

# Antigravity IDE

Antigravity IDE is an inventory-first backend. Its persisted conversations are
protobuf artifacts with no published schema, so agentgrep catalogues the
locations and exposes best-effort text inspection without adding them to
default search.

Base path: `~/.gemini/antigravity` (no observed env override).

`observed_version`: Google Antigravity IDE (observed 2026-06-21).

Antigravity IDE is documented as its own backend instead of being folded
into Gemini CLI.

## Stores

```{storage:agent} antigravity-ide
```

## Record schemas

### Conversation protobufs

{storage:storeref}`antigravity-ide.conversations` stores one protobuf conversation
artifact at `conversations/<conversation_uuid>.pb`. agentgrep extracts readable
UTF-8 strings best-effort and emits them as conversation-history records only
when the source is inspected.

### Implicit protobufs

{storage:storeref}`antigravity-ide.implicit` files at
`implicit/<conversation_uuid>.pb` are additional protobuf conversation
artifacts. They use the same inspectable, best-effort protobuf text path as the
primary conversation files.

### Brain and skills Markdown

{storage:storeref}`antigravity-ide.brain` and {storage:storeref}`antigravity-ide.skills` are
Markdown planning, memory, and instruction artifacts under `brain/**/*.md` and
`skills/**/*.md`. They are safe to inventory, but they are not prompt history or
chat transcripts and are not searched by default.

### Resolved task Markdown

{storage:storeref}`antigravity-ide.brain_resolved` covers expanded task Markdown at
`brain/<uuid>/task.md.resolved` plus numbered `.resolved.0..N` snapshots. The
`.resolved` suffix keeps it outside the `**/*.md` brain glob, so it is
catalogued separately as inspectable plan text.
