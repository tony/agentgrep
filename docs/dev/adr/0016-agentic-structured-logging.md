(adr-agentic-structured-logging)=

# ADR 0016: Agentic structured logging

## Status

Accepted.

## Context

ADR 0015 established local OpenTelemetry as an opt-in observability surface for
agentgrep. It also requires exported logs to be trace-linked and sanitized
because agentgrep reads local prompt-history stores.

The next gap is usefulness. A trace tree can show that work happened, but
agentic debugging loops often need a small stream of structured status records:
which surface ran, which boundary completed, how many sources or results were
involved, whether a handoff failed, and which trace to open next.

The logging systems studied for this decision repeat a few durable patterns.
Operational status is separated from result payloads. Sensitive or
security-adjacent decisions use typed, stable fields rather than prose blobs.
OpenTelemetry treats trace and span identifiers as first-class log correlation
fields. Those patterns fit agentgrep if logs stay sparse, schema-oriented, and
content-free.

## Decision

agentgrep uses structured Python logging as a low-volume status stream for
critical operation boundaries. Logs complement spans and metrics; they do not
replace them and they do not carry search result content.

Critical boundaries include CLI command lifecycle, MCP request/tool lifecycle,
TUI session and worker lifecycle, engine search and find lifecycle, profiling
and benchmark runs, OTel acceptance smoke work, and sensitive decisions such as
environment/config path handling or MCP argument summarization.

Each telemetry-oriented log record should carry stable `agentgrep_*` fields.
The preferred base fields are:

- `agentgrep_surface`
- `agentgrep_operation` or the established surface-specific operation key
- `agentgrep_outcome`
- safe counts such as source, planned-source, result, record, or batch counts
- elapsed duration when the surrounding boundary has an obvious timer
- bounded enums such as scope, strategy, method, tool, adapter, or error type

Logs are emitted from inside active project spans when they are meant for OTel.
That keeps Loki records correlated with Tempo traces and avoids orphan log
events. Library code still does not configure handlers, levels, console output,
or exporters; `agentgrep._telemetry` owns opt-in export.

Telemetry logs describe operation shape, not local content. They must not
record raw prompts, query terms, raw argv, raw MCP arguments, environment
values, file contents, full exception text that may contain content, or local
absolute paths. Use booleans, counts, enums, redacted path metadata, and
length/digest summaries instead.

High-volume detail belongs in spans, metrics, profiles, or bounded aggregate
fields. Per-record, per-line, per-keypress, render-frame, and hot-loop logs are
not accepted signal.

## Consequences

### Positive

- Loki becomes useful for local agentic loops without requiring operators to
  reconstruct every status transition from spans.
- Logs link directly to traces, so a Grafana drilldown can move from a status
  event to the exact CLI, MCP, TUI, or engine execution.
- Normal users still see no logging output unless an application configures it.
- Prompt-history content and local machine paths stay out of exported logs.

### Tradeoffs

- Boundary logs add maintenance surface because field names become
  compatibility-sensitive once dashboards or alert queries rely on them.
- Some spans and logs will describe the same operation. This duplication is
  accepted when the log is the searchable status record and the span is the
  timing/trace context.
- Sparse logging means deep per-record investigation still requires local
  reproduction, targeted debug logs, or a profiling artifact.

### Rejected options

- Logging every parsed record or match: rejected because it risks content
  leakage, high cardinality, and hot-loop overhead.
- Exporting logs outside active spans: rejected because unlinked Loki events
  are hard to use and were explicitly disallowed by ADR 0008.
- Console logging for local development by default: rejected because CLI and
  MCP stdout/stderr are public behavior.
- Free-form message-only logs: rejected because agentic loops need filterable
  fields and stable dashboards.

## Tests and verification

Unit tests for new telemetry logs should assert on structured attributes, not
formatted text. Use the in-memory telemetry backend when trace/span linkage is
part of the contract, and use `caplog.records` for ordinary logging schema
tests.

Live verification belongs in `just otel-acceptance`. The acceptance check must
continue to prove that current-run Loki logs have trace and span identifiers.
