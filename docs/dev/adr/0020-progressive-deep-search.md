(adr-progressive-deep-search)=

# ADR 0020: Progressive deep search

## Status

Proposed.

## Context

Searching human prompts and searching every message in every conversation are
different operations. Prompt search can use a compact purpose-built corpus.
Conversation search may need to discover, open, decode, and traverse large
origin stores and related sidecars.

Making both operations implicit in normal search gives common lookups the
latency, I/O, and cancellation behavior of the most expensive source. Making
conversation search unavailable by default, however, can hide useful content
that exists only in assistant responses or tool output.

The product therefore needs a fast default, a heuristic deep search with
bounded conversation attempts, and an explicit exhaustive escape hatch. Their
guarantees must be consistent across the library, CLI, TUI, MCP, JSON, and
NDJSON.

## Decision

Search requests have a frontend-neutral effort value:

| Effort | Surfaces examined | Completeness contract |
| --- | --- | --- |
| `prompt` | Admitted prompt evidence | Exact for the declared query over reported covered prompts |
| `targeted` | Prompt evidence plus heuristically selected conversations | Globally approximate; exact matching within selected conversations |
| `exhaustive` | Prompt evidence plus every eligible readable conversation, except work proved unable to affect the requested ordered result | Exact for the declared query over reported readable coverage |

`prompt` is the default for new requests. Exactness is always qualified by
reported coverage: unsupported, unavailable, suspect, private, or deliberately
excluded content does not become searchable merely because effort increases.

If adopted, this ADR amends ADRs 0004, 0006, and 0014 only where they describe
public pagination as a chain-wide `result_limit` plus a separate `page_size`.
The public contract below uses `limit` as the per-response cap. ADR 0014's
collector ownership, canonical ordering, representative selection, and
proof-bearing early-stop requirements remain unchanged.

### Effort and scope are separate

Scope determines which normalized record kinds may appear. Effort determines
how much eligible source material agentgrep reads. Neither is a hidden alias
for the other.

For compatibility, a current-schema request that omits effort and has any scope
capable of admitting conversations uses exhaustive effort. Omitted effort with
omitted or provably prompt-only scope uses prompt effort. Compound or negated
inline scope that cannot be proved prompt-only normalizes conservatively to
exhaustive.

With explicit effort, prompt effort accepts only prompt-only scope; targeted
and exhaustive effort accept conversation or all scope. An incompatible
combination is a validation error. A separately requested scope change may be
offered as a next action, but it is never applied during normalization. This
rule applies equally to dedicated scope fields and inline query scope. A future
request-schema version may support additional combinations, but it must not
reinterpret an existing request silently.

CLI `search` and `grep` expose `--deep` for targeted effort and `--exhaustive`
for exhaustive effort. They are standalone, mutually exclusive selectors;
exhaustive effort does not require targeted effort first. Public surfaces may
choose other idiomatic spellings, but all normalize to the same effort values.

Search effort applies only to query-to-record search. Similarity, export,
bookmarks, insights, synchronization, index management, model provisioning,
and enrichment own separate controls. Increasing effort authorizes additional
eligible reads only. It does not authorize durable writes, index construction,
model loading or download, embedding generation, enrichment, or expanded
retention.

### Escalation is explicit

The planner never raises effort because a stage returned few results, no
results, weak candidates, stale references, or a timeout. In particular, an
empty targeted result does not trigger an exhaustive sweep.

Results may offer structured next actions to raise effort or broaden an
explicit scope. Applying one starts a new normalized request. It preserves the
query and compatible filters, and it requires caller confirmation before
broadening an explicitly prompt-only scope. Frontends own the exact wording and
presentation of these actions under ADR 0006.

### Coverage and lifecycle stay honest

A normally completed targeted search reports `approximate` because heuristic
selection can omit a matching conversation. Exact matching inside selected
conversations, a full response page, or an empty candidate set does not change
that global claim.

Prompt and exhaustive efforts report completeness only over their declared
covered sources. Gaps, failures, unavailable origins, unsupported adapters, and
excluded content remain visible through the run-status and coverage contract
owned by ADR 0004. This ADR does not define a competing status precedence.

All stages belong to one logical request with shared cancellation and one final
collector. A caller cancellation or whole-request deadline stops the shared
operation. A provider or source timeout remains a typed operation outcome; it
does not masquerade as caller cancellation. Deterministic work bounds define
planned approximation. Wall-clock timing does not silently redefine the
candidate universe.

Prompt and conversation results pass through the same collector. Routing and
stage boundaries never own final matching, deduplication, representative
selection, ordering, or pagination.

### `limit` is a per-response cap

Public `limit` means the maximum canonical post-deduplication results returned
in one response or page. The target default for the new pageable request
contract is **25** when no explicit value is supplied. The current MCP omission
default is 20 and the current CLI has no omission default, so moving either
surface to 25 is an intentional compatibility change. It requires a versioned
migration or explicit superseding decision; adoption does not pretend the
legacy defaults were already 25.

The default is policy, not identity or query semantics. It may be tuned later
only through the same compatibility process and measurement of user-visible
behavior.

A cursor continues strictly below the prior page's last canonical key. It does
not replenish a chain-wide result budget because this decision defines no such
budget. If a future product needs a total-result ceiling across a cursor chain,
that ceiling receives a distinct public name and focused compatibility
decision; it must not overload `limit`.

The same per-response meaning applies to CLI `--limit`, compatible grep
aliases, JSON, and MCP `limit` after the applicable migration. Existing
cursorless calls remain one-page calls. Introducing continuation must not
silently truncate the logical query after the first page.

The response cap is independent of source, provider, candidate, routing,
operation, byte, and transcript-work bounds. A full page is not proof that
another result exists, that work is complete, or that targeted selection was
exhaustive.

### Continuations preserve the decision

A cursor binds the normalized request, effort, order, validated evidence
snapshot, matching and deduplication contracts, and enough continuation state
to prevent gaps, duplicate logical records, or representative drift.

A targeted cursor also binds the fixed bounded routing decision. A later page
does not reroute, widen the conversation budget, backfill from new evidence, or
move to a newer corpus or provider generation. If the required state is stale,
expired, unavailable, or invalidated, continuation fails explicitly rather
than substituting a new search.

ADR 0014 owns the canonical ordered sequence and proof requirements. The
prompt-corpus ADR owns evidence freshness. The routing ADR owns which bounded
conversation decision a targeted cursor fixes. Concrete token encoding,
storage, expiry, and resource budgets are implementation or focused protocol
decisions.

### Discoverability

Interactive CLI and TUI surfaces make deeper search discoverable after prompt
search without polluting machine-readable result streams. Structured surfaces
expose effort, approximation, coverage, and available next actions. ADR 0006
owns exact flags, fields, channels, and copy.

## Relationships

- ADR 0004 owns planning, execution lifecycle, cancellation, status, and
  coverage envelopes.
- ADR 0006 owns public spellings and structured next-action shapes.
- ADR 0014 owns canonical result order, deduplication, representative choice,
  and continuation sequence.
- {ref}`adr-durable-prompt-corpus-derived-search-indexes` proposes the durable
  prompt and freshness boundary.
- {ref}`adr-prompt-guided-conversation-routing` proposes the targeted
  conversation-selection policy and its independent work bound.

## Consequences

Ordinary search avoids transcript-wide I/O while deeper content remains
reachable through explicit effort. Targeted search is useful without being
misrepresented as complete, and exhaustive search remains available when
coverage matters more than latency. A single per-response `limit` gives CLI and
MCP pagination one meaning.

Users and integrations must understand three effort levels, and existing
conversation-scoped requests retain their expensive behavior until callers opt
into targeted effort. Stable targeted pagination requires retained snapshot
state. Exhaustive search can remain expensive because this decision exposes,
rather than removes, that cost.

## Rejected alternatives

**Search every conversation by default.** This makes common prompt lookups pay
the deepest source cost.

**Automatically escalate weak or empty searches.** This hides work and turns a
bounded request into an unrequested sweep.

**Treat targeted matching as globally exact.** Exact confirmation inside a
selected conversation cannot account for conversations the router omitted.

**Use one `limit` for page size and unrelated work budgets.** Result delivery
and routing cost have different meanings and must remain independently
observable and tunable.
