(adr-prompt-guided-conversation-routing)=

# ADR 0021: Prompt-guided conversation routing

## Status

Proposed.

## Context

Human prompts are useful clues about which conversations may contain a deeper
match. A library name, error fragment, or project term in a prompt can identify
a small set of conversations worth opening. Searching those transcripts can be
far cheaper than sweeping every conversation in every eligible store.

Prompt evidence is not a conversation-content index. A query may occur only in
an assistant response, reasoning block, or tool result, or it may use different
language from the prompt that led to it. A negative-only query may provide no
positive routing clue. Stale or unavailable locators can also prevent a likely
conversation from being searched.

Targeted search is therefore a recall-oriented routing heuristic followed by
the normal exact matcher. It must bound work and expose omissions without
letting candidate evidence become result evidence.

## Decision

Targeted deep search uses a deterministic, versioned routing policy over a
declared evidence snapshot. The router selects a bounded set of conversation
attempts. Selected transcripts then use the same matcher and collector as
exhaustive search.

Routing owns candidate evidence, candidate grouping, and candidate order. It
does not own final-result matching, scoring, representative selection,
deduplication, ordering, pagination, or result limits.

### Candidate evidence is separate from result evidence

The original compiled query remains unchanged for transcript matching. The
routing policy may derive broader positive clues or use conversation-level
metadata to rank candidates, but those clues can only select a conversation.
Every emitted record must satisfy the original query against a normalized
transcript record.

Routing score, evidence strength, provider origin, indexed-versus-live
provenance, locator freshness, and worker arrival never establish or reorder a
final match. Within selected conversations, the canonical matcher and ADR
0014's collector remain authoritative.

The baseline policy may use deterministic lexical prompt evidence, safe
positive-query relaxations, explicit request metadata, and scoped project
evidence. Negative clauses do not become positive clues. The baseline does not
require a global-recency fallback; spending the most work on the least
informative queries is not a sound default.

A semantic or model-backed routing tier requires an explicitly selected,
named, and versioned policy with a declared provider. Installing a dependency,
finding no lexical clues, or selecting deep search does not activate it. It may
not download a model, build an index, or send private content to a remote
service implicitly. Semantic evidence can select conversations only.

This restriction applies to ordinary search routing. A focused similarity or
insights operation may define its own disclosed, versioned derived score and
order over an immutable generation; it does not thereby become exact-search or
transcript authority.

### The routing snapshot is declared, not exhaustive

Targeted routing consumes a fixed snapshot of the prompt, metadata, and
eligible live evidence that its selected policy can represent. It does not need
to enumerate every eligible conversation before applying its bound. Requiring
complete live enumeration, including zero-prompt conversations, would move
exhaustive discovery cost in front of a supposedly targeted search.

The snapshot reports which sources and evidence classes it covered. Live-only,
zero-prompt, unsupported, unavailable, and otherwise unrepresented
conversations remain possible omissions. They are coverage information, not
negative evidence. Exhaustive effort owns complete eligible-conversation
enumeration.

Only conversation-invariant predicates may remove a conversation during
routing. Record-local predicates such as message role, message timestamp, or
message kind remain the transcript matcher's responsibility.

### Conversation work is bounded after grouping

Candidate evidence is grouped by a private conversation key before the routing
bound applies. Equal prompt text, duplicate index views, or multiple matching
prompt occurrences must not consume independent conversation slots when they
refer to the same proven conversation. Conversely, coincident titles, paths,
timestamps, or native identifiers do not justify grouping without the owning
adapter's equality proof.

The normalized targeted request carries `conversation_limit`, a positive
integer distinct from result `limit`. The initial default is **25 distinct
conversations** per logical targeted request or cursor chain. Failed, stale,
unavailable, and non-matching conversation attempts consume the bound. Backfill
may replace an unsuccessful candidate only while remaining within the same
bound.

The value is a configurable policy default, not a result limit or completeness
claim. Public surfaces may expose an idiomatic spelling for the normalized
field, and deployments may set a default; both validate to the same positive
integer. The value is fixed into a targeted continuation. It may be tuned later
from measured recall and cost. Changing the project default requires a
versioned migration or explicit superseding decision because it changes
expected targeted recall and work. A deployment may retain an explicit local
override without changing the project default.

This bound is independent of the per-response result `limit` defined by the
progressive-search ADR. Provider calls, retries, bytes, wall-clock deadlines,
and page size do not silently share the conversation counter. The targeted
product guarantee is a bound on conversation attempts, not a claim that every
form of routing work uses the number 25. Supporting providers and transports
declare finite operational safety bounds and report which bounds applied;
their values remain execution policy rather than additional public meanings
for `conversation_limit`.

Candidate selection and tie-breaking are deterministic for a fixed normalized
request, evidence snapshot, and routing-policy version. The ADR does not
prescribe a scoring formula, evidence-tier algebra, or physical sorting
implementation.

### Locators are proof-bound

Each candidate carries a private conversation locator bound to the source
observation that produced the routing evidence. The owning adapter determines
whether it still resolves safely. Resolution never guesses from similar text,
path, title, timestamp, or ordinal.

An unresolved or ambiguous locator yields a structured attempt outcome and
consumes a conversation slot. It cannot silently retarget another conversation
or trigger an exhaustive sweep. Provider/source timeouts remain typed operation
outcomes; caller cancellation and whole-request deadlines retain the shared
lifecycle meaning owned by ADR 0004.

Private conversation keys and locators are routing evidence. They do not
become public thread, bookmark, export, similarity, or resolver identities.

### Selected conversations use the exhaustive matcher

The transcript projection and matcher for a selected conversation are the same
ones used by exhaustive search for that source. Routing relaxations never alter
field, text, date, normalization, or match-range semantics.

ADR 0004 and ADR 0014 decide when records from a source scan are safe to commit
and emit. This ADR does not impose a second all-or-nothing scan rule or weaken
their stable-prefix and representative proofs.

Prompt and transcript matches enter one final collector. Cross-stage duplicate
views are handled by the canonical deduplication and representative policy.
Routing rank and the 25-conversation bound never change final result order or
the per-response result limit.

Execution may use inline, thread, process, worker, native, or provider
transports. Equivalent transports consume the same fixed plan and return typed
outcomes. Given the same typed outcomes, scheduling and arrival order cannot
change the logical routing decision or final result semantics. Timeout and
retry outcomes are consumed deterministically under the plan and never
masquerade as caller cancellation; different typed outcomes may legitimately
change membership, status, and coverage.

### Targeted search remains approximate

A completed targeted run is globally `approximate` even when every selected
transcript scan completes. No candidates, no matches, or 25 completed attempts
does not prove that no eligible conversation matches.

Coverage identifies the routing-policy version, applied conversation bound,
represented evidence classes, selected and attempted conversation counts, and
privacy-safe failure or gap categories. It does not expose prompt text, private
keys, raw locators, unredacted paths, or linkable snapshot identifiers.

Escalation to exhaustive effort is explicit. Targeted search never sweeps all
remaining conversations after a weak result, and a later cursor page never
reroutes, retries, backfills, or admits newer evidence. The fixed routing
decision either continues under its validated snapshot or fails as stale.

## Relationships

- ADR 0004 owns planning, execution lifecycle, typed outcomes, cancellation,
  status, and coverage envelopes.
- ADR 0006 owns public spelling and next-action schemas.
- ADR 0014 owns final representative choice, deduplication, order, pagination,
  and stable emission.
- {ref}`adr-durable-prompt-corpus-derived-search-indexes` proposes prompt
  evidence, freshness, private grouping, and locator boundaries.
- {ref}`adr-progressive-deep-search` proposes targeted effort, per-response
  `limit`, and explicit exhaustive escalation.

## Consequences

Prompt evidence can reduce transcript I/O without pretending to be a complete
conversation index. The 25-conversation default makes initial cost and recall
measurable, while policy versioning permits later tuning and optional semantic
routers. Final result behavior remains shared with exhaustive search.

Some useful conversations will be omitted, especially when the query appears
only in non-prompt content or the origin cannot supply a trustworthy locator.
Stable targeted pagination requires preserving the fixed bounded decision.
Adapters that cannot prove conversation grouping or resolution can contribute
prompt results but cannot safely advertise those occurrences as routable.

## Rejected alternatives

**Treat prompt misses as conversation misses.** Prompt text is not a superset
of conversation text.

**Enumerate every conversation before routing.** That moves exhaustive
discovery into the targeted path and makes its cost claim misleading.

**Guess stale conversation locations.** A plausible but wrong transcript is a
correctness and privacy failure.

**Automatically sweep after weak routing.** This hides work and defeats
explicit effort selection.

**Make a semantic provider the implicit baseline.** Dependency installation or
environment state must not silently change routing, privacy, or result meaning.
