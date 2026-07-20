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
indexes derive from that corpus. {ref}`ADR 0016
<adr-progressive-deep-search>` uses the distinction to make prompt search the
normal effort level and conversation-body search explicit; {ref}`ADR 0017
<adr-prompt-guided-conversation-routing>` may use prompt occurrences as clues,
not proof, when selecting conversations for a targeted deep search.

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

The prompt corpus is neither a universal native-record schema nor a transcript
archive. A new projection that needs assistant, reasoning, tool, attachment or
mutation data must read the origin store or obtain its own storage decision.
The current {class}`~agentgrep.SearchRecord` remains a projection rather than
becoming the durable schema.

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
stay derived. A new public query field cannot be advertised as exactly
rebuildable until its canonical value and provenance are retained in
`prompts.sqlite3`; until then, queries that need it route the affected source
live or require a separate durability decision.

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

### PC-3 — Every prompt has content identity and occurrence identity

Identical bytes and identical occurrences are different claims:

| Identity | Meaning |
| --- | --- |
| `source_id` | One physical/native source identity |
| `conversation_id` | One adapter-namespaced native conversation anchor |
| `prompt_content_id` | Full digest of the media type, encoding and exact prompt bytes |
| `prompt_occurrence_id` | One logical prompt occurrence within a conversation |
| `prompt_revision_id` | One observed revision of that occurrence and content |
| `projection_id` | One revision under a normalized read-model contract |

The full 256-bit digest is retained internally. `prompt_contents` may
content-address and physically share identical prompt bytes, but every
occurrence keeps its own timestamp, provenance and locators. Search dedupe and
presentation never delete occurrence identity.

Occurrence identity prefers native IDs, then stable native coordinates, then
adapter-owned source-order anchors. Every occurrence reports the corresponding
identity quality. SQLite rowids, local paths, current working directory, mtimes
and branches do not become public logical identity. Public refs are opaque,
pseudonymous values governed by {ref}`ADR 0006
<adr-public-cli-mcp-surface-contract>`:

- `PromptRef` is the public handle for one logical prompt occurrence and its
  selected revision; and
- `ConversationRef` is the public handle for the containing adapter-namespaced
  conversation anchor, without promising that the origin remains resolvable.

Both are specializations of ADR 0006's `RecordRef` drilldown contract. Their
encoding is versioned and may map through private repository state, but never
contains or exposes a local path, native identifier, database key, rowid,
content digest or locator coordinate. A ref returns a structured stale,
retained-only or unavailable state rather than being guessed. Pruning may
invalidate a ref according to the public retention contract; the opaque value
is not itself an archival promise.

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
`PromptRef` or `ConversationRef`.

Every locator is bound to the `source_id`, source observation, adapter contract
and discovery generation that produced it. Resolution first verifies that
binding. If the source changed, the adapter may rediscover the same native
identity and publish a new locator; it may not silently apply the old coordinate
to different content. An unresolved or ambiguous locator is `stale`, contributes
to coverage diagnostics and never opens a guessed conversation.

Stale and grace-period prompt bytes remain retained but do not participate in
ordinary prompt or deep search. They are available only through an explicit
retained-evidence storage inspection or export surface whose output labels the
retention state and pointer availability. Keeping evidence for recovery does
not turn it into a current search result or make its containing conversation
available.

### PC-5 — Observations and extraction contracts make evidence auditable

Every durable occurrence carries this logical evidence. Exact SQL placement is
an implementation detail; the information is not.

| Group | Required evidence |
| --- | --- |
| Contract | corpus schema, adapter contract, extraction contract and observed data version |
| Source | agent, store, format, coverage, privacy class and stable source id |
| Observation | scheduling fingerprint, consistent snapshot or cutoff evidence, dependency manifest and discovery generation |
| Prompt | complete bytes reference, content id, occurrence id, revision id and identity quality |
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

### PC-6 — SQLite stores the corpus; versioned indexes remain disposable

The default repository separates two SQLite roles:

- `prompts.sqlite3` stores prompt contents, occurrences, private locators,
  source observations, sync history, retention state and durable migrations.
- `index-vN.sqlite3` stores normalized search rows, details, facets, coverage
  and [FTS5](https://www.sqlite.org/fts5.html) data for one search contract.

The physical split keeps tokenizer, normalization and projection changes out
of the durable corpus migration path. Both databases use one serialized writer,
short transactions and independent read connections. [WAL
mode](https://www.sqlite.org/wal.html) is the default for active writable
databases because readers and the writer must coexist; checkpoint and
synchronous-mode tuning remains measurement-led.

The initial logical corpus has these responsibilities:

| Corpus table | Responsibility |
| --- | --- |
| `schema_migrations` | Ordered, checksummed durable-schema history |
| `sources` | Agent/store/adapter identity, privacy, private locator token and lifecycle state |
| `source_observations` | Scheduling fingerprints, snapshots/cutoffs, dependencies, generations and ingest outcomes |
| `prompt_contents` | Content-addressed complete extracted prompt bytes |
| `conversations` | Adapter-namespaced anchors and private current locators |
| `prompt_occurrences` | Occurrence/revision identity, prompt locator, semantic field values and provenance |
| `sync_runs` | Scope, progress, cancellation and completion |
| `health_findings` | Diagnosed source, schema, pointer and integrity conditions |
| `index_generations` | Published index contract, coverage and verification state |

The read-model database keeps narrow search rows, hydrated prompt details,
typed facets, FTS candidate rows and exact source-observation coverage. FTS
rows, snippets, normalized tokens, highlights, ranking scores and result sets
are not durable evidence. They can all be deleted and rebuilt from the prompt
bytes and versioned semantic values in `prompts.sqlite3`, without consulting an
origin store. Optional enrichments that cannot satisfy that rule are separate
derived capabilities and never part of an index generation's exact prompt-
query coverage claim.

Versioned NDJSON is the import/export and diagnostic interchange format, not a
second repository authority. Exports honor the same privacy and retention
rules and do not expose private locators by default.

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
but ordinary prompt and deep searches exclude them. The explicit retained-
evidence storage inspection/export surface is the only way to retrieve them.

No indexed result from a changed or suspect source is reported as current.
Current indexed streams and changed/unindexed live prompt streams enter the
single-owner merge from {ref}`ADR 0014
<adr-result-order-limit-and-streaming-merge>`. FTS and SQL predicates only
generate candidates; hydrated candidates pass the same normalized-record
matcher as live prompts. Queries the index cannot represent exactly scan the
canonical prompt rows or route affected sources live rather than silently
under-returning. Terms shorter than three Unicode code points therefore need an
explicit fallback when the [FTS5 trigram
tokenizer](https://www.sqlite.org/fts5.html#the_trigram_tokenizer) cannot
produce the required candidates.

The hybrid guarantee in this invariant applies to **prompt search**. ADR 0017's
prompt-guided selection of a subset of conversations is intentionally
heuristic and reports `approximate`; it must not weaken the exactness of the
canonical prompt index or describe an omitted conversation as a negative
match. ADR 0016 owns the user-visible transition between those effort levels.

Ordinary search is read-only with respect to durable storage. It never creates
or migrates a database, writes reconciliation state, builds an index or starts
a sync. Read-only reconciliation compares existing observations with current
sources for planning; changed or unrepresented sources stream through live
fallback. Only explicit sync or an independently enabled, visible synchronizer
may publish that reconciliation durably.

The default index mode is `auto`: use every verified current partition and
stream the rest live. `off` bypasses persistent reads for the invocation.
`require` fails with structured coverage diagnostics instead of silently
falling back. First use remains correct without a database, an ordinary query
does not create one, and an index may be deleted without making prompt search
unavailable.

### PC-8 — Sync, publication and repair are explicit and non-destructive

Explicit sync, or an independently enabled synchronizer whose running state is
visible, owns corpus and index writes. It prioritizes active conversations,
recent sources, changed indexed sources and then older backfill. Watchers may
wake that synchronizer early, but query-time read-only reconciliation and
periodic sync remain the correctness mechanism. A cheap stat fingerprint is
only a scheduling hint. Neither a normal query nor a one-off deep query enables
the synchronizer as a side effect.

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

An incompatible index builds beside the active generation, passes SQLite and
FTS integrity verification, closes successfully and is then published
atomically. Existing readers may finish against the old generation. An
incompatible corpus schema uses ordered, checksummed forward migrations and a
recoverable backup for destructive steps.

Derived-index corruption may trigger a visible rebuild. Corpus corruption never
triggers automatic deletion: agentgrep preserves or quarantines the damaged
database, reports the finding and reconstructs a replacement from readable
origin stores. Every repair and prune operation has a machine-readable dry-run
form. Read-only status and audit commands never create, migrate, sync, repair
or prune storage.

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
ordinary search still excludes them. When grace expires, pruning removes
unreachable occurrences and conversation locators; a content BLOB is removed
only after no retained occurrence references it. Storage accounting
distinguishes current, stale, grace-period and reclaimable bytes.

A future archive mode that retains origin-deleted prompts requires explicit
opt-in, visible deletion semantics and a separate decision. This ADR does not
authorize indefinite retention. A user-requested purge may bypass the grace
period after a dry-run identifies the exact scope.

### PC-10 — Privacy, observability and testing are part of correctness

Corpus admission follows the runtime store catalogue. `DEFAULT_SEARCH` stores
are eligible for automatic durable admission when explicit sync or the visible
synchronizer runs. `INSPECTABLE` stores require an explicit persistence/sync
opt-in naming that coverage; selecting them for one search, including a one-off
deep search, must not silently begin retaining them. `CATALOG_ONLY` data is not
copied unless another decision defines a safe payload, and `PRIVATE` stores are
never enumerated or copied.

The databases use owner-only permissions. Private locators, local paths,
native keys, query strings and prompt text do not enter logs, profiler
artifacts, public diagnostics or opaque public refs. ADR 0006-governed display
and debug paths are presentation only and never resolution inputs.

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
- `PromptRef` and `ConversationRef` remain opaque, resolve through private
  locators and never encode display/debug paths or native keys;
- a prompt locator and a conversation locator resolve only against their bound
  source observation, and stale or ambiguous resolution never opens different
  content;
- live-only, indexed-only and hybrid prompt plans produce the same semantic
  result set, order, dedupe and global limit;
- JSONL complete-prefix ingestion permits later append but retries after
  captured-prefix mutation; SQLite ingestion reads one transaction snapshot;
- cancellation or source mutation during ingest publishes no partial
  observation or index generation;
- corpus, index and FTS corruption choose the documented non-destructive repair;
- complete authoritative-root discovery starts source-level grace only for a
  missing source, while complete per-source reconciliation starts occurrence-
  level grace only for a deletion inside a source that still exists; bare path
  absence, partial or unavailable roots prove neither, and reappearance, expiry
  and shared-content collection follow PC-9;
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
- `agentgrep db rebuild-index`
- `agentgrep query explain`
- `agentgrep export --format ndjson`

Names are provisional until their public-surface implementation lands. The
required concepts are not: status is read-only; sync is explicit and
cancellable; verify distinguishes corpus/index/FTS and pointer health; repair
and prune preview their scope; rebuild never threatens canonical prompt
evidence; explain states which sources are indexed, live or suspect and why.

## Prior art

The adopted patterns are deliberately narrower than their source systems:

- [SQLite WAL](https://www.sqlite.org/wal.html) supplies local reader/writer
  coexistence, while [FTS5](https://www.sqlite.org/fts5.html) supplies trigram
  candidate search and documents the synchronization and integrity obligations
  of contentful, contentless and external-content tables.
- [Datasette 1.0a37](https://github.com/simonw/datasette/blob/1.0a37/datasette/database.py)
  demonstrates a serialized writer and independent reader connections. The
  reusable pattern is connection ownership, not Datasette's public query API.
- [claude-history v0.1.68 conversation
  refs](https://github.com/raine/claude-history/blob/v0.1.68/src/agent/refs.rs)
  separate a stable external conversation reference from local transcript
  resolution. agentgrep adopts that separation, not its backend-specific hash
  inputs or resolution rules.
- [Git v2.54.0 repository
  layout](https://github.com/git/git/blob/v2.54.0/Documentation/gitrepository-layout.adoc)
  separates content-addressed object identity from ref-based reachability.
  agentgrep adopts that distinction for shared prompt bytes and occurrence
  retention, not Git's object graph, ref encoding or permanence semantics.
- [Codex state](https://github.com/openai/codex/tree/5bed6447998c754d154dbd796517310b8f04d4ce/codex-rs/state)
  demonstrates WAL-backed derived state, ordered migrations and read-only
  audit. Its rollout JSONL remains an origin authority, which is the same
  authority boundary this ADR keeps for complete conversations.
- [pytest's cache provider](https://github.com/pytest-dev/pytest/blob/3fa8d9b15b733aadb8a043cca3e98447804e1f28/src/_pytest/cacheprovider.py)
  demonstrates temporary construction followed by atomic publication and
  cleanup of a concurrent loser. Derived index generations adopt that
  publication shape, not pytest's directory schema.

## Rejected alternatives

- **Mirror every admitted native record:** this stores assistant, reasoning,
  tool and unrelated payloads, expands privacy and migration obligations, and
  promises transcript replay that prompt search does not require.
- **Persist only `SearchRecord`:** it cannot distinguish repeated occurrences,
  prove its source observation, reproduce complete untruncated prompt text or
  resolve the containing conversation safely.
- **Persist prompt text without occurrence and conversation pointers:** it can
  answer lexical questions but cannot explain provenance, open the prompt or
  support ADR 0017's bounded conversation routing.
- **Make FTS rows canonical:** tokenizers, projections and query contracts
  change independently of extracted prompt evidence; placing both in one
  rebuild domain turns an optimization migration into evidence loss.
- **Treat an indexed observation as permanently current:** it admits stale
  positives and missed prompts after origin, WAL or sibling-dependency changes.
- **Require a complete index before prompt search:** first use, changed sources
  and corruption recovery would fail even though live adapters can answer.
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
  schema lifecycles do not belong in lexical prompt correctness. Future vectors
  remain disposable enrichment keyed by prompt content and an explicit model
  contract.

## Relationship to other ADRs

{ref}`ADR 0001 <adr-storage-version-detection>` continues to decide which
adapter and extraction contract interprets each observed source. This ADR
persists the extracted prompt evidence and detector result; it does not replace
shape-first detection.

{ref}`ADR 0004 <adr-headless-query-planning-non-blocking-execution>` owns the
query plan, driver, event stream and run-status vocabulary. This ADR adds
prompt-corpus coverage, index/live partitioning and sync phases to those
contracts.

{ref}`ADR 0006 <adr-public-cli-mcp-surface-contract>` owns public envelopes,
diagnostics, cursors and refs. Database rowids and private locators do not cross
that boundary.

{ref}`ADR 0011 <adr-non-blocking-tui-invariants>` owns the Textual pump and
worker discipline. This ADR requires database, extraction and hydration work to
obey it.

{ref}`ADR 0014 <adr-result-order-limit-and-streaming-merge>` owns global order,
the completeness barrier and the rule that limit follows order. Indexed and
live prompt sources enter that one merge rather than producing independent
result sets.

{ref}`ADR 0016 <adr-progressive-deep-search>` owns normal versus deep search
effort, public CLI/MCP/TUI behavior and escalation. This ADR supplies the
complete prompt surface used by normal search.

{ref}`ADR 0017 <adr-prompt-guided-conversation-routing>` owns heuristic
candidate generation, conversation budgets and targeted deep-search coverage.
This ADR supplies evidence and pointers but does not claim that prompt clues
form a complete conversation candidate set.

## Consequences

agentgrep gains a stable prompt-search corpus without becoming a second archive
of every agent conversation. Complete prompt bytes survive display and index
changes; versioned semantic field values make every exact prompt index
rebuildable without an origin read; separate occurrence identity preserves
repeated prompts; observation-bound locators make results inspectable and
provide safe clues for conversation routing. Read-only live fallback keeps
first-use and changed-source prompt search correct without making search a
hidden writer.

The narrower boundary also has limits. A future projection cannot recover
assistant, reasoning, tool or attachment data from `prompts.sqlite3`; it must
read the origin. Stale and grace-period prompt bytes remain available only to
explicit retained-evidence inspection/export, not ordinary search. Adapter
authors must define complete human-text extraction, semantic-field provenance,
native coordinates, dependencies, consistent snapshot/cutoff rules and
rediscovery behavior. Hybrid coverage, durable migrations, storage accounting
and privacy-safe pruning create a real subsystem even though it is smaller than
a universal mirror.

The chief footgun is allowing either side to overclaim authority. Persisting a
whole user-shaped native event can quietly retain tool results; trusting an old
coordinate can open the wrong conversation; treating a prompt-derived
candidate set as complete can hide a deep-search miss. The mitigation is
structural: an explicit extraction allow-list, distinct prompt and conversation
locators bound to a source observation, replaceable indexes with exact prompt
coverage, visible stale/grace states, and separate ADR 0017 approximation.

## Final position

agentgrep durably stores complete extracted human prompts, not everything an
agent wrote. Content identity saves bytes without erasing occurrence identity;
private observation-bound pointers lead back to the prompt and its containing
conversation without becoming blind long-lived paths; opaque `PromptRef` and
`ConversationRef` values keep those locators out of public identity; derived
FTS/read models remain disposable; indexed and live partitions preserve exact
current prompt-search semantics; and per-source occurrence reconciliation plus
a visible grace period prevents the corpus from quietly becoming an archive.
Ordinary search remains read-only, inspectable-store persistence remains
opt-in, complete conversations remain where their agents wrote them, and deep
search resolves them deliberately under ADRs 0016 and 0017.
