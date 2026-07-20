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

{ref}`ADR 0015 <adr-durable-prompt-corpus-derived-search-indexes>` makes the
distinction durable: agentgrep retains complete prompt text and prompt-to-
conversation references without retaining every assistant response, reasoning
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
| `targeted` | Prompt corpus plus conversation bodies selected by {ref}`ADR 0017 <adr-prompt-guided-conversation-routing>` | Approximate globally; exact matching within selected conversations |
| `exhaustive` | Prompt corpus plus every eligible readable conversation body | Exact over the reported readable source coverage |

`prompt` is the default. Here, *exact* describes coverage and query semantics;
it does not force literal matching or replace the query language's case,
operator or ranking rules. Conversation-only content is searchable only when an
adapter admits it to the normalized transcript projection. Native content that
is unreadable, unsupported or deliberately excluded remains outside the
reported coverage at every effort level.

The engine, library and MCP use the enum. Both `search` and `grep` expose the
same progressive CLI shorthand:

| CLI request | Normalized effort |
| --- | --- |
| no breadth option and no legacy `conversations`/`all` scope | `prompt` |
| no breadth option with legacy `conversations`/`all` scope | `exhaustive` during the DS-2 compatibility window |
| `--deep` | `targeted` |
| `--deep --exhaustive` | `exhaustive` |

`--exhaustive` requires `--deep`. Boolean flags are a CLI convenience, not
parallel engine semantics. Structured JSON, NDJSON and MCP sinks always echo
`requested_effort`; a completed result also records the highest
`completed_effort` stage. Quiet human output omits the routine
`requested_effort=prompt` label and discloses non-default effort or an abnormal
effort/status combination instead.

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

Existing callers require a compatibility rule. Before this ADR, a scope of
`conversations` or `all` requested direct conversation search. During the
compatibility window, every public adapter—the CLI, library, MCP and serialized
request loader—preserves whether `effort` was omitted and normalizes as follows:

| Supplied effort | Supplied scope | Normalized effort and scope |
| --- | --- | --- |
| omitted | omitted | `prompt`, `prompts` with inferred-scope provenance |
| omitted | `prompts` | `prompt`, explicit `prompts` |
| omitted | `conversations` or `all` | `exhaustive`, preserving the explicit scope |
| explicit | any compatible scope | the explicit effort and supplied or effort-dependent default scope |

An explicit effort always wins, subject to combination validation. In
particular, explicit `--deep` opts a legacy conversation or all scope into
targeted behavior. Help and deprecation diagnostics may teach the new
vocabulary, but omission must never weaken an existing request silently.

This compatibility normalization belongs at every public-surface boundary in
{ref}`ADR 0006 <adr-public-cli-mcp-surface-contract>`. The core planner receives
concrete values plus the inferred-versus-explicit scope provenance needed to
construct a safe next action; it does not reinterpret omission.

### DS-3 — Escalation is always explicit

The planner never changes `prompt` to `targeted`, or `targeted` to
`exhaustive`, based on result count, candidate quality, stale references or
elapsed time. In particular, zero targeted candidates do not trigger a whole-
corpus sweep.

Instead, a completed result carries structured `next_actions`:

- prompt effort can offer a request patch for `effort=targeted`;
- targeted effort offers a request patch for `effort=exhaustive`; and
- exhaustive effort offers no broader search action.

Each action has a stable kind, target effort, privacy-safe reason and normalized
request patch. Applying it preserves the query, agents, field filters, order,
dedupe and compatible explicit output scope. When prompt scope was inferred,
the prompt-to-targeted action patches both `scope=all` and `effort=targeted`.
When the caller explicitly selected `scope=prompts`, agentgrep never broadens it
silently: it offers a distinct scope-broadening action that names the
`scope=all` patch and requires user or caller confirmation. Explicit
`conversations` and `all` scopes remain unchanged when compatible with the
target effort. A caller should not have to reconstruct a command string or
infer the next step from an empty result list.

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

Effort and run status are orthogonal. `targeted` is at least approximate, but a
`prompt` or `exhaustive` plan may also report `approximate` when ADR 0004 or ADR
0014 assumptions, such as mtime-derived recency, can change completeness. A
result/page limit produces `bounded` only when no higher-precedence condition
applies.

Exhaustive describes the plan, not a guarantee that an unreadable or failed
source became searchable. It reports `complete` only when every planned source
that could affect the requested set was examined or was provably excluded from
affecting the requested ordered and limited set under ADR 0014's stop rule.
Limits, output budgets, cancellation, source failures and catalog-only or
unsupported stores retain the run-status and coverage meanings defined by
{ref}`ADR 0004
<adr-headless-query-planning-non-blocking-execution>`.

The final result and streaming summary include at least:

- `requested_effort` and nullable `completed_effort`;
- prompt-corpus generation and covered-source counts;
- eligible, candidate, resolved, searched, skipped, stale and failed
  conversation counts;
- the configured and reached candidate budget;
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
4. perform the final dedupe, order and limit merge.

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

Human CLI output may show stage progress, but it does not irreversibly print
result rows before the global order barrier. Its final rows therefore have the
same order as JSON and MCP. NDJSON may expose stage events, but emitted result
records remain subject to the declared streaming-order contract.

The TUI is different only because its list is mutable. It may display prompt
hits and later conversation hits provisionally while deep work continues, but
the list must be visibly marked provisional and may reorder only until the
final engine merge arrives. The settled list, selected record and result count
must match the engine result.

### DS-7 — Deep search is discoverable without making the default noisy

The `search` and `grep` help surfaces name both escalation steps and state that
`--deep` is approximate. After a zero-result prompt search in an interactive
terminal, a concise stderr hint offers `--deep`. A targeted completion always
discloses approximation and offers exhaustive search; this is a correctness
notice, not optional flavor text. Piped output and machine formats receive no
unsolicited prose on stdout.

Successful `complete`, `bounded` and `approximate` CLI runs use exit status `0`
when they emitted a match and `1` when they did not. Status `1` after a targeted
miss means only that this approximate run emitted no match; it is not a
corpus-wide negative. Operational failure or truncation uses `2`. A direct CLI
interrupt uses `130`; a non-interrupt cancellation uses `2`. JSON and NDJSON
always include the structured run status and secondary conditions regardless of
the process exit status. These rules apply equally to `search` and `grep` and
preserve grep-shaped match/no-match automation without hiding coverage.

The TUI keeps a panel-visible **Deep search** action available during prompt
results. During targeted work it shows the active effort, a cancellable stage
label and delayed progress; after completion it exposes **Search all
conversations**. Empty states distinguish:

- no prompt matched;
- no candidate conversation was selected;
- selected conversations contained no match; and
- an exhaustive search found no match over its reported coverage.

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

## UX, DX and usefulness tradeoffs

| Choice | UX | DX | Usefulness |
| --- | --- | --- | --- |
| Prompt default | Predictable and fast; may require a second action | Simplest common plan and smallest index contract | Strong for remembered requests and pasted commands |
| Targeted `--deep` | Bounded latency with an explicit approximation notice | Requires routing stats, versioning and coverage tests | Finds conversation context near plausible prompt clues |
| Exhaustive deep | Slow but unsurprising when explicitly requested | Maintains a reference plan and broader source fixtures | Finds content with no useful prompt clue |

The staged design makes cost a user choice without making the user learn store
formats. It also keeps a trustworthy escape hatch: targeted search is useful
because it is cheap enough to invoke, while exhaustive search is useful because
it can disprove omissions caused by the heuristic.

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
  normalize the compatibility matrix identically;
- `search` and `grep` expose the same `--deep` and
  `--deep --exhaustive` help, normalization and output semantics;
- no-option prompt search excludes conversation-body work;
- `--deep` never starts an exhaustive task, including after zero candidates;
- legacy conversation and all scopes retain exhaustive semantics when no new
  effort option is present;
- inferred prompt scope broadens through an explicit next-action patch, while
  explicit prompt scope requires confirmation and compatible explicit scopes
  remain unchanged;
- targeted results report `approximate`, the heuristic reason, coverage counts
  and an exhaustive next action;
- the primary status follows the declared precedence, secondary conditions are
  retained and orthogonal prompt/exhaustive approximations remain visible;
- exhaustive results match a complete admitted readable-transcript fixture
  sweep, including every source not provably excluded by ADR 0014;
- cancelled and failed runs preserve requested/completed effort and
  approximation coverage;
- candidate budget, result limit and output budget remain distinct;
- CLI text, JSON, NDJSON and MCP preserve the engine's final order;
- `search` and `grep` map matches, misses, failures, truncation and interruption
  to the declared exit statuses in human and structured modes;
- structured sinks always echo effort, while quiet human prompt output omits
  the routine effort label;
- human hints never contaminate machine-readable stdout;
- TUI provisional results are labeled, obsolete generations are ignored and
  the final list matches the engine result; and
- progress and completion remain responsive under ADR 0011's watchdog and
  large-store checks.

## Relationship to other ADRs

{ref}`ADR 0004 <adr-headless-query-planning-non-blocking-execution>` owns the
request/plan/driver/event layers and the run-status, result, coverage and
diagnostic vocabulary. This ADR adds search effort, stage lifecycle and status
specialization for progressive search.

{ref}`ADR 0006 <adr-public-cli-mcp-surface-contract>` owns public flag, schema,
help and next-action consistency. This ADR fixes the semantics that surface
must expose. {ref}`ADR 0011 <adr-non-blocking-tui-invariants>` continues to own
the Textual pump boundary. {ref}`ADR 0014
<adr-result-order-limit-and-streaming-merge>` owns the final order, dedupe,
limit and streaming barrier across all stages.

{ref}`ADR 0015 <adr-durable-prompt-corpus-derived-search-indexes>` owns the
durable prompt corpus, prompt/conversation references and disposable derived
indexes. {ref}`ADR 0017 <adr-prompt-guided-conversation-routing>` owns candidate
generation, prompt-to-conversation routing, budgets, pointer resolution and the
targeted planner's heuristic version. Neither storage nor routing may redefine
the public effort levels in this ADR.

## Consequences

Normal search becomes faster and easier to reason about because it has a small,
explicit corpus. Deep search becomes discoverable without being automatic, and
the user can choose bounded approximation or exhaustive coverage. CLI, TUI and
MCP clients receive enough state to distinguish "no prompt", "no candidate",
"no selected-conversation match" and "no exhaustive match".

The cost is a larger compatibility surface: every request adapter and result
sink gains effort, coverage and next-action fields; legacy scopes need explicit
normalization; and TUI staging needs cancellation-safe provisional state. The
targeted path also requires continuing heuristic evaluation rather than a
one-time correctness test.

## Final position

Normal search searches prompts. `--deep` searches prompt-guided conversation
candidates and says that it is approximate. `--deep --exhaustive` searches all
eligible conversation bodies and reports the coverage it could actually read.
Scope selects results, effort selects work, escalation is explicit, and every
frontend exposes the same lifecycle and escape hatch.
