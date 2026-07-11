(adr-deterministic-record-identity)=

# ADR 0015: Deterministic record identity

## Status

Accepted.

## Context

Search records need stable vocabulary for several different jobs. A physical
result must remain inspectable even when an agent store projects the same turn
through more than one adapter. Repeated text needs a content-equality handle,
while two occurrences of that text in one conversation must remain distinct.
Downstream bookmarks, export, and similarity also need a thread boundary that
does not depend on the current filesystem location.

One universal identifier cannot answer all of those questions without losing
information or making unsupported topology claims. This ADR therefore keeps
four concepts separate:

- `RecordRef` is the existing opaque physical locator for a stored result.
- `content_id` identifies semantic record content.
- `record_id` identifies a logical occurrence when the source supplies a
  defensible thread and coordinate.
- `thread_id` identifies a namespaced backend thread when the source supplies
  a defensible native anchor.

Engine deduplication is policy rather than another public ID. It decides when
two search candidates compete for one representative; it does not change what
the public handles mean.

## Decision

### Canonical encoding

Every canonical ID hashes a small JSON envelope with SHA-256. The digest is
truncated to the first 128 bits and encoded as lowercase, unpadded base32hex.
Every full value is 31 characters: a versioned five-character prefix plus 26
encoded characters. The complete values are the only form; there is no short
form, auto-widening prefix, or canonical-ID resolver.

The envelope uses compact, sorted JSON with `ensure_ascii=False` and
`sort_keys=True`. Canonical JSON uses `separators=(",", ":")`. The result is
encoded as UTF-8 with `surrogatepass` before hashing. That error handler makes
lone-surrogate text total without changing ordinarily encodable input.

The following fixed vectors use the normalized prompt text `hello`:

- content: `agc1:2vlm1978v1np5kg5fkqv539kic`
- thread: `agt1:bkd9k19ok4vvbsf73jornija04`
- native occurrence: `agr1:uuqn9q331f1fcgsr5gr8agefhs`

### Content identity

Content identity hashes the exact normalized record text once with UTF-8
`surrogatepass`, then places its hexadecimal SHA-256 digest in this exact
canonical payload:

```json
{"kind":"prompt","role":"user","text_sha256":"2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824","type":"record-content","v":1}
```

The payload produces the `agc1:` content vector above. Role normalization is
exactly `role.casefold()` for a non-empty role and null otherwise; values such
as `human` and `user` are not aliased. Record kind, normalized role, and exact
text define content equality.

Paths, timestamps, cwd, repository, worktree, branch, remote, project, title,
model, adapter, session, conversation, and agent are excluded. They are mutable
provenance or belong to a different identity job, not semantic content.

### Thread identity

A thread requires the record's agent, an adapter-owned identity namespace, and
an explicit backend key. Any truthy `session_id` is accepted without path-shape filtering.
It wins when both native fields are present. A fallback `conversation_id` must be non-path-shaped.
A missing namespace, missing key, or path-shaped conversation fallback produces
a null `thread_id` instead of a path-derived identity.

For agent `codex`, namespace `codex.session`, and session key `abc`, the exact
payload is:

```json
{"agent":"codex","key_kind":"session","key_value":"abc","namespace":"codex.session","type":"thread","v":1}
```

It produces `agt1:bkd9k19ok4vvbsf73jornija04`. Namespacing prevents equal
native values from unrelated agents or upstream domains from merging, while
duplicate adapters for the same upstream domain can deliberately share a
namespace.

### Record occurrence identity

A record occurrence requires both a non-null `thread_id` and a validated source
coordinate. A non-empty native string wins and has `native` stability. Without
one, a non-boolean, non-negative integer ordinal has `source_order` stability;
that fallback may change if an upstream store rewrites earlier entries. Missing
or malformed coordinates, or a missing thread, produce null `record_id` and
`record_id_stability` values.

For native coordinate `msg-1`, the exact payload is:

```json
{"agent":"codex","content_id":"agc1:2vlm1978v1np5kg5fkqv539kic","coordinate_kind":"native","coordinate_value":"msg-1","thread_id":"agt1:bkd9k19ok4vvbsf73jornija04","type":"record","v":1}
```

It produces `agr1:uuqn9q331f1fcgsr5gr8agefhs`. An ordinal payload changes
`coordinate_kind` to `ordinal` and uses the integer as `coordinate_value`.
Parent coordinates describe observed topology but do not enter occurrence
identity. Native identity also ignores physical store, adapter, and path, so
duplicate physical views of one native occurrence share the same `record_id`.

### Physical refs and cursors

Ordinary `agref1:` and `agcur1:` bytes remain unchanged. Canonical IDs are
sibling result fields, not additions to either token. An `agref1:` value remains
the physical locator accepted by `inspect_result`; `agc1:`, `agr1:`, and `agt1:`
values are not accepted refs. Ref-only inspection therefore keeps its existing
bounded physical lookup semantics rather than launching a global ID scan.

UTF-8 `surrogatepass` extends physical ref fingerprinting only to text that
previously could not be encoded. It does not change ordinary ref bytes.

### Coordinate-aware engine deduplication

Search candidates use one of six cheap tuple forms. In the forms below,
`semantic` is exactly `(kind, normalized role, exact text)`:

- `logical-native`:
  `("logical-native", agent, namespace, thread_kind, thread_value, native_id,
  *semantic)`
- `logical-ordinal`:
  `("logical-ordinal", agent, namespace, thread_kind, thread_value, ordinal,
  *semantic)`
- `physical-native`:
  `("physical-native", agent, store, path, native_id, *semantic)`
- `physical-ordinal`:
  `("physical-ordinal", agent, store, path, ordinal, *semantic)`
- `fallback-thread`:
  `("fallback-thread", agent, store, namespace, thread_kind, thread_value,
  *semantic)`
- `fallback-path`: `("fallback-path", agent, store, path, *semantic)`

A logical form is used only when a namespace and usable thread anchor exist.
Any truthy session value is usable; a conversation fallback must be
non-path-shaped. Otherwise a positioned record falls back to its physical
scope. Positionless records use `fallback-thread` when possible and
`fallback-path` otherwise.

This projection performs no cryptographic hashing on scan candidates. The
driver still owns representative and event order: inline execution keeps the
first view it streams, while frontier execution may retain the newest physical
view. The drivers promise equal logical membership, not byte-identical refs or
event ordering.

### Observed conversation topology

Conversation grouping describes only observed topology. It groups records with
a non-null `thread_id`, retains every physical view in a deterministic member
inventory, and supplies `linear_records` only when every member has a proven,
unique ordinal and logical occurrence coordinate. A validated native parent
fact may establish `native_tree` fidelity without establishing sibling order;
otherwise proven order is `source_order` and ambiguous order is `unordered`.

Those labels do not assert completeness, revision, connectivity, acyclicity, a
root, an active leaf, branch selection, or transcript order. The grouping layer
does not choose representatives or claim that its observed subset is the full
conversation. Complete-source loading and export policy belong to issue #81.

### Privacy and upstream schemas

The handles are pseudonymous rather than secret. They hide raw native values,
but stable equality remains visible, and low-entropy inputs are
dictionary-guessable. They are not secrets and not authentication. They provide
pseudonyms, not anonymization; callers must not use them as access-control
credentials or as proof that two people are the same.

Adapters populate only fields seen in the stores they read.
The observed upstream schemas are not stable APIs. An upstream rewrite can change
`source_order` occurrence IDs or make a previously available native anchor
unavailable. Missing evidence remains null instead of being reconstructed from
filenames, timestamps, cwd, branches, or containment guesses.

## Consequences

Repeated equal text has one `content_id` while distinct stored turns can have
different `record_id` values. Duplicate views of one native occurrence can
share all three canonical IDs without losing their distinct physical refs.
Flat stores remain useful through content identity and physical refs even when
thread and occurrence identity are null.

The public machine surfaces always carry the four identity fields, with nulls
where the source cannot support a claim. Human detail surfaces render the same
full handles. Cryptographic work stays at surviving output or detail boundaries
rather than the discovery and scan hot path.

Source-order IDs are deliberately less durable than native IDs. Consumers that
persist them must keep `record_id_stability` and a physical ref or content
fallback appropriate to their own policy.

## Rejected alternatives

- **One universal ID:** content equality, occurrence identity, thread grouping,
  and physical resolution have different invariants.
- **Short or auto-widening prefixes:** corpus-relative uniqueness is unstable,
  ambiguous, and unsuitable for machine interchange.
- **Path-, mtime-, or cwd-derived identity:** moving a store or checkout would
  change identity, and unrelated flat files could appear to be conversations.
- **Fabricated threads for flat stores:** absence of topology is information;
  replacing it with a path makes downstream completeness claims unsafe.
- **Cryptographic dedupe keys:** hashing every scan candidate adds work without
  improving the coordinate-aware membership policy.
- **Changing ref version 1:** physical resolution remains a separate,
  compatibility-sensitive job.
- **Merkle or complete-conversation identity:** the default search result set
  cannot prove revision completeness, active branches, or transcript order.

## Related ADRs

- {ref}`ADR 0004 <adr-headless-query-planning-non-blocking-execution>` owns the
  headless planning, driver, and event-stream architecture.
- {ref}`ADR 0006 <adr-public-cli-mcp-surface-contract>` owns CLI and MCP surface
  parity and compatibility.
- {ref}`ADR 0010 <adr-module-boundaries-and-facade-re-export-contract>` owns
  dependency direction and deferred imports.
- {ref}`ADR 0011 <adr-non-blocking-tui-invariants>` owns the Textual pump and
  worker boundary used to prepare HUD handles.
