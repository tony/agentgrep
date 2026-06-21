(backend-windsurf)=

# Windsurf (unsupported)

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

### windsurf.cascade

`cascade/<session_uuid>.pb` — per-session Cascade conversation
transcript, opaque encrypted binary (often multi-megabyte). No readable
text is recoverable; the row documents the transcript location only. The
top-level `~/.codeium/cascade/` directory mirrors this for the
non-Windsurf Codeium install.

### windsurf.implicit, windsurf.chat_state, windsurf.memories

`implicit/<uuid>.pb` (background context capture), `chat_state/<name>.pb`
(per-file legacy chat state), and `memories/<uuid>.pb` (Cascade memory)
are all opaque encrypted binary, documented by location only.

### windsurf.brain and windsurf.global_rules

`brain/<uuid>/plan.md` (agent-authored plans) and
`memories/global_rules.md` (user-authored global rules — the Windsurf
analogue of `CLAUDE.md`) are readable Markdown, but documented by
location only because Windsurf as a whole is unsupported.
