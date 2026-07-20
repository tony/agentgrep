(adr-universal-raw-storage-derived-search-indexes)=

# ADR 0015: Universal raw storage and derived search indexes

## Status

Proposed.

## Context

agentgrep reads independently versioned agent stores whose native records do
not share one message schema. The {ref}`backend catalogue <backends>` includes
append-only event logs, parent-linked trees, mutable JSON snapshots, SQLite
rows, key/value databases, protobuf blobs, Markdown and text. Some readable
stores are searched by default, some are inspectable only on explicit scopes,
and others remain catalog-only or private under {ref}`ADR 0008
<adr-unsupported-obfuscated-backends>` and the
[`StoreCoverage`](https://github.com/tony/agentgrep/blob/v0.1.0a41/src/agentgrep/stores.py#L59-L75)
contract.

The differences are semantic, not merely syntactic:

- [Codex rollouts](https://github.com/tony/agentgrep/blob/v0.1.0a41/tests/samples/codex/codex.sessions/rollout-2026-05-17T12-00-00-example.jsonl)
  interleave session metadata, turn context, response items, event messages,
  compaction and tool records.
- [Claude transcripts](https://github.com/tony/agentgrep/blob/v0.1.0a41/tests/samples/claude/claude.projects.session/example.jsonl)
  carry UUID/parent-UUID topology, sidechains and typed content blocks, while
  history expansion may depend on a sibling paste cache.
- [Pi sessions](https://github.com/tony/agentgrep/blob/v0.1.0a41/tests/samples/pi/pi.sessions/example.jsonl)
  form a parent-linked tree containing messages, model and thinking changes,
  tool results, compaction and branch summaries.
- [VS Code chat sessions](https://github.com/tony/agentgrep/blob/v0.1.0a41/tests/samples/vscode/vscode.chat_sessions/example.jsonl)
  are mutation logs whose splice and replacement events materialize a final
  snapshot.
- Cursor, Antigravity and other stores place JSON or protobuf payloads inside
  SQLite rows, while OpenCode exposes relational sessions, messages and parts.

The current {class}`~agentgrep.SearchRecord` is the correct frontend-neutral
search projection, but it is not a lossless archive. Its
[`SearchRecord` and `SourceHandle` fields](https://github.com/tony/agentgrep/blob/v0.1.0a41/src/agentgrep/records.py#L361-L455)
retain normalized text and common provenance while omitting native event types,
unknown fields, part boundaries, exact source bytes, tree edges, mutation
history, SQLite coordinates and protobuf field paths. Adapter-local or engine
deduplication can then collapse repeated text that represents distinct stored
occurrences. Expanding that projection until it contains every backend detail
would make every frontend pay for an unbounded union of upstream schemas and
would still lose facts whenever an adapter failed to anticipate a new field.

Two earlier branch studies establish useful but incomplete foundations. The
[deterministic identity ADR](https://github.com/tony/agentgrep/blob/4e49bbd3fb231e8f2c29210a3385fc4ae99ce012/docs/dev/adr/0015-deterministic-record-identity.md)
separates physical refs, semantic content, logical occurrences and threads.
The [persistent index ADR](https://github.com/tony/agentgrep/blob/76e9e61d5f569cac9333bf1e6153c332b4960d81/docs/dev/adr/0015-persistent-agentic-db-index.md)
demonstrates a useful narrow search table, wide detail table, FTS5 index and
source ledger, but it persists already-normalized records as disposable cache
state. A universal local data layer needs both ideas and one additional
boundary: **lossless source-native evidence must exist below normalized search
records.**

This ADR distinguishes two meanings of source of truth. Upstream agent stores
remain the *origin authority*: they own writes and determine whether content
still exists. After a successful ingest, agentgrep's lossless store becomes its
*primary local read source*: projections can be replayed from it without
reinterpreting the upstream file, while new, changing or unindexed content can
still stream directly from the origin authority.

## Decision

Eleven invariants govern the storage and indexing boundary (RS for *raw
storage*).

### RS-1 — Native evidence, projections and indexes are separate authorities

The data path has three layers:

1. **Origin stores** are independently written by Codex, Claude, Cursor,
   Gemini, Antigravity, Grok, Pi, OpenCode, VS Code and future agents.
2. **Raw evidence** is a lossless local mirror of admitted native entries,
   their source coordinates and their observed relationships. It is the
   primary agentgrep input after ingestion and survives projection changes.
3. **Read models** are versioned normalized records, facets and search indexes.
   They are derived, disposable and replaceable.

No adapter may deduplicate, flatten or discard a native entry before the raw
capture boundary. Dedupe, ranking and presentation operate on projections.
The current {class}`~agentgrep.SearchRecord` remains a projection rather than
becoming the raw schema.

### RS-2 — SQLite is the local container; NDJSON is the interchange format

The default repository contains two [SQLite](https://www.sqlite.org/) database
roles:

- `raw.sqlite3` stores durable evidence, the source ledger, sync history,
  health findings and migration state.
- `index-vN.sqlite3` stores normalized search rows, details, facets, coverage
  and [FTS5](https://www.sqlite.org/fts5.html) data for one index contract.

The physical separation limits the rebuild blast radius: an incompatible
tokenizer or projection may replace an index without placing raw evidence in a
drop-and-recreate path. Both databases use one serialized writer, short
transactions and independent read connections. [WAL mode](https://www.sqlite.org/wal.html)
is the default for active writable databases because readers and the writer
must coexist; checkpoint and synchronous-mode tuning remains measurement-led.

Versioned NDJSON envelopes are the import/export and diagnostic interchange
format. NDJSON is not the primary repository because point lookup, incremental
replacement, relational edges, facets, migrations and concurrent readers would
otherwise require a second control plane. Parquet, DuckDB, Lance and a
Node/pnpm sidecar are not baseline dependencies.

### RS-3 — The raw envelope preserves source-native evidence

Every admitted entry records the following logical envelope. Exact SQL column
placement is an implementation detail; the information is not.

| Group | Required evidence |
| --- | --- |
| Contract | raw-envelope schema, adapter contract and observed data version |
| Source | agent, store, format, coverage, privacy class and stable source id |
| Observation | source fingerprint, dependency manifest, discovery generation and pre/post-ingest fingerprints |
| Coordinate | format-specific location, native id, parent id, ordinal and identity quality |
| Native facts | event type, thread/session values, raw timestamp and unit, model/provider, cwd and source-time VCS facts when present |
| Payload | media type, encoding, byte length, full SHA-256 and a content-addressed BLOB reference |
| Derivation | decoder/projection versions and which fields are native, derived or synthetic |

Coordinates are format-specific:

- JSONL uses the physical line-start byte offset, line number and within-line
  candidate index.
- JSON arrays and objects use a JSON Pointer and array position while retaining
  the original containing bytes.
- SQLite uses the table plus a type-preserving primary-key tuple or rowid and,
  for key/value stores, the exact key.
- Protobuf uses the containing row/blob identity, the original blob and decoded
  field path.
- Mutation logs retain every mutation coordinate; materialized state is a
  separate projection.

JSON and JSONL payloads preserve exact bytes rather than reserialized objects,
so duplicate keys, number lexemes, whitespace, key order and invalid-text
handling are not rewritten accidentally. SQLite rows use a lossless logical
row envelope with explicit null, integer, real, text and BLOB tags; copying
database pages is neither required nor useful. Protobuf decoders preserve the
original blob before extracting searchable parts.

Large payloads remain content-addressed and out of line. Admission policy may
reject a store before ingestion, but a size limit does not silently make an
admitted source lossy. If a hard storage limit interrupts capture, ingestion
records an explicit gap with its coordinate, digest, size and reason, marks
the source incomplete, and routes that source live until a complete generation
can be captured.

### RS-4 — Identity is layered, deterministic and honest about stability

One identifier cannot represent physical location, semantic equality, logical
occurrence and revision. The repository uses domain-separated SHA-256 inputs
for these jobs:

| Identity | Meaning |
| --- | --- |
| `source_id` | One physical/native source identity |
| `raw_object_id` | Exact captured payload bytes |
| `thread_id` | Agent plus adapter-owned namespace plus native thread anchor |
| `entry_id` | One logical native occurrence; content is excluded |
| `revision_id` | One entry revision plus raw-object identity |
| `content_id` | Normalized kind, role and exact projected content |
| `record_id` | Entry plus projection selector, such as a content-block position |
| `projection_id` | Revision plus projection contract and selector |

The full 256-bit digest is retained internally. A public identity contract may
encode a full digest or a fixed pseudonymous form, but SQLite rowids, local
paths, mtimes, cwd and branches never become logical identity. Physical
`RecordRef` handles remain snapshot-relative locators rather
than database primary keys.

Every occurrence identity exposes one of four stability levels: `native`,
`anchored`, `source_order` or `synthetic`. A source-order coordinate is
deterministic but may change when earlier source content is rewritten.
Deterministic IDs must not be described as immutable, and missing identity
evidence remains null rather than being guessed from paths or timestamps.

### RS-5 — The logical schema separates durable evidence from hot search rows

The initial logical schema has these responsibilities:

| Raw database | Responsibility |
| --- | --- |
| `schema_migrations` | Ordered, checksummed durable-schema history |
| `sources` | Agent/store/adapter identity, format, privacy, locator token and lifecycle state |
| `source_observations` | Fingerprints, dependencies, checkpoints, generations and ingest outcomes |
| `raw_objects` | Content-addressed native payload BLOBs |
| `raw_entries` | Native entry, revision, coordinate, timestamp and provenance |
| `raw_edges` | Parent, reply, tool, branch, compaction, subagent and summary relationships |
| `sync_runs` | Scope, progress, cancellation and completion |
| `health_findings` | Diagnosed source, schema, integrity and repair conditions |
| `index_generations` | Published index contract, coverage and verification state |

| Read-model database | Responsibility |
| --- | --- |
| `threads` | Namespaced thread anchors and identity quality |
| `records_search` | Narrow identity, ordering and common-facet columns |
| `record_details` | Text, title, metadata and hydration fields |
| `record_parts` | Ordered text, reasoning, tool, attachment and structured parts |
| `record_facets` | Typed uncommon or multivalued facets with provenance |
| `records_fts` | Content-full trigram candidate index |
| `source_coverage` | Exact source observations and contracts represented by the index |

Agent, store, adapter, record/event kind, role, model, provider, timestamp,
thread, session, conversation, cwd, repository, worktree, branch, tool name,
status and part kind are common typed columns when available. Rare or
multivalued facts use a typed facet table rather than an unindexed JSON-only
surface. Derived facets record provenance and confidence. Branch and cwd are
source-time observations, not statements about current repository state.

### RS-6 — Freshness is per source and per contract

Freshness has four independent dimensions:

1. source bytes and declared dependencies;
2. adapter/raw-envelope contract;
3. projection and identity contracts;
4. FTS and query contracts.

Every query discovers or reconciles its source set and partitions it totally:

- **stable** sources have a ready indexed generation whose exact fingerprint
  and required contracts match;
- **live** sources are new, changed, unindexed, volatile or unsupported by the
  active index plan;
- **suspect** sources are incomplete, errored, missing from a complete
  discovery generation or associated with a failed integrity check.

No indexed result from a changed, missing or suspect source is reported as
current. A cheap stat fingerprint is a scheduling optimization, not proof when
an adapter has sibling dependencies or an upstream SQLite WAL. The source
fingerprint includes a dependency manifest such as SQLite `-wal` state, Claude
paste-cache entries, Gemini project metadata and Cursor/VS Code workspace
metadata. The existing source-scan cache already demonstrates why this is
necessary by [excluding Claude history when its sibling dependency cannot be
represented](https://github.com/tony/agentgrep/blob/v0.1.0a41/src/agentgrep/_engine/scanning.py#L327-L365).

Ingestion fingerprints before extraction and again before commit. A mismatch
rolls the staged generation back and leaves the source live. Tombstoning and
pruning require a complete, uncapped discovery generation; cancellation,
partial scopes and failed discovery never prove absence.

### RS-7 — Index coverage accelerates queries but never changes semantics

The query planner merges stable indexed sources and live source scans through
the single-owner sorted-stream contract from {ref}`ADR 0014
<adr-result-order-limit-and-streaming-merge>`:

```text
discover and classify
  -> stable indexed streams + changed or unindexed live streams
  -> candidate narrowing
  -> exact normalized-record matcher
  -> deterministic merge, dedupe and ordering barrier
  -> one global limit
  -> coverage and completion summary
```

FTS5 and SQL predicates are candidate generators. Hydrated candidates pass the
same Python semantic oracle as live records before they can affect results.
Queries the index cannot represent exactly must scan indexed rows or route the
affected sources live; they never silently under-return. The
[FTS5 trigram tokenizer](https://www.sqlite.org/fts5.html#the_trigram_tokenizer)
supports substring candidates, but terms shorter than three Unicode code
points need an explicit fallback.

The default mode is `auto`: use every verified stable partition and stream the
rest live. `off` bypasses persistent reads and writes for the invocation.
`require` fails with structured coverage diagnostics rather than silently
falling back. First use remains correct without a database, and an index may be
deleted without making search unavailable.

### RS-8 — Sync is resumable, prioritized and format-aware

Background sync prioritizes active sessions, recently modified sources,
changed indexed sources and then older backfill. Scheduling policy improves
time to value but does not confer trust; only a committed matching observation
does.

Per-format checkpoints obey these rules:

- JSONL tails from the last complete newline only while file identity, size
  and an anchor digest still match. Truncation, rotation or prefix rewrite
  causes source replacement.
- Upstream SQLite is opened read-only and query-only. Main-database and WAL
  evidence participate in freshness; native high-water keys are used only
  where the adapter proves monotonicity.
- Mutable JSON, Markdown and text sources replace their source generation when
  their fingerprint changes.
- Sibling-expanding adapters declare and fingerprint dependencies or remain
  live/volatile.

Watchers may wake the synchronizer early, but they are hints rather than the
correctness mechanism. Periodic and query-time reconciliation detects missed
events, offline writes and sources created before a watcher started.

### RS-9 — Raw migrations are durable; index migrations are rebuilds

`raw.sqlite3` uses ordered, checksummed, forward migrations. A destructive raw
migration requires a recoverable backup and may not rewrite `raw_objects`
merely because a decoder or projection changed. The database records separate
raw-schema, identity, adapter, projection and FTS/query contract versions.

An incompatible read model builds beside the active generation, runs SQLite
`integrity_check` and FTS integrity verification, closes successfully, and is
then published atomically. Existing readers may finish against the old
generation. Readers that encounter an unknown or incomplete generation route
live. The rebuild lease lives outside the index file it may replace.

Status and audit operations open read-only and never create, migrate, rebuild
or repair. This follows the useful separation in Codex between its
[WAL-backed state runtime](https://github.com/openai/codex/blob/5bed6447998c754d154dbd796517310b8f04d4ce/codex-rs/state/src/runtime.rs)
and [read-only audit path](https://github.com/openai/codex/blob/5bed6447998c754d154dbd796517310b8f04d4ce/codex-rs/state/src/audit.rs).

Derived-index corruption may trigger a visible generation rebuild. Durable raw
corruption never triggers automatic deletion: agentgrep preserves or
quarantines the damaged file, reports the finding, and reconstructs a
replacement from readable origin stores. A source-level mismatch causes an
atomic source reingest. Every repair has a machine-readable dry-run form.

### RS-10 — One event stream serves synchronous and non-blocking consumers

The synchronous event iterator is the computation contract because current
adapters perform blocking filesystem and SQLite work. Its asynchronous adapter
uses one worker and a queue bounded by both record count and bytes, extending
the existing [`aiter_search_events`](https://github.com/tony/agentgrep/blob/v0.1.0a41/src/agentgrep/_engine/search.py#L173-L264)
pattern. Collection APIs consume that same stream; no frontend-specific
callback lifecycle may become a second source of search semantics.

Every event carries a schema version, run id and monotonic sequence. The stream
must report start before discovery, phase progress for discovery/index/live
scan/merge, coverage changes, bounded record batches, recoverable diagnostics,
and one terminal status from {ref}`ADR 0004
<adr-headless-query-planning-non-blocking-execution>`. Cancellation reaches
discovery, ingestion, parsing and merge rather than merely suppressing late UI
updates.

Textual consumes the async boundary from `thread=True` workers, applies
count/byte-bounded chunks on the pump, and generation-gates all cross-thread
delivery under {ref}`ADR 0011 <adr-non-blocking-tui-invariants>`. The existing
[`offload`, `stream_apply` and gated-emitter helpers](https://github.com/tony/agentgrep/blob/v0.1.0a41/src/agentgrep/ui/_runtime.py#L255-L338)
remain the working pattern. Index open, migration, integrity checking, raw
hydration, filtering, sorting and syntax highlighting are not app-construction
or message-pump work.

Strict CLI/MCP collection emits only records released by the completeness
barrier. A TUI may opt into provisional upserts only when the protocol also
supports replacement/retraction and a final sealed order; scan-order appends
must not masquerade as final global ordering.

### RS-11 — Expensive enrichment stays outside lexical correctness

The baseline repository does not create embeddings, download models, load a
vector extension or start a model daemon. Rendered Rich/Textual content,
highlights, transient ranking scores, query result sets and progress frames are
also not persisted.

Future embeddings live in a separate disposable store keyed by `content_id`,
provider, model revision, chunker/identity contract, dimensions and metric.
Repeated occurrences may share one vector without merging their occurrence
identities. This follows the opt-in dependency and provisioning boundary from
{ref}`ADR 0005 <adr-local-insights-reports-model-backed-enrichment>`: lexical
search neither opens nor migrates a vector database.

## Privacy and retention

Raw capture applies the same catalogue admission policy as runtime discovery.
Private stores are never enumerated or copied. Catalog-only data is not copied
unless a future ADR defines an explicitly safe inventory payload. Searchable
and inspectable stores retain only the evidence required by their admitted
adapter contract.

The database is local private state and must be created with owner-only
permissions. Machine-readable public surfaces continue to redact local paths,
remote credentials, query strings and fragments under {ref}`ADR 0006
<adr-public-cli-mcp-surface-contract>`. Stable identifiers are pseudonyms, not
secrets or access-control credentials.

The default retention mode is **mirror**, not archive. A source absent from a
complete discovery generation stops contributing current results immediately,
then its raw revisions become eligible for policy-driven garbage collection.
An archive mode that retains origin-deleted content requires explicit opt-in,
visible storage accounting and a separate deletion contract. This prevents a
user from deleting sensitive upstream history while an unexpected permanent
copy remains hidden in agentgrep.

## Operational surface

The eventual CLI and machine interfaces expose lifecycle rather than hidden
side effects through `agentgrep db status --json`, `agentgrep db sync`,
`agentgrep db verify`, `agentgrep db repair --dry-run`,
`agentgrep db rebuild-index`, `agentgrep query explain`, and
`agentgrep export --format ndjson`.

Names are provisional until their public-surface implementation lands. The
required concepts are not: status is read-only; sync is explicit and
cancellable; verify distinguishes raw/index/FTS health; repair previews its
scope; rebuild never threatens raw evidence; explain states which sources are
stable, live or suspect and why.

Every machine result reports provenance, freshness, identity stability,
coverage, completion and fallback reason. A cache hit, live fallback,
approximation, omitted payload or repair is never silent.

## Test obligations

The storage boundary is not complete until tests prove:

- one redacted native fixture per supported adapter shape can round-trip into
  the raw envelope and reproduce its normalized projection;
- repeated identical text retains distinct native occurrences;
- live-only, indexed-only and hybrid plans produce the same semantic result
  set, order, dedupe and global limit;
- JSONL append, partial line, truncation, rotation and prefix rewrite choose
  the correct checkpoint path;
- SQLite WAL and declared sibling changes invalidate coverage;
- cancellation during discovery, ingest and merge leaves no published partial
  generation;
- every retained raw schema migrates forward and every incompatible index
  contract rebuilds beside the active generation;
- raw, index and FTS corruption select the documented non-destructive repair;
- a fake-clock event stream proves ordering, coverage, cancellation and
  count/byte backpressure without sleeps;
- Textual Pilot tests prove stale-generation rejection and bounded pump work.

Tests use `tmp_path`, redacted fixtures, injected clocks/fingerprints and fake
progress sinks. They do not read a real home directory, wait for filesystem
watchers, download models or depend on timing sleeps. Benchmarks compare live,
indexed and hybrid paths while checking result/identity parity; speed without
parity is not a successful optimization.

## Prior art

The adopted patterns are deliberately narrower than their source systems:

- [SQLite WAL](https://www.sqlite.org/wal.html) supplies local reader/writer
  coexistence, while [FTS5](https://www.sqlite.org/fts5.html) supplies trigram
  candidate search and documents the update/integrity tradeoffs of contentful,
  contentless and external-content tables.
- [Datasette 1.0a37](https://github.com/simonw/datasette/blob/1.0a37/datasette/database.py)
  demonstrates a serialized writer and independent reader connections. The
  reusable pattern is connection ownership, not Datasette's public query API.
- [aiosqlite 0.22.1](https://github.com/omnilib/aiosqlite/blob/v0.22.1/aiosqlite/core.py)
  demonstrates an async delivery protocol over a queued worker thread, the
  same boundary used here for blocking Python/SQLite work.
- [pytest's cache provider](https://github.com/pytest-dev/pytest/blob/3fa8d9b15b733aadb8a043cca3e98447804e1f28/src/_pytest/cacheprovider.py)
  demonstrates temporary construction followed by atomic publication and
  cleanup of a concurrent loser. The index generation adopts that publication
  shape, not pytest's directory schema.
- [Codex state](https://github.com/openai/codex/tree/5bed6447998c754d154dbd796517310b8f04d4ce/codex-rs/state)
  demonstrates WAL-backed derived state, ordered migrations and read-only
  audit. Its rollout JSONL remains an origin authority, so it informs runtime
  and repair boundaries rather than agentgrep's FTS schema.

## Rejected alternatives

- **One universal normalized row:** it loses native topology, unknown fields,
  mutation history and exact evidence, while forcing frontend projections to
  carry every upstream schema.
- **Persist only `SearchRecord`:** it accelerates current queries but cannot
  replay a future projection or recover facts discarded by today's adapters.
- **Put raw evidence and replaceable FTS tables in one rebuild domain:** a
  tokenizer or projection change would place durable evidence inside the
  index's destructive migration blast radius.
- **Treat the index as current without source reconciliation:** historical
  coverage cannot detect new sources, upstream WAL writes, sibling changes or
  deletions; it admits both stale positives and missed matches.
- **Use watchers as truth:** watchers miss offline writes, overflow and start
  races. They improve scheduling only.
- **Require a completed index before search:** first use, corruption recovery
  and changed-source queries would block or fail even though live adapters can
  answer correctly.
- **Use NDJSON, Parquet or DuckDB as the only local store:** each is useful for
  interchange or analytics but adds a second mechanism for transactional
  source replacement, graph edges, point lookup, concurrent TUI reads and
  migrations.
- **Create embeddings during ingest:** it introduces model, privacy,
  provisioning and vector-schema lifecycles into a lexical correctness path
  that does not need them.
- **Auto-delete a corrupt raw database:** it converts a diagnosable local fault
  into irreversible evidence loss. Preserve, audit and reconstruct instead.
- **One canonical ID:** content equality, logical occurrence, revision,
  thread membership and physical resolution have different invariants.

## Relationship to other ADRs

{ref}`ADR 0001 <adr-storage-version-detection>` continues to decide which
adapter/raw contract interprets each observed source. This ADR persists that
evidence and its detector result; it does not replace shape-first detection.

{ref}`ADR 0004 <adr-headless-query-planning-non-blocking-execution>` owns the
query plan, driver, event stream and run-status vocabulary. This ADR adds
source coverage, index/live partitioning and sync phases to those contracts.

{ref}`ADR 0006 <adr-public-cli-mcp-surface-contract>` owns public envelopes,
diagnostics, cursors and refs. Database rowids and private locators do not cross
that boundary.

{ref}`ADR 0011 <adr-non-blocking-tui-invariants>` owns the Textual pump and
worker discipline. This ADR requires database and hydration work to obey it.

{ref}`ADR 0014 <adr-result-order-limit-and-streaming-merge>` owns global order,
the completeness barrier and the rule that limit follows order. Indexed and
live sources are inputs to that one merge rather than independent result sets.

## Consequences

agentgrep gains a stable internal raw-data contract even when upstream stores
disagree. New projections, facets and query indexes can be built without
re-reading mutable origin files or pretending today's `SearchRecord` captured
everything. Deterministic identities, explicit coverage and source-native
coordinates make results inspectable by people and agents. A verified index
can answer broad searches early while live fallback preserves first-use and
changed-source correctness.

The cost is a real storage subsystem. Exact native payloads consume disk and
may duplicate sensitive local data, so permissions, retention, deletion and
storage accounting become product contracts. Adapter authors must define
coordinates, dependencies, fingerprints, incremental strategy and projection
rules. Hybrid ordering and coverage add state to the planner. Durable raw
migrations require longer compatibility testing than replaceable indexes.

The chief footgun is allowing a convenient derived layer to become silent
authority. The mitigation is structural: raw and index databases have
different migration policies; every indexed row names the source observation
and contracts that justify it; every query classifies all sources; every
fallback and partial result is reported; and every destructive action stops at
the raw boundary unless the user explicitly selects a retention operation.

## Final position

agentgrep stores what each backend actually wrote before deciding what that
evidence means for search. SQLite holds the lossless local mirror and a
separate replaceable read model; deterministic identities describe content,
occurrence, revision and thread without conflating them; per-source coverage
decides indexed versus live work; the existing semantic matcher and ordered
merge remain authoritative; migrations preserve raw evidence and rebuild
derived indexes; and optional embeddings stay outside the baseline. That is
the smallest universal storage contract that can represent every readable
backend without making their inconsistencies the user's query problem.
