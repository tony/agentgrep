(adr-durable-prompt-corpus-derived-search-indexes)=

# ADR 0019: Durable prompt corpus and derived search indexes

## Status

Proposed.

## Context

agentgrep reads independently versioned stores with different schemas and
lifecycles. A normalized search record is useful at runtime, but it is not
enough to rebuild prompt search: it may omit extraction provenance, source
observation evidence, native prompt coordinates, and a separately resolvable
conversation coordinate.

A universal mirror of native records would retain assistant responses,
reasoning, tool traffic, attachments, mutations, and unknown fields merely to
support prompt search. That would duplicate the most sensitive conversation
content and make agentgrep responsible for replaying formats it does not own.

The durable boundary should instead match the common search question. Human
prompts are retained as canonical local evidence. Complete conversations stay
with their origin stores. Replaceable indexes derive from the prompt evidence,
and deeper conversation search resolves the origins only when requested.

## Decision

agentgrep adopts three separate authority layers:

1. **Origin stores** own complete conversations, native structure, and current
   source state.
2. **The prompt corpus** owns the durable extracted human-prompt projection at
   a recorded source observation.
3. **Exact read models** are derived, versioned, and rebuildable. Enrichments
   are derived and removable, with reproducibility and lifecycle owned by ADR
   0005.

If adopted, this decision supersedes earlier storage designs in which one
agentgrep-managed database simultaneously acts as transcript mirror, prompt
corpus, exact index, enrichment cache, and insights store. Reusable extraction
or indexing code may remain, but it must respect these authority boundaries.

### Durable evidence boundary

For every admitted prompt occurrence, the corpus retains the complete ordered
human-text projection before display truncation, tokenization, case folding,
snippet generation, or result limiting. An adapter defines how typed text
blocks and admitted prompt sidecars compose that projection.

The corpus also retains the source-derived values and provenance required by
the versioned prompt-query contract, together with enough source-observation
evidence to judge whether the projection remains current. This includes the
private coordinates needed to distinguish the occurrence and locate both the
prompt and its containing conversation.

The durable default excludes:

- assistant and model responses;
- reasoning, tool calls, and tool results;
- attachments and non-text parts not deliberately expanded into a human
  prompt;
- compaction, mutation history, and branch topology beyond query-semantic
  provenance or locator evidence; and
- complete native rows, events, blobs, or conversations merely because they
  contain a prompt.

An adapter that cannot prove complete extraction records a coverage gap. It
must not silently retain a truncated value as canonical prompt evidence.

This boundary does not prohibit a future durable transcript store. Such a
store requires a separate decision covering admission, authority, retention,
privacy, migration, and public surfaces. It cannot present itself as an exact
prompt-index generation.

### Private provenance, identity, and locators

The corpus distinguishes these concepts even when an implementation can reuse
storage internally:

- prompt content equality;
- one prompt occurrence;
- one origin source observation;
- one containing conversation;
- prompt and conversation resolution evidence;
- collector deduplication; and
- public identity and pagination coordinates.

Equal prompt text never proves occurrence equality. Repeated identical prompts
remain distinct occurrences. Coincident paths, timestamps, titles, native
identifiers, or storage row numbers do not prove that two sources,
conversations, or occurrences are equal.

Corpus keys and locators are private. They may support storage sharing,
freshness, deduplication, routing, and cursor state, but they do not mint a
public content, record, thread, bookmark, export, similarity, or resolver
identity. Public identity remains owned by a focused identity decision, and
{class}`~agentgrep.RecordRef` remains the physical prompt-drilldown contract
owned by ADRs 0004 and 0006.

Prompt and conversation locators are separate, observation-bound evidence.
Resolution succeeds only when the owning adapter can reconcile the locator
against the relevant source observation. A stale or ambiguous locator produces
an unavailable or coverage outcome; agentgrep never guesses from similar text,
path, title, timestamp, or ordinal.

### Derived indexes and freshness

Exact prompt indexes consume committed corpus evidence and are replaceable read
models. They are never the only copy of canonical prompt evidence. Their
physical backend, schema, tokenizer, digest, filenames, and publication
protocol are implementation choices unless a later compatibility decision
makes one public.

This ADR owns the meanings of prompt-index freshness, exactness, coverage,
activation, and fallback. Adapters own the native observation proof from which
those claims are derived. Providers own how they encode their artifacts and
evidence; they cannot redefine the claims.

A prompt-search snapshot distinguishes:

- corpus evidence represented by a verified current exact index;
- eligible prompt evidence that must be read from the corpus or origin because
  it is new, changed, or unindexed; and
- suspect, unavailable, or unsupported evidence that must remain visible as a
  coverage gap.

The snapshot identifies the corpus incarnation and its monotonic logical
revision, the relevant source observations, and any participating provider
generation. A continuation remains on the same validated snapshot or fails as
stale; it does not silently move to newer evidence.

An exact provider may produce candidates only when its contract is
candidate-complete for the predicate it claims to represent. Candidate records
still hydrate through the canonical prompt matcher. Provider choice, indexed
versus live provenance, and freshness never supply a hidden final-ranking
boost. Missing, corrupt, incompatible, or stale provider state falls back to a
canonical corpus scan or eligible live prompt path when that fallback can
preserve the declared coverage.

Provider activation is explicit. Installing an optional dependency does not
build, activate, or replace an index. Semantic indexes, embeddings, summaries,
and inferred ranks remain enrichments under
{ref}`ADR 0005 <adr-local-insights-reports-model-backed-enrichment>` and cannot
become exact-search authority. A focused similarity or insights operation may
own its own disclosed, versioned derived score and order without changing this
rule for ordinary exact search.

### Mutation, repair, and retention

Ordinary search is read-only. Corpus synchronization, index construction,
activation, repair, and pruning are explicit operations. Readers observe a
complete old or complete new logical revision; they do not observe partial
publication. A failed build or repair leaves the last valid generation usable,
and repair does not rewrite origin stores.

The prompt corpus is a retained mirror, not an archive. Prompt evidence becomes
eligible for removal only after the owning adapter proves that the represented
origin occurrence is absent from a sufficiently current observation and the
configured grace policy has elapsed. An unavailable source, unsupported
adapter, stale observation, or incomplete scan is not proof of absence.

External bookmarks and exports do not pin corpus retention unless their own
focused adopted decisions explicitly establish such a pin. Portable export and
import remain independently owned contracts; corpus inspection does not become
a second portable format or restore path.

Logical removal and physical reclamation are distinct. Diagnostics describe
what agentgrep can establish and must not call ordinary pruning or backend
reclamation secure erasure.

### Privacy and observability

Persisting prompt evidence is explicit and inspectable. Diagnostics may report
counts, versions, source classes, coverage, ages, and integrity states, but not
prompt text, secrets, unredacted local paths, or private locator material.
Search effort never authorizes writes, synchronization, model provisioning,
enrichment, or expanded retention.

## Relationships

- ADR 0001 owns native storage-version and source-observation detection.
- ADR 0004 owns planning, execution lifecycle, status, and coverage envelopes.
- ADR 0006 owns public CLI and MCP spelling.
- ADR 0014 owns final matching inputs, deduplication, representative selection,
  order, and pagination.
- {ref}`adr-progressive-deep-search` proposes how prompt and conversation
  surfaces map to search effort.
- {ref}`adr-prompt-guided-conversation-routing` proposes how prompt evidence may
  select conversations without becoming result authority.

## Consequences

Prompt search can be rebuilt without repeatedly parsing every origin store, and
the durable copy contains materially less sensitive data than a transcript
mirror. Exact-index implementations remain replaceable, and future identity,
bookmark, export, similarity, and insights decisions retain their own public
contracts.

The cost is adapter work: every admitted format must define complete human-text
extraction, observation evidence, occurrence distinction, and proof-bound
locators. Prompt bytes are still sensitive duplicated data and require explicit
retention and privacy controls. Deep operations continue to depend on origin
availability because the corpus deliberately does not archive full
conversations.

## Rejected alternatives

**Mirror every native record.** This expands privacy, migration, and replay
obligations far beyond prompt search.

**Treat an exact index as canonical storage.** Index implementations and
tokenization strategies must remain replaceable without losing evidence.

**Use prompt content as occurrence or conversation identity.** Equal content
does not establish occurrence equality, topology, or a safe resolver target.
