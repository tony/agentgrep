(adr-cache-schema-versioning-rebuilds)=

# ADR 0016: Cache schema versioning and rebuilds

## Status

Accepted.

## Context

{ref}`adr-persistent-agentic-db-index` establishes the DB index as
derived state: Codex, Claude, Cursor, and the other agent stores remain
the source of truth, and deleting the agentgrep database loses nothing
that a resync cannot rebuild. The schema will keep evolving — tokenizer
choices, new columns, new artifact tables — and every change raises the
same question: what happens when an agentgrep build opens a database
written by a different schema?

Hand-written incremental migrations are the wrong default for a cache.
They add a permanently growing surface that must be tested against
every historical shape, and they preserve rows that the next sync would
regenerate anyway. The failure mode they guard against — data loss —
does not apply while every row is derivable from upstream stores.

## Decision

The database records a single integer schema version under the
``meta.schema_version`` key. On open, a stored version that differs
from the running build's ``SCHEMA_VERSION`` — in either direction —
drops every agentgrep table and recreates the schema empty. The next
``agentgrep db sync`` repopulates it.

Schema changes therefore ship as a one-line ``SCHEMA_VERSION`` bump,
and no incremental migration code exists.

## Consequences

- Schema changes stay cheap and honest: the new shape is the only
  shape, and no historical-migration paths accumulate.
- A version bump costs one full resync on first use. That is the
  designed trade for a derived cache.
- Two agentgrep builds with different schema versions sharing one
  database path will rebuild each other's cache on alternate opens.
  Acceptable for a local cache; pointing builds at separate paths via
  ``AGENTGREP_DB`` avoids the thrash.
- If the index ever carries non-derivable rows (user annotations,
  review decisions), that data must move out of the rebuild blast
  radius first — a future ADR gates that change.
