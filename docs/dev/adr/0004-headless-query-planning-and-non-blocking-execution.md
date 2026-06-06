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
  optimizer phases before scheduling work. Its scheduler contract separates
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
result stream, and measurement are distinct contracts.

## Decision

agentgrep will evolve the search backend into a typed query planning and
execution system. The backend remains headless. CLI, TUI, and MCP are
frontends over the same library contracts.

The architecture has six layers:

1. **Query request**: immutable user intent, including terms, field predicates,
   scope, agents, limits, dedupe, ranking, and cancellation policy.
2. **Logical plan**: a normalized, frontend-neutral plan describing source
   roles, source predicates, record predicates, ordering, limits, dedupe, and
   required capabilities.
3. **Planner/optimizer**: rewrites the logical plan into cheaper equivalent
   work, pushes source predicates into discovery, chooses direct lookup paths,
   and avoids version metadata or source construction that is not needed for
   the query.
4. **Physical plan**: an ordered set of source tasks with adapter strategies,
   cost hints, concurrency limits, output ordering rules, and fallback rules.
5. **Execution driver**: runs the physical plan using inline, threaded, async,
   or future worker-backed execution while preserving the same events and
   result semantics.
6. **Result sinks**: translate backend events into CLI Rich/text/JSON/NDJSON,
   TUI updates, and MCP response models.

The public contract is the event stream and result models, not the concrete
execution strategy. A query that runs inline for tests, threaded for a classic
CLI command, and async for the TUI must produce the same records, ordering,
dedupe semantics, errors, and privacy-safe profile observations.

## Interfaces

Names below describe the intended internal contracts. They are not all public
APIs until implemented and documented.

`QueryRequest`
: Frozen user intent. It includes query text or a compiled query, target
  scope, selected agents, record limit, dedupe, ranking mode, and a
  cancellation token. It does not include output formatting.

`LogicalSearchPlan`
: Frontend-neutral work description. It contains source-role requirements,
  field predicates, text predicates, source predicates, record predicates,
  ordering, dedupe, and limit semantics.

`AdapterCapability`
: Per-adapter declarations for cheap operations: metadata-only discovery,
  source predicate support, path prefiltering, raw text prefiltering, SQLite
  predicate pushdown, JSONL line prefiltering, streaming records, and
  source-level cost hints.

`PhysicalSearchPlan`
: Executable plan made of `SourceTask` items. Each task chooses one adapter
  strategy, declares whether it can stream records, records whether it emits
  newest-first records, records whether it may stop after satisfying the query
  limit, and records how output order will be restored when work runs
  concurrently.

`ExecutionDriver`
: The scheduling boundary. The first required drivers are an inline
  deterministic driver for tests and a non-blocking async/thread-backed driver
  for CLI/TUI/MCP. Future process or worker drivers must keep the same logical
  and physical plan contracts.

`SourceScanResult`
: The source-local execution boundary. A worker scans one `SourceTask` and
  returns candidates, counters, and timing. Global dedupe, top-K ordering,
  frontier pruning, and record emission stay with the driver so worker
  completion order cannot change search semantics.

`SearchEvent` / `FindEvent`
: The stream contract. Existing events remain the baseline. Future events may
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

## Execution rules

Discovery must be planned. A query that can be answered from source metadata
must not construct record parsers. A prompt-only query must not discover
conversation-only stores unless the requested scope requires them. A field
predicate such as `agent:grok` or `path:*session*` must prune before record
parsing whenever the adapter can prove the predicate from source metadata.

Planning must choose the cheapest correct adapter strategy:

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
- Full Python parsing when the store format, query semantics, or privacy rules
  require it.

Execution must be cancellable and bounded. Drivers poll cancellation between
source tasks and record batches. A task that declares bounded source behavior
can stop before older records are parsed once the source-local candidate limit
is satisfied. Source scans compile query matchers once per task so record
loops do not rebuild term, regex, surface, or predicate state for each
candidate record. The frontier driver can run eligible source tasks
concurrently, merges candidates on the owner thread, and stops submitting
lower-priority bounded sources once the global result limit is filled.
Profiling controls the default worker count because local JSONL parsing is
often CPU-bound enough that unbounded worker fan-out hurts latency. Interactive
CLI runs may map blank Enter to an answer-early request. The TUI maps
Esc/Ctrl-C and replacement searches to the same cancellation contract. MCP maps
client cancellation or timeout to the same contract when the framework exposes
it.

The TUI must remain non-blocking. It may receive events on the event loop, but
broad discovery, subprocess work, SQLite reads, JSON/JSONL parsing, ranking,
and large result filtering must run through the execution driver. Event
delivery uses bounded queues or backpressure so a fast parser cannot overwhelm
rendering.

CLI output modes are sinks:

- Rich/text progress for humans.
- JSON for complete machine-readable envelopes.
- NDJSON or equivalent streaming output for consumers that want events as they
  arrive.
- Optional answer-early behavior in interactive terminals.

MCP tools are sinks over the same event stream. A tool may collect events into
the existing response models, but the collection must happen through a
non-blocking wrapper so the MCP server event loop is not blocked by local
store scans.

## Observability and benchmarks

The planner and executor must be easy to profile. Each run can emit:

- query shape: scope, agent count, terms/predicate count, limit presence;
- discovery counts by agent, store, adapter, and path kind;
- planner decisions: predicates pushed down, sources pruned, direct paths
  chosen, fallback reasons;
- execution counts: sources started, submitted, completed, skipped, records
  seen, matches seen, emitted records, dedupe drops, cancellation point;
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
  becomes concurrent.
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
execute, observe, and render are separate contracts. The first implementation
target is still Python, but the structure must be ready for non-blocking TUI
execution, fast CLI streaming, MCP collection, richer profiling, and future
parallel or worker drivers without changing user-visible search semantics.
