(adr-durable-prompt-corpus-derived-search-indexes)=

# ADR 0015: Durable prompt corpus and derived search indexes

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

The differences matter when extracting a human prompt:

- [Codex rollouts](https://github.com/tony/agentgrep/blob/v0.1.0a41/tests/samples/codex/codex.sessions/rollout-2026-05-17T12-00-00-example.jsonl)
  interleave session metadata, user input, model responses, compaction and tool
  records.
- [Claude transcripts](https://github.com/tony/agentgrep/blob/v0.1.0a41/tests/samples/claude/claude.projects.session/example.jsonl)
  carry UUID/parent-UUID topology and typed content blocks, while a complete
  prompt may depend on a sibling paste cache.
- [Pi sessions](https://github.com/tony/agentgrep/blob/v0.1.0a41/tests/samples/pi/pi.sessions/example.jsonl)
  form a parent-linked tree in which human messages sit beside thinking,
  tools, compaction and branch summaries.
- [VS Code chat sessions](https://github.com/tony/agentgrep/blob/v0.1.0a41/tests/samples/vscode/vscode.chat_sessions/example.jsonl)
  are mutation logs whose splice and replacement events materialize a final
  prompt snapshot.
- Cursor, Antigravity and other stores place JSON or protobuf payloads inside
  SQLite rows, while OpenCode exposes relational sessions, messages and parts.

The current {class}`~agentgrep.SearchRecord` is the correct frontend-neutral
projection, but not a durable corpus. Its
[`SearchRecord` and `SourceHandle` fields](https://github.com/tony/agentgrep/blob/v0.1.0a41/src/agentgrep/records.py#L361-L455)
retain normalized text and common provenance while omitting source observation,
extraction contract, native prompt coordinates and a separately resolvable
conversation coordinate. Adapter-local or engine deduplication can also
collapse identical text that represents distinct prompt occurrences.

A lossless mirror of every native record would fix those omissions, but it
would retain far more than prompt search needs: assistant messages, reasoning,
tool traffic, attachments, compaction payloads, mutations and unknown fields.
That would duplicate the most sensitive parts of every conversation, create a
large durable migration surface and imply that agentgrep can replay arbitrary
future transcript projections. It does not need to make that promise.

This ADR therefore draws a narrower boundary. Upstream stores remain the
**origin authority** for complete conversations and all non-prompt content.
agentgrep keeps a **durable prompt corpus** containing complete extracted human
prompt text plus enough evidence to identify the occurrence, validate its
source observation and resolve its containing conversation. Replaceable search
indexes derive from that corpus. {ref}`adr-progressive-deep-search` uses the
distinction to make prompt search the normal effort level and conversation-body
search explicit; {ref}`adr-prompt-guided-conversation-routing` may use prompt
occurrences as clues, not proof, when selecting conversations for a targeted
deep search.

## Decision

Ten invariants govern the corpus and indexing boundary (PC for *prompt
corpus*).

### PC-1 — Origin stores remain the complete-conversation authority

The data path has three layers:

1. **Origin stores** are independently written by Codex, Claude, Cursor,
   Gemini, Antigravity, Grok, Pi, OpenCode, VS Code and future agents. They
   remain authoritative for complete conversations and native structure.
2. **Canonical prompt evidence** is agentgrep's durable local input for prompt
   search. It contains full extracted human prompt bytes, every value that the
   public prompt-query contract treats as semantic, occurrence provenance and
   private prompt/conversation locators.
3. **Read models** are versioned normalized rows, facets and lexical indexes.
   They are derived, disposable and replaceable.

`prompts.sqlite3` is canonical only for this prompt evidence. It is neither a
universal native-record schema nor a transcript archive. A new projection that
needs assistant, reasoning, tool, attachment or mutation data must read the
origin store or obtain its own storage decision. The current
{class}`~agentgrep.SearchRecord` remains a projection rather than becoming the
durable schema.

### PC-2 — The retention boundary is complete human prompt semantics

For every admitted prompt occurrence, agentgrep retains the complete extracted
human prompt text bytes. *Complete* means the adapter's deterministic,
ordered human-text projection before display truncation, snippet generation,
case folding, tokenization or result limiting. Where a prompt spans typed text
blocks or an admitted sibling paste cache, the extraction contract defines
their order and expansion. The stored payload records its media type, encoding,
byte length and full SHA-256 digest.

The corpus also retains the canonical value, explicit absent state and
provenance for every field that the versioned public prompt-query contract
treats as semantic. For the initial contract, that set includes prompt text,
kind, title, role, timestamp, model and session/conversation identity; the
source-derived agent, store, adapter, scope, path and mtime values; and cwd,
repository, worktree, branch, project and cwd-hash origin fields. Private path
values remain inside the corpus boundary; ADR 0006 governs whether a redacted
display path is rendered publicly. This semantic-field set is versioned with
the extraction contract so an index generation can prove which corpus values
it consumed.

Optional summaries, inferred topics, embeddings, ranks and other enrichments
stay derived under {ref}`ADR 0005
<adr-local-insights-reports-model-backed-enrichment>` and live outside both the
canonical corpus and exact prompt-index generations. A new public query field
cannot be advertised as exactly rebuildable until its canonical value and
provenance are retained in `prompts.sqlite3`; until then, queries that need it
route the affected source live or require a separate durability decision.

The durable default excludes:

- assistant and model responses;
- hidden or visible reasoning;
- tool calls, tool results and synthetic user-role tool envelopes;
- attachments and non-text parts except text deliberately expanded into the
  human prompt by the adapter contract;
- compaction, mutation history and branch topology, history or summary payloads
  beyond canonical query-semantic values or locator evidence;
- complete native rows, events, blobs or conversations merely because they
  contain a prompt; and
- unrelated source records and unknown fields.

Extraction may preserve prompt block boundaries as selectors and lengths when
needed for resolution, but it does not retain excluded block payloads. If an
adapter cannot prove that it extracted complete human text, it records an
explicit gap, marks the affected prompt coverage suspect and does not persist a
silently truncated prompt as canonical. The live prompt path may preserve the
adapter's currently declared runtime coverage, but it cannot turn that gap into
an exact durable-corpus claim.

### PC-3 — Private corpus keys do not redefine public identity

The repository keeps these private keys:

| Private key | Meaning |
| --- | --- |
| `prompt_blob_digest` | Full SHA-256 digest of the versioned media type, encoding and exact extracted prompt bytes |
| `corpus_occurrence_key` | One admitted prompt occurrence under an adapter-owned source observation and coordinate contract |
| `corpus_conversation_key` | Private anchor for the containing origin conversation and its current locator evidence |
| revision, projection and observation keys | Private versions of admitted evidence and its normalized read-model projection |
| locator keys | Private prompt and conversation coordinates bound to one source observation |

`prompt_blob_digest` may content-address and physically share identical prompt
bytes, but every occurrence keeps its own timestamp, provenance and locators.
The repository also keeps private source identity and identity-quality evidence.
SQLite rowids, local paths, working directories, mtimes and branches never
become public logical identity.

This ADR does not redefine public identity. The existing `content_id`, nullable
`record_id`, nullable `thread_id` and `record_id_stability` values are preserved
exactly when the normalized adapter record can defend them. Missing public
`record_id` and `thread_id` values remain null; the corpus never fabricates a
public identity from a private key, path, rowid, mtime, locator or repository
state. `RecordRef` remains the only public physical prompt-drilldown handle
governed by {ref}`ADR 0004
<adr-headless-query-planning-non-blocking-execution>` and {ref}`ADR 0006
<adr-public-cli-mcp-surface-contract>`. This ADR introduces no public prompt or
conversation reference, and `thread_id` remains a comparison/grouping handle
rather than a resolver.

Cross-stage deduplication of the same human prompt occurrence uses canonical
`record_id` when it is available. When it is null, a live and corpus projection
may deduplicate through `corpus_occurrence_key` only when the owning adapter can
prove that both projections identify the same occurrence under the bound source
observation and coordinate contract. Equal text, `content_id`,
`prompt_blob_digest`, title, path or timestamp alone never proves occurrence
identity. Dedupe changes result presentation, not the retained occurrence
inventory.

### PC-4 — Prompt and conversation locators are distinct, private evidence

Each prompt occurrence records both:

- a **native prompt locator** precise enough for the owning adapter to find or
  validate that occurrence; and
- a **containing-conversation locator** precise enough to reopen the origin
  conversation for inspection or deep search.

A locator contains the adapter/store namespace, native identifiers and the
least format-specific coordinate required by that adapter, such as JSONL byte
offset and ordinal, a JSON Pointer, a typed SQLite key tuple, a protobuf field
path or a mutation-snapshot selector. Locators remain private storage data;
absolute paths and native database keys do not cross public envelopes.
ADR 0006-governed human display or debug paths may be rendered separately, but
they are never accepted as resolution identity and never substituted for a
`RecordRef`, public canonical ID or private locator.

Every locator is bound to the `source_id`, source observation, adapter contract
and discovery generation that produced it. Resolution first verifies that
binding. If the source changed, the adapter may rediscover the same native
identity and publish a new locator; it may not silently apply the old coordinate
to different content. An unresolved or ambiguous locator is `stale`, contributes
to coverage diagnostics and never opens a guessed conversation.

Stale and grace-period prompt bytes remain retained but do not participate in
ordinary prompt or deep search. They are available only through an explicit
read-only retained-evidence storage inspection whose output labels the retention
state and pointer availability and never exposes private locators. Inspection is
metadata-only by default; a bounded body requires an explicit body request and
does not become a portable artifact. Keeping evidence for recovery does not turn
it into a current search result, a portable export or an available containing
conversation.

### PC-5 — Observations and extraction contracts make evidence auditable

Every durable occurrence carries this logical evidence. Exact SQL placement is
an implementation detail; the information is not.

| Group | Required evidence |
| --- | --- |
| Contract | corpus schema, adapter contract, extraction contract and observed data version |
| Source | agent, store, format, coverage, privacy class and stable source id |
| Observation | scheduling fingerprint, consistent snapshot or cutoff evidence, dependency manifest and discovery generation |
| Prompt | `prompt_blob_digest`, `corpus_occurrence_key`, private revision key, preserved public IDs and identity quality |
| Query semantics | canonical values, explicit absent states and field-level native/extracted/synthetic provenance |
| Provenance | native prompt locator, conversation locator, native timestamp and minimal classification facts |
| Derivation | decoder/projection versions and native, extracted or synthetic provenance |

`role` is retained only to prove why the adapter classified the text as a
human prompt; storing `role="user"` does not authorize retention of tool-result
content wrapped in a user-shaped native entry. Model, project, repository and
other values are durable when the public prompt-query contract treats them as
semantic; richer inferred facets remain replaceable enrichment.

Ingestion uses a format-specific consistent snapshot or cutoff rather than
requiring a coarse whole-source fingerprint to remain identical before and
after extraction. The committed observation records the exact range and
dependencies whose consistency was proved. Mutation that invalidates that
captured range rolls the staged observation back and retries. The dependency
manifest includes evidence such as an upstream SQLite transaction snapshot,
Claude paste-cache entries and workspace metadata when an adapter depends on
them. The existing source-scan cache demonstrates why this is necessary by
[`excluding Claude history when its sibling dependency cannot be represented`](https://github.com/tony/agentgrep/blob/v0.1.0a41/src/agentgrep/_engine/scanning.py#L327-L365).

### PC-6 — SQLite stores canonical evidence; exact indexes are provider-based

The initial corpus backend is `prompts.sqlite3`. It stores canonical prompt
evidence, private keys and locators, source observations, sync history,
retention state and durable migrations. This ADR defines no pluggable corpus
backend: origin stores remain complete-conversation authority and SQLite is the
selected implementation for canonical prompt evidence.

A different canonical corpus backend would require a future storage and
migration decision. Selecting SQLite here fixes the initial implementation and
repair contract without declaring that no later backend can exist.

Exact prompt read models are provider-based derived generations. The default
provider stores normalized search rows, details, facets, coverage and
[FTS5](https://www.sqlite.org/fts5.html) data in `index-vN.sqlite3`. An alternate
provider is permitted only through an explicit configuration and capability
selection; installing a dependency never activates or replaces a provider.

Every immutable provider generation publishes a manifest containing its
provider identifier and version; corpus generation; source-observation
coverage; adapter, extraction, projection, identity and query contracts;
tokenizer and normalization contracts; and integrity state. A provider consumes
only committed corpus evidence, exposes locally sorted candidate streams to the
ADR 0014 merge, and hydrates candidates through the same normalized-record
matcher as live search. It never writes or migrates `prompts.sqlite3`, becomes
result authority or claims coverage for a predicate it cannot prove
candidate-complete. Missing, corrupt or unsupported provider state falls back
to canonical-row scanning or live source execution.

This physical separation keeps provider, tokenizer, normalization and
projection changes out of the durable corpus migration path. Active SQLite
databases use short transactions, independent read connections and [WAL
mode](https://www.sqlite.org/wal.html) so readers can coexist with the
repository mutation owner. Checkpoint and synchronous-mode tuning remains
measurement-led.

The initial logical corpus has these responsibilities:

| Corpus table | Responsibility |
| --- | --- |
| `schema_migrations` | Ordered, checksummed durable-schema history |
| `sources` | Agent/store/adapter identity, privacy, private locator token and lifecycle state |
| `source_observations` | Scheduling fingerprints, snapshots/cutoffs, dependencies, generations and ingest outcomes |
| `prompt_contents` | Complete extracted prompt bytes keyed by `prompt_blob_digest` |
| `conversations` | Private `corpus_conversation_key` anchors and current locators |
| `prompt_occurrences` | `corpus_occurrence_key`, private revision key, preserved public IDs, prompt locator, semantic values and provenance |
| `sync_runs` | Scope, progress, cancellation and completion |
| `health_findings` | Diagnosed source, schema, pointer and integrity conditions |
| `index_generations` | Immutable provider manifests, active-generation publication state, coverage and verification |

Provider rows, snippets, normalized tokens, highlights, ranking scores and
result sets are not durable evidence. They can all be deleted and rebuilt from
the prompt bytes and versioned semantic values in `prompts.sqlite3`, without
consulting an origin store.

ADR 0005 summaries, topics, sketches, embeddings, vector indexes, ranks and
other enrichments live outside both `prompts.sqlite3` and exact prompt-index
generations. They are independently versioned, removable and rebuildable under
their owning derivation contract; deleting or rebuilding them never migrates,
deletes or changes canonical prompt evidence or exact-index coverage.

This ADR authorizes no corpus import and no second portable export contract.
Portable record export belongs to its existing public-surface owner. Retained
corpus evidence is reachable only through the explicit read-only storage
inspection in PC-4; NDJSON never populates or restores `prompts.sqlite3`.

### PC-7 — Indexed, stale and live partitions preserve prompt-search semantics

Every prompt query discovers its source set and performs a read-only freshness
reconciliation that partitions it:

- **current indexed** sources have a ready generation whose exact source
  observation and adapter, extraction, projection, identity, FTS and query
  contracts match;
- **live** sources are new, changed, unindexed, volatile or unsupported by the
  active index plan; and
- **suspect** sources are incomplete, errored, missing from a complete
  discovery generation or associated with failed integrity or pointer checks.

Stale, grace-period and otherwise retained-only occurrences form no search
partition. Their bytes stay in the corpus until prune policy permits removal,
but ordinary prompt and deep searches exclude them. The explicit read-only
retained-evidence storage inspection is the only way to retrieve them.

No indexed result from a changed or suspect source is reported as current.
Current indexed streams and changed/unindexed live prompt streams enter the
single-owner merge from {ref}`ADR 0014
<adr-result-order-limit-and-streaming-merge>`. Provider predicates only generate
candidates; hydrated candidates pass the same normalized-record matcher and
declared scorer as live prompts. Freshness or provider choice never supplies a
hidden ranking boost. Queries the provider cannot represent exactly scan the
canonical prompt rows or route affected sources live rather than silently
under-returning. The default provider therefore needs an explicit fallback for
terms shorter than three Unicode code points when the [FTS5 trigram
tokenizer](https://www.sqlite.org/fts5.html#the_trigram_tokenizer) cannot
produce the required candidates.

The hybrid guarantee in this invariant applies to **prompt search**. The
prompt-guided selection defined by
{ref}`adr-prompt-guided-conversation-routing` is intentionally heuristic and
reports `approximate`; it must not weaken the exactness of the canonical prompt
index or describe an omitted conversation as a negative match.
{ref}`adr-progressive-deep-search` owns the user-visible transition between
those effort levels.

Ordinary search is read-only with respect to durable storage. It never creates
or migrates a database, writes reconciliation state, builds an index or starts
a sync. Read-only reconciliation compares existing observations with current
sources for planning; changed or unrepresented sources stream through live
fallback. Only explicit sync or an independently enabled, visible synchronizer
may publish that reconciliation durably.

The default index mode is `auto`: use every verified current partition from the
active provider—FTS5 by default, or an alternate only when explicitly
selected—and stream the rest live. `off` bypasses persistent reads for the
invocation. `require` fails with structured coverage diagnostics instead of
silently falling back. First use remains correct without a database, an ordinary
query does not create one, and an index may be deleted without making prompt
search unavailable.

### PC-8 — Sync, publication and repair are explicit and non-destructive

Explicit sync, or an independently enabled synchronizer whose running state is
visible, owns corpus and index writes. It prioritizes active conversations,
recent sources, changed indexed sources and then older backfill. Watchers may
wake that synchronizer early, but query-time read-only reconciliation and
periodic sync remain the correctness mechanism. A cheap stat fingerprint is
only a scheduling hint. Neither a normal query nor a one-off deep query enables
the synchronizer as a side effect.

Every mutating corpus migration, sync, repair, prune, purge and
provider-generation publication acquires one repository-wide interprocess
mutation lease for the whole operation. Read-only status, audit and dry-run
forms do not. The lease is an operating-system-enforced ownership primitive;
PID and timestamp metadata are diagnostic only. A loser reports a structured
`repository_busy` result and performs no mutation. SQLite transactions and WAL
reader/writer coexistence do not replace this repository lease. After acquiring
it, the writer reads the active corpus generation and repository epoch that its
publication must compare and advance.

Mutable sources replace or revise their observed prompt occurrences
atomically under format-specific consistency rules:

- JSONL ingestion captures stable file identity, the last complete-record
  prefix cutoff and an anchor covering that prefix. Extraction reads only
  through that cutoff. A later append is allowed; truncation, rotation or a
  rewrite that invalidates the captured prefix aborts and retries.
- Upstream SQLite is opened read-only and query-only, and all required rows are
  read within one SQLite read-transaction snapshot. WAL growth after that
  snapshot is allowed; a failed or invalidated snapshot aborts and retries.
- Mutable JSON, key/value, protobuf and sibling-expanding adapters define an
  equally explicit snapshot, revision or captured-range contract. If all
  dependencies cannot be bound to it, the source remains live.

These rules prove consistency of the bytes actually admitted without rejecting
benign changes outside the captured range.

An exact-index provider builds an immutable generation at a staged path that is
not reachable from the active manifest. Its manifest binds the expected corpus
generation and repository epoch. The provider completes its own integrity
checks and closes all build handles before publication. Immediately before
publication, the writer compares and swaps the active generation under the
repository lease: a changed corpus generation or epoch aborts publication and
removes the staged generation. A successful compare-and-swap records the
publication attempt, installs the closed artifact under its immutable generation
name and then atomically commits the active manifest. The artifact is not
discoverable until that manifest commit; existing readers may finish against
their already-open old generation.

The next lease holder reconciles the publication journal with committed
manifests. It may remove abandoned staging or temporary paths and immutable
final-name artifacts that no committed active or recovery manifest ever
referenced, after proving no live reader holds them. Crash recovery never
removes an active or recoverable corpus generation or a published provider
generation still held by a live reader. Previously published inactive provider
generations follow the normal reclamation policy after readers release them. An
incompatible corpus schema uses ordered, checksummed forward migrations and a
recoverable backup for destructive steps.

Derived-index corruption may trigger a visible rebuild. Corpus corruption never
triggers automatic deletion: agentgrep preserves or quarantines the damaged
database, reports the finding and reconstructs a replacement from readable
origin stores. Every repair, prune and purge operation has a machine-readable
dry-run form. Read-only status, audit and dry-run commands never acquire the
mutation lease or create, migrate, sync, repair, prune or purge storage.

### PC-9 — Retention follows origin reachability with a grace period

The default mode is **cache with grace**, not permanent archive. A prompt
occurrence is current only while read-only freshness reconciliation or the last
complete sync can bind it to an admitted origin source and occurrence. It stops
contributing an ordinary search result as soon as that binding is stale or
suspect, but that state alone does not start its grace clock.

Grace has two valid absence proofs:

- a complete, uncapped discovery of an available authoritative root proves that
  an entire previously admitted source is absent, starting source-level grace
  for its retained occurrences; or
- when the source still exists, a complete, uncapped **per-source occurrence
  reconciliation**, performed against a format-specific consistent snapshot,
  enumerates its current occurrences and proves that a particular occurrence is
  absent.

A bare missing path, cancellation, partial scope, unavailable or unproven root,
failed read or changed source fingerprint proves neither kind of absence.

The grace period protects against transient source loss, offline stores, rename
reconciliation and accidental eager deletion. Its duration is configurable and
visible in storage status. During grace, bytes are retained evidence only and
ordinary search still excludes them. After grace expires, pruning logically
removes unreachable occurrences and their private locators; a prompt-content
row is removed only after no retained occurrence references its
`prompt_blob_digest`. Publication invalidates or rebuilds any exact provider
generation that referred to removed evidence. Storage accounting distinguishes
logical current, stale, grace-period and reclaimable evidence from physical
database and WAL size.

Bookmarks do not pin corpus retention, extend grace or cause prompt evidence to
be retained. Prune and purge never delete or rewrite bookmarks; a bookmark may
instead resolve to a structured unavailable result after its evidence expires.
User-created portable exports are independently owned and are outside corpus
retention and reclamation.

A future archive mode that retains origin-deleted prompts requires explicit
opt-in, visible deletion semantics and a separate decision. This ADR does not
authorize indefinite retention. Routine prune and user-requested purge promise
logical removal, not secure erasure. Purge may bypass the grace period only
after a dry-run identifies the exact scope. It then attempts physical
reclamation with SQLite secure-delete behavior plus truncating checkpoint and
compaction only when no reader or recoverability constraint makes those steps
unsafe. Its machine result reports `physical_reclamation` as `complete`,
`deferred_by_readers` or `unsupported`.

Physical reclamation is reported across every agentgrep-managed exact copy that
contained the removed evidence: the corpus database and its WAL/SHM files plus
active or inactive exact-provider generations. Aggregate `complete` requires
the evidence to be absent from each such copy. A provider generation still held
by a live reader keeps the result `deferred_by_readers`; after release it is
reclaimed rather than retained for rollback. A backend that cannot make the
claim reports `unsupported`. Independently owned bookmarks, portable exports
and enrichment caches are outside this result and follow their own lifecycle
operations.

Even `complete` does not promise erasure from copy-on-write storage, snapshots,
backups, crash dumps or user exports. No prune, purge or reclamation diagnostic
may describe the result as secure erase.

### PC-10 — Privacy, observability and testing are part of correctness

Corpus admission follows the runtime store catalogue. `DEFAULT_SEARCH` stores
are eligible for automatic durable admission when explicit sync or the visible
synchronizer runs. `INSPECTABLE` stores require an explicit persistence/sync
opt-in naming that coverage; selecting them for one search, including a one-off
deep search, must not silently begin retaining them. `CATALOG_ONLY` data is not
copied unless another decision defines a safe payload, and `PRIVATE` stores are
never enumerated or copied.

The databases use owner-only permissions. Raw private locators, local paths,
native keys, query strings and prompt text do not enter logs, profiler
artifacts or public diagnostics. How `RecordRef` derives or resolves its opaque
physical handle remains owned by ADRs 0004 and 0006; this storage ADR neither
defines its encoding nor exposes a second locator field. ADR 0006-governed
display and debug paths are presentation only and never resolution inputs.

Every machine result reports provenance, freshness, identity quality,
conversation-pointer state, coverage, completion and fallback reason. A cache
hit, live fallback, stale locator, extraction gap, approximation, omitted
payload, prune or repair is never silent.

The corpus boundary is not complete until tests prove:

- one redacted fixture per supported adapter shape extracts the complete human
  prompt while excluding assistant, reasoning, tool and unrelated payloads;
- typed blocks, paste expansion and mutation snapshots preserve deterministic
  ordering without display truncation;
- every semantic prompt-query field and its absent state can rebuild a
  behaviorally identical index from `prompts.sqlite3`, while optional
  enrichment is excluded from exact coverage;
- repeated identical bytes share content safely while retaining distinct
  occurrences, timestamps, locators and conversation membership;
- public `content_id`, nullable `record_id`, nullable `thread_id` and
  `record_id_stability` match the defensible adapter values exactly, null IDs
  are never fabricated and private corpus keys never cross a public envelope;
- `RecordRef` remains the only public physical prompt drilldown; prompt and
  conversation locators resolve only against their bound source observation,
  never appear as a second public locator field and never open different content
  when stale or ambiguous;
- cross-stage prompt dedupe uses canonical `record_id`, or
  `corpus_occurrence_key` only with adapter proof; identical text, content
  digest, title, path and timestamp never suffice;
- live-only, indexed-only and hybrid prompt plans produce the same semantic
  result set, order, dedupe and global limit for every exact provider;
- provider selection is explicit, installing an optional dependency never
  activates it, and each active immutable generation has a verified manifest
  bound to the corpus generation and exact contracts it consumed;
- JSONL complete-prefix ingestion permits later append but retries after
  captured-prefix mutation; SQLite ingestion reads one transaction snapshot;
- cancellation or source mutation during ingest publishes no partial
  observation or provider generation;
- concurrent mutation attempts prove the repository lease has one owner, a
  loser performs no mutation, stale compare-and-swap publication fails and
  crash cleanup removes unreferenced staging paths and never-published
  final-name artifacts but preserves every committed or live-reader generation;
- corpus and provider corruption choose the documented non-destructive repair;
- complete authoritative-root discovery starts source-level grace only for a
  missing source, while complete per-source reconciliation starts occurrence-
  level grace only for a deletion inside a source that still exists; bare path
  absence, partial or unavailable roots prove neither, and reappearance, expiry
  and shared-content collection follow PC-9;
- bookmarks never pin corpus evidence and survive prune or purge unchanged;
- there is no corpus import path, retained evidence is available only through
  read-only storage inspection, and NDJSON never populates or restores the
  corpus;
- prune and purge prove logical removal and qualified physical-reclamation
  reporting across the corpus and every managed exact-provider copy, while
  excluding separately owned bookmarks, exports and enrichments and never
  claiming secure erasure;
- ordinary and one-off deep searches perform no durable writes, and an
  `INSPECTABLE` store is admitted only after explicit persistence opt-in; and
- event-stream and Textual consumers keep database, extraction, matching and
  hydration work off the message pump under {ref}`ADR 0011
  <adr-non-blocking-tui-invariants>`.

Tests use `tmp_path`, redacted fixtures, injected clocks/fingerprints and fake
progress sinks. They do not read a real home directory, wait for filesystem
watchers, download models or depend on timing sleeps. Benchmarks compare live,
indexed and hybrid prompt paths while checking result and identity parity;
speed without parity is not a successful optimization.

## Operational surface

The eventual CLI and machine interfaces expose lifecycle rather than hidden
side effects through these provisional commands:

- `agentgrep db status --json`
- `agentgrep db sync`
- `agentgrep db verify`
- `agentgrep db repair --dry-run`
- `agentgrep db prune --dry-run`
- `agentgrep db purge --dry-run`
- `agentgrep db inspect-retained --json`
- `agentgrep db rebuild-index`
- `agentgrep query explain`

Names are provisional until their public-surface implementation lands. The
required concepts are not: status is read-only; sync is explicit and
cancellable; verify distinguishes corpus, selected provider and pointer health;
repair, prune and purge preview their scope; retained-evidence inspection is
read-only; rebuild never threatens canonical prompt evidence; explain states
which sources are indexed, live or suspect, by which provider and why.

## Prior art

The adopted patterns are deliberately narrower than their source systems:

- [SQLite WAL](https://www.sqlite.org/wal.html) supplies local reader/writer
  coexistence, while [FTS5](https://www.sqlite.org/fts5.html) supplies trigram
  candidate search and documents the synchronization and integrity obligations
  of contentful, contentless and external-content tables.
- [Datasette 1.0a37](https://github.com/simonw/datasette/blob/1.0a37/datasette/database.py)
  demonstrates a serialized writer and independent reader connections. The
  reusable pattern is connection ownership, not a substitute for the
  repository-wide interprocess mutation lease or Datasette's public query API.
- [Git v2.54.0 repository
  layout](https://github.com/git/git/blob/v2.54.0/Documentation/gitrepository-layout.adoc)
  separates content-addressed object identity from ref-based reachability.
  agentgrep adopts that distinction for private prompt-blob sharing and
  occurrence retention, not public identity, Git's object graph, ref encoding
  or permanence semantics.
- [Codex state](https://github.com/openai/codex/tree/5bed6447998c754d154dbd796517310b8f04d4ce/codex-rs/state)
  demonstrates WAL-backed derived state, ordered migrations and read-only
  audit. Its rollout JSONL remains an origin authority, which is the same
  authority boundary this ADR keeps for complete conversations.
- [pytest's cache provider](https://github.com/pytest-dev/pytest/blob/3fa8d9b15b733aadb8a043cca3e98447804e1f28/src/_pytest/cacheprovider.py)
  demonstrates temporary construction followed by atomic publication and
  cleanup of a concurrent loser. Derived index generations adopt that
  publication shape with an additional repository lease and corpus-generation
  compare-and-swap, not pytest's directory schema.

## Rejected alternatives

- **Mirror every admitted native record:** this stores assistant, reasoning,
  tool and unrelated payloads, expands privacy and migration obligations, and
  promises transcript replay that prompt search does not require.
- **Persist only `SearchRecord`:** it cannot distinguish repeated occurrences,
  prove its source observation, reproduce complete untruncated prompt text or
  resolve the containing conversation safely.
- **Persist prompt text without occurrence and conversation pointers:** it can
  answer lexical questions but cannot explain provenance, open the prompt or
  support bounded prompt-guided conversation routing.
- **Make FTS rows canonical:** tokenizers, projections and query contracts
  change independently of extracted prompt evidence; placing both in one
  rebuild domain turns an optimization migration into evidence loss.
- **Treat an indexed observation as permanently current:** it admits stale
  positives and missed prompts after origin, WAL or sibling-dependency changes.
- **Require a complete index before prompt search:** first use, changed sources
  and corruption recovery would fail even though live adapters can answer.
- **Activate an index provider when its dependency is installed:** environment
  drift would silently change execution, manifests and performance. Provider
  selection is explicit and capability-checked.
- **Treat a bare missing path or partial/unavailable root as deletion:**
  removable media, transient mounts, renamed stores and incomplete discovery do
  not prove source absence. Source-level grace requires complete discovery of
  an available authoritative root; occurrence-level grace inside a source
  requires complete per-source reconciliation.
- **Retain deleted origin prompts forever:** hidden archival retention violates
  user expectations and needs a separate opt-in deletion contract.
- **Use watchers as truth:** watchers miss offline changes, overflow and start
  races. They improve scheduling only.
- **Create embeddings during ingest:** model, provisioning, privacy and vector-
  schema lifecycles do not belong in lexical prompt correctness. ADR 0005 owns
  any future vector derivation outside the corpus and exact-index generations,
  under an explicit model contract and with independent removal.

## Relationship to other ADRs

{ref}`ADR 0001 <adr-storage-version-detection>` continues to decide which
adapter and extraction contract interprets each observed source. This ADR
persists the extracted prompt evidence and detector result; it does not replace
shape-first detection.

{ref}`ADR 0004 <adr-headless-query-planning-non-blocking-execution>` owns the
query plan, driver, event stream, run-status vocabulary and public `RecordRef`
drilldown contract. This ADR adds prompt-corpus coverage, index/live
partitioning and sync phases without creating another public resolver.

{ref}`ADR 0005 <adr-local-insights-reports-model-backed-enrichment>` owns
insights, optional dependencies, model provisioning and enrichment lifecycle.
Those artifacts remain outside `prompts.sqlite3` and exact prompt-index
generations and are independently removable; this ADR neither selects their
backends nor authorizes model downloads.

{ref}`ADR 0006 <adr-public-cli-mcp-surface-contract>` owns public envelopes,
diagnostics, cursors and identity/reference presentation. This ADR preserves
the focused public identity contract and keeps corpus keys, database rowids and
private locators outside that boundary.

{ref}`ADR 0011 <adr-non-blocking-tui-invariants>` owns the Textual pump and
worker discipline. This ADR requires database, extraction and hydration work to
obey it.

{ref}`ADR 0014 <adr-result-order-limit-and-streaming-merge>` owns global order,
the completeness barrier and the rule that limit follows order. Indexed and
live prompt sources enter that one merge rather than producing independent
result sets.

{ref}`adr-progressive-deep-search` owns normal versus deep search
effort, public CLI/MCP/TUI behavior and escalation. This ADR supplies the
complete prompt surface used by normal search.

{ref}`adr-prompt-guided-conversation-routing` owns heuristic
candidate generation, conversation budgets and targeted deep-search coverage.
This ADR supplies evidence and pointers but does not claim that prompt clues
form a complete conversation candidate set.

## Consequences

agentgrep gains a stable prompt-search corpus without becoming a second archive
of every agent conversation. Complete prompt bytes survive display and index
changes; versioned semantic field values make every exact prompt index
rebuildable without an origin read; private occurrence keys preserve repeated
prompts without fabricating public IDs; observation-bound locators make results
inspectable and provide safe clues for conversation routing. Read-only live
fallback keeps first-use and changed-source prompt search correct without
making search a hidden writer.

The narrower boundary also has limits. A future projection cannot recover
assistant, reasoning, tool or attachment data from `prompts.sqlite3`; it must
read the origin. Stale and grace-period prompt bytes remain available only to
explicit read-only retained-evidence inspection, not ordinary search or a
portable export. Adapter authors must define complete human-text extraction,
semantic-field provenance, native coordinates, dependencies, consistent
snapshot/cutoff rules and rediscovery behavior. Hybrid coverage, durable
migrations, storage accounting and privacy-safe pruning create a real subsystem
even though it is smaller than a universal mirror.

The chief footgun is allowing either side to overclaim authority. Persisting a
whole user-shaped native event can quietly retain tool results; trusting an old
coordinate can open the wrong conversation; treating a prompt-derived
candidate set as complete can hide a deep-search miss. The mitigation is
structural: an explicit extraction allow-list, distinct prompt and conversation
locators bound to a source observation, a repository-wide mutation lease,
compare-and-swap provider publication, replaceable exact indexes, visible
stale/grace states, and the separate prompt-guided routing approximation.

## Final position

agentgrep durably stores complete extracted human prompts, not everything an
agent wrote. Private `prompt_blob_digest`, `corpus_occurrence_key`,
`corpus_conversation_key`, revision, projection, observation and locator keys
retain prompt evidence without redefining public identity. Defensible
`content_id`, nullable `record_id` and nullable `thread_id` values are preserved
exactly, and `RecordRef` remains the only public physical prompt handle. Exact
provider read models remain disposable; indexed and live partitions preserve
current prompt-search semantics; and per-source occurrence reconciliation plus
a visible grace period prevents the corpus from quietly becoming an archive.
Ordinary search remains read-only, inspectable-store persistence remains opt-in,
complete conversations remain where their agents wrote them, and deep search
resolves them deliberately under the progressive-search and prompt-guided
routing decisions.
