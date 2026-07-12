(adr-portable-record-export)=

# ADR 0017: Portable record export

## Status

Accepted.

## Context

Search results are useful outside agentgrep, but source histories are not an
interchange format. They contain backend-specific paths, metadata, revisions,
and partial conversation views. Copying those records wholesale would expose
more local context than the user selected and would make every upstream schema
part of agentgrep's compatibility surface.

{ref}`ADR 0015 <adr-deterministic-record-identity>` supplies portable content,
occurrence, and thread handles. Export needs a narrower contract on top: a
deterministic allowlist, honest observed-thread labels, explicit body policy,
and output rules that can never replace an agent's source store.

## Decision

### Formats and payload

The initial contract has exactly two formats: `ndjson` and `markdown`. Both
render the same ordered records and the same allowlisted values:

| Field | Meaning |
| --- | --- |
| `schema_version` | Version of the normalized record schema |
| `agent` | Source agent family |
| `store` | Stable store identifier |
| `kind` | Normalized record kind |
| `role` | Normalized role, or null |
| `timestamp` | Recorded timestamp, or null |
| `model` | Recorded model, or null |
| `content_id` | Canonical content handle |
| `record_id` | Canonical logical-occurrence handle, or null |
| `record_id_stability` | `native`, `source_order`, or null |
| `thread_id` | Canonical observed-thread handle, or null |
| `text` | Exact normalized body, only when bodies are included |

The allowlist excludes raw and source paths, display paths, adapter metadata,
origin and working directories, repository paths, titles, session IDs,
conversation IDs, physical refs, and arbitrary record metadata. Canonical IDs
are pseudonymous equality handles rather than anonymization; omitting a body
does not make the remaining activity metadata secret.

NDJSON is canonical: one canonical JSON object per line, stable key order,
compact separators, and a final newline. ASCII JSON escaping keeps the byte
stream valid UTF-8 and preserves lone surrogates as JSON escapes.

Markdown emits human-readable allowlisted metadata and, when requested, the
exact body. It first validates every emitted value as UTF-8 and rejects lone
surrogates with a path-free encoding error rather than replacing or dropping
them. Each body uses a dynamic backtick fence longer than every backtick run in
that body, so valid UTF-8 text is preserved without changing fence semantics.

### Deterministic selection and observed threads

Record exports accept zero or more normalized records. Output is independent
of input permutation: the total order compares canonical thread ID, timestamp,
record ID or content ID, content ID, and finally the complete canonical
allowlisted payload, with missing values ordered explicitly. This inventory
order is deterministic; it does not claim chronology.

A thread export requires every record to share one non-null canonical thread
ID. The artifact calls the selection an **observed thread** and carries one of
the conversation fidelity labels defined by ADR 0015:

- `native_tree` means native parent facts establish observed tree structure;
  it does not establish sibling order.
- `source_order` means unique comparable source ordinals establish a linear
  order for the observed records.
- `unordered` means only deterministic inventory order is available.

An observed thread does not claim completeness, does not claim chronology,
and does not assert a root, active leaf, revision, connectivity, or chosen
branch. Fidelity describes the available source evidence; it does not replace
the artifact's deterministic inventory order. Export serializes the selected
view and does not rescan a backend to invent a complete conversation.

### Surface defaults

Defaults express the authority of each caller:

| Surface | Selection | Format | Bodies | Sink |
| --- | --- | --- | --- | --- |
| CLI | Search matches, up to 100 by default | NDJSON | Included | Standard output |
| TUI | Selected record, or explicit observed thread | Markdown | Included | Private export directory, or explicit path |
| MCP | One to 20 existing search refs | NDJSON | Excluded | One bounded inline response |

The CLI accepts limits from 1 through 1000 and an explicit `-o -` standard
output sink. A file refuses overwrite unless the user supplies `--force`.

The HUD commands `/export [PATH]` and `/export-thread [PATH]` default to private
Markdown files with bodies. The latter selects only records in the current
filtered result set whose canonical thread ID matches the selected record.
Identity, rendering, and disk work run off the Textual message pump. Only one
accepted write may be pending, and a changed result snapshot cancels an
observed-thread export instead of writing a mixed view.

The MCP {tooliconl}`export_records` tool accepts one to 20 unique `agref1:`
search refs and no query, cursor, or local destination. It resolves refs with
the same position-aware and historical compatibility semantics as
{tooliconl}`inspect_result`, rejects duplicate physical selections, and returns
one `TextContent` artifact plus structured metadata. Bodies default to false
and require `include_bodies=true`. The UTF-8 artifact is capped at 400 KiB and
must also fit the server's response-envelope limit. {tooliconl}`search` owns
discovery and pagination.

Each opaque search ref is limited to 49,152 characters (48 KiB). Linux
`PATH_MAX` leaves 4,095 path bytes after its trailing NUL; worst-case JSON
escaping expands those bytes sixfold and base64url adds another four-thirds,
for 32,760 characters before the versioned envelope. Both MCP consumers
enforce the ceiling before token decoding or source discovery, and audit
redaction hashes only a bounded prefix of oversized sensitive inputs.

### Durable file output

Explicit file output uses a same-directory private temporary file. Complete
writes precede synchronization of the file and parent directory. A fresh
destination uses an atomic no-clobber install. Only explicit force replaces an
existing regular file atomically.

Every path component is inspected without following links. The writer refuses
symlink destinations, symlinked parent traversal, non-regular destinations,
and source-store aliases. Alias detection covers lexical, resolved, and
same-inode relationships. CLI file output protects all discovered stores, not
only matching records; a TUI explicit path protects the selected snapshot's
sources.

The TUI-owned default export directory is mode `0700`, and artifact and
temporary files are mode `0600`. Private filenames derive only from canonical
IDs and structural metadata, never prompt text, a title, or a source path, and
collisions allocate a new name rather than replacing an older export. Errors
remain path-free.

### Deferred tiers

The initial feature adds no new dependency. HTML, CSV, Mermaid, provider
training profiles, and re-import stay deferred; nested metadata or richer
topology does not silently enter one of the portable formats.

| Tier | Why it remains deferred |
| --- | --- |
| HTML | Rich rendering needs an explicit sanitization and embedded-resource policy rather than inheriting Markdown trust. |
| CSV | Nested metadata, nullable identity, and multiline bodies need a documented lossless tabular projection. |
| Mermaid | A graph would make stronger topology and completeness claims than an observed result set can support. |
| Provider training profiles | Provider-specific schemas add provider coupling and need separate redaction and consent review. |
| Re-import and revision selection | Writing or reconciling history needs a versioned trust model, provenance checks, and conflict policy. |

These formats can build on the allowlist only after their added semantics are
specified; they are not aliases for the two accepted formats.

## Consequences

CLI scripts get stable NDJSON, people get readable Markdown, the HUD can save a
selection without freezing, and MCP clients can request a bounded artifact
without gaining filesystem write authority. All three surfaces use one
renderer, so privacy and ordering do not drift by frontend.

The narrow contract intentionally leaves presentation richness and round-trip
editing out. Thread exports describe an observed unit rather than a complete
conversation, Markdown rejects text it cannot preserve as UTF-8, and file
destinations trade convenience for source-store safety.

## Related ADRs

- {ref}`ADR 0004 <adr-headless-query-planning-non-blocking-execution>` owns the
  shared search engine and frontend-neutral result flow.
- {ref}`ADR 0006 <adr-public-cli-mcp-surface-contract>` owns CLI and MCP public
  surface parity.
- {ref}`ADR 0011 <adr-non-blocking-tui-invariants>` owns the Textual pump and
  worker boundary.
- {ref}`ADR 0015 <adr-deterministic-record-identity>` owns canonical IDs and
  observed conversation fidelity.
