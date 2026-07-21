(adr-result-order-limit-and-streaming-merge)=

# ADR 0014: Result order, limit, and the streaming merge contract

## Status

Accepted as a contract; not yet implemented. The defect and engine work remain
tracked in [#113](https://github.com/tony/agentgrep/issues/113).

## Context

The existing engine can stop after accepting a requested count and only then
sort the surviving records. A record discarded before global comparison can be
newer or more relevant than every retained record. That is not a sorting bug:
ordering and bounding were assigned to different layers.

Concurrent or indexed source streams add another risk. Arrival order can choose
which duplicate view survives or which record fills the final slot unless one
collector owns the global semantics.

## Decision

Source selection precedes one collector-owned result stage. That stage owns
canonical matching inputs, representative selection, cross-source
deduplication, declared order, stable emission, and response bounding.

### Coverage precedes ordering

The planner declares the admitted source universe and whether its coverage is
complete or approximate. An exact or exhaustive plan omits a source only with
proof that it cannot affect the declared result. A targeted plan may select an
incomplete universe, but routing or candidate bounds never justify a complete
claim.

Within the admitted universe, no source or frontend may apply a count cutoff in
an order different from the caller's declared order.

### Order and representative choice are one stage

`order` is a normalized request value and is echoed in structured results. The
collector chooses each deduplication representative through one deterministic
policy over the request snapshot. A driver cannot keep the first physical view
it encounters while another driver selects a different view.

The core order vocabulary is:

- `newest`: a stable newest-first total order;
- `relevance`: a versioned score with a stable total-order tiebreak; and
- `scan`: the explicit physical-plan encounter order, never the default.

Frontends may group, highlight, redact, or truncate display, but they do not
reorder or re-bound the semantic response.

A future scorer or origin-weight policy must be explicit and versioned.
Provider choice, indexed-versus-live provenance, freshness, transport, and
arrival time never become hidden score terms.

### Early emission and stopping require proof

A collector may emit a stable prefix or stop work only when it can establish
both of these facts:

1. no unseen admitted record can outrank the retained frontier under the
   declared order; and
2. no unseen physical view can replace the chosen representative of a retained
   or emitted deduplication class.

A full response, accepted-candidate count, source-local cap, or routing budget
is not that proof. If proof is unavailable, the collector drains the admitted
work or reports the approximation or truncation honestly.

Filesystem modification time may guide scheduling, but it is not proof of
record recency; restores and clock skew can invert it. If `newest` ordering or a
frontier bound relies on mtime without adapter-owned proof, the run reports
`approximate`, and mtime cannot justify an exact early stop.

### Transport does not own semantics

Inline, thread, process, worker, native, asynchronous, and provider transports
may all implement the execution-driver contract. They produce locally ordered
typed batches or outcomes for one semantic collector. “Single owner” means one
logical authority, not necessarily one synchronous thread or merge algorithm.

For the same request, validated snapshot, and equivalent source outcomes,
transport and scheduling do not change logical membership, representative
selection, order, status, coverage, or deterministic work accounting. Progress
timing, opaque tokens, and physical-performance diagnostics may differ.

### Pagination follows one canonical sequence

A core search cursor traverses the collector's canonical post-deduplication
sequence. Its order key is stable, collision-free within the validated request
snapshot, and independent of arrival order. Continuation resumes strictly after
the prior page's last logical key and preserves enough deduplication and
representative state to prevent gaps, duplicate classes, or representative
drift.

The cursor binds the normalized request, semantic contract versions, and a
snapshot that can be preserved or revalidated for its lifetime. Stale or
invalid state fails explicitly; continuation never substitutes a newly routed
or newly indexed search.

Core live-search keyset pagination is initially limited to `newest`. Relevance
pagination requires a focused contract over an immutable or materialized ranked
sequence. Similarity, export, and persisted reports may define their own
versioned orders and cursors without changing core search.

If {ref}`adr-progressive-deep-search` is adopted, public `limit` becomes the
per-response cap and each cursor continues the same canonical sequence below
the prior key. That ADR owns the target default and migration. Adoption
explicitly amends the older interpretation of a result limit as a chain-wide
top-k. A future total cap must use another name. Until that proposal is
adopted, released surface behavior remains a compatibility fact.

A full page alone does not prove another page exists. Status and cursor
availability remain separate: an exactly exhausted boundary is complete, while
a response with a proven continuation reports its page bound without hiding a
higher-priority approximate, truncated, cancelled, or failed state.

### Freshness is supplied, not inferred by the collector

The collector merges sorted streams without treating their physical origin as
rank. Adapters own native source-observation proof. If adopted, the durable
prompt-corpus ADR owns exact-index freshness, current/live/gap partitioning,
activation, coverage, and fallback; providers own only their evidence encoding.
Without a current index, every admitted source can participate through the live
path.

## Relationships

- If adopted, ADR 0003 owns native and worker boundary classification, ADR
  0004 owns planning and lifecycle, and ADR 0006 owns public spelling and
  schema adaptation. This Accepted collector contract does not depend on their
  adoption.
- ADR 0011 owns non-blocking TUI delivery.
- Proposed corpus, progressive-search, and routing ADRs may supply freshness,
  page, and approximate source-selection decisions without changing the one
  collector.

## Consequences

Ordering, deduplication, representative selection, and response bounding become
one testable semantic stage. Parallel or indexed execution can improve work
without making results arrival-dependent, and pagination can continue a stable
logical sequence instead of slicing a rerun.

The collector may need more state and may have to inspect more work than the
current accepted-count stop. Relevance pagination is deferred until it can be
grounded in an immutable ranked sequence. These costs are preferable to
returning the wrong top result or a cursor with gaps and duplicates.
