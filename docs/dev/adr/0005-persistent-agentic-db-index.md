(adr-persistent-agentic-db-index)=

# ADR 0005: Persistent agentic DB index

## Status

Accepted.

Initial implementation landed with `agentgrep db sync|status|explain`,
a SQLite/WAL/FTS5 `DbStore`, `DbRuntime`, cache-aware
`SearchRuntime`, and `--cache` / `--no-cache` controls for search-shaped CLI
commands.

## Context

agentgrep currently treats local agent history stores as the live search
surface. That keeps the tool read-only and simple, but it also means broad
queries repeatedly pay for source discovery, source planning, JSON/JSONL
parsing, SQLite reads, text prefiltering, and record normalization.

Recent local profiling showed the current bottleneck shape:

- Prompt search is already fast enough for many interactive uses, around
  0.35 seconds for the local all-agent limited profile.
- Conversation search is still meaningfully slower, around 1.45 seconds for
  the local all-agent limited profile, because it repeatedly plans and
  collects across thousands of conversation sources.

{ref}`adr-headless-query-planning-non-blocking-execution` gives agentgrep a
planner and execution boundary. The next durable step is to give that planner
a persistent, privacy-safe DB index that can answer common source,
record, and text questions without reparsing every upstream store on every
query.

Prior systems point to the same direction:

- Lance treats table versions, fragments, and indices as separate lifecycle
  objects. Its table and index specifications show why an index can be
  incomplete and still useful when the query planner can split work into
  indexed and unindexed fragments:
  [table format](https://github.com/lance-format/lance/blob/8760b24b926e140b2809885dd061d1074cfc1722/docs/src/format/table/index.md),
  [index format](https://github.com/lance-format/lance/blob/8760b24b926e140b2809885dd061d1074cfc1722/docs/src/format/index/index.md),
  and [transaction format](https://github.com/lance-format/lance/blob/8760b24b926e140b2809885dd061d1074cfc1722/docs/src/format/table/transaction.md).
- Lucene keeps writes and reads centered on durable segment metadata. Its
  `IndexWriter`, `SegmentInfos`, and `IndexSearcher` contracts are the useful
  pattern for immutable search state and reader snapshots:
  [IndexWriter](https://github.com/apache/lucene/blob/releases/lucene/9.12.3/lucene/core/src/java/org/apache/lucene/index/IndexWriter.java),
  [SegmentInfos](https://github.com/apache/lucene/blob/releases/lucene/9.12.3/lucene/core/src/java/org/apache/lucene/index/SegmentInfos.java),
  and [IndexSearcher](https://github.com/apache/lucene/blob/releases/lucene/9.12.3/lucene/core/src/java/org/apache/lucene/search/IndexSearcher.java).
- Tantivy gives the same lesson in a compact Rust shape: an index exposes
  searchable segment metadata and readers over per-segment inverted data:
  [Index](https://github.com/quickwit-oss/tantivy/blob/0.26.1/src/index/index.rs),
  [SegmentReader](https://github.com/quickwit-oss/tantivy/blob/0.26.1/src/index/segment_reader.rs),
  and [InvertedIndexReader](https://github.com/quickwit-oss/tantivy/blob/0.26.1/src/index/inverted_index_reader.rs).
- Chroma separates system metadata, segment management, log state, and
  execution. That is the useful shape for treating a database as a materialized
  read model rather than the only copy of the data:
  [sysdb mixin](https://github.com/chroma-core/chroma/blob/43171c54dd8d1e6823ed30f15332d9904f0da935/chromadb/db/mixins/sysdb.py),
  [local segment manager](https://github.com/chroma-core/chroma/blob/43171c54dd8d1e6823ed30f15332d9904f0da935/chromadb/segment/impl/manager/local.py),
  and [local executor](https://github.com/chroma-core/chroma/blob/43171c54dd8d1e6823ed30f15332d9904f0da935/chromadb/execution/executor/local.py).
- ripgrep separates traversal, haystack admission, and search workers. That
  is the useful pattern for keeping source discovery and per-source scanning
  distinct:
  [haystack admission](https://github.com/BurntSushi/ripgrep/blob/4857d6fa67db69a95cd4b6f2adda5d807d4d0119/crates/core/haystack.rs),
  [search workers](https://github.com/BurntSushi/ripgrep/blob/4857d6fa67db69a95cd4b6f2adda5d807d4d0119/crates/core/search.rs),
  and [ignore-aware walking](https://github.com/BurntSushi/ripgrep/blob/4857d6fa67db69a95cd4b6f2adda5d807d4d0119/crates/ignore/src/walk.rs).

SQLite is still the right default storage system for this DB index.
It is local, transactional, portable, available through Python's standard
library, and has native full-text indexing through
[FTS5](https://www.sqlite.org/fts5.html). SQLite
[WAL](https://www.sqlite.org/wal.html) gives the reader/writer behavior a
local cache needs without introducing a server.

## Decision

agentgrep will introduce a persistent DB index as rebuildable derived
state over local agent history stores.

The default implementation is SQLite with WAL enabled and an FTS5 text index.
The DB index is not the source of truth. Codex, Claude, Cursor,
Gemini, Grok, Pi, OpenCode, and future source stores remain the source of
truth; the agentgrep database can be deleted and rebuilt from them.

The DB index owns three groups of data:

1. **Source inventory**: discovered source files, directories, SQLite
   databases, adapter ids, store roles, source capabilities, fingerprints,
   sync cursors, and tombstone state.
2. **Normalized records**: stable derived record ids, native ids, source ids,
   agent/store metadata, project/workspace hints, session/conversation ids,
   role, kind, timestamps, text hashes, normalized text hashes, and metadata.
3. **Search indexes**: FTS5 text index, metadata indexes, migration state, and
   planner-visible index coverage.

The planner must treat the DB index as an acceleration path. If the
index is missing, stale, incomplete, or unable to prove a query predicate, the
planner falls back to raw source scanning through the execution system from
ADR 0004.

## Interfaces

Names below describe intended internal contracts. They are not public APIs
until implemented and documented.

`DbStore`
: SQLite-backed database handle. It owns migrations, WAL configuration,
  transaction boundaries, and schema-version checks.

`SourceLedger`
: Persistent source inventory. It records adapter id, source role, source
  fingerprint, last sync generation, cursor, error summary, and tombstone
  state.

`RecordDb`
: Persistent normalized records and searchable text. It stores derived
  records and maintains the FTS5 external-content table.

`SyncPlanner`
: Builds a logical sync plan from source discovery plus the current ledger.
  It chooses stat probes, append-tail parsing, SQLite incremental reads,
  small-file rescans, tombstones, and index maintenance tasks.

`SyncTask`
: Physical sync work item. Each task declares the source, adapter strategy,
  expected write shape, cursor behavior, fallback condition, and profile span
  names.

`DbCoverage`
: Planner-facing summary of which sources and records are represented by the
  DB index. Search planning uses this to decide when indexed and raw
  source plans must be combined.

## Cache control and transparency

Every cache layer must expose a bypass lever and a hit-or-miss signal.
A cache a user cannot turn off, or whose effect on a result cannot be
observed, is a correctness liability rather than an optimization.

The levers, in precedence order where they overlap:

- `AGENTGREP_DB` selects the cache location; pointing it at a
  scratch path isolates tests, benchmarks, and experiments.
- `--cache {auto,require,off}` (with `--no-cache` as the `off`
  shorthand) selects the mode per invocation, and `AGENTGREP_CACHE`
  supplies the same mode for whole environments — benchmark harnesses,
  CI jobs, and MCP server configuration blocks. An explicit flag beats
  the environment; the default is `auto`. `require` exists so warm
  measurements fail loudly instead of silently timing a live scan.
- The `search.cache.decision` profile span reports one aggregate
  sample per consulted query — mode, whether the cache served it, the
  served record count, and the fallback reason — and
  `search.collect.source_scan_cache` reports per-source scan-cache
  lookups. Together they make every cache's contribution visible in
  profiles and benchmark artifacts.

## Sync rules

Sync is itself planned work:

```text
discover sources -> logical sync plan -> rewrite/optimize
  -> physical sync tasks -> bounded scheduler -> index writers
  -> profile/explain output
```

The first required task shapes are:

- `stat_probe`: compare cheap file metadata and stored fingerprints.
- `jsonl_tail`: seek from a stored byte cursor, parse appended lines, and
  fall back to full rescan if the file was truncated or rotated.
- `sqlite_incremental`: open upstream SQLite stores read-only and query-only,
  then use native ids, rowids, timestamps, or table fingerprints when the
  adapter can prove they are safe.
- `small_file_rescan`: fully parse small JSON, TOML, Markdown, or text sources
  when a cheap fingerprint changes.
- `tombstone_missing`: mark sources absent after discovery without immediately
  deleting their historical index rows.
- `index_optimize`: run bounded FTS maintenance when configured thresholds are
  crossed.

Sync profiles follow ADR 0004 privacy rules: no prompt text, no raw argv, no
secret values, and no local absolute paths in saved artifacts.

## Consequences

### Positive

- Repeated broad queries can skip discovery and parsing work when the
  DB coverage is current enough.
- Frontends get a faster cache path without changing their output contracts.
- Local profiling can distinguish raw-source bottlenecks from DB-index
  bottlenecks.
- The same DB store can later feed deterministic insight generation.

### Tradeoffs

- agentgrep gains schema migrations and cache invalidation concerns.
- Adapters must expose stable source identity and cursor semantics instead of
  only parser functions.
- Tests need fixture-backed rebuild and stale-index paths, not only direct
  source parsing tests.

### Risks

Stale index results: the DB index may be behind the raw store. The
mitigation is explicit `DbCoverage` plus raw fallback when coverage is
missing or untrusted.

Privacy drift: a cache can accidentally retain text from a source later made
private. The mitigation is to keep the same store-catalog privacy boundary as
runtime discovery and to make cache deletion/rebuild straightforward.

SQLite contention: a busy sync could interfere with interactive reads. The
mitigation is WAL, short write transactions, bounded sync tasks, and planner
fallback to raw sources if the cache is unavailable.

## Final position

The persistent DB index is agentgrep's default local acceleration
layer. It is SQLite-first, rebuildable, privacy-scoped, and planner-visible.
It does not replace raw source parsing; it lets the planner choose a cheaper
path when the indexed coverage is current enough to trust.
