(adr-progressive-deep-search)=

# ADR 0016: Progressive deep search

## Status

Proposed.

## Context

Searching a user's prompts and searching every message in every conversation are
different operations. Prompt search reads a compact, purpose-built corpus. A
conversation-body search may need to discover, open, decode and traverse large
JSONL files, mutable snapshots, SQLite rows and related sidecars. Treating both
as one default operation makes an ordinary lookup inherit the worst latency,
I/O and cancellation behavior of the deepest source.

{ref}`adr-durable-prompt-corpus-derived-search-indexes` makes the
distinction durable: agentgrep retains complete prompt text plus private prompt
and conversation locators without retaining every assistant response, reasoning
block, tool result or attachment. That prompt corpus can answer the common
question directly and can also supply clues for a deeper search. It cannot, by
itself, prove that a term absent from the prompts is absent from conversation
bodies.

The product therefore needs three different guarantees:

1. a fast default that searches the complete admitted prompt corpus;
2. a bounded deep search that uses prompt evidence to select likely
   conversations; and
3. an exhaustive escape hatch that examines every eligible conversation when
   completeness matters more than latency.

Those guarantees must mean the same thing in the library, CLI, TUI, MCP, JSON
and NDJSON. In particular, an empty targeted search is not evidence that no
conversation contains the query, and a frontend must not silently turn a
bounded request into an expensive sweep because its first stage found nothing.

## Decision

Nine invariants govern progressive search (DS for *deep search*).

### DS-1 — Search effort is a frontend-neutral request field

The normalized request gains one compatibility-sensitive `SearchEffort` enum:

| `effort` | Surfaces examined | Completeness contract |
| --- | --- | --- |
| `prompt` | Durable prompt corpus plus any correctness-preserving prompt fallback | Exact for the declared query over covered prompts |
| `targeted` | Prompt corpus plus conversation bodies selected by {ref}`adr-prompt-guided-conversation-routing` | Approximate globally; exact matching within selected conversations |
| `exhaustive` | Prompt corpus plus every eligible readable conversation body | Exact over the reported readable source coverage |

`prompt` is the default. Here, *exact* describes coverage and query semantics;
it does not force literal matching or replace the query language's case,
operator or ranking rules. Conversation-only content is searchable only when an
adapter admits it to the normalized transcript projection. Native content that
is unreadable, unsupported or deliberately excluded remains outside the
reported coverage at every effort level.

`SearchEffort` applies only to query-to-record search performed by `search` and
`grep`. It is not a general quality, cost or capability level. Similarity
retrieval, export, bookmarks, insights and enrichment, storage synchronization,
index management and model provisioning own separate controls and lifecycles.
If one of those features invokes search internally, it names the search effort
explicitly and reports that nested search's coverage; it does not derive effort
from its own format, tier or quality setting.

Increasing search effort authorizes additional reads from eligible search
sources only. It never authorizes a durable write, synchronization, index build,
model load or download, embedding generation, enrichment or expanded retention.

The engine, library and MCP use the enum. Both `search` and `grep` expose the
same progressive CLI shorthand:

| CLI request | Normalized effort |
| --- | --- |
| neither effort flag and no separate or inline scope that can admit conversations | `prompt` |
| neither effort flag with separate or inline scope that can admit conversations | `exhaustive` under the current request schema |
| `--deep` | `targeted` |
| `--exhaustive` | `exhaustive` |

`--deep` and `--exhaustive` are standalone, mutually exclusive effort
selectors. `--exhaustive` neither requires nor implies `--deep`; supplying both
is a usage error. With omitted scope, either flag receives its effort-dependent
`all` default. With explicit scope, the flag changes effort only and preserves
every compatible scope. Boolean flags are a CLI convenience, not parallel
engine semantics. Structured JSON, NDJSON and MCP sinks always echo
`requested_effort`; a completed result also records the highest
`completed_effort` stage. DS-7 governs the concise human disclosure.

### DS-2 — Scope filters results; effort controls work

`scope` and `effort` are separate axes. Scope determines which record kinds may
be emitted. Effort determines how broadly the planner may search to produce
them. The normalized request carries both rather than deriving engine cost from
a record-kind name.

For new requests, omitting scope selects `prompts` at `prompt` effort and `all`
at targeted or exhaustive effort. Explicit combinations that cannot produce a
selected record kind are validation errors, not silent rewrites. Public
adapters retain whether scope was omitted or explicit until normalization and
next-action construction have finished; the core plan still receives one
concrete scope.

The current request schema preserves existing callers permanently. Before this
ADR, a scope of `conversations` or `all` requested direct conversation search.
Every public adapter—the CLI, library, MCP and serialized request loader—keeps
whether `effort` was omitted and normalizes as follows:

| Supplied effort | Supplied scope | Normalized effort and scope |
| --- | --- | --- |
| omitted | omitted | `prompt`, `prompts` with inferred-scope provenance |
| omitted | `prompts` | `prompt`, explicit `prompts` |
| omitted | `conversations` or `all` | `exhaustive`, preserving the explicit scope |
| explicit | any compatible scope | the explicit effort and supplied or effort-dependent default scope |

Inline query-language `scope:` is the same compatibility-sensitive provenance,
not an ordinary record predicate for effort normalization. With omitted effort,
an inline scope expression proven to admit only prompts maps to `prompt`; one
that admits conversations maps to `exhaustive`. `scope:prompts`,
`scope:conversations` and `scope:all` therefore map exactly like their separate
scope-field forms. A compound or negated scope expression that cannot be proven
prompt-only normalizes conservatively to `exhaustive`. The existing validation
that rejects combining a separate scope field with inline `scope:` remains in
force, so normalization has one source of scope intent.

An explicit effort always wins, subject to combination validation. In
particular, explicit `--deep` opts a legacy conversation or all scope into
targeted behavior. This omission rule is permanent for the current request
schema. A future request schema may change it only through a separately
versioned compatibility decision; omission must never weaken a current-schema
request silently.

This normalization belongs at every public-surface boundary in
{ref}`ADR 0006 <adr-public-cli-mcp-surface-contract>`. The core planner receives
concrete values plus the inferred-versus-explicit scope provenance needed to
construct a safe next action; it does not reinterpret omission.

### DS-3 — Escalation is always explicit

The planner never changes `prompt` to `targeted`, `prompt` to `exhaustive` or
`targeted` to `exhaustive` based on result count, candidate quality, stale
references or elapsed time. In particular, zero targeted candidates do not
trigger a whole-corpus sweep.

Instead, deep-search completion uses two stable action kinds:

- `search.escalate_effort` carries a target effort and normalized request patch
  for prompt-to-targeted, prompt-to-exhaustive or targeted-to-exhaustive
  escalation; and
- `search.broaden_scope` carries the normalized scope patch required when an
  explicit prompt scope must become `all`, and requires user or caller
  confirmation.

Exhaustive effort offers no broader search action. Each deep-search action has a
privacy-safe reason. Applying its patch preserves the query, agents, field
filters, order, dedupe and compatible explicit output scope. When prompt scope
was inferred, `search.escalate_effort` patches both `scope=all` and
the target effort. When the caller explicitly selected `scope=prompts`,
agentgrep never broadens it silently; a `search.broaden_scope` patch used to
enter deep search names both `scope=all` and the requested effort. Explicit
`conversations` and `all` scopes remain unchanged when compatible with the
target effort.

Prompt completion offers targeted effort as the primary escalation and
exhaustive effort as the direct completeness escape hatch. Both are typed
request patches; neither requires a caller to perform the other first.

Pagination, inspection, query refinement and other grounded actions remain in
ADR 0006's general next-action vocabulary and need not carry a target effort.
Consumers ignore action kinds they do not implement while continuing to honor
run status and coverage. A caller should not have to reconstruct a command
string or infer the next step from an empty result list.

### DS-4 — Approximation and coverage are visible

A normally completed targeted run reports ADR 0004's `approximate` status with
reason `heuristic_candidate_selection`. Candidate selection may omit a
conversation even though matching inside every selected conversation uses the
original query exactly. A candidate budget or a zero-candidate outcome never
changes that status to `complete`.

One primary `RunStatus` is selected with this fixed precedence:
`failed` > `cancelled` > `truncated` > `approximate` > `bounded` > `complete`.
Every lower-precedence condition remains in structured status details,
diagnostics and coverage. A failed or cancelled targeted run therefore retains
`selection="heuristic"`, the approximation reason and the highest completed
effort so the omission risk is not lost. A successful targeted page remains
`approximate`; page and candidate bounds are reported by their dedicated
fields rather than disguising heuristic recall as ordinary pagination.

Result pagination belongs to the final collector, not to conversation routing.
A targeted cursor continues one fixed routing decision: requesting the next
page does not rerun routing against a newer corpus, widen the candidate budget
or select additional conversations. The candidate budget applies to the whole
cursor chain rather than to each page.

A targeted cursor is available only for `order="newest"`, as required by ADR
0014, and only when the engine can retain or validate the routing snapshot for
the cursor's documented lifetime. The opaque cursor carries or references the
normalized request, routing-policy version, routing snapshot, selected
conversation identities and the full collision-free last-emitted total-order
key required by ADR 0014. It exposes no private locator or native identifier.

If the routing snapshot can no longer be resumed, the engine rejects the cursor
with a structured `cursor_stale` diagnostic. It never silently reroutes and
presents a different candidate set as the next page. An implementation that
cannot retain or validate the snapshot returns no cursor. Effort escalation is
a new request and never inherits a targeted cursor.

Effort and run status are orthogonal. `targeted` is at least approximate, but a
`prompt` or `exhaustive` plan may also report `approximate` when ADR 0004 or ADR
0014 assumptions, such as mtime-derived recency, can change completeness. A
result/page limit produces `bounded` only when no higher-precedence condition
applies.

Exhaustive describes the plan, not a guarantee that an unreadable or failed
source became searchable. It reports `complete` only when every eligible
readable source that could affect the requested set was examined or was
provably excluded from affecting the requested ordered and limited set under
ADR 0014's stop rule.
Limits, output budgets, cancellation, source failures and catalog-only or
unsupported stores retain the run-status and coverage meanings defined by
{ref}`ADR 0004
<adr-headless-query-planning-non-blocking-execution>`.

The final result and streaming summary include at least:

- `requested_effort` and nullable `completed_effort`;
- nullable prompt-corpus generation, which is null when no corpus participated,
  and covered-source counts;
- eligible, ranked, attempted, usable-resolved, scan-completed, skipped, stale
  and failed conversation counts;
- the routing decision's `completed_scan_target`, `candidate_attempt_cap`,
  `fallback_min_resolved`, fallback activity and stop reason;
- source coverage and unavailable-store classifications;
- selection mode and heuristic contract version; and
- status, diagnostics and structured `next_actions`.

These are aggregate, privacy-safe fields. They do not expose prompt text,
private paths, native database keys or raw command arguments.

ADR 0004 records that `approximate` currently has no producer and that CLI JSON
and NDJSON do not yet carry run status. Targeted deep search cannot ship until
the planner produces the status and every structured sink serializes status,
coverage, effort and `next_actions`; a prose warning alone does not satisfy this
contract.

### DS-5 — Stages have explicit lifecycle and cancellation

Deep search is one request with observable stages, not a hidden chain of
frontend calls:

1. search the prompt corpus;
2. for targeted effort, plan and search selected conversations;
3. for exhaustive effort, plan and search all eligible conversations; and
4. merge dedupe, order and limit under the global total-order barrier.

These are logical dependencies and reporting boundaries, not a requirement to
finish every scan before merging. Targeted conversation planning waits for the
prompt evidence needed to fix the prompt-guided routing work universe. Source
scanning and the single-owner collector may then overlap. The collector may
emit a result prefix only when the total-order barrier proves that no admitted,
queued or deterministically reserved backfill input can produce a record ahead
of that prefix. It never emits according to source arrival or worker completion
order.
*Final merge* names the component that owns order, dedupe and limit; it does not
require collect-all execution when that proof is available.

Stage-started, progress and stage-finished events carry the request generation
and effort. Cancellation is polled between source tasks and record batches as
required by ADR 0004. Cancellation after prompt search but before conversation
search reports `completed_effort=prompt` and `status=cancelled`; it does not
return the prompt results as though the requested deep search completed.

A replacement TUI query cancels the old generation. Late events from that
generation cannot alter the new result set or completion state. Progress chrome
may be delayed briefly to avoid flashing for fast prompt searches, but terminal
completion is never delayed or inferred from a quiet event stream.

### DS-6 — One final merge owns order, dedupe and limit

Prompt and conversation matches enter the same collector. The collector owns
cross-stage dedupe, the declared order and the final result limit under
{ref}`ADR 0014 <adr-result-order-limit-and-streaming-merge>`. A candidate
conversation budget is planner work control; it is not the user's result
`limit`, and satisfying it cannot justify dropping a result that is already in
the final merge.

Human CLI output may show stage progress, but it does not irreversibly print a
result prefix until the total-order barrier proves that prefix final. Its final
rows therefore have the same order as JSON and MCP. NDJSON may expose stage
events, but emitted result records remain subject to the declared streaming-
order contract.

The TUI is different only because its list is mutable. It may display prompt
hits and later conversation hits provisionally while deep work continues, but
the list must be visibly marked provisional and may reorder only until the
final engine merge arrives. The settled list, selected record and result count
must match the engine result.

### DS-7 — Deep search is consistently discoverable without contaminating output

The `search` and `grep` help surfaces name both escalation steps and state that
`--deep` is approximate. Every prompt-effort completion in an interactive
terminal emits exactly this line once on stderr, whether or not it emitted a
match:

```text
Searched prompts only. Use --deep to search selected conversations, or --exhaustive to search all readable conversations.
```

It never appears on stdout, in JSON, NDJSON or MCP results, or when the
invocation is not interactive. A targeted completion always discloses
approximation and offers exhaustive search; this is a correctness notice, not
optional flavor text.

Successful `complete`, `bounded` and `approximate` CLI runs use exit status `0`
when they emitted a match and `1` when they did not. Status `1` after a targeted
miss means only that this approximate run emitted no match; it is not a
corpus-wide negative. Operational failure or truncation uses `2`. A direct CLI
interrupt uses `130`; a non-interrupt cancellation uses `2`. JSON and NDJSON
always include the structured run status and secondary conditions regardless of
the process exit status. These rules apply equally to `search` and `grep` and
preserve grep-shaped match/no-match automation without hiding coverage.

The TUI keeps a panel-visible **Deep search** action and a secondary **Search
all conversations** action available during prompt results. During targeted
work it shows the active effort, a cancellable stage label and delayed progress;
after completion it retains **Search all conversations**. Human CLI and TUI
output distinguish these terminal empty outcomes when the corresponding effort
is implemented:

- no prompt matched;
- no candidate conversation was selected;
- selected conversations contained no match; and
- an exhaustive search found no match over its reported coverage.

The prompt-plus-exhaustive slice implements the first and fourth outcomes. The
targeted slice implements the middle two before `--deep` is exposed. Once
effort and routing outcome are known, human output does not fall back to generic
"No matches found." or "No results."

MCP schemas expose `effort` directly. MCP responses include the same requested
and completed effort, status, coverage, diagnostics and request-patch actions
as JSON. Tool descriptions explain that targeted recall is heuristic so an
agent does not present a targeted miss as a corpus-wide negative.

All TUI discovery, routing, transcript I/O, parsing, matching and final ranking
remain off the Textual message pump under {ref}`ADR 0011
<adr-non-blocking-tui-invariants>`.

### DS-8 — The default optimizes the common question without hiding the costlier one

Users commonly remember what they asked, a command they pasted or the project
in which they asked it. Prompt search makes those lookups quick and stable.
Targeted deep search serves the next question—"what happened around that
prompt?"—without opening unrelated transcripts. Exhaustive search remains
available for terms that may exist only in an assistant response, reasoning
block, tool output or other conversation-only content admitted by an adapter's
normalized transcript projection.

For developers, one enum and one lifecycle avoid separate search engines per
frontend. Planner stages can be benchmarked independently, candidate quality
can change behind the versioned heuristic contract, and exhaustive fixture
sweeps remain a correctness oracle. The price is additional result vocabulary
and a deliberate approximation state; that cost is preferable to fast results
whose omissions are invisible.

### DS-9 — Performance claims require stage evidence

Benchmarks report prompt, candidate-planning, pointer-resolution,
conversation-scan and final-merge time separately. Targeted benchmarks include
candidate and transcript counts; exhaustive benchmarks include eligible and
searched source counts. Timing artifacts follow ADR 0004's privacy boundary.

No fixed candidate cap, progress-delay threshold or claim that targeted search
is faster is established without representative local measurements. UX
semantics are stable; tuning values are planner configuration governed by ADR
0017 and profiling evidence.

## Delivery sequence

These decisions land as thin, independently testable slices. A later slice must
not silently change the meaning of an already shipped flag.

1. **Correctness and status foundation.** Implement engine-owned status,
   coverage, diagnostics and next actions from ADRs 0004 and 0006, plus ADR
   0014's correct ordered-limit stopping. Measure the current accepted-count
   cutoff as a migration baseline, then choose full drain or a proof-bearing
   frontier as the default implementation.
2. **Prompt and exhaustive efforts.** Add `SearchEffort`, preserve prompt search
   as the default and expose standalone `--exhaustive` over the existing live
   conversation readers. Ship the prompt and exhaustive empty outcomes and
   record a baseline before exposing targeted search.
3. **Durable prompt read path.** Add the durable prompt corpus and default exact
   provider from {ref}`adr-durable-prompt-corpus-derived-search-indexes` while
   retaining correctness-preserving live fallback. Public identity may land in
   parallel; private occurrence and conversation keys cover its absence.
4. **Minimum targeted routing.** Implement the deterministic lexical,
   explicit-metadata and current-project policy from
   {ref}`adr-prompt-guided-conversation-routing`, then expose `--deep` with
   `approximate` status and its two targeted empty outcomes. Return no targeted
   cursor when the routing snapshot cannot be preserved or validated.
5. **Measured extensions.** Add stable targeted cursors, optional routing
   policies and alternate exact providers only after representative recall,
   latency and I/O measurements justify them.

ADR numbering remains provisional until integration order is known. It is
landing bookkeeping, not a runtime milestone.

## UX, DX and usefulness tradeoffs

| Choice | UX | DX | Usefulness |
| --- | --- | --- | --- |
| Prompt default | Predictable and fast; one interactive stderr line states its coverage and escalation paths | Simplest common plan and smallest index contract | Strong for remembered requests and pasted commands |
| Targeted `--deep` | Bounded latency with an explicit approximation notice; pagination stays within one fixed routing decision | Requires routing stats, versioning, resumable snapshots and coverage tests | Finds conversation context near plausible prompt clues |
| Standalone `--exhaustive` | Slow but unsurprising when explicitly requested | Maintains a reference plan and broader source fixtures | Finds content with no useful prompt clue |

The staged design makes cost a user choice without making the user learn store
formats. It also keeps a trustworthy escape hatch: targeted search is useful
because it is cheap enough to invoke, while exhaustive search is useful because
it can disprove omissions caused by the heuristic. The one-line interactive
prompt-search disclosure is a deliberate small cost: a result-bearing prompt
search still does not establish anything about conversation-only content.

## Prior art

[claude-history](https://github.com/raine/claude-history/blob/v0.1.68/src/agent/search.rs)
(tag `v0.1.68`) uses a bounded first-stage conversation shortlist, loads only
shortlisted transcripts and performs message-level retrieval in a second
stage. Its
[`retrieval` module](https://github.com/raine/claude-history/blob/v0.1.68/src/agent/retrieval.rs)
also returns focused read ranges instead of requiring every consumer to load an
entire transcript. It demonstrates the usefulness of progressive retrieval;
agentgrep adds an explicit approximation contract and exhaustive escape hatch
because its first stage is a prompt corpus spanning heterogeneous agents.

[fzf](https://github.com/junegunn/fzf/blob/v0.72.0/src/matcher.go) (tag
`v0.72.0`) attaches revisions, cancellation and progress to matcher requests,
then returns worker results through a
[`Merger`](https://github.com/junegunn/fzf/blob/v0.72.0/src/merger.go). The
relevant lesson is that interactive progress and replacement search belong to
the request lifecycle, while final result ordering belongs to the merger.

[ripgrep](https://github.com/BurntSushi/ripgrep/blob/15.1.0/README.md) (tag
`15.1.0`) defaults to a deliberately narrower, useful search by respecting
ignore rules and skipping hidden and binary files, while explicit flags broaden
the work. Its
[`--hidden` contract](https://github.com/BurntSushi/ripgrep/blob/15.1.0/crates/core/flags/defs.rs#L2768-L2813)
shows the value of naming a costly coverage expansion. agentgrep cannot copy
the implied completeness model: a targeted conversation shortlist can have
false negatives, so its approximation and exhaustive alternative must be
reported directly.

## Rejected alternatives

### Search every conversation by default

This gives the simplest completeness story but makes the common prompt lookup
pay for broad source discovery and transcript parsing. It increases cold-start
latency, output-order barriers and cancellation pressure, and it makes TUI
responsiveness depend on the largest private history store.

### Offer targeted search without exhaustive search

This keeps latency bounded but leaves no reliable recovery path when the prompt
router misses a conversation. Users and MCP agents would eventually treat an
approximate miss as authoritative because the product offers no stronger
operation.

### Automatically escalate after weak or empty results

This makes runtime depend on search contents: the same command can finish
quickly or sweep the whole machine without a visible decision. "Weak" is also a
ranking policy, not a stable authorization to perform more I/O. Structured
next actions preserve convenience while keeping cost explicit.

### Encode effort only through `scope`

`scope=conversations` describes which records are returned, not whether their
sources were chosen heuristically. Overloading it cannot represent targeted and
exhaustive conversation search independently, and changing its current meaning
would silently weaken existing commands.

### Require effort for existing explicit conversation scopes

Requiring existing `scope=conversations` or `scope=all` callers to add effort
would turn omission into a breaking behavior change even though the current
schema can preserve their exhaustive request unambiguously. That normalization
therefore remains permanent for the current schema. A future schema may choose
a different rule through an explicit versioned decision.

### Make exhaustive a modifier of deep

Requiring `--deep --exhaustive` makes `--deep` stop naming one stable effort and
adds redundant syntax to the completeness escape hatch. It also invites a
temporary exhaustive implementation of `--deep` that would later become
targeted and silently weaken saved commands. Standalone, mutually exclusive
selectors keep each command's completeness contract stable.

### Treat deep search as permission for other expensive capabilities

Using `--deep` to build an index, download or load a model, generate embeddings,
write durable state or retain more content would turn one visible read-breadth
choice into unrelated side effects. Those features keep their own explicit
controls; search effort authorizes eligible reads only.

### Create separate deep-search APIs per frontend

A CLI subcommand, TUI-only action and MCP-only tool would drift in defaults,
status and coverage. One request enum with frontend adapters gives each surface
appropriate ergonomics without multiplying semantics.

### Report targeted matches as complete because stage-two matching is exact

Exact matching inside selected conversations says nothing about conversations
the first stage omitted. This confuses precision with recall and is rejected.

## Test obligations

Implementation is not complete until fixtures prove:

- CLI, library, MCP and serialized requests preserve effort omission and
  normalize the permanent current-schema compatibility matrix identically;
- `search` and `grep` expose the same `--deep` and
  `--exhaustive` help, normalization and output semantics, permit
  `--exhaustive` without `--deep` and reject combining the flags;
- `SearchEffort` is confined to query-to-record search boundaries, and a
  feature that invokes search internally names its nested effort explicitly;
- no-option prompt search excludes conversation-body work, and increasing
  effort does not write durable state, start synchronization, build an index,
  load or download a model, generate embeddings, enrich or expand retention;
- `--deep` never starts an exhaustive task, including after zero candidates;
- standalone `--exhaustive` never invokes targeted routing, and prompt
  completion can patch directly to exhaustive effort;
- explicit current-schema conversation and all scopes retain exhaustive
  semantics permanently when effort is omitted, including equivalent inline
  query-language scopes;
- `search` and `grep` normalize inline `scope:prompts`,
  `scope:conversations` and `scope:all` identically to separate scope fields,
  and a compound inline scope that is not provably prompt-only defaults to
  exhaustive effort;
- inferred prompt scope broadens through `search.escalate_effort`, while
  explicit prompt scope requires a confirmed `search.broaden_scope` patch,
  compatible explicit scopes remain unchanged and unrelated ADR 0006 actions
  need no target effort;
- targeted results report `approximate`, the heuristic reason, coverage counts
  and an exhaustive next action;
- targeted pagination uses `order="newest"` and one fixed routing decision for
  the whole cursor chain, never widens the candidate budget or reruns routing,
  rejects an unresumable snapshot with `cursor_stale` and otherwise omits the
  cursor when snapshot retention or validation is unavailable;
- the primary status follows the declared precedence, secondary conditions are
  retained and orthogonal prompt/exhaustive approximations remain visible;
- exhaustive results match a complete admitted readable-transcript fixture
  sweep, including every source not provably excluded by ADR 0014;
- cancelled and failed runs preserve requested/completed effort and
  approximation coverage;
- candidate budget, result limit and output budget remain distinct;
- overlapping scan and merge stages emit only prefixes proved final by the
  total-order barrier, ignore worker arrival order and settle to the same list
  as non-overlapped execution;
- CLI text, JSON, NDJSON and MCP preserve the engine's proved prefixes and final
  order;
- `search` and `grep` map matches, misses, failures, truncation and interruption
  to the declared exit statuses in human and structured modes;
- every interactive prompt-effort completion emits the exact declared coverage
  line once on stderr regardless of match count, while non-interactive, JSON,
  NDJSON and MCP output omit it;
- each effort exposes its applicable terminal empty states before its public
  selector ships, without falling back to a generic empty message once status
  is known;
- human disclosures never contaminate machine-readable stdout;
- TUI provisional results are labeled, obsolete generations are ignored and
  the persistent deep-search action remains available while the final list
  matches the engine result; and
- progress and completion remain responsive under ADR 0011's watchdog and
  large-store checks.

## Relationship to other ADRs

{ref}`ADR 0004 <adr-headless-query-planning-non-blocking-execution>` owns the
request/plan/driver/event layers and the run-status, result, coverage and
diagnostic vocabulary, including opaque `PageInfo` cursors and their documented
lifetime. This ADR adds search effort, stage lifecycle, targeted-cursor
specialization and status specialization for progressive search.

{ref}`ADR 0006 <adr-public-cli-mcp-surface-contract>` owns public flag, schema,
help and next-action consistency. This ADR fixes the semantics that surface
must expose. {ref}`ADR 0011 <adr-non-blocking-tui-invariants>` continues to own
the Textual pump boundary. {ref}`ADR 0014
<adr-result-order-limit-and-streaming-merge>` owns the final order, dedupe,
limit, newest-only keyset pagination and streaming barrier across all stages.

{ref}`adr-durable-prompt-corpus-derived-search-indexes` owns the
durable prompt corpus, private occurrence/conversation keys and locators, and
disposable exact read models. {ref}`adr-prompt-guided-conversation-routing` owns
candidate generation,
prompt-to-conversation routing, budgets, private locator resolution and the
targeted planner's heuristic version. Neither storage nor routing may redefine
the public effort levels in this ADR. Other features may invoke a nested search
with an explicit effort, but they do not reuse search effort as their own
capability control.

## Consequences

Normal search becomes faster and easier to reason about because it has a small,
explicit corpus, and one interactive stderr line makes that prompt-only boundary
visible even when the run found matches. Deep search becomes discoverable
without being automatic, and the user can choose bounded approximation or
exhaustive coverage. CLI, TUI and MCP clients receive enough state to
distinguish "no prompt", "no candidate", "no selected-conversation match" and
"no exhaustive match".

The cost is a larger compatibility surface: every request adapter and result
sink gains effort, coverage and next-action fields; current-schema legacy scopes
need permanent omission-aware normalization; targeted cursors need resumable
routing snapshots; and TUI staging needs cancellation-safe provisional state.
The targeted path also requires continuing heuristic evaluation rather than a
one-time correctness test. Search effort stays deliberately narrower than the
controls for storage, similarity, export and enrichment.

## Final position

Normal search searches prompts. `--deep` searches prompt-guided conversation
candidates and says that it is approximate. `--exhaustive` searches all
eligible conversation bodies and reports the coverage it could actually read.
Interactive prompt search says once that it searched prompts only. Scope selects
results, while effort authorizes only additional eligible search reads; it does
not authorize writes, indexes, models, enrichment or retention. Current-schema
conversation and all scopes with omitted effort remain exhaustive permanently.
Stages may overlap, but only total-order-safe prefixes are emitted. A targeted
cursor pages one fixed routing decision or fails stale without rerouting.
Escalation is explicit, and every frontend exposes the same lifecycle and escape
hatch.
