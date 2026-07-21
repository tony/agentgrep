(adr-public-cli-mcp-surface-contract)=

# ADR 0006: Public CLI and MCP surface

## Status

Proposed.

## Context

CLI, TUI, MCP, JSON, NDJSON, and library callers expose the same search engine
through different interaction styles. If each frontend invents request fields,
status meanings, identities, or pagination behavior, apparently equivalent
operations diverge and become difficult to evolve safely.

## Decision

Public surfaces adapt one normalized request, event, and result vocabulary.
Core semantics live in the planning and focused behavior ADRs; schemas and
frontends expose those semantics without redefining them.

### Surface ownership

The core owns normalized query, scope, effort, order, deduplication, lifecycle,
coverage, and pagination meanings. CLI and TUI own human interaction and
rendering. MCP and Pydantic models own schema adaptation and validation. JSON
and NDJSON serializers own wire representation.

An adapter layer may rename a field idiomatically or preserve a released alias,
but its mapping is explicit and tested. Pydantic behavior is not the semantic
source of truth for CLI, MCP, or search.

Public descriptions and capability metadata derive from one registry-backed
definition where practical. Root help remains cold; focused introspection may
load the registry when explicitly requested.

### Search vocabulary

Search and grep expose the same normalized effort and order vocabulary. If the
progressive-search ADR is adopted:

- omitted effort uses its compatibility rule;
- `--deep` selects targeted effort;
- `--exhaustive` selects exhaustive effort without requiring `--deep`; and
- `--limit` and MCP/JSON `limit` mean the maximum canonical results returned in
  one response or page.

Compatible grep spellings such as `-m` or `--max-count` may remain aliases for
`--limit`. The progressive-search ADR owns the target default and the versioned
migration from current omission behavior. Existing cursorless calls remain
single-response calls while continuation is introduced.

No public `result_limit`/`page_size` split is created by this ADR. A future
total-result cap across pages requires a distinct name and focused decision.
Candidate, routing, provider, operation, byte, and display bounds also use
distinct names and never normalize to result `limit`.

`find --type` selects discoverable source or storage roles. The search and grep
`scope` field selects normalized record kinds. Public descriptions and schemas
keep these axes distinct and do not treat them as aliases without an explicit
migration.

Until the progressive-search ADR is adopted, released public meanings remain
compatibility facts. Adoption supplies the explicit amendment rather than this
Proposed ADR silently rewriting an Accepted contract.

### Pagination and next actions

`next_cursor` is opaque. A continuation reuses the normalized request and the
validated semantic snapshot required by its owning operation. It does not ask
callers to reconstruct local paths, private keys, or offsets, and it fails
explicitly when required state is stale or unavailable.

The existing public `agcur1` search cursor is an offset-over-rerun token. It
does not carry the snapshot, canonical key, deduplication state,
representative-policy version, or provider generation required by ADR 0014.
A compliant implementation therefore issues a new cursor version and never
reinterprets `agcur1` as a keyset cursor. At the versioned implementation
cutover authorized by this migration, new searches stop issuing `agcur1` and
an `agcur1` continuation is rejected with a stable unsupported-cursor outcome.

A continuation may vary only the per-response `limit`. An explicitly supplied
query, scope, effort, order, filter, or other normalized field that conflicts
with the cursor is a validation error. Cursor state never silently overwrites a
conflicting caller value.

ADR 0014 owns core search order and continuation. Focused similarity, export,
insights, and source-discovery operations may define their own stable cursors;
they do not inherit a search cursor merely because they return records.

Results may include additive typed next actions for pagination, effort
escalation, scope broadening, inspection, export, similarity, or other focused
operations. Each action kind defines its own request patch. Consumers ignore
unknown action kinds while continuing to honor status and coverage.

### Source coverage and identity

Public source selection and capability reporting use stable store-family
identity. A discovered file, database, or other physical source is a local
instance; its path, adapter coordinate, and locator remain local or private
data, not portable public identity.

Coverage terms have stable meanings. `searchable` means some supported search
request may include the store family. `search_by_default` means an ordinary
request includes it without explicit scope or coverage opt-in and therefore
implies `searchable`. `inspectable` means the source has a supported read-only
record-inspection path; it neither implies default search nor turns a physical
locator into public identity.

### Results and identity

Every structured result preserves the lifecycle established by ADR 0004:
normalized request summary, records, status, coverage, privacy-safe
diagnostics, counts, applied order and limit, and page metadata when supported.
NDJSON finishes with an equivalent summary rather than emitting records alone.

{class}`~agentgrep.RecordRef` is the public physical record-drilldown handle. It
is not canonical equality, grouping, bookmark, export, similarity, or
conversation identity. Public content, record, thread, and stability field
names remain reserved for a focused identity decision. Private corpus keys,
locators, paths, row numbers, and cursor coordinates never acquire public
identity merely because an internal resolver uses them.

A public thread identifier does not imply a public conversation resolver.
Bookmark reopening, portable export, import, and conversation resolution each
require their owning public-surface contract. Storage inspection does not
create a second portable format or import path.

### Channels and discoverability

Interactive CLI and TUI surfaces may advertise deeper search or next actions.
Machine-readable stdout contains only the selected result format; hints and
progress use the appropriate interactive or structured channel. Exact wording,
layout, and exit-code mapping live in user-facing documentation and tests, not
in this architecture decision.

MCP tools return structured lifecycle results and use capability metadata to
describe safety and side effects. A tool does not infer writes, provisioning,
or deeper effort from an installed dependency or response format.

### Compatibility

Public request fields, enum values, status values, result fields, and action
kinds are compatibility-sensitive. Additions should be ignorable where safe.
Renames, removals, changed defaults, and changed meanings require a versioned
migration or explicit superseding decision.

## Relationships

- ADR 0004 owns normalized planning, lifecycle, status, coverage, and
  `RecordRef` semantics.
- ADR 0014 owns canonical core-search ordering and continuation.
- The proposed prompt-corpus ADR introduces no public identity, import, or
  portable export surface.
- The proposed progressive-search and routing ADRs own effort guarantees and
  routing work, while this ADR owns their public spelling.

## Consequences

Equivalent operations gain one semantic contract across CLI, MCP, TUI, and
library use. New actions and fields can be added without making frontends parse
human text or private paths.

The shared vocabulary creates coordination work across schemas, serializers,
docs, and tests. Compatibility aliases may outlive their preferred spelling,
but they remain adapters to one meaning rather than competing semantics.
