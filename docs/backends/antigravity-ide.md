(backend-antigravity-ide)=

# Antigravity IDE

Base path: `~/.gemini/antigravity` (no observed env override).

`observed_version`: Google Antigravity IDE (observed 2026-06-21).

Antigravity IDE is documented as its own backend instead of being folded
into Gemini CLI. Its conversation and implicit transcript files are
protobuf artifacts with no published schema, so agentgrep inventories them
as inspectable sources and leaves them out of default search.

## Stores

```{storage:agent} antigravity-ide
```

## Record schemas

### antigravity-ide.conversations

`conversations/<conversation_uuid>.pb` stores one protobuf conversation
artifact. agentgrep extracts readable UTF-8 strings best-effort and emits
them as conversation-history records only when the source is inspected.

### antigravity-ide.implicit

`implicit/<conversation_uuid>.pb` files are additional protobuf conversation
artifacts. They use the same inspectable, best-effort protobuf text path as
the primary conversation files.

### antigravity-ide.brain and antigravity-ide.skills

`brain/**/*.md` and `skills/**/*.md` are Markdown planning, memory, and
instruction artifacts. They are safe to inventory, but they are not prompt
history or chat transcripts and are not searched by default.

### antigravity-ide.brain_resolved

`brain/<uuid>/task.md.resolved` (plus numbered `.resolved.0..N`
snapshots) is the expanded task Markdown. The `.resolved` suffix keeps
it outside the `**/*.md` brain glob, so it is catalogued separately as
inspectable plan text.
