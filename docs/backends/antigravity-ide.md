(backend-antigravity-ide)=

# Antigravity IDE

Antigravity IDE is an inventory-only backend for chat. Its persisted
conversation artifacts are encrypted, so agentgrep catalogues where they live
but cannot read them — `--agent antigravity-ide` never returns chat recall, at
any scope. What you *can* reach is its Markdown: brain notes, resolved task
plans, and skills. For searchable Antigravity history, reach for
{ref}`backend-antigravity-cli` instead.

Base path: `~/.gemini/antigravity` (no observed env override).

`observed_version`: Google Antigravity IDE (observed 2026-06-21).

Antigravity IDE is documented as its own backend instead of being folded
into Gemini CLI.

## Stores

```{storage:agent} antigravity-ide
```

## Record schemas

### Conversation artifacts (encrypted, unsupported)

{storage:storeref}`antigravity-ide.conversations` stores one conversation
artifact at `conversations/<conversation_uuid>.pb`. The payloads are
high-entropy bytes with no extractable UTF-8 runs, no protobuf field framing,
and no gzip/zlib magic: they are encrypted or custom-encoded, and agentgrep
cannot read them. The store is catalogued for storage inventory only and is
never searched.

The encryption is on the loose `.pb` file, not on protobuf. The sibling
`user_settings.pb` is plaintext protobuf, and the protobuf blobs inside
{storage:storeref}`antigravity-cli.conversations`' SQLite rows still decode —
which is why that store stays readable while this one does not.

### Implicit artifacts (encrypted, unsupported)

{storage:storeref}`antigravity-ide.implicit` files at
`implicit/<conversation_uuid>.pb` have the same encrypted payload shape and the
same catalogue-only treatment.

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
