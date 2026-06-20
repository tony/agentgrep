(adr-opentelemetry-local-observability)=

# ADR 0008: OpenTelemetry local observability

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

Root spans are app-level operations only: CLI invocation or interactive
session, TUI session, MCP tool execution, profile-engine run, pytest session or
test case, and the live OTel smoke workload. Child spans cover logical work
such as dispatch, discovery, planning, collection, filtering, detail building,
and thread/async boundaries. Low-level keypresses, render frames, event-loop
callbacks, and orphaned auto-instrumentation roots are not accepted signal.

SQLite spans use a project connection factory for `sqlite3.Connection`
shortcut methods such as `execute`, `executemany`, and `executescript`.
The upstream SQLite DB-API instrumentation wraps cursor execution, but
agentgrep's source parsers use connection shortcuts. SQL spans therefore stay
under an existing app root and do not record bound parameter values, raw
prompts, file contents, or local database paths.

Logs are exported only when there is an active project span so Loki records are
trace-linked. OTel log records are sanitized before export to avoid local
absolute source paths.

Metrics start with span count/duration plus explicit low-cardinality project
metrics for search/source/result counts and live smoke evidence. High-cardinality
debug identifiers are not metric labels by default; traces, logs, and profiles
carry debug-loop identity.

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
- Run-scoped metric filtering relies on fresh sample timestamps rather than
  debug-session labels to avoid high-cardinality metric labels.
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
argument redaction, CLI/MCP span shape, named OTel custom metrics, and log path
sanitization.

Live LGTM verification is opt-in through `scripts/otel_acceptance.py` and
`just otel-acceptance`. It starts or reuses `grafana/otel-lgtm`, runs a smoke
workload plus a real CLI search, and verifies:

- both `agentgrep.otel.smoke` and `agentgrep.cli.invocation` roots are
  multi-span traces for the current debug session;
- no current-run single-root trace is accepted;
- at least one checked trace contains `agentgrep.sqlite.*` spans from SQLite
  connection shortcut work;
- fresh span and custom smoke metrics are visible in Prometheus;
- current-run Loki logs contain trace and span identifiers;
- Pyroscope exposes both the `agentgrep` service and current debug session.
