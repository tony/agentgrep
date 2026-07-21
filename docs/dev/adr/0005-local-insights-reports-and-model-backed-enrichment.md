(adr-local-insights-reports-model-backed-enrichment)=

# ADR 0005: Local insights reports and model-backed enrichment

## Status

Proposed.

## Context

Users may want summaries, activity reports, topics, similarity, graphs, or
model-backed interpretation over their local agent history. These operations
have different cost, dependency, reproducibility, and privacy properties from
ordinary exact search.

Making enrichment an implicit search tier would let installed dependencies or
local model state change query behavior without an explicit request. Making
enrichment canonical would also couple prompt evidence to replaceable models
and indexes.

## Decision

Insights use a deterministic local reporting pipeline with optional enrichment:

1. a normalized request selects admitted evidence;
2. deterministic reducers build the base report;
3. explicitly selected enrichers may add derived annotations or rankings; and
4. renderers present the report without performing discovery, provisioning, or
   additional analysis.

The deterministic base remains available without optional models or native
dependencies. Enrichment output records its input identity, provider or model,
policy version, and enough provenance to explain how it was produced.

### Enrichment is derived

Summaries, embeddings, vector indexes, clusters, inferred topics, graph edges,
and model ranks are versioned, removable read models. They do not become
canonical prompt evidence, public identity, or exact-search authority.
Deleting or rebuilding enrichment must not migrate prompt evidence, change
exact prompt coverage, or alter ordinary search results.

This boundary does not forbid a similarity or insights operation from owning a
derived score and order. Such an operation declares its metric, provider,
generation, approximation, and pagination contract. It may not silently export
that score into ordinary search or conversation routing.

### Provisioning and activation are explicit

Installing an optional dependency does not activate an enricher, build an
index, download or load a model, contact a remote service, or change routing.
Operations that require provisioning expose that requirement before analysis
and require explicit user or deployment authorization.

An explicitly selected unavailable provider produces a capability outcome. It
does not silently switch metrics or models unless the caller selected a named,
versioned fallback policy that discloses the substitution.

Network use and remote processing are opt-in. Local history, prompts, derived
features, and queries are not sent remotely by default.

### Storage and lifecycle

Enrichment caches remain outside the canonical prompt corpus and exact-index
generations. Cache identity includes the canonical input reference plus the
model, metric, policy, and relevant normalization versions; raw text alone is
not a sufficient reproducibility key.

Reports and caches have their own retention and invalidation rules. A stable
report cursor may be exposed only when a focused report contract defines an
immutable generation and deterministic order. Search effort and search cursors
do not supply those rules.

### Privacy and evidence

Insights report reduced evidence rather than silently copying full transcript
bodies. Outputs identify unavailable, excluded, or unsupported inputs and do
not claim completeness beyond admitted evidence. Diagnostics avoid prompt
text, secrets, raw local paths, and private locator material.

## Relationships

- ADR 0004 owns search planning and lifecycle when an insights operation
  explicitly invokes search.
- ADR 0006 owns public CLI and MCP spelling.
- The prompt-corpus ADR keeps enrichments outside canonical evidence and exact
  indexes.
- The routing ADR permits an optional semantic candidate policy only through
  explicit, versioned activation; it does not make insights a search tier.

## Consequences

agentgrep can offer useful deterministic reports on a minimal installation and
add model-backed capabilities without changing exact search by environment
accident. Providers and caches remain replaceable and auditable.

Reproducible enrichment requires more provenance and explicit capability
handling. Model-backed output may be expensive or approximate, and callers must
choose that tradeoff rather than inheriting it from installed packages.
