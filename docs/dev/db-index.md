(dev-db-index)=

# DB index

The DB index is a SQLite-backed cache and evidence store for
normalized agent records. It is derived state: Codex, Claude, Cursor,
Gemini, Grok, Pi, and OpenCode stores remain the source of truth.
Deleting the agentgrep database removes cached records and generated
artifacts, not the original agent history.

## Roles

The agentgrep db has one job: cache search results that can be served
without rescanning source stores.

Search commands default to `--cache auto`, which uses the DB index
only when it can answer the query. Use `--no-cache` to force the live
scanner or `--cache require` to require the DB path.

## Sync shape

Sync is intentionally planner-shaped:

```text
discover sources -> sync plan -> physical tasks -> bounded execution -> SQLite writers -> explain output
```

The DB stores source ledger rows, source state, normalized records,
and an FTS5 text index. This keeps the default
backend local, transactional, and inspectable.

Repeated syncs consult `source_state` fingerprints before opening
record iterators, so unchanged source files are skipped unless the
caller uses `--force`.

## Commands

Use the CLI pages for the exact parser surface:

- {ref}`cli-db`
- {ref}`cli-db-sync`
- {ref}`cli-db-status`
- {ref}`cli-db-explain`

The architecture decision is {doc}`adr/0005-persistent-agentic-db-index`.
