(adr-headless-query-planning-non-blocking-execution)=

# ADR 0004: Headless query planning and non-blocking execution

## Status

Proposed.

## Context

CLI, TUI, MCP, and library callers need the same query behavior without making
one frontend the semantic owner. Source discovery, parsing, matching, and
ranking can be slow or blocking, while interactive and asynchronous frontends
must remain responsive and cancellable.

A threaded wrapper around a frontend-specific search function is not an
architecture. agentgrep needs a headless plan, typed lifecycle, transport-neutral
execution, and result contract that every frontend can consume.

## Decision

Search and discovery use this semantic pipeline:

1. A **normalized request** records the query, scope, effort, source selection,
   ordering, deduplication, and public response bound.
2. A **logical plan** states the eligible source universe, query semantics,
   required coverage, and whether any source selection is approximate.
3. A **physical plan** selects adapter operations, pushdowns, fallbacks,
   deterministic work bounds, and failure policy without changing the logical
   request.
4. An **execution driver** schedules source operations and returns typed
   outcomes through bounded delivery.
5. One **collector** owns final matching inputs, representative selection,
   deduplication, ordering, stable emission, and pagination.
6. **Event and result sinks** render or serialize the same lifecycle without
   redefining it.

The layers are semantic boundaries, not required Python class or module names.
Implementations may combine them when the ownership remains testable.

### Plans declare coverage and approximation

The logical plan declares every source class eligible under the normalized
request and the evidence needed for its completeness claim. Exact or exhaustive
plans omit work only with proof that the omission cannot affect the declared
result. A targeted or otherwise heuristic plan may deliberately select an
incomplete source universe, but it reports `approximate`.

The physical plan may push predicates down, use indexes, batch operations, or
apply deterministic work bounds. Unsupported pushdown falls back to canonical
matching or becomes a visible coverage outcome. A provider, transport, or
timing condition never silently changes the logical query or source policy.

Wall-clock deadlines terminate the shared operation as cancellation. Planned
approximation uses deterministic logical work bounds. Individual source or
provider timeouts remain typed operation outcomes under the declared failure
policy; they do not masquerade as caller cancellation.

### Execution transport is replaceable

The initial driver may use inline or thread execution. Equivalent process,
worker, native, asynchronous, or provider transports remain permitted under ADR
0003. All consume the same fixed plan and return typed source outcomes.

Scheduling and arrival order do not decide final membership, representative
selection, ordering, status, coverage, or deterministic work accounting.
Drivers may differ in progress timing and physical measurements. Cancellation
may shorten the result prefix, but every committed prefix remains valid under
the collector's contract.

Drivers use bounded queues or equivalent backpressure and propagate cooperative
cancellation to source work. A sink may stop accepting output without leaving
unbounded producers or hidden background work.

### One collector owns result semantics

The collector applies the canonical matcher to hydrated candidates, then owns
cross-source representative selection, deduplication, order, and response
pagination. A source-local count, routing budget, provider fetch size, or full
response page is not a substitute for that global stage.

Early stop or early emission requires evidence that omitted work cannot outrank
the retained frontier or replace the chosen representative of an emitted
deduplication class. ADR 0014 owns the detailed ordered-merge proof.

If adopted, {ref}`adr-progressive-deep-search` defines public `limit` as the
per-response cap. Until then, existing released surface meanings remain
compatibility facts. A future total cap across a cursor chain requires a
separate name and decision.

### Lifecycle status is public behavior

Machine-readable and interactive sinks expose one primary terminal status:

- `complete`: all eligible work that could affect the declared result was
  examined or safely omitted;
- `bounded`: a documented non-heuristic bound stopped otherwise eligible work;
- `truncated`: an output or transport budget prevented delivery of the intended
  response;
- `cancelled`: the caller or whole-request deadline stopped the run;
- `approximate`: an accepted heuristic can affect global completeness; or
- `failed`: an unrecovered error stopped the run.

When several conditions apply, primary precedence is `failed`, `cancelled`,
`truncated`, `approximate`, `bounded`, then `complete`. Lower-precedence
conditions remain available in structured details and coverage. A targeted
page therefore remains primarily `approximate` while still reporting its page
and work bounds.

A full page alone does not prove that another result exists. `bounded` due to a
response page requires a valid continuation or equivalent proof that another
canonical result exists. Exactly exhausted work is `complete` when no
higher-precedence condition applies. Cursor presence or absence never selects
status by itself.

Status values and precedence are compatibility-sensitive. Streaming sinks
finish with a lifecycle summary equivalent to collected JSON or MCP responses.

### Public results preserve lifecycle and drilldown

Structured results include the normalized request summary, emitted records,
status, privacy-safe diagnostics, coverage and counts, applied order and
response limit, and page metadata when continuation is supported. Exact field
spelling belongs to ADR 0006.

{class}`~agentgrep.RecordRef` is an opaque physical handle for drilling into an
emitted or representative record. It is not canonical content, occurrence,
thread, bookmark, export, similarity, or conversation identity, and it does not
imply a public conversation resolver. Callers must not reconstruct drilldown
from local paths or adapter offsets.

### Frontends remain thin and non-blocking

CLI, MCP, and TUI submit normalized requests and consume typed events or
results. They may group, highlight, redact, or truncate display, but they do not
re-run matching, reorder semantic results, or infer completeness.

TUI work leaves the Textual pump through either an ADR 0011 offload worker or
the execution driver, as appropriate. Pump-side application is bounded and
yielding; broad filtering, parsing, or rendering does not become permissible
merely because it is frontend work.

### Observability

Plans and events expose privacy-safe phase, source-class, count, duration,
queue, cancellation, and outcome information sufficient to diagnose latency
and coverage. They do not expose prompt text, secrets, raw command arguments,
private locators, or unredacted local paths.

## Relationships

- ADR 0003 owns native and worker boundary classification.
- ADR 0006 owns public CLI, MCP, and result-field spelling.
- ADR 0011 owns Textual pump safety.
- ADR 0014 owns global result order, representative selection, deduplication,
  stable emission, and cursor sequence.
- The proposed prompt-corpus, progressive-search, and routing ADRs may provide
  freshness and source-selection inputs; they do not replace this lifecycle or
  collector.

## Consequences

Every frontend can share one query engine and lifecycle while choosing an
appropriate delivery mechanism. Planning exposes approximation and coverage
before execution, and transport can evolve without changing results.

The architecture requires typed boundaries and coordinated cancellation. Some
optimizations must wait for proof before emitting results, and stable ordered
pagination requires more state than offset-over-rerun. Those costs are accepted
because frontend post-processing and arrival-dependent results are not reliable
public contracts.
