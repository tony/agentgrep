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

The DB stores source ledger rows, source state, and normalized
records split into a search read-model: a narrow `records_search`
table (identity, sort, session, and hash columns), a `record_details`
table with the text/title/role/model/metadata payload, and a
content-full trigram FTS5 table that owns the casefolded haystack.
This keeps the default backend local, transactional, and inspectable
while letting search touch dense pages.

Limited searches run a keyset probe: lean columns ordered by
`(COALESCE(timestamp,''), agent, path, rowid) DESC` in windows of
`max(4*limit, 200)`, hydrating each page's admitted rows from
`record_details` and sealing the window only when `limit` records
survive the scope filter, per-session dedup, and the `matches_record`
oracle. Under-filled windows continue from a row-value keyset cursor,
so dedup collapse and oracle rejections can never starve the result.
Unlimited searches reuse the same lean fetch and deterministic order.
The probe phases appear in profiles as `records.probe_fts` /
`records.probe_scan` / `records.hydrate` statement samples.

Repeated syncs consult `source_state` fingerprints before opening
record iterators, so unchanged source files are skipped unless the
caller uses `--force`.

## Commands

Use the CLI pages for the exact parser surface:

- {ref}`cli-db`
- {ref}`cli-db-sync`
- {ref}`cli-db-status`
- {ref}`cli-db-explain`

The architecture decision is {doc}`adr/0015-persistent-agentic-db-index`.
