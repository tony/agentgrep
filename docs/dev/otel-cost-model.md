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
- Cross-commit runs can execute `git checkout`, `git diff-index`,
  `uv sync --quiet`, and the configured probe command around each target ref.
- Benchmark runs emit `agentgrep.benchmark.run`,
  `agentgrep.benchmark.command`, and `agentgrep.benchmark.subprocess` spans
  when telemetry is enabled. Subprocess metrics record count and duration by
  subprocess kind and benchmark command, without raw argv or local paths.

`scripts/otel_acceptance.py` also runs subprocesses:

- `scripts/lgtm/generate_pyroscope_source_map.py` to write the ignored local
  `.tmp/lgtm/.pyroscope.yaml` source map used when validating Pyroscope source
  links.
- Docker inspect/start/run for the local `grafana/otel-lgtm` container.
  Container creation mounts `scripts/lgtm/grafana-datasources.yaml` and
  `scripts/lgtm/pyroscope-config.yaml`; unlabeled older containers are removed
  and recreated because Docker cannot add mounts to an existing container.
- `scripts/otel_smoke.py` to generate traces, metrics, logs, SQLite spans, and
  Pyroscope samples.
- `python -m agentgrep --help` for traced help output.
- `python -m agentgrep grep --invert-match --only-matching ...` for a traced
  parse-error path.
- `python -m agentgrep grep --invert-match ...` for a traced successful
  inverted-output path.
- `python -m agentgrep search ...` for a traced app CLI search.
- `scripts/profile_engine.py grep-prompts ... --json` for profiler traces.
- `scripts/benchmark.py run ...` for benchmark harness roots, command spans,
  subprocess spans, and benchmark subprocess metrics.
- a short Python `-c` TUI smoke that fakes the blocking Textual app while
  exporting an `agentgrep.tui.session` root and child TUI span.
- `python -m pytest tests/test_agentgrep.py::test_streaming_ui_app_mounts_cleanly`
  for a traced direct Textual `run_test()` path.
- `python -m pytest
  tests/test_agentgrep_mcp.py::test_mcp_lists_tools_resources_prompts_and_templates`
  for FastMCP request spans under a pytest item root.

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

## Cost multipliers

Benchmark timings are multiplied by warmups, timed samples, command count, and
commit count. A `profile-engine-*` benchmark row has an extra profile capture
after timing; that capture explains span shape but is not part of `samples`.

Live OTel adds process-level setup and shutdown work. Each process configures
the SDK, installs live/debug auto-instrumentation, starts Pyroscope when
available, exports OTLP spans/logs/metrics, and flushes providers at shutdown.
The timeout is intentionally short so a missing collector does not break the
app.

SQLite and asyncio auto-instrumentation run only in local/debug/live modes.
Project SQLite helper spans also wrap `sqlite3.Connection` shortcut methods so
source-parser database work remains visible. The same shortcut wrapper emits
`agentgrep.otel.sqlite_total` metrics from normal app paths when the SQLite
work belongs to an active app trace.

Engine scheduling and source scanning emit `agentgrep.otel.cpu_loops` metrics
for source counts, submitted/completed sources, batches, emitted records, and
records scanned. These metrics document CPU-impacting work and cost centers;
they are observability signal, not a performance fix.

Run-scoped metric labels increase local QA series count. That is accepted for
this branch because the goal is to close observability blindspots. If the
series count becomes a project-threatening problem, reduce cardinality in a
follow-up with measurement rather than hiding metrics in this PR.

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
strategy-group counts, `root_full_scan` counts, and dominant strategy fields;
it never records raw query text, prompt text, argv, or local paths. Benchmark
analysis warns when a query-language conversation profile payload contains
`search.collect.source` spans using `root_full_scan`, so the blindspot remains
visible in saved benchmark artifacts.

## Acceptance evidence

Live acceptance must prove all four signals for the same debug session:

- Tempo has multi-span app roots for smoke, CLI, TUI, profile engine, and
  pytest.
- Tempo has benchmark run roots, benchmark command/subprocess spans, and MCP
  request spans for the debug session.
- No current-run trace has exactly one span.
- At least one checked trace contains `agentgrep.sqlite.*` spans.
- Prometheus has fresh span, engine CPU-loop, SQLite, and benchmark
  subprocess metrics with `agentgrep_debug_session_id`.
- Loki has current-run agentgrep logs selected through a query-stage JSON parse
  and no selected logs without trace and span identifiers.
- Pyroscope exposes the `agentgrep` service and the debug session label.

Future instrumentation changes that add subprocesses, benchmark rows,
exporters, auto-instrumentation, profile loops, or run-scoped metrics must
update this page with the cost and signal impact.
