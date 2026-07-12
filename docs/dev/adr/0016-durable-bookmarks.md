(adr-durable-bookmarks)=

# ADR 0016: Durable bookmarks

## Status

Accepted.

## Context

Search is deliberately read-only over agent history stores, but users still
need a durable way to return to a useful result. Copying prompt bodies, source
paths, or backend metadata into a second index would enlarge the privacy and
migration surface. Saving a physical `agref1:` locator would instead tie the
bookmark to one store location and adapter revision.

{ref}`ADR 0015 <adr-deterministic-record-identity>` provides the smaller
contract this feature needs: canonical content, logical occurrence, and thread
handles with explicit availability limits. Bookmarks persist those handles as
local user intent and resolve them against current records only when requested.

## Decision

### Canonical scopes

Every bookmark has exactly one scope and one complete canonical target:

| Scope | Target | Recall semantics |
| --- | --- | --- |
| `record` | `agr1:` plus its `agc1:` content validator | Require both IDs to match one current logical occurrence. |
| `thread` | `agt1:` | Open a representative current record from the matching thread. |
| `content` | `agc1:` | Open an equal-content occurrence currently available. |

No scope accepts a shortened handle, canonical-ID prefix lookup, physical
`agref1:` locator, or path. A record bookmark persists `content_id` as a second
check because resolving an occurrence to different content is worse than
leaving it unresolved. Thread and content bookmarks do not pretend to select a
particular occurrence.

Resolution runs a fresh, un-deduplicated search over all supported agents and
both prompt and conversation scopes. It hashes candidates away from the TUI
message pump and stops once all targets have either matched or the current scan
has finished. When no match is available, unresolved bookmarks remain saved; a
source may be unavailable now and return later. Opening one record changes only
the detail pane and does not replace the user's loaded result list.

### Bounded, idempotent mutations

Add and remove are idempotent. Adding an existing target returns `unchanged`
when its validator agrees; removing an absent target also returns `unchanged`.
A new target returns `added`, an existing target returns `removed` when toggled,
and a successful explicit removal returns `removed`.

The default capacity is 200. Capacity is checked only for a new target, so
re-adding or removing remains possible when the store is full. A new target is
refused without evicting an older choice. Creation order is preserved; there
is no implicit least-recently-used policy.

### Storage and privacy

The versioned JSON snapshot lives in agentgrep's XDG data directory, separate
from query history and from every discovered source. Its top level contains
`schema_version` and an ordered entry list. Each entry contains only
`target_id`, `scope`, `content_id`, and `created_at`; `content_id` is null
outside record scope.

The snapshot excludes prompt text, titles, source paths, working directories,
repository paths, agent metadata, and physical refs. Canonical IDs are
pseudonymous comparison handles rather than secrets, authentication, or
anonymization, and creation times are activity metadata. The directory,
snapshot, and coordination file therefore use private permissions.

Every read validates the complete schema and every entry before returning
anything. An unknown schema, duplicate targets, malformed entries, or an
over-capacity list refuses the snapshot as a whole. Mutation does not salvage,
partially load, or overwrite corrupt state. Successful replacement is atomic
and serialized across processes.

### Surface boundary

The CLI adds, removes, and lists canonical targets. It does no history scan.
The HUD toggles a selected target and resolves the saved list on demand; `b` is
the exact-record shortcut, while `/bookmark` names any scope and `/bookmarks`
opens recall.

MCP exposes neither bookmark mutation nor this machine's local bookmark list in
the initial contract. Bookmark state expresses local user intent, while MCP
clients already receive canonical identity fields they can store under their
own policy. A future MCP surface would need an explicit local-state authority
and privacy review rather than inheriting CLI access accidentally.

Bookmark persistence is the only write introduced here; during discovery,
resolution, and detail display, all source stores remain read-only.

## Consequences

Users can keep a small durable collection across TUI sessions and manage the
same targets headlessly without duplicating prompt bodies. Missing stores and
conservative identity gaps appear honestly as unresolved entries, not silently
rewritten bookmarks.

The collection is intentionally not a tag database, sync protocol, export
format, or source-store annotation. It has a fixed capacity and one global
creation-ordered list. Content and thread recall choose a current
representative rather than promising physical continuity; exact continuity
requires a record bookmark and its content validator.
