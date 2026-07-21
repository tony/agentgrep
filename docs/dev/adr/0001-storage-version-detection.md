(adr-storage-version-detection)=

# ADR 0001: Storage version detection

## Status

Accepted.

## Context

agentgrep reads local stores written by independently released AI assistants.
Applications can change filenames, record shapes, database migrations, and
embedded metadata, while old and new data may coexist after an upgrade. The
installed application version therefore does not reliably identify every
source shape on disk.

Discovery must also avoid turning private or noisy storage into searchable
content merely to determine its format.

## Decision

Adapters detect source versions from concrete data evidence, in this order:

1. embedded version metadata in the source;
2. narrow shape evidence such as known filenames, keys, tables, columns, or
   migration suffixes;
3. local version metadata that can be read without invoking the upstream
   application; and
4. catalog observation metadata as an explicitly lower-confidence fallback.

When application metadata and source shape disagree, the source shape governs
parsing. Agentgrep interprets local history through its own adapters and does
not invoke an upstream assistant CLI to identify, parse, summarize, or otherwise
interpret that history.

For opt-in inventory sources that are unsafe or noisy as text, an adapter may
use structural summaries such as known top-level keys, event names, manifest
keys, suffixes, sizes, or counts. Such evidence establishes the observed shape
without admitting raw logs, configuration values, commands, cache payloads, or
secrets to search.

Version evidence is privacy-safe. It may identify known structural markers,
but it must not contain prompt text, credentials, arbitrary configuration
values, or unredacted local paths. Private stores remain catalog-only unless a
separate decision establishes a safe runtime inventory contract.

Version detection selects an adapter and interpretation contract; it does not
prove that a prior corpus or index observation remains current. An adapter that
supports durable prompt evidence owns its native consistent-observation proof.
If adopted, {ref}`adr-durable-prompt-corpus-derived-search-indexes` owns how
those proofs establish prompt-corpus and exact-index freshness, coverage, and
fallback. Providers may encode the proof but may not redefine its meaning.

## Consequences

Adapters require explicit shape detectors and confidence-aware fallbacks.
Runtime discovery records the strategy actually used, while the store catalog
describes supported strategies. Sources without a concrete detector may remain
available under a lower-confidence catalog observation until their adapter is
improved.

The approach avoids subprocess side effects and keeps evidence auditable, but
every newly supported format needs maintained structural knowledge. Narrow
shape checks are preferred to broad inference from arbitrary application
state.
