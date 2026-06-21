(adr-opentelemetry-local-observability)=

# ADR 0014: OpenTelemetry local observability

## Status

Accepted.

## Context

agentgrep is primarily headless but has several execution surfaces: the CLI,
interactive CLI/TUI, MCP tools, async/threaded search execution, profiler
scripts, and pytest. Development needs traces, metrics, logs, and profiles in
Grafana LGTM without making normal package use depend on Docker, a collector,
network availability, or telemetry SDK imports.

The codebase also handles local prompt-history data. Telemetry must therefore
describe operation shape without recording raw prompts, raw MCP arguments, raw
argv, environment values, file contents, secrets, or local absolute paths.

## Decision

Application code instruments through `agentgrep._telemetry`, a dependency-light
project boundary. OpenTelemetry SDK, OTLP exporters, auto-instrumentation, and
Pyroscope setup live in `agentgrep._telemetry_otel` and are imported lazily
only when telemetry is enabled.

`AGENTGREP_OTEL` is the single project switch. Local source checkouts default
to passive local telemetry. Packaged installs stay quiet unless explicitly
enabled. The accepted modes are `off`, `local`, `debug`, `debug-console`,
`test`, and `live`.

OTel setup is best effort. If the SDK, exporters, LGTM, Docker, or an OTLP
endpoint is unavailable, agentgrep continues to run. Export failures do not
change CLI, TUI, MCP, or test correctness.

`service.version` is the installed package version only. Debug identity uses
separate attributes such as `agentgrep.debug.session_id`,
`agentgrep.debug.candidate_id`, `agentgrep.debug.attempt`, and
`agentgrep.pytest.run_id`.

Root spans are app-level operations only: CLI invocation, TUI session, MCP
request or tool execution, benchmark run, profile-engine run, pytest session or
test case, and the live OTel smoke workload. CLI roots start before argument
parsing so help output and parse errors are visible. Benchmark roots wrap the
benchmark harness and parent command/subprocess spans so benchmark-only cost is
visible in LGTM. Profile-engine roots wrap parse, engine execution, and
rendering so benchmark-only profiler runs are visible in LGTM. Child spans
cover logical work such as parse, dispatch, discovery, planning, collection,
filtering, detail building, rendering, subprocess execution, and thread/async
boundaries. Low-level keypresses, render frames, event-loop callbacks, and
orphaned auto-instrumentation roots are not accepted signal.

SQLite spans use a project connection factory for `sqlite3.Connection`
shortcut methods such as `execute`, `executemany`, and `executescript`.
The upstream SQLite DB-API instrumentation wraps cursor execution, but
agentgrep's source parsers use connection shortcuts. SQL spans therefore stay
under an existing app root and do not record bound parameter values, raw
prompts, file contents, or local database paths.

Logs are exported only when there is an active project span or a valid current
OTel span so Loki records are trace-linked. OTel log records are sanitized
before export to avoid local absolute source paths.

Metrics start with span count/duration plus explicit project metrics for
search/source/result counts, CPU-impacting engine loops, SQLite shortcut
execution, benchmark subprocess work, pytest documentation subprocess work, and
live smoke evidence. When `AGENTGREP_OTEL` is enabled and
`AGENTGREP_DEBUG_SESSION_ID` is present, metrics carry
`agentgrep_debug_session_id` so Grafana QA can prove run-scoped coverage. CPU
and SQLite metrics must be emitted by normal app paths, not only by synthetic
smoke scripts. If that series count becomes a project-threatening problem,
cardinality reduction belongs in a follow-up with measurement rather than
hiding metrics in this PR.

Pytest remains offline by default. When `AGENTGREP_OTEL` is explicitly set for
a pytest process, pytest session hooks set up telemetry once and wrap every
collected item in an `agentgrep.pytest.test` root. That covers direct tests,
custom documentation items, doctest-style items, and Textual `run_test()` cases
that bypass `agentgrep.main()`, without creating single-root traces.

Pyroscope profiles use `application_name="agentgrep"` and must not duplicate
the generated `service_name` label in custom tags.

## Consequences

### Positive

- Developers can run `just otel-acceptance` against Grafana LGTM and verify
  traces, metrics, logs, and profiles.
- Default pytest remains offline and deterministic through in-memory telemetry.
- Packaged users do not pay for OTel SDK imports or network exporters unless
  telemetry is enabled.
- Async/thread work preserves project trace context through local wrappers.

### Tradeoffs

- Local source checkouts are more observable by default than packaged installs.
  This is intentional for development but must stay failure-tolerant.
- Run-scoped metric labels add local QA series count. This is accepted here
  because this branch closes blindspots; later cardinality reductions need
  evidence.
- Console exporters are reserved for `debug-console` so normal dev runs do not
  pollute stdout or stderr.

### Rejected options

- Normal runtime dependency on OTel SDK/exporters: rejected because it creates a
  packaging and startup hazard for downstream users.
- Root logger level mutation or unparented log export: rejected because it
  creates log noise and trace blind spots.
- Console exporters by default: rejected because CLI and MCP stdout/stderr are
  public behavior.
- Docker or collector requirement for ordinary tests: rejected because default
  pytest must be deterministic and offline.
- Encoding debug attempts in `service.version`: rejected because package
  version and debug-loop identity answer different questions.
- Capturing raw prompts, raw MCP args, full argv, environment values, file
  contents, or absolute local paths: rejected for privacy and cardinality.

## Tests and verification

Fast tests cover mode resolution, `service.version` separation, linked
in-memory logs, non-single-root traces, thread context propagation, MCP
argument redaction and request roots, CLI/MCP span shape, benchmark subprocess
spans and metrics, pytest item roots, documentation subprocess telemetry, named
OTel custom metrics, and log path sanitization.

Live LGTM verification is opt-in through `scripts/otel_acceptance.py` and
`just otel-acceptance`. It starts or reuses `grafana/otel-lgtm`, runs smoke,
CLI help, CLI parse-error, CLI search, profile-engine, TUI smoke, and direct
Textual pytest workloads, and verifies:

- `agentgrep.otel.smoke`, `agentgrep.cli.invocation`,
  `agentgrep.tui.session`, `agentgrep.benchmark.run`,
  `agentgrep.profile_engine.run`, and `agentgrep.pytest.test` roots are
  multi-span traces for the current debug session;
- no current-run single-root trace is accepted;
- benchmark command/subprocess spans and MCP request spans are present;
- at least one checked trace contains `agentgrep.sqlite.*` spans from SQLite
  connection shortcut work;
- fresh span, app CPU-loop, app SQLite, and benchmark subprocess metrics with
  the current `agentgrep_debug_session_id` are visible in Prometheus;
- current-run Loki logs all contain trace and span identifiers;
- Pyroscope exposes both the `agentgrep` service and current debug session.

The subprocess and signal-cost inventory lives in
{ref}`otel-cost-model`.
