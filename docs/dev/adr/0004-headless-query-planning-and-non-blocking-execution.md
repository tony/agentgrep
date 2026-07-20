(adr-headless-query-planning-non-blocking-execution)=

# ADR 0004: Headless query planning and non-blocking execution

## Status

Proposed.

## Context

agentgrep is a headless search library with multiple frontends: CLI, Textual
TUI, and MCP. The same backend behavior must serve all of them.

Recent profiling showed two different bottleneck shapes:

- Prompt and find paths are often discovery-bound. Repeated store discovery,
  path enumeration, subprocess startup, and source-handle construction can
  dominate a query before record parsing begins.
- Conversation paths are often collection-bound. Large JSONL transcripts,
  recursive message extraction, SQLite reads, and record-level filtering can
  dominate the run after sources have been selected.

The current event-stream engine gives agentgrep a useful producer/consumer
boundary, but it is still a synchronous generator that frontends wrap in
threads. That is good enough for the CLI and many tests, but it is not the
long-term shape for a fully non-blocking TUI. Textual must never run broad
discovery, JSON parsing, SQLite reads, ranking, or slow rendering work on the
UI event loop. MCP tools also need a backend that can run from async tool
wrappers without blocking the server.

Prior systems point to the same direction:

- Dask keeps user intent as a logical expression tree and lowers it through
  optimizer phases before scheduling work. Its scheduler boundary separates
  graph execution from the concrete submit function:
  [expression planning](https://github.com/dask/dask/blob/a588170/dask/_expr.py)
  and [local async scheduling](https://github.com/dask/dask/blob/a588170/dask/local.py).
- DataFusion separates logical/physical planning from runtime state:
  [physical planner](https://github.com/apache/datafusion/blob/53.1.0/datafusion/core/src/physical_planner.rs),
  [execution plan](https://github.com/apache/datafusion/blob/53.1.0/datafusion/physical-plan/src/execution_plan.rs),
  and [session state](https://github.com/apache/datafusion/blob/53.1.0/datafusion/core/src/execution/session_state.rs).
- Tokio separates runtime construction from scheduler internals:
  [runtime builder](https://github.com/tokio-rs/tokio/blob/tokio-1.52.3/tokio/src/runtime/builder.rs)
  and [scheduler implementations](https://github.com/tokio-rs/tokio/tree/tokio-1.52.3/tokio/src/runtime/scheduler).
- Daft and Polars show Python-facing plan construction with separate physical
  and streaming execution:
  [Daft native runner](https://github.com/Eventual-Inc/Daft/blob/v0.7.14/daft/runners/native_runner.py),
  [Daft logical plan](https://github.com/Eventual-Inc/Daft/blob/v0.7.14/src/daft-logical-plan/src/logical_plan.rs),
  [Polars lazy frame](https://github.com/pola-rs/polars/blob/rs-0.53.0/crates/polars-lazy/src/frame/mod.rs),
  and [Polars physical stream graph](https://github.com/pola-rs/polars/blob/rs-0.53.0/crates/polars-stream/src/physical_plan/to_graph.rs).
- Ray and Flink make remote/distributed execution a driver concern behind
  stable plan and execution interfaces:
  [Ray Data streaming executor](https://github.com/ray-project/ray/blob/ray-2.55.1/python/ray/data/_internal/execution/streaming_executor.py),
  [Ray execution options](https://github.com/ray-project/ray/blob/ray-2.55.1/python/ray/data/_internal/execution/interfaces/execution_options.py),
  [Flink planner interface](https://github.com/apache/flink/blob/release-2.3.0-rc1/flink-table/flink-table-api-java/src/main/java/org/apache/flink/table/delegation/Planner.java),
  and [Flink pipeline executor factory](https://github.com/apache/flink/blob/release-2.3.0-rc1/flink-core/src/main/java/org/apache/flink/core/execution/PipelineExecutorFactory.java).
- ClickHouse and DuckDB split plans from executable pipelines and scheduler
  work:
  [ClickHouse QueryPlan](https://github.com/ClickHouse/ClickHouse/blob/v26.5.1.882-stable/src/Processors/QueryPlan/QueryPlan.h),
  [ClickHouse processors](https://github.com/ClickHouse/ClickHouse/blob/v26.5.1.882-stable/src/Processors/IProcessor.h),
  [DuckDB planner](https://github.com/duckdb/duckdb/blob/bbd990d554/src/planner/planner.cpp),
  [DuckDB physical plan generator](https://github.com/duckdb/duckdb/blob/bbd990d554/src/execution/physical_plan_generator.cpp),
  and [DuckDB task scheduler](https://github.com/duckdb/duckdb/blob/bbd990d554/src/parallel/task_scheduler.cpp).
- Hyperfine keeps benchmark execution and export as first-class surfaces:
  [benchmark types](https://github.com/sharkdp/hyperfine/tree/v1.20.0/src/benchmark)
  and [export formats](https://github.com/sharkdp/hyperfine/tree/v1.20.0/src/export).

agentgrep does not need to become any of those systems. The useful pattern is
the separation: user intent, logical plan, physical plan, execution driver,
result stream, and measurement are distinct responsibilities.

## Decision

agentgrep will evolve the search backend into a typed query planning and
execution system. The backend remains headless. CLI, TUI, and MCP are
frontends over the same library request, result, and event types.

The architecture has six layers:

1. **Query request**: immutable user intent, including terms, field predicates,
   scope, agents, limits, dedupe, ranking, and cancellation policy.
2. **Logical plan**: a normalized, frontend-neutral plan describing source
   roles, source predicates, record predicates, ordering, limits, dedupe,
   required capabilities, and whether source selection claims complete or
   approximate coverage.
3. **Planner/optimizer**: rewrites the logical plan into cheaper equivalent
   work, pushes source predicates into discovery, chooses direct lookup paths,
   and avoids version metadata or source construction that is not needed for
   the query.
4. **Physical plan**: an ordered set of source tasks with adapter strategies,
   cost hints, concurrency limits, output ordering rules, fallback rules, and
   the declared source-selection basis.
5. **Execution driver**: runs the physical plan using inline, threaded, async,
   or future worker-backed execution while preserving the same events and
   result semantics.
6. **Result sinks**: translate backend events into CLI Rich/text/JSON/NDJSON,
   TUI updates, and MCP response models.

The public surface is the event stream and result models, not the concrete
execution strategy. A query that runs inline for tests, threaded for a classic
CLI command, and async for the TUI must produce the same records, ordering,
dedupe semantics, errors, and privacy-safe profile observations.

## Interfaces

Names below describe the intended internal types and boundaries. They are not
all public APIs until implemented and documented.

`QueryRequest`
: Frozen user intent. It includes query text or a compiled query, target
  scope, selected agents, declared order, record limit, dedupe, ranking mode,
  search effort for query-to-record search, and a cancellation token.
  Frontends normalize these compatibility-sensitive values before planning.
  The request does not include output formatting.

`LogicalSearchPlan`
: Frontend-neutral work description. It contains source-role requirements,
  field predicates, text predicates, source predicates, record predicates,
  search effort, ordering, dedupe, limit semantics, and the complete-versus-
  approximate source-selection claim.

`AdapterCapability`
: Per-adapter declarations for cheap operations: metadata-only discovery,
  source predicate support, path prefiltering, raw text prefiltering, SQLite
  predicate pushdown, JSONL line prefiltering, streaming records, and
  source-level cost hints.

`PhysicalSearchPlan`
: Executable plan made of `SourceTask` items. Each task chooses one adapter
  strategy, declares whether it can stream records, records whether it emits
  newest-first records, records what source-order evidence can prove a safe
  frontier stop under the query limit, records scheduler-facing cost and
  source-group hints, and records how output order will be restored when work
  runs concurrently. The plan also
  records whether its task set is the complete eligible source universe, a
  provably equivalent pruned universe, or an explicitly approximate selected
  universe.

`ExecutionDriver`
: The scheduling boundary. The first required drivers are an inline
  deterministic driver for tests and a non-blocking async/thread-backed driver
  for CLI/TUI/MCP. Source-local scanning and driver scheduling are separate
  modules so future process or worker drivers can keep the same logical and
  physical plan types.

`SourceScanResult`
: The source-local execution boundary. A worker scans one `SourceTask` and
  returns locally sorted candidates, counters, and timing. The single-owner
  collector, coordinated by the driver, exclusively owns global dedupe,
  ordering, frontier proof, limit, and record emission so worker completion
  order cannot change search semantics.

`SourceScanBatch`
: The incremental source-local execution boundary. A source scan may yield
  matching candidates and counters in batches before the source is fully
  drained. `SourceScanResult` remains the compatibility wrapper that collects
  those batches for call sites that still need a whole-source result. Batch
  scheduling is an execution-driver choice, not a parser behavior change.

`LimitPolicy`
: The collector-owned proof for deciding whether an admitted, queued source can
  be skipped after enough candidates reach the owner-thread frontier. It may
  skip only when verified source metadata proves no unexamined record can
  outrank the current k-th record under the declared order. Approximate routing
  selects its work universe before this stage and never authorizes a frontier
  skip inside that universe.

`SearchEvent` / `FindEvent`
: The stream types. Existing events remain the baseline. Future events may
  add planning, warning, cancellation, or profile summaries only if old
  consumers can continue to ignore unknown event variants safely.

`ResultSink`
: Output adapters. Rich/text, JSON, NDJSON/streaming, TUI, and MCP sinks
  consume events. They do not discover stores, parse records, or decide search
  semantics.

`ProfileSink`
: Privacy-safe measurement. It records phase spans, adapter decisions,
  source-task counts, subprocess families, bytes/counts, cancellation, and
  output backpressure without prompt text, raw argv, or local absolute paths.

## Result types

The result types must answer the user-facing questions raised by
[#55](https://github.com/tony/agentgrep/issues/55): did the run finish, was it
bounded, is there another page, and how can a caller inspect one result without
guessing at source paths?

Record emission alone is not sufficient. Search and find collectors must
consume lifecycle events, counters, cancellation, warnings, and finish state,
then expose that state through the frontend result payload.

### Run status vocabulary

`RunStatus`
: The terminal run state exposed by JSON, NDJSON final summaries, MCP tool
  responses, and TUI completion chrome.

  `complete`
  : Every eligible source and batch that could affect the requested result set
    was examined, or the planner proved that each omitted source could not
    affect it. Finishing every source in a heuristically selected subset is not
    `complete`.

  `bounded`
  : The run intentionally stopped at a documented semantic bound, such as a
    requested result/page limit, source-local bounded scan, answer-now request,
    or configured result cap. A normal paginated response that emits a complete
    page and a usable `next_cursor` is `bounded`, not `truncated`. More records
    may exist outside the examined bound.

  `truncated`
  : The sink stopped emitting because of an output budget, byte budget, tool
    response budget, or client-imposed response limit before it could deliver
    the requested page/result payload. More matching records are known or
    likely to exist, and cursor continuation may be unavailable or unreliable.

  `cancelled`
  : The caller, terminal user, TUI, MCP client, timeout, or replacement search
    cancelled the run before normal completion.

  `approximate`
  : The run used an accepted approximation whose assumptions can affect
    completeness, such as heuristic source selection, mtime-as-recency, or
    bounded newest-first scanning across stores with shared dedupe keys.

  `failed`
  : The run stopped because of an unrecovered error. Partial results may be
    present only if the result payload marks them as partial and includes a
    diagnostic.

`RunStatus` values are compatibility-sensitive. Additive values require tests
for every sink that renders or serializes run status.

### Reachability today

Only `complete` and `bounded` have a producer. The rest are declared in the MCP
`RunStatusModel` literal but no code path constructs them, so a caller cannot
learn from the payload that a run was cut short, cancelled, approximated, or
failed. A state with no producer is a promise the payload cannot keep, so each
one below names the layer that owes it.

| State | Producer today | Owed by |
| --- | --- | --- |
| `complete` | `_page_status`, in the MCP search and discovery tool modules, when a page has no `next_cursor` | — |
| `bounded` | the same helpers, with `reason="page_limit"`, when a page has a `next_cursor` | — |
| `truncated` | none | the sinks that own an output budget: MCP response limits, and the JSON/NDJSON writers |
| `cancelled` | none | the execution driver's cancellation path (`SearchControl`), surfaced through the collectors |
| `approximate` | none | the planner, wherever heuristic source selection, mtime-as-recency, or a bounded newest-first scan can omit a result that would otherwise qualify (see {ref}`adr-result-order-limit-and-streaming-merge`) |
| `failed` | none | the collectors, on an unrecovered source or run error |

The CLI JSON and NDJSON payloads carry no run status at all yet; only the MCP
tool responses do.

### Result payload fields

`SearchResult` / `FindResult`
: The default machine-readable result types for JSON and MCP collection. A
  streaming NDJSON sink may emit events incrementally, but it must finish with
  an equivalent lifecycle summary.

Minimum result payload fields:

- `schema_version`: response schema version.
- `request`: normalized query/request summary, excluding private text that is
  not already part of the user's command input.
- `stats`: counts for sources discovered, eligible, searched, skipped,
  cancelled, records seen, matches seen, records emitted, dedupe drops, elapsed
  time, and the active limit/page size.
- `page`: page metadata with `limit`, emitted count, and opaque `next_cursor`
  when another page can be requested.
- `status`: `RunStatus` plus optional reason, source/budget that
  caused truncation, cancellation point, and approximation notes.
- `diagnostics`: privacy-safe warnings and errors, including unsupported
  pushdown, malformed stores, unavailable optional tools, timeout/cancellation,
  and source-level failures.
- `results`: emitted records in sink-specific record models.

`PageInfo`
: The pagination type. `next_cursor` is opaque, stable only for the
  documented cursor lifetime, and must carry enough planner/execution state to
  resume without callers reconstructing source paths. Absence of `next_cursor`
  means there is no supported next-page request for that result payload. Every
  cursor binds the normalized request, validated source snapshot and a
  collision-free last-emitted key under its owning order contract; record-result
  cursors follow ADR 0014. A cursor over an approximate selected universe must
  additionally preserve or validate that exact selection; it may not silently
  choose a new universe for the next page.

`Diagnostic`
: A privacy-safe warning or error record with a stable code, severity, message,
  optional source/store classifier, and optional remediation. Diagnostics must
  not include prompt text, raw argv, secret values, or local absolute paths.

`RecordRef`
: An opaque physical handle for result drilldown. It resolves an emitted record
  or source-scoped record position through a private representation chosen by
  agentgrep. It is not a canonical equality, grouping, bookmark, or export
  identity, and private repository keys do not become canonical public identity
  merely because the handle resolves through them. Callers use the handle with
  an inspect/drilldown operation
  instead of building tool calls from local file paths, adapter ids, or record
  offsets. A public conversation resolver requires a separate public-surface
  decision; a public thread identifier alone does not imply resolution. Source
  path, adapter id, and line or offset metadata may still appear as display or
  local debug metadata, but they are not the primary public drilldown input.

MCP, JSON, and NDJSON collectors must preserve these result fields by default.
Collecting only `RecordEmitted` events and discarding started, progress,
warning, cancellation, and finished events is not compliant with this ADR.

## Execution rules

Discovery must be planned. A query that can be answered from source metadata
must not construct record parsers. A request whose normalized scope and search
effort require only prompt evidence must not discover conversation-only stores.
A progressive deep-search request may use prompt evidence to select
conversation work according to its declared effort. A field predicate such as
`agent:grok` or `path:*session*` must prune before record parsing whenever the
adapter can prove the predicate from source metadata.

The planner must distinguish exact pruning from approximate selection. An exact
or exhaustive plan may omit a source only when source metadata or a verified
exact read-model generation proves predicate-complete coverage for that source
observation and proves the source cannot affect the result. Enrichment indexes,
semantic-routing evidence and heuristic scores never supply that proof. A
targeted plan may deliberately choose a smaller source universe, but it must
declare that selection as approximate and keep its routing budget separate from
the result limit. The collector then applies one order, dedupe, and limit
contract to every source the planner admitted.

Planning must choose the cheapest correct adapter strategy. `find` remains a
first-class fd/find-shaped source and storage discovery command; it may share
planner, driver, pagination, diagnostics, and result collection internals with
search, but it must not be replaced by a parallel source-listing API with
different semantics.

Planning strategies include:

- Direct metadata enumeration for `find`-shaped queries.
- SQLite predicates for stores whose schema can answer them safely.
- Path or source prefiltering before JSON/JSONL parsing.
- Raw text prefiltering only when it preserves parser semantics. Literal
  JSONL prefilters compare both raw and JSON-escaped query terms, while keeping
  Unicode-escaped lines conservative so decoded text matches are not lost.
  Haystack JSONL prefilters may only run for adapters whose per-record text,
  role, model, title, and source path are available without cross-record
  context; source-path matches are treated as static terms so path-only matches
  cannot be filtered before decoding.
- Bounded newest-first JSONL scans for limited append-only sources when record
  predicates do not require metadata that only appears earlier in the file.
- Lazy source admission for bounded text-surface append-only JSONL root
  sources. These sources can skip eager whole-root text prefiltering because
  raw JSONL line checks and newest-first execution are cheaper than a separate
  root scan in the bounded path. Haystack searches keep eager root
  prefiltering for broad content terms, but must admit sources whose source
  path satisfies at least one query term — regardless of limit or adapter —
  because a content-only root prefilter cannot prove those path matches
  impossible. Other unbounded, unknown-order, and non-JSONL root sources keep
  the eager prefilter path.
- Full Python parsing when the store format, query semantics, or privacy rules
  require it.

Optimizations interact with parser state along four axes: record order
(reverse scans), line visibility (raw skip predicates), file admission
(root and direct prefilters), and result reuse (the source scan cache
fingerprint). An adapter may join an optimization set only when every
emitted field is derivable from the record line plus the source path, or
when the optimization carries an explicit exemption — header markers
that bypass skip predicates and seed reverse scans, cache exemption for
adapters that expand sibling files, and unconditional admission for
stores whose searchable text is not greppable in place. The
`STATEFUL_HEADER_JSONL_ADAPTERS` set names the parsers that carry
leading-header state. Source ordering also assumes file mtime tracks
record recency; restored backups or clock skew can violate that, which
is accepted alongside the bounded-scan approximations below.

Execution must be cancellable and bounded. Drivers poll cancellation between
source tasks and record batches. A task that declares bounded source behavior
can stop before older records are parsed only when its source-order metadata
proves that no unseen record can outrank the collector frontier under the
declared order. A source-local candidate count alone is not that proof.
Cross-source dedupe that removes frontier records requires deeper admissible
work unless the result separately reports the approximation or stopping
condition that prevents it. Source scans compile query matchers once per task so
record loops do not rebuild term, regex, surface, or predicate state for each
candidate record. The frontier driver can run eligible source tasks
concurrently and the owner-thread collector merges their output. It stops
submitting a lower-priority admitted source only when `LimitPolicy` proves that
source cannot improve the global ordered frontier. The default frontier driver
consumes whole-source results because profiling showed
single-worker batch queueing was slower than the skip opportunity on local
Claude/Codex JSONL stores. Incremental `SourceScanBatch` scheduling remains
available behind driver configuration for experiments and future worker-count
tuning. Bounded text-surface JSONL tasks keep the inline driver by default when
profiling shows scheduler overhead is larger than skip opportunity; they may
opt into frontier execution when a configured worker count makes source-level
parallelism worthwhile. Profiling controls the default worker count because
local JSONL parsing is often CPU-bound enough that unbounded worker fan-out
hurts latency. Interactive CLI runs may map blank Enter to an answer-early
request. The TUI maps Esc/Ctrl-C and replacement searches to the same
cancellation path. MCP maps client cancellation or timeout to the same path
when the framework exposes it.

The TUI must remain non-blocking. It may receive events on the event loop, but
broad discovery, subprocess work, SQLite reads, JSON/JSONL parsing, ranking,
and large result filtering must run through the execution driver. Event
delivery uses bounded queues or backpressure so a fast parser cannot overwhelm
rendering.

CLI output modes are sinks:

- Rich/text progress for humans.
- JSON for complete machine-readable result payloads.
- NDJSON or equivalent streaming output for consumers that want events as they
  arrive.
- Optional answer-early behavior in interactive terminals.

MCP tools are sinks over the same event stream. A tool must collect lifecycle
events into result payloads that expose stats, page info, run status,
diagnostics, emitted records, and opaque drilldown handles by default. The
collection must happen through a non-blocking wrapper so the MCP server event
loop is not blocked by local store scans. MCP collectors must consume started,
progress, warning/cancellation when present, emitted-record, and finished
events; collecting only emitted records hides truncation and is not compliant
with this ADR.

## Observability and benchmarks

The planner and executor must be easy to profile. Each run can emit:

- query shape: scope, agent count, terms/predicate count, limit presence;
- discovery counts by agent, store, adapter, and path kind;
- planner decisions: predicates pushed down, exact or approximate source
  selection, sources eligible, admitted, pruned, or omitted, direct paths
  chosen, root prefilters skipped, fallback reasons;
- execution counts: sources started, submitted, completed, skipped, cancelled,
  batches yielded, records seen, matches seen, emitted records, dedupe drops,
  cancellation point;
- timing spans: discovery, planning, per-source execution, output sink
  backpressure, subprocess families;
- warning summaries: unsupported pushdown, malformed sources, unavailable
  optional tools.

Profiler and benchmark artifacts must keep their current privacy boundary:
no prompt text, no raw command argv, no secret values, and no local absolute
paths. They should keep `schema_version` and `artifact_kind` fields so future
CI or issue artifacts can be distinguished from local evidence.

Deterministic counters belong in CI tests. Wall-clock profiling remains local
evidence unless a fixture-only benchmark is explicitly designed for CI.

## Relationship to progressive search decisions

This ADR owns the shared request-plan-driver-event-result architecture. It does
not choose a storage backend, a default search effort, or a conversation-routing
heuristic. {ref}`ADR 0014 <adr-result-order-limit-and-streaming-merge>` owns the
collector's order, dedupe, and limit contract after the planner declares its
source universe. {ref}`adr-durable-prompt-corpus-derived-search-indexes` owns
durable prompt evidence and derived exact read models.
{ref}`adr-progressive-deep-search` owns
search-effort semantics and fixed-snapshot targeted pagination.
{ref}`adr-prompt-guided-conversation-routing` owns the explicitly approximate
selection of a conversation universe.

## Native boundary

This ADR does not approve native code.

The architecture deliberately creates a future native boundary that would fit
{ref}`adr-native-boundary-execution-architecture` if measurement ever proves
Python cannot resolve a user-visible bottleneck structurally. Any future native
work must cross at a plan, batch, buffer, or protocol boundary. It must not
cross per record, per JSON token, per callback, or per UI event.

The Python implementation remains the semantic source of truth. A native
accelerator for a public Python API must follow
{ref}`adr-pure-python-rust-accelerator-compatibility`; a native engine or
worker must follow {ref}`adr-native-boundary-execution-architecture`.

## Consequences

### Positive

- Frontends can improve independently without changing search semantics.
- The TUI can stay responsive during broad scans.
- Profiling identifies whether time is spent in discovery, planning,
  collection, output backpressure, or a specific adapter strategy.
- Planner tests can prove useless work is avoided without requiring large
  local history stores.
- Future source-level parallelism or worker execution has a typed place to
  attach.

### Tradeoffs

- The backend will carry more internal types than a direct scan loop.
- Adapters must describe capabilities honestly, not just expose parser
  functions.
- Deterministic ordering and dedupe need explicit merge rules once execution
  becomes concurrent. Those rules are settled in
  {ref}`adr-result-order-limit-and-streaming-merge`.
- Sinks must handle events incrementally instead of assuming a completed list.

### Risks

Planner overreach: an optimization could prune a source incorrectly. The
mitigation is a reference inline driver, fixture-backed equivalence tests, and
capability tests per adapter.

Concurrency nondeterminism: parallel source tasks can change output order. The
mitigation is explicit merge rules in the physical plan and tests that compare
inline and concurrent drivers.

Backpressure bugs: a streaming sink can either lag or block too much. The
mitigation is bounded queues, cancellation tests, and profile spans for sink
wait time.

Frontend leakage: CLI/TUI/MCP code can start making semantic decisions again.
The mitigation is a strict `ResultSink` boundary: formatting code consumes
events and never discovers stores or parses records.

Native shortcutting: future native work could bypass Python semantics. The
mitigation is ADR 0002, ADR 0003, and this ADR's plan/batch/protocol boundary.

## Final position

agentgrep's scalable shape is a typed, headless query system: discover, plan,
execute, observe, and render are separate responsibilities. The first
implementation target is still Python, but the structure must be ready for
non-blocking TUI execution, fast CLI streaming, MCP collection, richer
profiling, and future parallel or worker drivers without changing user-visible
search semantics.
