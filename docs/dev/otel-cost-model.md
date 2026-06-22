(otel-cost-model)=

# OpenTelemetry cost model

This page records the runtime and observability costs of agentgrep's local
OpenTelemetry workflow. It is for development and QA work against Grafana LGTM,
not packaged-user setup.

## Signal policy

`AGENTGREP_OTEL` is the only project environment switch that turns agentgrep
OpenTelemetry on. Endpoint configuration still uses the standard OTel and
backend variables, such as `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_EXPORTER_OTLP_TIMEOUT`, and `PYROSCOPE_SERVER_ADDRESS`.

When `AGENTGREP_OTEL` is enabled, traces, metrics, logs, and profiles should be
visible. Do not add another agentgrep-specific feature flag to hide one signal
for local QA. If the debug run has `AGENTGREP_DEBUG_SESSION_ID`, all four
signals must carry enough run identity to prove coverage in Grafana.

Packaged users stay quiet unless they explicitly enable telemetry. OTel SDK
setup, exporter failures, missing Docker, missing LGTM, and closed OTLP
endpoints must not change CLI, TUI, MCP, pytest, or profiler correctness.

## Subprocess inventory

`scripts/benchmark.py` has several subprocess paths:

- `hyperfine -N` runs each benchmark command for configured warmups and timed
  samples.
- The pure-Python fallback uses `subprocess.run()` once for each warmup and
  sample when `hyperfine` is unavailable or `--no-hyperfine` is set.
- `profile-engine-*` benchmark rows run one extra post-timing
  `scripts/profile_engine.py ... --json` capture for `profile_payload`.
- `profile-engine-cursor-ide-fixture-*` rows run the same profiler subprocess
  with `--fixture cursor-ide-state-vscdb`. The profiler creates one temporary
  Cursor `state.vscdb`, scans one synthetic record, and deletes the temporary
  home before returning. The payload records fixture kind/source/record counts,
  not the temporary path or fixture text.
- Cross-commit runs can execute `git checkout`, `git diff-index`,
  `uv sync --quiet`, and the configured probe command around each target ref.
- Benchmark runs emit `agentgrep.benchmark.run`,
  `agentgrep.benchmark.command`, and `agentgrep.benchmark.subprocess` spans
  when telemetry is enabled. Subprocess metrics record count and duration by
  subprocess kind and benchmark command, without raw argv or local paths.

`scripts/otel_acceptance.py` also runs subprocesses:

- `scripts/lgtm/generate_pyroscope_source_map.py` to write the ignored local
  `.tmp/lgtm/.pyroscope.yaml` source map used when validating Pyroscope source
  links. The generated mappings use repository- and package-relative prefixes,
  not local checkout or virtualenv paths.
- Docker inspect/start/run for the local `grafana/otel-lgtm` container.
  Container creation mounts `scripts/lgtm/grafana-datasources.yaml` and
  `scripts/lgtm/pyroscope-config.yaml`; unlabeled older containers are removed
  and recreated because Docker cannot add mounts to an existing container.
- `scripts/otel_smoke.py` to generate traces, metrics, logs, SQLite spans, and
  Pyroscope samples.
- a candidate-tagged short-lived CLI matrix:
  `python -m agentgrep --help`, `python -m agentgrep search ...`,
  `python -m agentgrep grep '['` for parse-error traces,
  `python -m agentgrep grep --invert-match ...`,
  `python -m agentgrep find codex --json`,
  `python -m agentgrep search --json ...` for a no-hit exit, and
  `python -m agentgrep ui --help`.
- `scripts/profile_engine.py grep-prompts ... --json` for profiler traces.
- `scripts/benchmark.py run ...` for benchmark harness roots, command spans,
  subprocess spans, and benchmark subprocess metrics.
- a short Python `-c` TUI smoke that fakes the blocking Textual app while
  exporting an `agentgrep.tui.session` root with lifecycle and shutdown child
  spans.
- a short Python `-c` MCP smoke that fakes `FastMCP.run()` while exporting an
  `agentgrep.mcp.server` root with lifecycle and flush child spans.
- one `python -m pytest ...` subprocess that runs
  `tests/test_agentgrep.py::test_streaming_ui_app_mounts_cleanly` for a traced
  direct Textual `run_test()` path and
  `tests/test_agentgrep_mcp.py::test_mcp_lists_tools_resources_prompts_and_templates`
  for FastMCP request spans under pytest item roots.

The pytest documentation harness also runs subprocesses:

- `git status`, local `git clone`, `git init`, and an empty sandbox commit
  prepare isolated project trees for documentation examples.
- Console and page-level Python examples run through `/bin/sh` inside a
  temporary home and redirected project checkout.
- Sphinx doctest recipes run through `just` in a temporary build directory.

Those documentation subprocesses emit
`agentgrep.pytest.documentation.subprocess` spans and count/duration metrics
when telemetry is active. Attributes identify subprocess kind, documentation
example kind, language, and outcome; they must not include raw shell scripts,
raw argv, environment values, prompt text, or local absolute paths.

The engine also records subprocess profile samples when
`agentgrep._engine.profiling` is active. Those samples must use command shape,
counts, return code, and duration. They must not contain raw argv, prompt text,
environment values, or local absolute paths.

Telemetry resource setup runs bounded read-only `git` commands with optional
locks disabled to resolve branch, revision, and repository identity. Each call
has a short timeout and exports only VCS semantic-convention attributes, not
raw command output.

agentgrep intentionally does not monkeypatch `subprocess.Popen` globally for
OTel. The subprocess call sites that matter for this local workflow live in
the benchmark, acceptance, engine profiling, and pytest documentation
harnesses, where explicit spans and metrics can keep command shape bounded and
privacy-safe. Adding global subprocess instrumentation would increase coverage
ambiguity without reducing the documented harness costs.

## Cost multipliers

Benchmark timings are multiplied by warmups, timed samples, command count, and
commit count. A `profile-engine-*` benchmark row has an extra profile capture
after timing; that capture explains span shape but is not part of `samples`.

Live OTel adds process-level setup and shutdown work. Each process configures
the SDK, installs live/debug auto-instrumentation, starts Pyroscope when
available, exports OTLP spans/logs/metrics, and flushes providers at shutdown.
The timeout is intentionally short so a missing collector does not break the
app.

The benchmark surface resolves `service.version` from the repo `pyproject.toml`
when the `agentgrep` dist is not installed in its isolated PEP 723 `uv` env,
instead of falling back to `0+unknown`; this adds at most one bounded file read
on the already-failing `PackageNotFoundError` path and only when a repo root is
known, so packaged users (no repo root, installed dist) pay nothing.

The MCP stdio entrypoint also emits an `agentgrep.mcp.server` root with
`agentgrep.mcp.server.lifecycle` and `agentgrep.mcp.flush` child spans. The
flush span calls the active telemetry handle with a 2 second timeout so a very
short stdio process can drain request and lifecycle spans before final provider
shutdown. The final shutdown still performs the last drain for the flush span
itself. Pyroscope profile samples still require enough runtime and CPU to be
sampled; the short MCP smoke proves traces/logs/metrics, not profile density.

Each observable MCP request opens an `mcp.server.request` root (and, for tool
calls, an `mcp.server.tool` child) that owns the trace, carries the redacted
`agentgrep_*` / `agentgrep_mcp_args.*` attributes, and emits the audit log. The
pinned `fastmcp` (3.x) auto-emits its own `SpanKind.SERVER` spans (`tools/list`,
`tools/call {name}`, etc.) carrying `mcp.method.name`, `gen_ai.tool.name`, and
`mcp.session.id`, gated only by a global OTel `TracerProvider` being present —
the same provider agentgrep installs. Those native SERVER spans nest directly
under the agentgrep roots, so agentgrep does not compose its own. When the caller
sends a `traceparent` in the request metadata, the request root inherits that
context so the caller's trace links to agentgrep's. Stock MCP clients (including
agentgrep's own CLI) do not inject one, so this inheritance fires only for a
traceparent-propagating caller and is otherwise exercised by the inbound-context
unit test. Raw arguments never reach the SERVER span (only the public tool name
does).

OTel log export keeps the plain log message as the record body and carries the
redacted `agentgrep_*` extras as native OTel log attributes (Loki structured
metadata), rather than re-encoding them into a JSON body. Sensitive extras are
still redacted to shape metadata before export, and the active span's trace and
span ids ride the OTel log record. This does not install console handlers or
change packaged-user output; trace-linked logs and their fields are queryable in
Loki without a query-stage JSON parse.

Passive local telemetry (a git checkout with `AGENTGREP_OTEL` unset) still
exports traces, metrics, and logs but skips Pyroscope and auto-instrumentation
so an in-repo `agentgrep --help` stays fast. Setting `AGENTGREP_OTEL`
explicitly (or passing an explicit mode) opts into all four signals, including
profiles. Asyncio auto-instrumentation therefore runs only for explicit
local/debug/live runs. SQLite spans
come solely from the project `sqlite3.Connection` shortcut factory, which wraps
the `Connection.execute` path agentgrep uses for source parsing;
`SQLite3Instrumentor` only covers the cursor path agentgrep never takes, so it
is not installed and cannot double-count. The same shortcut wrapper emits
`agentgrep.otel.sqlite_total` metrics from normal app paths when the SQLite
work belongs to an active app trace.

Engine scheduling and source scanning emit `agentgrep.otel.cpu_loops` metrics
for source counts, submitted/completed sources, batches, emitted records, and
records scanned. Both `agentgrep.otel.cpu_loops` and `agentgrep.otel.sqlite_total`
export as monotonic counters, not histograms. Engine CPU-loop metrics and
top-level search/find spans carry
`agentgrep_component=core` and `agentgrep_component_kind=in_process` so Grafana
can filter core cost without modeling fake CLI-to-core service edges. These
metrics document CPU-impacting work and cost centers; they are observability
signal, not a performance fix.

The Tempo datasource drills out to Prometheus (`tracesToMetricsV2`) and
Pyroscope (`tracesToProfilesV2`) so a span links to its RED metrics and its
flamegraph, alongside the existing exemplar (metric to trace) and
`tracesToLogsV2` (trace to log) links. Root-level profile linking works today:
`PyroscopeSpanProcessor` tags the thread that creates a root span, so the root
carries a `pyroscope.profile.id` attribute and the run's flamegraph is reachable
by its `span_name`. Per-span (child) filtering is a backend limitation, not an
agentgrep one: `grafana/otel-lgtm`'s bundled Pyroscope indexes `span_name` but
drops the high-cardinality `span_id`. The configured `tracesToProfilesV2` pivot
therefore joins by `service.name` (a working service-level flamegraph); a true
per-span `span_id` filter would need a Pyroscope ingestion change, not an
agentgrep one.

Inverted grep (`agentgrep grep --invert-match ...`) deliberately clears the
positive text terms it sends to the engine and then applies line-level
inversion after records are parsed. That makes the product surface correct for
records that contain no positive match, but it also means the candidate scan can
touch every record allowed by the agent/scope/source predicates. The dispatcher
emits `agentgrep.grep.candidate.count`, `agentgrep.grep.emitted.count`, and
`agentgrep.grep.duration` metrics plus one structured completion log so Grafana
can show the cost without logging raw patterns, argv, prompts, or paths.

Handled non-exception failures (such as a CLI parse error that exits non-zero)
set `StatusCode.ERROR` on the active span through `mark_span_error`, so Tempo's
error filter selects them even though no exception propagates. `--help` and
other zero-exit paths stay unset.

Each TUI launch emits `agentgrep.tui.lifecycle` and
`agentgrep.tui.shutdown` child spans under `agentgrep.tui.session`. The
lifecycle span covers app construction and `app.run()`, while shutdown records
the post-run cleanup boundary. That keeps blank or idle sessions non-root-only
without adding per-keypress, per-render, or per-record logging. Normal unmount
also asks Textual's worker manager to cancel active workers before timers are
disarmed, which is a shutdown-only control-plane cost. Pressing `q` in an empty
focused search/filter input emits one `agentgrep.tui.quit` child span and one
structured log before exiting; non-empty inputs still edit normally and do not
emit a keypress log.

Run-scoped metric labels increase local QA series count. That is accepted for
this branch because the goal is to close observability blindspots. The
per-attempt `agentgrep.debug.candidate_id` is the exception: it stays a
resource attribute on traces (so per-attempt drill-down survives via the
trace), but it is not stamped on every metric point, where it is pure series
fan-out. If the remaining series count becomes a project-threatening problem,
reduce cardinality in a follow-up with measurement rather than hiding metrics.

Explicitly instrumented pytest runs open one `agentgrep.pytest.session` root for
the run lifecycle and add low-cardinality xdist context to each
`agentgrep.pytest.test` span when xdist metadata is
present: worker id, whether xdist is active, and the distribution mode. Each
`agentgrep.pytest.test` is its own independent trace, not a child of the session
root. Default pytest remains offline and uninstrumented unless `AGENTGREP_OTEL`
is set. Each pytest worker is its own process and emits its own roots under the
shared debug session; agentgrep does not stitch cross-worker parent spans together.
Ordinary test subprocesses are not monkeypatched globally. They either run a
telemetry-enabled agentgrep entrypoint themselves, or they stay visible only as
part of the parent pytest item unless a focused harness such as the
documentation plugin wraps the subprocess call.

## Benchmark interpretation

Timing conclusions come from benchmark `samples`. Nested `profile_payload`
spans explain where time went in the profile capture, which is a separate
post-timing subprocess.

Known slow conversation benchmark shapes are expected to remain visible:

- `profile-engine-grep-all-conversations-query-max-count-500`
- `profile-engine-search-all-conversations-query-limit-500`

Those rows are dominated by conversation root scans across Claude
project/subagent JSONL and Codex session roots. This page documents that cost;
it does not prescribe a performance fix.

For query-language profiler runs, `scripts/profile_engine.py` also attaches a
safe strategy summary to the root span and emits one trace-linked structured
log. The summary uses query length, a short query hash, source counts,
strategy-group counts, `root_full_scan` counts, source ratios, record and
match counters, aggregate `root_full_scan` duration, and dominant
agent/store/adapter/strategy fields; it never records raw query text, prompt
text, argv, or local paths. These fields are computed from profile samples
already collected for the profiler payload, so they add one bounded
aggregation pass and no extra source reads. Benchmark analysis warns when a
query-language conversation profile payload contains `search.collect.source`
spans using `root_full_scan`, including source-span counts, record/match
counts, total duration, and the top contributing safe strategy label so the
blindspot remains visible in saved benchmark artifacts.

Cursor IDE benchmark rows now split real-local and fixture-backed coverage.
Real-local rows preserve workstation truth and can legitimately discover zero
sources. Fixture rows use a synthetic populated SQLite database so benchmark
and telemetry checks always exercise Cursor IDE parsing, SQLite spans, and
profile source labels. Analyzer warnings call out real-local zero-source rows
and point to the fixture selector for populated coverage.

## Acceptance evidence

Live acceptance must prove all four signals for the same debug session:

- Tempo has multi-span app roots for smoke, CLI, TUI, profile engine, and
  pytest.
- Tempo has benchmark run roots, benchmark command/subprocess spans, and MCP
  request spans for the debug session.
- Tempo has a short `agentgrep.mcp.server` root with lifecycle and flush child
  spans for the debug session.
- Tempo has one `agentgrep.cli.invocation` trace for each candidate-tagged
  short-lived CLI subprocess in the acceptance matrix.
- No current-run trace has exactly one span.
- No current-run trace has child spans whose parent span id is absent from the
  retrieved trace.
- At least one checked trace contains `agentgrep.sqlite.*` spans.
- The TUI trace contains `agentgrep.tui.lifecycle` and
  `agentgrep.tui.shutdown` child spans even when the acceptance app exits
  without running a search.
- Prometheus has fresh span, engine CPU-loop, SQLite, and benchmark
  subprocess metrics with `agentgrep_debug_session_id`; engine CPU-loop
  metrics must include the in-process core component labels.
- Loki has current-run agentgrep logs selected through OTLP structured metadata
  (no query-stage JSON parse) and no selected logs without trace and span
  identifiers.
- Loki log checks reject selected records whose structured metadata carries no
  trace and span ids, and streams missing the current VCS labels.
- Pyroscope exposes the `agentgrep` service and the debug session label.
- Pyroscope label checks are scoped by service, debug session, current VCS
  labels, `service_repository`, and exact `service_git_ref` so broad Grafana
  time windows do not mix older revisions with current-run evidence.

Future instrumentation changes that add subprocesses, benchmark rows,
exporters, auto-instrumentation, profile loops, or run-scoped metrics must
update this page with the cost and signal impact.
