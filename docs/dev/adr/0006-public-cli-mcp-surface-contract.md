(adr-public-cli-mcp-surface-contract)=

# ADR 0006: Public CLI and MCP surface

## Status

Proposed.

## Context

agentgrep has one search backend and multiple user-facing surfaces: CLI,
Textual TUI, MCP tools, JSON, and NDJSON. ADR 0004 defines the headless
planning, execution, event-stream, and result-type architecture. It does
not by itself define the public vocabulary users and MCP clients rely on.

The open design issues describe two related gaps:

- [#54](https://github.com/tony/agentgrep/issues/54) asks for the query
  language, fields, verbs, defaults, and flag vocabulary to be discoverable
  instead of hidden in implementation details.
- [#55](https://github.com/tony/agentgrep/issues/55) asks MCP tools to expose
  result completeness, pagination, source coverage, drilldown handles, and
  loop-friendly next actions instead of forcing clients to infer state from a
  partial list of records.

Greenfield and policy reviews in
[#57](https://github.com/tony/agentgrep/issues/57),
[#58](https://github.com/tony/agentgrep/issues/58), and
[#59](https://github.com/tony/agentgrep/issues/59) converge on the same
direction: keep AGENTS.md operational, keep planning/execution in ADR 0004, and
give the CLI/MCP surface its own durable vocabulary. ADR 0005 defines the local
insights report and model-backed enrichment architecture; this ADR follows it
with the public surface vocabulary that those reports and the core search tools
share.

## Decision

agentgrep will treat the public CLI/MCP surface as compatibility-sensitive.
AGENTS.md may point to this ADR, but it is not the source of public behavior.
CLI help, rendered docs, MCP tool schemas, MCP resources, JSON result payloads,
and NDJSON lifecycle summaries should be audited against this ADR and the
focused public-surface docs/tests that implement it.

### Surface ownership

The public surface owns:

- command and tool vocabulary;
- query field names, aliases, comparison rules, and examples;
- default behaviors for scope, agent selection, limits, ordering, ranking,
  dedupe, and case handling;
- result payloads, pagination, diagnostics, run status, and drilldown handles
  as defined by ADR 0004;
- source catalog vocabulary and coverage states;
- MCP loop shape and next-action guidance.

The implementation may use argparse, FastMCP, Pydantic, Textual, Rich, or other
libraries to expose the surface. Those libraries adapt the public request and
result types; they do not define them.

### Registry-backed discovery

agentgrep should converge on registry-backed public descriptions:

- CLI help and examples;
- library docs for the query language;
- MCP tool descriptions and JSON schemas;
- MCP resources for query fields, source coverage, and capability summaries;
- future shell completion or TUI help panels.

The registry can be implemented incrementally, but any new query field, flag,
tool argument, response field, or source state should have one canonical public
description and tests that prove the CLI/MCP/docs surfaces do not drift.

### Command and flag vocabulary

The CLI keeps the existing verbs, but their shared vocabulary becomes explicit:

- `search`: general record search over the selected scope.
- `grep`: grep-shaped text search that preserves familiar grep expectations
  where they do not conflict with agentgrep privacy or source semantics.
- `find`: fd/find-shaped source and storage discovery across agent stores.
- `ui`: Textual browsing over the same query, result, and event types.

Canonical shared options:

- `--agent`: selected agent or `all`.
- `--scope`: selected record scope, such as `all`, `prompts`, or
  `conversations`.
- `--limit`: primary result limit name across CLI, JSON, and MCP.
- `--format`: output format where a command exposes more than one sink.

Compatibility aliases are allowed when they match a familiar tool shape. For
example, `grep -m` and `grep --max-count` may remain aliases for `--limit`.
Aliases must normalize into the canonical request model before planning.

Future case handling should prefer a single explicit option such as
`--case {smart,ignore,respect}`. Existing compatibility flags may remain as
aliases, but the normalized request should carry one case policy.

`find --type` and `--scope` must not silently describe different axes. If
`--type` filters source roles while `--scope` filters record scopes, help text,
docs, JSON, and MCP metadata must name that distinction. If they become aliases,
they must normalize before planning and diagnostics must explain conflicts.

### Query introspection

The query language must be inspectable without reading private stores. The CLI
surface should provide bounded introspection commands such as
`agentgrep query fields` and
`agentgrep query explain 'agent:codex AND timestamp:2026-06'`.

The MCP surface should expose the same capability through a tool or resource
that is safe to call before search. The output should include field names,
aliases, supported operators, value kinds, examples, whether a field can prune
sources, whether it requires record parsing, and diagnostics for malformed
queries.

Query introspection may import the query registry and planner even when root
`agentgrep --help` stays cold-start sensitive. Introspection commands exist to
load and explain that registry.

### Source catalog vocabulary

Source discovery must expose machine-readable coverage instead of free-form
strings alone. A source or source-family response should include:

- stable source or store identifier;
- agent identifier;
- source role and record scopes;
- source kind and path kind;
- coverage level;
- `searchable` and `search_by_default`;
- `searchable_reason` when a source is skipped, opaque, unsafe, unavailable,
  or intentionally out of default search;
- `inspectable` when result drilldown can target the source;
- version-detection strategy or availability state;
- page info and diagnostics when discovery is paginated or partial.

Display paths may be rendered for humans, but MCP clients should use stable
identifiers, result cursors, and `RecordRef` handles rather than local paths
as primary inputs.

### MCP loop

MCP tools should support this loop:

1. Discover capabilities, query fields, and source coverage.
2. Explain or validate the intended query when needed.
3. Search with an explicit scope, limit, and output expectation.
4. Read the result payload's stats, run status, diagnostics, and
   `next_cursor`.
5. Request the next page when `next_cursor` is present.
6. Inspect a result through `RecordRef` when the user needs more context.
7. Refine the query using diagnostics and next actions rather than guessing at
   backend-specific flags or file paths.

MCP responses should include concise next-action hints only when they are
grounded in result state, such as "request next page", "narrow by agent",
"inspect this record", or "enable non-default source coverage". Next actions
must not include prompt text, secret values, raw argv, or local absolute paths.

### Result payloads

ADR 0004 owns the event streams and result type vocabulary. This ADR makes that
vocabulary public-surface policy:

- JSON responses expose the result payload by default.
- NDJSON responses emit lifecycle events and finish with an equivalent summary.
- MCP tool responses expose stats, page info, run status, diagnostics,
  records, and `RecordRef` handles by default.
- Pydantic models and FastMCP schemas adapt those fields for MCP clients but do
  not own the semantics.

## Consequences

### Positive

- CLI help, docs, MCP tools, JSON, and NDJSON can converge on one vocabulary.
- MCP clients can build reliable loops without path guessing or silent
  truncation.
- Query-language discovery becomes a user-facing capability rather than an
  implementation detail.
- Pydantic schemas remain useful without becoming the source of truth.

### Tradeoffs

- New flags, fields, tools, and response keys need surface tests or generated
  descriptions to prevent drift.
- Some compatibility aliases must be maintained and normalized carefully.
- The source catalog needs stable public names for states that were previously
  implicit.

### Risks

Surface sprawl: too many public names can make the CLI and MCP harder to
learn. The mitigation is canonical vocabulary plus aliases that normalize
before planning.

Generated-description drift: registry-backed docs can still drift if generation
is partial. The mitigation is focused tests that compare parser help, docs, MCP
schemas, and registry metadata where a field or flag is shared.

Privacy leakage: richer source and diagnostic metadata can expose local details.
The mitigation is the existing privacy boundary: no prompt text, secret values,
raw argv, or local absolute paths in machine-readable diagnostics or next
actions.

## Relationship to other ADRs

ADR 0001 owns storage-version evidence and source compatibility. ADR 0004 owns
planning, execution, events, result payloads, run status, pagination,
diagnostics, and record references. ADR 0005 owns local insights reports and
model-backed enrichment. This ADR owns how those names appear in public CLI and
MCP surfaces.

## Final position

agentgrep should feel like one tool whether reached from a terminal, a TUI, or
an MCP client. The public surface is the shared vocabulary, normalized request,
result payload, source catalog, and drilldown loop. Implementation libraries can
make that surface convenient; they should not redefine it.
