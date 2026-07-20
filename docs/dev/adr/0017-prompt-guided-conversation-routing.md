(adr-prompt-guided-conversation-routing)=

# ADR 0017: Prompt-guided conversation routing

## Status

Proposed.

## Context

{ref}`adr-durable-prompt-corpus-derived-search-indexes` gives
agentgrep a durable corpus of complete human prompts, private corpus grouping
keys and locators for their prompt occurrences and containing conversations.
Public result drilldown remains the `RecordRef` contract owned by ADRs 0004 and
0006; routing does not turn a corpus key or locator into public identity.
{ref}`adr-progressive-deep-search` makes that corpus the normal search surface and
reserves conversation-body work for explicit targeted or exhaustive deep
search.

The prompt corpus can do more than answer prompt queries. Prompt hits are often
good clues about which conversations contain the answer to a broader query. A
search for a library name, error fragment or project term can shortlist a few
conversations, after which agentgrep can load and search only those transcripts.
That avoids an expensive sweep over every conversation in every eligible
store.

The clue is not proof. A query may occur only in an assistant response,
reasoning block or tool result. It may use different words from the prompt that
led to that response. A negative-only query has no positive prompt term to use
as a seed. Prompt evidence may also be stale or unroutable, an attempt bound may
omit a conversation, or a stored locator may no longer resolve against the
observed source generation. Exact matching inside the selected conversations
cannot repair a conversation that routing never selected.

Established search systems make this distinction explicit:

- claude-history first ranks conversations, then
  [loads only a bounded shortlist of transcripts](https://github.com/raine/claude-history/blob/v0.1.68/src/agent/search.rs#L311-L385),
  reports how many transcripts it loaded, derives
  [namespace- and identity-based conversation references](https://github.com/raine/claude-history/blob/v0.1.68/src/agent/refs.rs#L14-L55),
  and performs
  [collision-aware reference resolution](https://github.com/raine/claude-history/blob/v0.1.68/src/agent/refs.rs#L222-L266).
- ripgrep distinguishes a line that is already `Confirmed` from a
  [`Candidate` that must be searched for verification](https://github.com/BurntSushi/ripgrep/blob/15.1.0/crates/matcher/src/lib.rs#L519-L531),
  and its searcher
  [runs the full matcher before emitting a candidate line](https://github.com/BurntSushi/ripgrep/blob/15.1.0/crates/searcher/src/searcher/core.rs#L491-L515).
- Lucene's
  [`TwoPhaseIterator`](https://github.com/apache/lucene/blob/dadfd90b4401947f4d0387669dc94999fbb2c830/lucene/core/src/java/org/apache/lucene/search/TwoPhaseIterator.java#L23-L109)
  permits exact two-phase search only because the approximation is a superset
  of matching documents and every candidate is confirmed.
- Tantivy's
  [block-WAND implementation](https://github.com/quickwit-oss/tantivy/blob/7152d5318273f5dbcefa78dc26176a9fc6dd971b/src/query/boolean_query/block_wand_union.rs#L145-L213)
  skips work only when a score upper bound proves that a block cannot beat the
  active threshold. The useful principle is that a fast stage may preserve an
  exact claim only when its pruning rule proves that omitted work cannot affect
  the result.

Prompt-guided routing does not claim the Lucene or Tantivy condition: prompt
text is not a superset of conversation text. It is therefore a
recall-oriented heuristic followed by an exact matcher, not an exact index over
conversation contents. This ADR defines that boundary, the planner contract
and the evidence users and callers need to decide whether to escalate to an
exhaustive search.

## Decision

Targeted deep search uses a staged, snapshot-aware conversation-routing plan.
It searches current indexed and eligible changed or unindexed live prompt
evidence plus explicit metadata, adds bounded fallback evidence when the
resolved-candidate floor requires it, groups all evidence by a private corpus
conversation key, and fixes a deterministic routing work universe containing
the ranked candidates and reserves that may be attempted under the declared
bounds. Resolution, transcript scanning and collection may then overlap under
one order barrier. Successful complete scans form the final selected
conversation-source universe, and the original query supplies the only match
semantics inside it.

The pipeline is:

```text
original query + eligible source universe
  -> indexed + live prompt, explicit-metadata and bounded-fallback evidence
  -> candidates grouped by private corpus conversation key
  -> fixed approximate routing work universe with deterministic reserves
  -> ranked resolution/scan attempts with bounded backfill
  -> original query over every normalized record in each complete scan
  -> global merge, prompt-occurrence dedupe, order and limit
  -> completed selected universe, coverage and next-action summary
```

Twelve invariants govern the routing boundary (CR for *conversation routing*).

### CR-1 — Routing and result matching are separate contracts

The planner retains two query representations:

`result_query`
: The original compiled query, unchanged. The transcript matcher uses this
  query and the same field, text, date and normalization semantics as
  exhaustive conversation search.

`routing_plan`
: A versioned collection of routing-evidence tiers, safe universe constraints,
  deterministic relaxations and fallback tiers used only to rank candidate
  conversations.

A relaxed phrase, disjunction, token variant, metadata clue or recency fallback
may select a conversation. It may never create a result, change a matched
range, satisfy a predicate or raise a final relevance score by itself. Every
emitted deep result must satisfy `result_query` against a hydrated transcript
record. Routing score, evidence tier, index origin, worker arrival, locator
freshness and fallback origin never establish, drop, promote, demote or reorder
a final match. They decide only which conversation sources are attempted.

The routing and result-query digests, routing-policy version and applied
relaxation tiers travel on the physical plan and result summary. A frontend
does not reconstruct or reinterpret either query.

### CR-2 — Candidate selection begins from an explicit universe and snapshot

The planner first enumerates the conversation sources eligible under the
request's agent, store, privacy and explicit source-scope constraints. It
captures the discovery generation and the source observations represented by
the prompt corpus. This forms the `RoutingSnapshot`.

As part of that enumeration, the owning adapter produces an ephemeral private
`corpus_source_instance_key`, a snapshot-bound live source observation or
cutoff, an ephemeral private `corpus_conversation_key` and a conversation
locator bound to that source key and observation for every eligible live
conversation. This includes live-only conversations and conversations
containing zero admitted prompts. The ephemeral and persisted forms share the
adapter-owned, repository-global equality and collision-domain contracts from
the prompt-corpus decision: for any two eligible source or conversation
observations, ephemeral keys compare equal if and only if corpus ingestion
under the same adapter and observation contracts would assign them the same
private keys. Live routing may not introduce a second grouping rule based on
path, title, timestamp, ordinal or prompt text. These keys, observations and
locators live only for the routing snapshot unless an explicit corpus
synchronization operation persists admitted prompt evidence; targeted search
itself never writes them.

An adapter cannot advertise targeted-routing support for a live source unless
it can enumerate the source-instance key and observation plus the conversation
key and bound locator for every eligible conversation in that source
observation. A missing value, broken source binding, key collision or
live/persisted equality mismatch is a structured adapter-contract and coverage
failure; the planner never silently removes the affected conversation merely
because it contains no admitted prompt.

Only predicates proven to be conversation-invariant may remove a conversation
from this universe. Agent and store selection are examples. A record-time,
role, message-kind or other record-local predicate cannot be applied to prompt
metadata to exclude a conversation; the transcript matcher owns it.

The prompt stage consumes both current prompt partitions defined by
{ref}`adr-durable-prompt-corpus-derived-search-indexes`:

- verified current indexed prompt rows; and
- eligible changed, new or unindexed sources through the live prompt fallback.

Both partitions hydrate through the same prompt matcher, produce the same
routing-evidence shapes and converge before conversation grouping. Being
indexed or live is provenance, never candidate or final-result rank. The live
fallback extracts prompt records only; it does not materialize every transcript
record or run the conversation-body matcher merely to construct a shortlist.

A live prompt occurrence contributes prompt evidence only when the adapter
binds it to the enumerated private `corpus_conversation_key` and snapshot-bound
containing-conversation locator. When it cannot, the occurrence remains
eligible as a normal prompt result but is reported as
`live_prompt_unroutable`; the conversation may still contribute independently
through explicit-metadata or fallback evidence. The router does not fabricate a
thread, guess a locator or infer a conversation from text, title, path or time.
Stale, grace-period and retained-only prompt evidence is excluded by the
durable prompt-corpus contract. A source observation covered by neither the
current indexed partition nor live enumeration is a coverage gap, not a
negative result. Targeted routing does not scan every missing transcript to
repair that gap, because that would be an undisclosed exhaustive search.

### CR-3 — Routing evidence is deterministic, ordered and explainable

The minimum routing policy uses these evidence tiers, strongest first:

1. **Exact prompt expression**: the prompt-applicable positive text expression
   matches a complete stored prompt under the normal prompt-query contract.
2. **Conjunctive text clues**: all positive literals or terms occur in one
   prompt after documented query normalization, even when the original
   expression required a structure the prompt index cannot represent.
3. **Disjunctive text clues**: at least one positive literal or term occurs in
   a prompt.
4. **Explicit metadata clues**: request metadata identifies or ranks an
   eligible conversation but is not safe as a record-level exclusion.
5. **Current-project fallback**: recently active eligible conversations in the
   explicit or deterministically resolved current project.

Negative clauses never become positive clues. Phrase decomposition,
conjunction-to-disjunction relaxation and the documented query normalizations
are allowed because they broaden routing only.

Two extension positions are reserved but absent from the minimum implementation:
semantic prompt evidence after disjunctive clues and before explicit metadata,
and global-recency evidence after current-project fallback. A future named,
versioned and explicitly selected policy may declare either. The baseline
exposes no routing-policy selector; a future extension first defines that public
selection contract under ADR 0006 and binds it into the normalized request,
cursor and coverage report. Selecting `--deep`, finding weak or no lexical
evidence, or installing a dependency never activates an extension.

If an implemented extension policy provides semantic prompt clues, selection
authorizes only already provisioned local capability. Routing never implicitly
downloads a model, builds a semantic index, starts provisioning or sends
prompt/query content to a remote service. Provider, model, index generation,
score contract, threshold and semantic sub-cap are compatibility-sensitive
inputs to that policy version and appear in privacy-safe coverage. An explicitly
selected unavailable policy or provider is a structured planning failure, not a
silent substitution. Semantic evidence selects candidates only; every result
still requires CR-6's exact matcher and receives no final-rank contribution from
semantic score. The minimum implementation need not expose or accept a semantic
policy.

The plan declares three distinct bounds:

`completed_scan_target`
: The desired number of distinct conversations whose transcript scan completes
  under CR-6 and may contribute to the fixed selected universe.

`candidate_attempt_cap`
: The maximum distinct ranked conversations for which locator resolution or a
  transcript scan may be attempted, including unsuccessful attempts and every
  fallback tier.

`fallback_min_resolved`
: The minimum number of distinct, usable resolved conversations sought from
  all non-fallback evidence enabled by the selected policy before fallback stops
  contributing candidates. In the baseline, that means prompt and explicit
  metadata evidence.

The plan requires `0 <= completed_scan_target <= candidate_attempt_cap` and
`0 <= fallback_min_resolved <= candidate_attempt_cap`. The resolved floor may
exceed the completed target when the policy deliberately prepares backfill
reserves. If `completed_scan_target` is zero, both other bounds must also be zero
and the plan performs no conversation work.

Fallback activation is deterministic and depends on usable resolutions, not raw
evidence rows or distinct ranked keys. The planner resolves higher-tier
candidates in canonical order. If fewer than `fallback_min_resolved` are usable,
the current-project tier contributes attempts toward the floor up to its
sub-cap. Stale, ambiguous, unavailable and failed resolutions do not satisfy the
floor. Fallback does not depend on final result count, a confidence adjective,
elapsed worker order or final-match score. Evaluation occurs at deterministic
tier barriers so parallel completion cannot change whether a fallback tier ran.
The baseline never expands from current-project to global recency. A future
named and versioned fallback extension must define its own sub-cap, evidence
variant, deterministic tier barrier and coverage fields.

Before transcript scans and collection overlap, the router freezes the ranked
work universe, including deterministic reserves and sub-caps for every eligible
fallback tier. Resolution and scan outcomes decide which frozen reserves are
attempted; they never discover or append a new candidate source outside that
universe.

The current-project fallback is eligible only when project identity comes from
an explicit request constraint or an exact frontend/project-catalog binding.
Path suffix similarity, prompt text and probabilistic repository inference are
not project resolution. Conversation activity time comes from snapshot-bound
conversation or source metadata under the routing policy's declared timestamp
contract. Results disclose the resolved floor, whether each fallback ran, its
sub-cap, ranked and attempted conversations, usable resolutions, completed
scans, failures, contributions to the selected universe and whether its input
metadata was unavailable.

### CR-4 — Private conversation keys are deduplicated before attempts

Every evidence item carries a private `corpus_conversation_key` supplied by the
owning adapter under the shared persisted-and-live equality contract. Persisted
prompt evidence reads the key from the durable prompt-corpus boundary; live
prompt, metadata and fallback evidence use the snapshot's ephemeral form. The
planner groups all evidence by that key before ranking or applying an attempt
or fallback sub-cap. Repeated prompts, many matching prompts in one
conversation, indexed/live projections of one occurrence and overlapping
metadata/fallback evidence therefore describe one candidate. A conversation
does not need an admitted prompt to participate through metadata or fallback
evidence.

`corpus_conversation_key` is query-planning identity only. Persisted and
ephemeral evidence may share its equality contract without making the key a
public identifier or authorizing search to persist the ephemeral form. It never
crosses a public result envelope and is not the bookmark, export, similarity or
drilldown identity. If the focused identity contract later supplies a public
grouping or thread field, routing preserves only values defensibly supplied by
the normalized record and never fabricates one. Equality of two such non-null
values may be evidence used by the adapter when it constructs its private
grouping key, but this ADR neither groups directly on a public identity field
nor treats one as a resolver.

`RoutingEvidence` has three baseline shapes:

- **Prompt evidence** carries the private `corpus_occurrence_key`, prompt tier,
  supported positive-clause set, prompt-match score, supporting occurrence
  time and source observation.
- **Explicit metadata evidence** carries the set of explicit request
  constraints matched by the conversation, snapshot-bound activity time and
  source observation.
- **Current-project fallback evidence** carries the exact current-project
  identity, snapshot-bound activity time and source observation.

An implemented semantic extension adds evidence carrying the private
`corpus_occurrence_key`, selected policy/provider contract, semantic score,
supporting occurrence time and source observation. A future global-recency
extension defines its own versioned evidence shape. Neither is a minimum type.

Evidence aggregation uses only commutative reductions, so shuffled rows and
worker completion cannot change a candidate. The aggregate retains all
contributing evidence and ranks under its strongest tier; weaker tiers remain
explanation and coverage data. Within one prompt tier it unions supported
positive-clause sets, takes the maximum prompt-match score, takes the newest
supporting occurrence time with missing last, and retains the bytewise-smallest
`corpus_occurrence_key` as the evidence tie-break. Explicit metadata evidence
unions canonical constraint keys and takes the newest activity time. The
current-project fallback takes the newest activity time. An implemented semantic
extension takes the maximum semantic score, then the newest supporting
occurrence time with missing last, then the bytewise-smallest
`corpus_occurrence_key`. Any other extension defines an equally commutative
reduction in its policy version.

The deterministic candidate order is tier first, followed by a tier-local key:

- prompt tiers order by distinct supported positive clauses, greatest first;
  prompt-match score, greatest first; then supporting occurrence time, newest
  first with missing last; then the retained `corpus_occurrence_key`, bytewise
  ascending;
- explicit metadata orders by number of distinct matched request constraints,
  greatest first; the canonical sorted constraint-key set, bytewise ascending;
  then activity time, newest first with missing last;
- current-project fallback orders by activity time, newest first with missing
  last; and
- every tier ends with the private `corpus_conversation_key`, bytewise
  ascending.

An implemented semantic extension occupies its reserved position and orders by
declared score, greatest first; supporting occurrence time, newest first with
missing last; then retained `corpus_occurrence_key`, bytewise ascending. A
future global-recency extension occupies its reserved position after
current-project and defines its deterministic tier-local order in the policy
version.

The canonical constraint key is a request-local identifier over one canonical
compiled predicate. Neither that private key nor its raw constraint value
enters diagnostics or profiles. `RecordRef` encoding, any future public identity
field, private locator bytes, index/live provenance, arrival order, worker
completion order and SQLite rowid never participate in grouping or candidate
ordering.

`candidate_attempt_cap` and each fallback sub-cap count distinct private corpus
conversation keys after dedupe. `completed_scan_target` counts only complete
CR-6 scans and is not an evidence-row or ranking cutoff. Any lower-level row,
time or memory bound that can stop evidence collection before conversation
dedupe is complete is an additional approximation and appears in the routing
report.

Scoring weights and tie-break behavior are compatibility-sensitive parts of
the routing-policy version. They may be tuned without changing final matching,
dedupe or ordering semantics, but a result must say which policy selected its
candidates. No candidate score survives as a final result score.

### CR-5 — Conversation locators are generation-aware resolution evidence

A conversation candidate carries its private `corpus_conversation_key`, private
`corpus_source_instance_key`, observed source generation, private
observation-bound locator and locator stability from the shared
persisted-and-live adapter contract.
Persisted candidates use locator evidence from the durable prompt corpus;
live-only and zero-prompt candidates use the ephemeral locator captured in the
`RoutingSnapshot`. Resolution succeeds directly only against the same current
source observation. A public `RecordRef` is created only for an emitted record
or supported drilldown projection; it is not resolution identity and does not
participate in candidate grouping, ranking or backfill.

When a source changed after the evidence was recorded, an adapter may reconcile
the locator only when native identity or an adapter-owned stable
anchor proves that the new locator identifies the same logical conversation.
Matching title, prompt text, path suffix, mtime or ordinal proximity is not
proof. Ambiguous, missing and unprovable mappings remain stale and are not
silently redirected.

The owner processes distinct candidates in CR-4 rank order. Starting locator
resolution consumes one `candidate_attempt_cap` slot. A current or provably
reconciled locator whose snapshot-bound transcript can be opened increments the
usable-resolved count. Stale, ambiguous, unavailable, changed and failed
outcomes consume the attempt slot but do not satisfy `fallback_min_resolved` or
`completed_scan_target`; a later invalidation also removes the attempt from the
usable-resolved count. The owner then tries the next ranked key. A complete CR-6
transcript scan increments `completed_scan_target`; a read, parse, consistency
or source-change failure does not. Deterministic backfill continues until the
completed target is met, the attempt cap or a declared routing/scan-time budget
is exhausted, or no ranked evidence remains.

Parallel resolution and scanning may reduce latency, but outcome commitment and
backfill admission follow candidate rank, never completion order. The routing
report distinguishes candidates ranked, attempted, usable-resolved and
scan-completed; resolution and scan failures by current, reconciled, stale,
ambiguous, unavailable, changed and failed state; both configured caps; fallback
activity; and the final stop reason.

A source that changes while its transcript is being materialized invalidates
that attempt's snapshot. Its provisional results are discarded unless the
adapter proves a consistent snapshot, the attempt does not count toward the
completed target, and deterministic backfill continues when budget remains.

### CR-6 — Selected transcripts use the same matcher as exhaustive search

After locator resolution, the planner materializes only attempted conversations.
Parent-linked sessions, mutation logs, SQLite parts and other native shapes must
produce the same normalized record stream they would produce during exhaustive
deep search. A cheaper targeted-only parser is not a second semantic authority.

The original compiled query is evaluated against every relevant normalized
record in each attempted conversation. Exact matching here means that a
completed scan produces no false final matches and no matcher-specific false
negatives. It says nothing about conversations omitted by routing. Routing
evidence, score and tier cannot satisfy the query or alter the match/rank of a
record that the exhaustive matcher confirms.

Source-local transcript limits, parser omissions, unsupported record kinds and
unverified snapshots make a scan incomplete. Only complete scans contribute
matches to the selected conversation-source universe. Matches buffered by a
partial, failed or invalidated scan are discarded; that attempt remains visible
and CR-5 backfills when budget permits.

### CR-7 — Prompt and conversation results share one merge

Routing first fixes an explicit, approximate routing work universe: the ranked
candidates and deterministic reserves that may still be admitted under the
snapshot, attempt cap, fallback policy and completed-scan target. Normal prompt
results and exact matches from complete transcript scans then enter the global
single-owner collector from {ref}`ADR 0014
<adr-result-order-limit-and-streaming-merge>`. The declared order, dedupe and
result limit apply once across all streams; the deep stage may not append an
independently truncated list after the prompt results.

Resolution, transcript scanning and collection may overlap after the work
universe is fixed. Before emitting a prefix, the collector's total-order barrier
accounts for every admitted, queued or reserved candidate that the fixed routing
decision could still backfill. A proven prefix may stream without waiting for
all scans only when no remaining record can outrank the prefix and no remaining
physical view can replace a selected representative under the dedupe contract.
When either proof is unavailable, emission waits. Arrival or worker completion
order never makes the decision.

Execution drivers may expose different candidate, progress and completion-event
timing. Given the same normalized request, routing snapshot and readable source
observations, they nevertheless select the same final representative for every
dedupe class and produce the same final record order. Inline versus frontier
collection, buffering strategy and first arrival are not representative
selection policies. Final representative choice and order remain governed by
{ref}`ADR 0014 <adr-result-order-limit-and-streaming-merge>`.

The routing bounds are source-work controls, not result limits or proof that an
omitted conversation could not contain a better record. They are the reason the
global result remains approximate. Within the completed selected universe, ADR
0014 applies without exception: no routing score, evidence tier, indexed/live
origin, arrival order, fallback source or freshness classification may
establish, discard, truncate, promote, demote or reorder a confirmed match.
Only the original matcher, cross-stream dedupe and declared final order/limit do
so.

When a transcript hit identifies the same human prompt occurrence already
returned from the prompt corpus, the storage decision's cross-stage contract
uses a canonical occurrence identifier when the focused identity contract
supplies one, or private `corpus_occurrence_key` only with adapter proof. Equal
text alone never collapses occurrences. The durable prompt projection remains
the canonical prompt result, while the deep scan may contribute verified
conversation provenance and match evidence. This dedupe applies only to human
prompts. Assistant, reasoning and tool hits remain distinct normalized records
under the exhaustive matcher; this ADR does not define a stable occurrence
identity or cross-projection dedupe contract for them. A user prompt found only
in a changed live transcript is emitted once with its live-record projection,
provenance and coverage state.

Targeted pagination is available only for ADR 0014's `order="newest"` and only
when the page sequence reuses one fixed progressive-search request snapshot. The
opaque cursor carries or privately references the query digest, routing-policy
and provider versions, `RoutingSnapshot`, fixed selected-universe state and the
full collision-free global sort key required by ADR 0014. Continuation searches
that same universe and resumes below the full key; it never reruns routing,
activates fallback, backfills candidates or admits new prompt/source evidence.
If the snapshot or selected locator state can no longer be validated, the
cursor is stale and the request fails without a partial replacement page. Other
orders return no targeted cursor under ADR 0014.

### CR-8 — Targeted search is always globally approximate

A targeted run reports `approximate` with reason
`heuristic_candidate_selection`. Exact matching and ordering inside the fixed
selected universe is not a completeness proof for the eligible universe, and
the `targeted` effort does not promote itself to `complete` based on query
shape, index coverage or benchmark results.

In general, a routed strategy could justify completeness only with all of the
following:

- the eligible conversation universe is complete for one discovery snapshot;
- every eligible source observation is represented by the routing input;
- the routing predicate is proven to include every conversation that could
  satisfy `result_query`;
- no pre-dedupe, candidate-attempt, completed-scan, fallback, time or memory
  bound can remove such a conversation;
- every selected locator resolves against the proved snapshot; and
- all selected transcript scans complete under the exhaustive matcher.

Prompt-guided targeted routing in this ADR does not claim that proof, including
for a narrow user-prompt-only query over a complete prompt corpus. Any future
complete routed strategy needs a separate contract and effort semantics; it
cannot silently upgrade `targeted`.

Zero completed conversations therefore means *no attempted conversation
completed a valid scan under this routing plan*. It does not mean no
conversation contains a match. Attempt-cap saturation, an unmet completed
target, fallback use, live-unroutable prompts and omitted source partitions
remain visible even when the selected universe produces zero exact hits.

Status follows ADR 0004's global precedence. A normally completed targeted run
remains `approximate`. Exhausting a planned evidence-collection,
`candidate_attempt_cap`, fallback sub-cap, routing-time or scan-time budget, or
ending with an unmet `completed_scan_target`, is a secondary approximation
detail with its cap, saturation and stopping stage; it does not replace the
primary status with `bounded`.

External timeout or user/client cancellation reports `cancelled`. A sink output
budget reports `truncated`. Recovered candidate failures preserve `approximate`
with incomplete coverage and attempt diagnostics when at least one scan
completes. A run with zero completed scans remains `approximate` when a declared
attempt, time, evidence or source-work bound stopped the plan while another
candidate or reserve could have been attempted. It is `failed` only when no
declared bound caused the stop and every attempted candidate ended in a terminal
resolution/read/parse/consistency failure, or when the planner or driver has an
unrecovered error. Each more severe status retains the heuristic-selection,
bounds, stop reason and coverage details so the routing omission risk remains
visible.

### CR-9 — Unseeded, unsupported and failed routing stays visible

The planner handles weak or unavailable routing evidence as follows:

| Condition | Required behavior |
| --- | --- |
| Negative-only query | Do not invert prompt matches into candidates; use eligible metadata/fallback tiers if available and report `routing_query_unseeded` |
| No positive prompt-compatible text | Use explicit metadata/fallback tiers if available and report the missing lexical seed |
| Unsupported routing predicate | Do not use it to exclude candidates; preserve it for the final matcher and report `unsupported_routing_predicate` |
| Missing or stale prompt partition | Search current indexed and eligible live prompt partitions, record the uncovered source observation and recommend exhaustive escalation |
| Live prompt cannot be bound to its enumerated conversation key and locator | Keep it eligible as a prompt result, exclude that prompt evidence from conversation attempts, retain independently eligible metadata/fallback evidence and report `live_prompt_unroutable` |
| Explicit optional routing policy or required provider unavailable | Fail planning with a structured capability diagnostic; do not silently substitute a policy or provision capability |
| Pre-dedupe bound or `candidate_attempt_cap` reached | Stop only at the declared bound and report the affected tier, cap, saturation, completed count and stop reason |
| Planned routing- or scan-time budget reached | Preserve results only from complete scans, keep primary status `approximate`, and report the stopping stage, completed target and unsearched counts when known |
| External timeout or user/client cancellation | Stop through the shared cancellation path, report `cancelled`, and retain approximation details |
| Locator stale, ambiguous or unavailable | Consume one attempt, reconcile only with proof, otherwise report the resolution state and deterministically backfill |
| Transcript read or parse failure | Consume one attempt, discard its provisional matches and deterministically backfill; report `failed` only when terminal candidate failures—not a declared bound—leave no valid completed scan |
| Source changes during scan | Consume one attempt, discard inconsistent results, deterministically backfill and report `source_changed` |
| Targeted cursor snapshot is stale | Reject continuation without rerouting or returning a partial replacement page; offer a fresh targeted request |
| Unrecovered planner or driver error | Stop with `failed` and retain any safely reportable partial coverage |
| Sink output budget reached | Stop with `truncated`; do not relabel it as routing approximation alone |

A zero-candidate unseeded run is a valid approximate result with an exhaustive
next action. It is not `failed`, because the router completed its declared
heuristic plan without selecting work.

### CR-10 — Exhaustive escalation is explicit and never hidden

Targeted routing never starts an exhaustive sweep automatically because it
found no candidates, too few matches or low-confidence evidence. Automatic
exhaustive fallback would make `--deep` latency and I/O unpredictable and would
hide the search-effort boundary the user selected.

Every approximate targeted result provides the structured next action for the
equivalent exhaustive request. CLI and TUI may render that action as a hint or
control; JSON, NDJSON and MCP expose it as data. Reissuing the exhaustive
request is a new explicit user or caller decision.

### CR-11 — Routing is observable without exposing conversation content

The physical plan, final summary and privacy-safe profile expose at least:

- routing-policy version, run-local snapshot identifier and applied evidence
  tiers, including any explicitly selected semantic provider/model/index
  contract;
- prompt partitions current-indexed, live-routable, live-unroutable, stale,
  missing and unsupported;
- routing evidence seen by variant and evidence rows stopped by any pre-dedupe
  bound;
- distinct conversations ranked and attempted, before and after each fallback;
- `completed_scan_target`, `candidate_attempt_cap`,
  `fallback_min_resolved`, per-fallback sub-caps, saturation and the final stop
  reason;
- locators usable-resolved, current, reconciled, stale, ambiguous, unavailable,
  changed and failed;
- transcript scans attempted, completed, skipped, changed and failed;
- normalized records examined, exact matches and duplicate prompt occurrences;
- per-stage elapsed time and coarse bytes or records read; and
- selection-completeness claim (`not_claimed` for targeted), approximation
  reasons and exhaustive next action.

The snapshot identifier exported in reports or profiles is generated for one
run or fixed targeted page sequence and cannot be derived from source ids,
paths, locators, native keys or observation digests. It is not a stable
fingerprint of a private store.

Profiles and diagnostics never contain a private `corpus_conversation_key`,
prompt text, transcript text, raw query expansions, local absolute paths,
native locator values, public identity values or `RecordRef` values. The private
grouping key and locators are sensitive routing evidence and may become stale;
`RecordRef` remains the public result/drilldown contract elsewhere. Agent/store
classifications, counts, durations, policy/provider versions and coarse byte
totals are sufficient for planner and benchmark analysis.

### CR-12 — Exactness, recall and cost are tested separately

The implementation requires focused contract tests for:

- deterministic routing under shuffled source and worker completion order;
- exact, conjunctive, disjunctive, explicit-metadata and current-project
  fallback tier precedence;
- adapter enumeration of every eligible live conversation, including live-only
  and zero-prompt conversations, with an equality-parity ephemeral private
  source-instance key, snapshot-bound live source observation or cutoff,
  conversation key and locator bound to both;
- structured adapter-contract and coverage failure when any eligible live
  conversation lacks any value or binding, without silently treating a
  zero-prompt conversation as ineligible;
- equality and collision-domain parity between ephemeral and persisted private
  source-instance and conversation keys, including indexed/live dedupe for one
  conversation, separation when the adapter cannot prove equality, and no
  collision between identical native ids from different source domains;
- snapshot-lifetime containment of ephemeral keys and locators, with no public
  exposure or search-triggered persistence;
- indexed and live prompt evidence parity, live-unroutable prompt-result
  preservation, continued eligibility of independent metadata/fallback
  evidence and exclusion of retained-only evidence;
- prompt, explicit-metadata and current-project evidence construction and
  tier-local ordering;
- fallback activation and stopping at `fallback_min_resolved`, based on usable
  resolutions rather than evidence count and independent of result count,
  confidence, timing and arrival order;
- commutative same-tier evidence reduction under shuffled evidence rows;
- private corpus-conversation-key dedupe before every attempt bound and stable
  private-key tie-breaks;
- absence before the focused public identity contract, preservation without
  fabrication after it and exclusion of public identity fields and `RecordRef`
  from grouping and ordering;
- proof that relaxed routing evidence never emits a non-matching result;
- parity between targeted and exhaustive matching inside the same candidate;
- proof that routing score, tier, index/live origin, arrival and freshness never
  change final matching, dedupe, score, order or limit inside the selected
  universe;
- identical final dedupe representatives and final record order across inline,
  frontier and overlapped execution drivers under the same request and
  snapshot, plus an identical immutable `RecordEmitted` sequence when duplicate
  physical-view arrival is reversed, while allowing candidate and progress-event
  timing to differ;
- negative-only, unsupported and zero-completed-scan status semantics;
- incomplete prompt coverage, stale locators and source changes during scan;
- deterministic backfill in candidate rank order, including proof that stale,
  ambiguous, unavailable, changed and failed attempts consume
  `candidate_attempt_cap` but not `completed_scan_target`;
- pre-dedupe bounds, both routing caps, time budgets, exhausted evidence and
  source failures with the required stop reason;
- ADR 0004's global status precedence for planned bounds, external
  cancellation, recovered source failures, all-candidate failure, planner
  failure and sink truncation;
- prompt-only occurrence dedupe, distinct non-prompt normalized records and one
  global order-and-limit merge;
- `order="newest"` targeted continuation over one fixed progressive-search
  snapshot, including stale-cursor rejection and proof that continuation never
  reroutes or backfills;
- run-local or page-sequence non-linkable snapshot identifiers;
- the absence of an automatic exhaustive sweep; and
- equivalent routing summaries across CLI JSON/NDJSON, TUI and MCP sinks.

An implementation that adds semantic or global-recency evidence also proves
explicit activation only; unavailable policy/provider failure; absence of
implicit provisioning, index construction and remote calls; evidence
construction and reserved tier precedence; commutative reduction under shuffled
rows; sub-cap and coverage reporting; and that baseline `--deep` never activates
the extension. A semantic implementation specifically retains the maximum
score, newest support time and bytewise-smallest occurrence key under shuffled
evidence rows.

An exhaustive fixture sweep is the correctness oracle for final transcript
matches. Targeted tests measure selection recall separately: for each attempt
cap, completed target and routing policy, they compare which exhaustive-match
conversations entered the completed selected universe. A high observed recall
never upgrades the semantic status to complete.

Benchmarks compare normal prompt search, targeted deep search and exhaustive
deep search over the same sanitized fixture and representative local-store
shapes. They report latency distributions, routing evidence by variant,
ranked and attempted candidates, usable resolutions, completed scans, failures,
fallback activity, stop reason, records or bytes examined, exact hits and recall
at the declared attempt cap and completed target. Real local profiles guide
defaults because CI does not contain representative history stores; committed
artifacts contain no prompt text, raw query, native locator, private grouping
key, `RecordRef` value, stable store fingerprint or local path.

A default targeted policy is justified only when it materially reduces
transcript loads or bytes versus exhaustive search while providing useful
measured recall. If it does not, progressive search still provides correct
normal and exhaustive modes; this ADR does not authorize a misleading middle
tier.

## Planner and result types

Names describe intended internal boundaries. They do not become public Python
APIs until implemented and documented.

`RoutingSnapshot`
: Eligible conversation universe, run- or page-sequence-local non-linkable
  identifier, discovery generation, current indexed prompt observations,
  ephemeral private source-instance and conversation keys, snapshot-bound live
  source observations or cutoffs and bound locators for every eligible live
  conversation, and query-time source coverage. A targeted cursor privately
  binds this snapshot to the fixed work and completed selected universes.

`RoutingPlan`
: Original query digest, routing-policy version, safe universe predicates,
  ordered evidence tiers, optional explicitly selected extension contract,
  `completed_scan_target`, `candidate_attempt_cap`, `fallback_min_resolved`,
  per-fallback sub-caps, time budgets and required locator stability.

`RoutingWorkUniverse`
: The fixed, deterministically ranked candidates and reserves that may be
  resolved, scanned or admitted through backfill under one `RoutingSnapshot`.
  The collector's order barrier treats every still-admissible member as a
  possible input until the routing policy completes or rules it out.

`RoutingEvidence`
: A tagged union whose common fields are the private
  `corpus_conversation_key` and source observation. Its baseline variants are
  `PromptEvidence` (prompt occurrence, prompt tier, supported clauses, prompt
  score and support time), `ExplicitMetadataEvidence` (matched request
  constraints and activity time) and `CurrentProjectFallbackEvidence` (exact
  project identity and activity time). An implemented extension may add a
  versioned `SemanticPromptEvidence`, `GlobalRecencyFallbackEvidence` or
  successor variant; those are not minimum types. Routing evidence does not
  carry prompt text or raw metadata values into planner diagnostics.

`ConversationCandidate`
: Private `corpus_conversation_key`, aggregate routing evidence, deterministic
  rank key, source observation and private snapshot-relative locator. The key
  and locator may be persisted corpus evidence or ephemeral live evidence under
  the same adapter equality contract. Public identity fields are absent until
  their focused contract exists and are preserved only when supplied
  defensibly afterward; public result/drilldown projections use the separately
  owned `RecordRef` contract.

`CandidateAttempt`
: Candidate rank and attempt ordinal, private corpus key, locator resolution
  state, optional scan completion state, fallback provenance and privacy-safe
  failure code. It consumes one `candidate_attempt_cap` slot and contributes to
  `completed_scan_target` only after a complete CR-6 scan.

`ConversationScanResult`
: One attempted candidate's snapshot, normalized records, exact matches,
  completion state, counters and diagnostics. Only complete results enter the
  fixed selected universe; global order, dedupe and limit stay with the merge
  owner.

`RoutingReport`
: Policy/provider, coverage, tier, ranked/attempted/resolved/completed counts,
  both caps, resolved fallback floor and sub-caps, locator/scan failures,
  fallback activity, stop reason and approximation statistics plus the
  exhaustive next action.

The core request receives the progressive-search effort value. CLI flags, TUI
controls and MCP field spelling remain frontend concerns; they lower into the
same `RoutingPlan` and consume the same `RoutingReport`.

## Rejected alternatives

### Use only exact prompt hits

This is cheap but fails whenever the conversation contains the query only in a
response or the initiating prompt used different vocabulary. Exact prompt hits
remain the strongest prompt-evidence tier, not the entire router.

### Treat no prompt hit as proof of no conversation hit

Prompt text is not a complete index of conversation text. This would turn a
fast heuristic into a silent false-negative contract.

### Cap prompt occurrences before conversation dedupe

Long conversations and repeated prompts would consume the budget and crowd out
otherwise useful conversations. The user pays to open conversations, so the
attempt and completed-scan budgets count private corpus conversation keys.

### Count unresolved candidates toward the completed target

Stale or unavailable high-ranked locators would exhaust useful work before any
transcript is searched. They consume the attempt cap because resolution costs
work, but deterministic backfill—not false completion—preserves the target's
meaning.

### Resolve stale locators by best-effort similarity

Titles, paths, timestamps and repeated text can map an old prompt to the wrong
conversation. An unresolved locator is less harmful than confidently searching
and presenting evidence from the wrong conversation; the next ranked candidate
is the safe recovery path.

### Automatically sweep everything after a weak targeted run

This hides I/O and makes targeted latency unbounded. Explicit exhaustive
escalation keeps the UX predictable and the result status truthful.

### Reroute independently for every targeted page

New prompt evidence, changed fallback activation or a different resolved set
would make page two a different result universe from page one. A continuation
therefore reuses the fixed progressive-search snapshot and selected universe or
fails as stale.

### Call the run bounded instead of approximate

An attempt bound or heuristic ranking can remove a conversation that contains a
match. `bounded` describes an intentional page or result frontier; it must not
conceal a heuristic completeness loss.

### Make embeddings or an LLM router the baseline

Either may be an additive candidate tier only under an explicitly selected
named/versioned policy and provider with already provisioned local capability.
Installed capability and `--deep` alone do not activate it. Neither changes the
need for exact confirmation and an exhaustive escape hatch. The minimum
implementation defers this tier until the lexical baseline has measured recall
and cost.

### Make global recency a baseline fallback

The exact matcher prevents unrelated records from being emitted, but global
recency still spends transcript reads without query evidence and biases recall
toward recent conversations. The baseline stops after current-project fallback.
A future named and versioned policy may add global recency only through explicit
selection and its own measured sub-cap and coverage contract.

## Relationship to other ADRs

{ref}`ADR 0004 <adr-headless-query-planning-non-blocking-execution>` owns the
logical/physical planning layers, execution drivers, event stream, diagnostics
and run-status vocabulary. This ADR adds a targeted physical-plan shape and
requires that plan to report `approximate`.

{ref}`ADR 0014 <adr-result-order-limit-and-streaming-merge>` owns the one global
order, dedupe and result-limit stage. This ADR first fixes an explicitly
approximate routing work universe; ADR 0014 then applies across prompt and
completed-transcript streams while accounting for every queued or reserved
input that may still enter through deterministic backfill. Routing attempts are
source-work controls, not a second result frontier or proof that omitted
conversations cannot contain better-ranked results.

{ref}`adr-durable-prompt-corpus-derived-search-indexes` owns complete
prompt occurrences, private `corpus_source_instance_key` and
`corpus_conversation_key` contracts, defensible public identity preservation
after the focused identity contract exists, private locator stability, source
observations and current-indexed/live prompt coverage. This ADR consumes those
contracts and does not expand durable retention to full transcripts or define
public bookmark/export/similarity identity. The storage boundary exposes
persisted private grouping keys separately from public record metadata. Owning
adapters produce equality-parity ephemeral source and conversation keys,
snapshot-bound live observations or cutoffs and bound locators for every
eligible live conversation, including live-only and zero-prompt conversations;
routing does not persist or publish them.

{ref}`ADR 0006 <adr-public-cli-mcp-surface-contract>` and ADR 0004 own the
public `RecordRef` result/drilldown boundary. The private corpus key, any future
public identity field, routing snapshot and locator never substitute for it.
Public stable identity for bookmarks, exports and similarity remains a separate
dependency, including [#80](https://github.com/tony/agentgrep/issues/80); this
routing decision does not mint it.

{ref}`adr-progressive-deep-search` owns public search-effort
semantics, CLI/TUI/MCP discoverability, explicit exhaustive escalation and
compatibility treatment of existing scope controls. This ADR owns only the
targeted effort's candidate and confirmation machinery. Its targeted cursor
reuses the progressive-search decision's fixed request and routing snapshot
across a page sequence. An implementation that cannot preserve or validate that
snapshot returns no targeted cursor rather than rerouting silently.

## Consequences

Most deep searches can open a small, explainable set of conversations instead
of sweeping the entire local history. Current indexed and live prompt
occurrences become useful routing evidence as well as direct results. Ephemeral
private keys keep live-only and zero-prompt conversations eligible for metadata
and fallback routing, private keys deduplicate the work across persisted and
live evidence, and observation-bound conversation locators become a real
execution primitive rather than inert provenance. Failed attempts remain
bounded without consuming the completed-scan target, so stale evidence degrades
coverage visibly instead of silently wasting every useful slot.

The cost is an explicitly approximate middle tier with more planner state,
statistics and failure modes. Attempt and backfill policy need versioning.
Prompt coverage and locator freshness directly affect usefulness.
Fixed-snapshot pagination needs private continuation state and must fail stale
instead of rerouting. Users and callers must understand that an exactly matched
and ordered hit list from the selected universe is not a complete hit list
across all conversations. Later semantic or global-recency policies add provider,
model, threshold, timestamp, sub-cap, privacy and failure contracts; they are
not baseline costs.

That honesty improves both UX and DX. Normal search remains fast. Targeted deep
search is predictable and inspectable. Optional routing capability remains
deferred, explicitly selected and unable to provision itself. Exhaustive search
remains the explicit correctness oracle. Implementers can tune routing recall
and cost without changing final query semantics or hiding a transcript sweep
behind a friendly flag.

## Final position

Prompts are clues to conversations, not a complete index of them. agentgrep
will use current indexed and eligible live prompt evidence, explicit metadata,
current-project fallback, private corpus grouping keys and observation-bound
locators to make bounded, deterministic attempts with backfill toward a
completed-scan target. That lexical and explicit-metadata policy is the minimum
implementation. Semantic and global-recency evidence are deferred named,
versioned opt-ins, not baseline tiers. One fixed routing work universe bounds
attempts and backfill; complete scans define the final selected
conversation-source universe. Within it the original matcher and one collector
exclusively own matching, dedupe, final rank, order and result limit. Targeted
search remains globally approximate, reuses one fixed snapshot when it can page,
and leaves exhaustive search as an explicit escape hatch. A future complete
routed strategy requires its own
contract instead of silently upgrading `targeted`.
