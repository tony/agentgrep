(backend-windsurf)=

# Windsurf (unsupported)

Windsurf is catalogued so readers can see that agentgrep looked for the
backend and why it does not appear in normal search. Its Cascade transcript
files are opaque encrypted protobuf blobs, so the backend remains documented
but unsupported.

Base path: `~/.codeium/windsurf` (Codeium's Windsurf "Cascade" agent).

`observed_version`: Windsurf Cascade (observed 2026-06-21).

```{warning}
Windsurf is **documented but unsupported**. Its per-session Cascade
conversation transcripts (`cascade/`, `implicit/`, `chat_state/`,
`memories/` as `.pb`) are high-entropy, apparently-encrypted binary:
the payloads are not gzip/zlib and yield no extractable UTF-8 text, so
agentgrep cannot read them without Codeium's format and key. agentgrep
catalogues the storage locations for inventory completeness but does
**not** cover Windsurf — it is excluded from `--agent` selection and
default search.
```

## Stores

```{storage:agent} windsurf
```

All rows are catalog-only: agentgrep documents *where* the data lives
but does not parse it.

## Record schemas

### Cascade transcripts

{storage:storeref}`windsurf.cascade` covers per-session Cascade conversation
transcripts at `cascade/<session_uuid>.pb`. They are opaque encrypted binary
(often multi-megabyte). No readable text is recoverable; the row documents the
transcript location only. The top-level `~/.codeium/cascade/` directory mirrors
this for the non-Windsurf Codeium install.

### Implicit, chat-state, and memory blobs

{storage:storeref}`windsurf.implicit`, {storage:storeref}`windsurf.chat_state`, and
{storage:storeref}`windsurf.memories` cover `implicit/<uuid>.pb` (background context
capture), `chat_state/<name>.pb` (per-file legacy chat state), and
`memories/<uuid>.pb` (Cascade memory). They are all opaque encrypted binary,
documented by location only.

### Brain plans and global rules

{storage:storeref}`windsurf.brain` and {storage:storeref}`windsurf.global_rules` cover
`brain/<uuid>/plan.md` (agent-authored plans) and `memories/global_rules.md`
(user-authored global rules — the Windsurf analogue of `CLAUDE.md`). They are
readable Markdown, but documented by location only because Windsurf as a whole
is unsupported.
