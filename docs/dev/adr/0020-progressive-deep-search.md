(adr-progressive-deep-search)=

# ADR 0020: Progressive deep search

## Status

Accepted. The effort ladder is implemented.

## Context

Prompt history is cheap to search but does not contain every conversation
record. Opening every transcript for every request is complete but costs more
I/O and parsing than a prompt-only question needs. The public request must make
that trade-off explicit without turning scope, result limits, and routing into
competing depth controls.

## Decision

Search effort is an engine-owned, ordered read policy with three values:

- `prompt` discovers and reads only prompt-history sources. It does not open
  conversation bodies.
- `targeted` searches prompt evidence, then uses the fixed routing decision in
  {ref}`ADR 0021 <adr-prompt-guided-conversation-routing>` to add selected
  conversations. It is approximate because candidate selection can omit a
  conversation.
- `exhaustive` discovers and reads every eligible conversation source admitted
  by the normalized scope and source predicates.

The CLI normalizes `--deep` to `targeted` and `--exhaustive` to `exhaustive`;
a bare CLI `--exhaustive` retains prompt scope. MCP exposes the same `prompt`,
`targeted`, and `exhaustive` vocabulary, but an omitted scope with targeted or
exhaustive effort infers `all`; an explicitly supplied `scope="prompts"`
remains invalid for targeted effort. `scope` controls record kinds, while
`effort` controls how broadly the engine reads sources, and neither is a result
limit.

Each effort uses the same matcher, declared order, dedupe policy, and result
limit. The result summary reports requested and completed effort, status,
coverage, diagnostics, and engine-authored escalation actions. A targeted
summary reports `approximate` with the candidate-selection condition unless a
higher-precedence failure, cancellation, or truncation condition applies.

## Consequences

Readers can begin with prompt history and explicitly spend more I/O only when
their question warrants it. An empty result remains interpretable: the summary
distinguishes no prompt match, no candidate conversation, no match in selected
conversations, and no exhaustive match. Frontends must use that engine-owned
evidence and next actions rather than infer coverage from an empty result list.

This ADR owns effort semantics and normalization. {ref}`ADR 0021
<adr-prompt-guided-conversation-routing>` owns targeted candidate selection.
{ref}`ADR 0014 <adr-result-order-limit-and-streaming-merge>` owns ordering and
result limits; a result-limit migration to 25 did not occur.
