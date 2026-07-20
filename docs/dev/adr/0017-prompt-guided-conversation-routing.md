(adr-prompt-guided-conversation-routing)=

# ADR 0017: Prompt-guided conversation routing

## Status

Proposed.

## Context

{ref}`ADR 0015 <adr-durable-prompt-corpus-derived-search-indexes>` gives
agentgrep a durable corpus of complete human prompts, public pseudonymous refs
and private locators for their prompt occurrences and containing conversations.
{ref}`ADR 0016
<adr-progressive-deep-search>` makes that corpus the normal search surface and
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
as a seed. A prompt index may also be stale, a candidate cap may omit a
conversation, or a stored locator may no longer resolve against the observed
source generation. Exact matching inside the selected conversations cannot
repair a conversation that routing never selected.

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
It searches the durable prompt corpus and explicit metadata for evidence, adds
bounded fallback evidence when the declared candidate floor requires it,
groups all evidence by internal
conversation identity, resolves and validates only the selected conversations,
then applies the original query to their normalized transcript records.

The pipeline is:

```text
original query + eligible source universe
  -> prompt, explicit-metadata and bounded-fallback evidence
  -> candidates grouped by full internal conversation identity
  -> deterministic conversation cap
  -> generation-aware private-locator resolution
  -> original query over selected transcript records
  -> global merge, prompt-occurrence dedupe, order and limit
  -> coverage, approximation and next-action summary
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
record.

The routing and result-query digests, routing-policy version and applied
relaxation tiers travel on the physical plan and result summary. A frontend
does not reconstruct or reinterpret either query.

### CR-2 — Candidate selection begins from an explicit universe and snapshot

The planner first enumerates the conversation sources eligible under the
request's agent, store, privacy and explicit source-scope constraints. It
captures the discovery generation and the source observations represented by
the prompt corpus. This forms the `RoutingSnapshot`.

Only predicates proven to be conversation-invariant may remove a conversation
from this universe. Agent and store selection are examples. A record-time,
role, message-kind or other record-local predicate cannot be applied to prompt
metadata to exclude a conversation; the transcript matcher owns it.

The prompt stage uses every current, cheaply available prompt partition. A
source not represented by a verified prompt observation is a coverage gap, not
a negative result. Targeted routing does not scan every missing transcript
merely to construct its shortlist, because that would be an undisclosed
exhaustive search.

### CR-3 — Routing evidence is deterministic, ordered and explainable

The initial routing policy uses these evidence tiers, strongest first:

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
6. **Global-recency fallback**: recently active conversations from the eligible
   universe when that separately configured fallback is enabled.

Negative clauses never become positive clues. Phrase decomposition,
conjunction-to-disjunction relaxation and the documented query normalizations
are allowed because they broaden routing only. The baseline policy does not
use an LLM-generated query, embedding-only neighbor or nondeterministic
expansion. Future evidence types require a new routing-policy version and the
same disclosure and testing obligations.

Fallback activation is deterministic. The plan declares
`fallback_min_candidates`. Prompt and explicit-metadata evidence are grouped
first. When their distinct-conversation count is below that floor, the
current-project tier fills toward it up to its own sub-cap; if the count is
still below the floor and global recency is enabled, that tier fills the
remaining gap up to its separate sub-cap. Fallback does not depend on result
count, a confidence adjective, elapsed time or worker arrival order. Reaching
the floor stops fallback even when the global candidate cap has room. The plan
validates that the floor and sub-caps are non-negative and that the floor does
not exceed the global candidate cap; the global cap still applies after all
tiers are grouped.

The current-project fallback is eligible only when project identity comes from
an explicit request constraint or an exact frontend/project-catalog binding.
Path suffix similarity, prompt text and probabilistic repository inference are
not project resolution. Conversation activity time comes from snapshot-bound
conversation or source metadata under the routing policy's declared timestamp
contract. Results disclose the floor, whether each fallback ran, its cap, how
many conversations it contributed and whether its input metadata was
unavailable.

### CR-4 — Canonical conversations are deduplicated before bounding

Every evidence item carries the full internal `conversation_id` from ADR 0015.
The planner groups all evidence by that identity before applying a tier sub-cap
or the global conversation cap. Repeated prompts, many matching prompts in one
conversation, multiple indexed projections of one occurrence and overlapping
metadata/fallback evidence therefore consume one candidate slot.

`RoutingEvidence` has four evidence shapes:

- **Prompt evidence** carries the prompt occurrence identity, prompt tier,
  supported positive-clause set, prompt-match score, supporting occurrence
  time and source observation.
- **Explicit metadata evidence** carries the set of explicit request
  constraints matched by the conversation, snapshot-bound activity time and
  source observation.
- **Current-project fallback evidence** carries the exact current-project
  identity, snapshot-bound activity time and source observation.
- **Global-recency fallback evidence** carries snapshot-bound activity time and
  source observation.

Evidence aggregation uses only commutative reductions, so shuffled rows and
worker completion cannot change a candidate. The aggregate retains all
contributing evidence and ranks under its strongest tier; weaker tiers remain
explanation and coverage data. Within one prompt tier it unions supported
positive-clause sets, takes the maximum prompt-match score, takes the newest
supporting occurrence time with missing last, and retains the bytewise-smallest
internal prompt-occurrence identity as the evidence tie-break. Explicit
metadata evidence unions canonical constraint keys and takes the newest
activity time. Each fallback tier takes the newest activity time.

The deterministic candidate order is tier first, followed by a tier-local key:

- prompt tiers order by distinct supported positive clauses, greatest first;
  prompt-match score, greatest first; then supporting occurrence time, newest
  first with missing last; then the retained internal prompt-occurrence
  identity, bytewise ascending;
- explicit metadata orders by number of distinct matched request constraints,
  greatest first; the canonical sorted constraint-key set, bytewise ascending;
  then activity time, newest first with missing last;
- current-project and global-recency fallbacks order by activity time, newest
  first with missing last; and
- every tier ends with the full internal `conversation_id`, bytewise ascending.

The canonical constraint key is a request-local identifier over one canonical
compiled predicate. Neither that private key nor its raw constraint value
enters diagnostics or profiles. Public
`ConversationRef` encoding, private locator bytes, arrival order, worker
completion order and SQLite rowid never participate in grouping or ordering.

The candidate cap and each fallback sub-cap count internal conversation
identities after dedupe. Any lower-level row, time or memory bound that can stop
evidence collection before conversation dedupe is complete is an additional
approximation and appears in the routing report.

Scoring weights and tie-break behavior are compatibility-sensitive parts of
the routing-policy version. They may be tuned without changing final match
semantics, but a result must say which policy selected its candidates.

### CR-5 — Conversation locators are generation-aware resolution evidence

A conversation candidate carries its full internal `conversation_id`, source
identity, observed source generation, private observation-bound locator and
locator stability from ADR 0015. Resolution succeeds directly only against the
same current source observation. A public `ConversationRef` is a pseudonymous
result/drilldown handle; it is not resolution identity and does not participate
in candidate grouping or ranking.

When a source changed after the evidence was recorded, an adapter may reconcile
the locator only when native identity or an adapter-owned stable
anchor proves that the new locator identifies the same logical conversation.
Matching title, prompt text, path suffix, mtime or ordinal proximity is not
proof. Ambiguous, missing and unprovable mappings remain stale and are not
silently redirected.

The routing report distinguishes locators that were current, reconciled,
stale, ambiguous, unavailable and failed. A source that changes while its
transcript is being materialized invalidates that scan's snapshot; its results
are discarded unless the adapter can prove a consistent snapshot and the
diagnostic is propagated.

### CR-6 — Selected transcripts use the exhaustive semantic matcher

After locator resolution, the planner materializes only selected
conversations. Parent-linked sessions, mutation logs, SQLite parts and other
native shapes must produce the same normalized record stream they would
produce during exhaustive deep search. A cheaper targeted-only parser is not a
second semantic authority.

The original compiled query is evaluated against every relevant normalized
record in each selected conversation. Exact matching here means that selected
conversations produce no false final matches and no matcher-specific false
negatives. It says nothing about conversations omitted by routing.

Source-local transcript limits, parser omissions, unsupported record kinds and
timeouts must be represented as scan coverage gaps. They may not be hidden
under the fact that candidate selection was already approximate.

### CR-7 — Prompt and conversation results share one merge

Normal prompt results and targeted transcript results enter the global
single-owner merge from {ref}`ADR 0014
<adr-result-order-limit-and-streaming-merge>`. The declared order, dedupe and
limit apply once across both streams; the deep stage may not append an
independently truncated list after the prompt results.

When a transcript hit identifies the same human prompt occurrence already
returned from the prompt corpus, ADR 0015's canonical occurrence identity
collapses the duplicate. The durable prompt projection remains the canonical
prompt result, while the deep scan may contribute verified conversation
provenance and match evidence. Canonical cross-stage occurrence dedupe applies
only to human prompts. Assistant, reasoning and tool hits remain distinct
normalized records under the exhaustive matcher; this ADR does not define a
stable occurrence identity or cross-projection dedupe contract for them. A user
prompt found only in a changed live transcript is emitted once with its live
prompt occurrence identity and coverage state.

### CR-8 — Targeted search is always globally approximate

A targeted run reports `approximate` with reason
`heuristic_candidate_selection`. Exact matching inside candidates is not a
completeness proof, and the `targeted` effort does not promote itself to
`complete` based on query shape, index coverage or benchmark results.

In general, a routed strategy could justify completeness only with all of the
following:

- the eligible conversation universe is complete for one discovery snapshot;
- every eligible source observation is represented by the routing input;
- the routing predicate is proven to include every conversation that could
  satisfy `result_query`;
- no pre-dedupe, candidate, fallback, time or memory bound can remove such a
  conversation;
- every selected locator resolves against the proved snapshot; and
- all selected transcript scans complete under the exhaustive matcher.

Prompt-guided targeted routing in this ADR does not claim that proof, including
for a narrow user-prompt-only query over a complete ADR 0015 corpus. Any future
complete routed strategy needs a separate contract and effort semantics; it
cannot silently upgrade `targeted`.

Zero selected conversations therefore means *no candidate conversation was
selected under this routing plan*. It does not mean no conversation contains a
match. Candidate-cap saturation, fallback use and omitted source partitions
remain visible even when the selected transcripts produce zero exact hits.

Status follows ADR 0016's priority. A normally completed targeted run remains
`approximate`. Exhausting a planned evidence-collection, candidate, fallback,
routing-time or scan-time budget is a secondary approximation detail with its
cap, saturation and stopping stage; it does not replace the primary status
with `bounded`.

External timeout or user/client cancellation reports `cancelled`. A sink output
budget reports `truncated`. Recovered source-level failures preserve
`approximate` with incomplete coverage and source diagnostics. If selected
candidates exist but every candidate fails before one valid transcript scan
completes, the run is `failed`; an unrecovered planner or driver error is also
`failed`. Each more severe status retains the heuristic-selection and coverage
details so the routing omission risk remains visible.

### CR-9 — Unseeded, unsupported and failed routing stays visible

The planner handles weak or unavailable routing evidence as follows:

| Condition | Required behavior |
| --- | --- |
| Negative-only query | Do not invert prompt matches into candidates; use eligible metadata/fallback tiers if available and report `routing_query_unseeded` |
| No positive prompt-compatible text | Use explicit metadata/fallback tiers if available and report the missing lexical seed |
| Unsupported routing predicate | Do not use it to exclude candidates; preserve it for the final matcher and report `unsupported_routing_predicate` |
| Missing or stale prompt partition | Search current partitions, record the uncovered source observation and recommend exhaustive escalation |
| Pre-dedupe or candidate cap reached | Stop only at the declared bound and report the affected tier, cap and saturation |
| Planned routing- or scan-time budget reached | Preserve completed candidate results, keep primary status `approximate`, and report the stopping stage and unsearched counts when known |
| External timeout or user/client cancellation | Stop through the shared cancellation path, report `cancelled`, and retain approximation details |
| Locator stale or ambiguous | Reconcile only with proof; otherwise skip it and report the resolution state |
| Transcript read or parse failure | Continue independent candidates; retain a privacy-safe source diagnostic and incomplete coverage; report `failed` if selected candidates exist but none completes one valid transcript scan |
| Source changes during scan | Discard inconsistent source results and report `source_changed` |
| Unrecovered planner or driver error | Stop with `failed` and retain any safely reportable partial coverage |
| Sink output budget reached | Stop with `truncated`; do not relabel it as routing approximation alone |

A zero-candidate unseeded run is a valid approximate result with an exhaustive
next action. It is not `failed`, because the router completed its declared
heuristic plan without selecting work.

### CR-10 — Exhaustive escalation is explicit and never hidden

Targeted routing never starts an exhaustive sweep automatically because it
found no candidates, too few matches or low-confidence evidence. Automatic
fallback would make `--deep` latency and I/O unpredictable and would hide the
boundary the user selected in ADR 0016.

Every approximate targeted result provides the structured next action for the
equivalent exhaustive request. CLI and TUI may render that action as a hint or
control; JSON, NDJSON and MCP expose it as data. Reissuing the exhaustive
request is a new explicit user or caller decision.

### CR-11 — Routing is observable without exposing conversation content

The physical plan, final summary and privacy-safe profile expose at least:

- routing-policy version, run-local snapshot identifier and applied evidence
  tiers;
- prompt partitions current, live, stale, missing and unsupported;
- routing evidence seen by variant and evidence rows stopped by any pre-dedupe
  bound;
- distinct conversations before and after each fallback and the final cap;
- candidate cap, fallback minimum and sub-caps, saturation and stopping
  reasons;
- locators current, reconciled, stale, ambiguous, unavailable and failed;
- transcripts selected, opened, searched, skipped, changed and failed;
- normalized records examined, exact matches and duplicate prompt occurrences;
- per-stage elapsed time and coarse bytes or records read; and
- selection-completeness claim (`not_claimed` for targeted), approximation
  reasons and exhaustive next action.

The snapshot identifier exported in reports or profiles is generated for one
run and cannot be derived from source ids, paths, locators, native keys or
observation digests. It is not a stable fingerprint of a private store.

Profiles and diagnostics never contain an internal `conversation_id`, prompt
text, transcript text, raw query expansions, local absolute paths, native
locator values or public reference values. Public refs remain pseudonymous
result/drilldown handles, not secrets; private locators are the sensitive
resolution evidence and may become stale. Agent/store classifications, counts,
durations, policy versions and coarse byte totals are sufficient for planner
and benchmark analysis.

### CR-12 — Exactness, recall and cost are tested separately

The implementation requires focused contract tests for:

- deterministic routing under shuffled source and worker completion order;
- exact, conjunctive, disjunctive, metadata and fallback tier precedence;
- prompt, explicit-metadata, current-project and global-recency evidence
  construction and tier-local ordering;
- fallback activation and stopping at the declared distinct-conversation
  floor, independent of result count, confidence, timing and arrival order;
- commutative same-tier evidence reduction under shuffled evidence rows;
- full internal conversation-identity dedupe before every cap and stable
  internal-identity tie-breaks;
- proof that relaxed routing evidence never emits a non-matching result;
- parity between targeted and exhaustive matching inside the same candidate;
- negative-only, unsupported and zero-candidate status semantics;
- incomplete prompt coverage, stale locators and source changes during scan;
- pre-dedupe caps, candidate caps, time budgets and source failures;
- ADR 0016's status priority for planned bounds, external cancellation,
  recovered source failures, all-candidate failure, planner failure and sink
  truncation;
- prompt-only occurrence dedupe, distinct non-prompt normalized records and one
  global order-and-limit merge;
- exclusion of public reference encoding from grouping and ordering, and
  run-local non-linkable snapshot identifiers;
- the absence of an automatic exhaustive sweep; and
- equivalent routing summaries across CLI JSON/NDJSON, TUI and MCP sinks.

An exhaustive fixture sweep is the correctness oracle for final transcript
matches. Targeted tests measure candidate recall separately: for each cap and
routing policy, they compare which exhaustive-match conversations entered the
candidate set. A high observed recall never upgrades the semantic status to
complete.

Benchmarks compare normal prompt search, targeted deep search and exhaustive
deep search over the same sanitized fixture and representative local-store
shapes. They report latency distributions, routing evidence by variant,
canonical candidates, transcripts loaded, records or bytes examined, exact
hits and recall at the declared candidate cap. Real local profiles guide
defaults because CI does not contain representative history stores; committed
artifacts contain no prompt text, raw query, native locator, public reference
value, stable store fingerprint or local path.

A default targeted policy is justified only when it materially reduces
transcript loads or bytes versus exhaustive search while providing useful
measured recall. If it does not, ADR 0016 still provides correct normal and
exhaustive modes; this ADR does not authorize a misleading middle tier.

## Planner and result types

Names describe intended internal boundaries. They do not become public Python
APIs until implemented and documented.

`RoutingSnapshot`
: Eligible conversation universe, run-local non-linkable identifier, discovery
  generation, represented prompt observations and query-time source coverage.

`RoutingPlan`
: Original query digest, routing-policy version, safe universe predicates,
  ordered evidence tiers, candidate cap, fallback minimum, per-fallback
  sub-caps, budgets and required locator stability.

`RoutingEvidence`
: A tagged union whose common fields are the full internal `conversation_id`
  and source observation. Its variants are `PromptEvidence` (prompt occurrence,
  prompt tier, supported clauses, prompt score and support time),
  `ExplicitMetadataEvidence` (matched request constraints and activity time),
  `CurrentProjectFallbackEvidence` (exact project identity and activity time)
  and `GlobalRecencyFallbackEvidence` (activity time). It does not carry prompt
  text or raw metadata values into planner diagnostics.

`ConversationCandidate`
: Full internal `conversation_id`, aggregate routing evidence, deterministic
  rank key, source observation and private snapshot-relative locator. A public
  `ConversationRef` is added only to result/drilldown projections.

`ConversationScanResult`
: One resolved candidate's snapshot, normalized records, exact matches,
  counters and diagnostics. Global order and dedupe stay with the merge owner.

`RoutingReport`
: Policy, coverage, tier, cap, locator, scan and approximation statistics plus
  approximation reasons and the exhaustive next action.

The core request receives ADR 0016's search-effort value. CLI flags, TUI
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
semantic budget counts conversations.

### Resolve stale locators by best-effort similarity

Titles, paths, timestamps and repeated text can map an old prompt to the wrong
conversation. An unresolved locator is less harmful than confidently searching
and presenting evidence from the wrong conversation.

### Automatically sweep everything after a weak targeted run

This hides I/O and makes targeted latency unbounded. Explicit exhaustive
escalation keeps the UX predictable and the result status truthful.

### Call the run bounded instead of approximate

A candidate bound can remove a conversation that contains a match. `bounded`
describes an intentional page or result frontier; it must not conceal a
heuristic completeness loss.

### Make embeddings or an LLM router the baseline

Either may become an additive candidate tier after measurement, versioning and
privacy review. Neither is required for a deterministic, dependency-light
first implementation, and neither changes the need for exact confirmation and
an exhaustive escape hatch.

## Relationship to other ADRs

{ref}`ADR 0004 <adr-headless-query-planning-non-blocking-execution>` owns the
logical/physical planning layers, execution drivers, event stream, diagnostics
and run-status vocabulary. This ADR adds a targeted physical-plan shape and
requires that plan to report `approximate`.

{ref}`ADR 0014 <adr-result-order-limit-and-streaming-merge>` owns the one global
order, dedupe and limit stage. Candidate routing selects source work; it does
not create a second result-order frontier.

{ref}`ADR 0015 <adr-durable-prompt-corpus-derived-search-indexes>` owns complete
prompt occurrences, prompt occurrence identity, internal conversation identity,
public pseudonymous refs, private locator stability, source observations and
prompt-index coverage. This ADR consumes those contracts and does not expand
durable retention to full transcripts or define occurrence identity for
non-prompt records.

{ref}`ADR 0016 <adr-progressive-deep-search>` owns public search-effort
semantics, CLI/TUI/MCP discoverability, explicit exhaustive escalation and
compatibility treatment of existing scope controls. This ADR owns only the
targeted effort's candidate and confirmation machinery.

## Consequences

Most deep searches can open a small, explainable set of conversations instead
of sweeping the entire local history. Prompt occurrences become useful routing
evidence as well as direct results, and observation-bound conversation locators
become a real execution primitive rather than inert provenance.

The cost is an explicitly approximate middle tier with more planner state,
statistics and failure modes. Candidate policy needs versioning. Prompt index
coverage and locator freshness directly affect usefulness. Users and callers
must understand that an exact hit list from selected conversations is not a
complete hit list across all conversations.

That honesty improves both UX and DX. Normal search remains fast. Targeted deep
search is predictable and inspectable. Exhaustive search remains the explicit
correctness oracle. Implementers can tune routing recall and cost without
changing final query semantics or hiding a transcript sweep behind a friendly
flag.

## Final position

Prompts are clues to conversations, not a complete index of them. agentgrep
will use complete prompt occurrences, explicit metadata and observation-bound
locators to choose a bounded, deterministic conversation shortlist; it will
confirm every selected result with the original query; it will report the
shortlist as globally approximate; and it will leave exhaustive search as an
explicit, visible escape hatch rather than an automatic surprise. A future
complete routed strategy requires its own contract instead of silently
upgrading targeted search.
