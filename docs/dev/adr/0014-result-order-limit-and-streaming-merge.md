(adr-result-order-limit-and-streaming-merge)=

# ADR 0014: Result order, limit, and the streaming merge contract

## Status

Accepted as a contract; not yet implemented. The defect that forced it and the
engine work remain tracked in [#113](https://github.com/tony/agentgrep/issues/113).
A focused order/limit regression is required with that implementation.

## Context

`agentgrep search --limit N` describes itself as "Limit the number of results
after ranking". It is not that. The driver stops collecting once the cap is
reached while it walks sources in source-mtime order, and the newest-first sort
is a post-hoc pass over whatever survived the cutoff. Two Codex sessions whose
file mtime and record recency disagree are enough to expose it: with `--limit 1`
the record from the newer-mtime source is returned even though the older-mtime
source holds the newest prompt. The genuinely newest record is discarded before
the ranker ever compares it.

That is not a sorting bug. It is a layering error — **ordering was treated as a
frontend post-pass over a set the engine had already truncated.** A limit is
only meaningful relative to a declared order, so the two cannot live in
different layers.

Three further facts frame the fix.

- The engine has no notion of order. `search` ranks by relevance in the
  frontend after collection, `grep` and the TUI want recency, and the collector
  itself yields in scan order. Nothing in the request names the desired list, so
  nothing in the result can name the list the caller got.
- MCP pagination is an offset over a re-run scan: each page re-executes the
  search with an inflated limit and slices the sorted buffer. It inherits the
  defect above and pays a full rescan per page.
- The concurrency question is already answered by the shipped code rather than
  by taste. `ExecutionDriverConfig.max_workers` defaults to `1`, and nothing
  outside the tests raises it, so collection runs single-threaded today.

[#100](https://github.com/tony/agentgrep/issues/100) surveyed twelve search and
observability engines — Lucene, OpenSearch, tantivy, Meilisearch, fzf, Chroma,
DataFusion, ClickHouse, Prometheus, Loki, Tempo, Pyroscope — and found one
recipe under all of them: concurrent per-source fan-out that returns *locally
sorted, bounded* output; a stable total-order key whose tiebreak is intrinsic
and never arrival time; a completeness barrier before the global minimum is
released; and a bounded top-k that lets a source stop early. This ADR adopts
that recipe and settles the two decisions #100 deliberately left open — the
ordering axis of the stream, and what defines the live watermark.

It also closes the tradeoff {ref}`ADR 0004
<adr-headless-query-planning-non-blocking-execution>` left standing:
*"deterministic ordering and dedupe need explicit merge rules once execution
becomes concurrent."*

## Decision

Six invariants govern result order and bounding (OL for *order and limit*), in
the enumerated style of {ref}`ADR 0011 <adr-non-blocking-tui-invariants>`.

- **OL-1 — Source selection precedes the one order-and-limit stage.** The
  planner first declares the source universe and whether it claims complete or
  approximate coverage. An exact or exhaustive plan may omit a source only
  when it can prove that source cannot affect the result. A targeted plan may
  deliberately select an incomplete universe, but the run remains
  `approximate`; a routing or candidate budget is not a result limit and never
  justifies `complete` or `bounded`. Within the admitted universe, no layer may
  apply a count cutoff in an order other than the order the caller declared.
  Every bounded source scan, frontier skip, or page fill must be justifiable as
  *"no unexamined record in the admitted universe can outrank the k-th record we
  already hold, under the declared order"*. A stop rule that cannot state that
  invariant does not run. Where recency is inferred from file mtime rather than
  record timestamps, the invariant itself is approximate and the run reports
  `approximate` (ADR 0004's run-status vocabulary).
- **OL-2 — `order` is a request parameter, executed in the collector, and
  echoed on the result.** The vocabulary is fixed below. `limit` becomes a
  bounded top-k *under the declared order* — a k-sized frontier the collector
  maintains while it merges sources — not a cutoff on collection. The applied
  order travels back on the result payload and on the streaming summary, because
  a caller that cannot name the order it received cannot page it, diff it, or
  cache it. A frontend may still *present* records differently (grouping,
  highlighting, truncation), but it may not reorder or re-truncate the set.
- **OL-3 — asyncio is a boundary protocol, never the engine's concurrency
  model.** The engine's inner loop is CPU-bound under the GIL: `json.loads` →
  field extraction → casefold → substring test. An event loop cannot make that
  work finish sooner; it can only interleave it. asyncio's job at the edges is
  real and stays: `aiter_search_events` already offloads the synchronous engine
  with `asyncio.to_thread` and pumps events through a bounded `asyncio.Queue`,
  which is what keeps the MCP server and the Textual pump responsive
  ({ref}`ADR 0011 <adr-non-blocking-tui-invariants>`). That is a *protocol* for
  delivering results without blocking a loop, not a strategy for computing them
  faster. The merge itself stays single-owner and synchronous, so the emitted
  order is a function of the records, never of arrival time.
- **OL-4 — Parallelism comes from OS threads, and its ceiling is the
  interpreter build.** Source scans fan out over `concurrent.futures` threads;
  the collector merges their locally sorted output on the owner thread. Under
  the default build the GIL caps the win, and measurement bears that out — a
  thread pool over local JSONL stores has measured *slower* than inline
  collection, which is why the shipped worker count is `1` and why ADR 0004
  already records batch queueing as a loss. The lever is therefore the
  interpreter, not the architecture: the same thread fan-out that wins nothing
  under the GIL scales on a free-threaded build ([PEP
  703](https://peps.python.org/pep-0703/), supported since CPython 3.14 per [PEP
  779](https://peps.python.org/pep-0779/)) with no code change. Design for
  threads; let the build decide the throughput.
- **OL-5 — Cursor pagination is defined only for `order="newest"`.** The cursor
  is a keyset anchored on `(timestamp, agent, stable_source_order_key,
  stable_record_coordinate)`. The final two components are deterministic,
  snapshot-stable and jointly injective for every record in the request's
  source snapshot; the existing `(timestamp, agent, path)` prefix alone is not
  a total record order. A page resumes *strictly below* the full last-emitted
  key, so equal timestamps and records from one source do not disappear between
  pages. Every cursor also binds the normalized query and a source snapshot that
  can be preserved or validated for its documented lifetime. When an adapter
  cannot provide a collision-free coordinate or the snapshot is stale, the
  result returns no cursor or rejects continuation; it never substitutes a new
  snapshot as the next page. Keyset cursors and relevance ordering do not
  compose — a relevance score is a function of the query and the corpus, not a
  position in a total order, and a stable score has no successor to resume from.
  A relevance-ordered request therefore returns **no cursor**: it reports
  `bounded` with a diagnostic that names the order as the reason. Callers who
  want to page rank by recency; callers who want relevance ask for one bounded
  top-k.
- **OL-6 — The tier interface is decided now; the tier policy is deferred.**
  The collector merges *sorted source streams*; whether a stream is a live file
  scan or an index cursor is invisible to it. That interface — one merge, N
  sorted inputs, one total-order key, one barrier — is settled by this ADR. What
  defines the live watermark (file mtime past the index build, an ingest
  opstamp, an explicit coverage flag) is **not** settled, and must not be, until
  an index exists to have a watermark. With no index every admitted source is
  live, the barrier is trivially satisfied, and the design degrades cleanly to
  today's behavior.

A cursor over a targeted source universe has one additional constraint: every
page continues the same routing decision and snapshot. If that state cannot be
preserved or validated, the result has no cursor; a continuation never reruns
routing and presents a different source universe as the next page.

### Order vocabulary

| `order` | Key | Limit means | Cursor |
| --- | --- | --- | --- |
| `newest` (default) | `(timestamp, agent, stable_source_order_key, stable_record_coordinate)`, descending | the k newest matching records | keyset over a validated snapshot (OL-5) |
| `relevance` | score, then the `newest` key as tiebreak | the k best-scoring matching records | none |
| `scan` | source order, then record order | the first k records the plan encountered | none |

`scan` is the honest name for "whatever the plan happened to reach first". It is
useful for `grep`-shaped streaming and for profiling, and it is the only order
in which a caller may assume nothing about global rank. It is never a default.

## Prior art

Two upstream systems answer OL-3 and OL-4 directly, and both were read at the
pinned ref below.

[ripgrep](https://github.com/BurntSushi/ripgrep/blob/15.1.0/crates/core/main.rs)
(tag `15.1.0`) is the closest analogue: a local, CPU-bound, filesystem-wide
search. It runs its parallel search on OS threads —
[`build_parallel`](https://github.com/BurntSushi/ripgrep/blob/15.1.0/crates/ignore/src/walk.rs)
over the directory walk — with no async runtime anywhere. More to the point for
the order-and-limit contract: when the user asks for a *sorted* output, ripgrep
disables parallelism outright rather than merging out-of-order results
afterward. Ordering is a property of the execution stage, not a post-pass
bolted onto it.

[aiosqlite](https://github.com/omnilib/aiosqlite/blob/v0.22.1/aiosqlite/core.py)
(tag `v0.22.1`) is the canonical shape of OL-3: its `Connection` is a worker
`Thread` fed by a `SimpleQueue`, and each coroutine awaits a future the thread
resolves. The async surface is a delivery protocol over a threaded core — which
is exactly what `aiter_search_events` already is, and exactly what it should
remain.

## Relationship to other ADRs

{ref}`ADR 0004 <adr-headless-query-planning-non-blocking-execution>` owns
planning, execution, the event stream, and the run-status/result vocabulary.
This ADR resolves the merge-rules tradeoff it left open and adds `order` to the
request and the result; the layering, the driver protocol, and the sink boundary
are unchanged. {ref}`ADR 0011 <adr-non-blocking-tui-invariants>` is untouched:
OL-3 keeps the pump-facing async bridge exactly where it is. {ref}`ADR 0006
<adr-public-cli-mcp-surface-contract>` governs how `order` and the echoed
applied order surface on the CLI and MCP payloads. {ref}`ADR 0003
<adr-native-boundary-execution-architecture>` is not invoked: nothing here
approves native code, and OL-4's throughput lever is an interpreter build, not a
Rust engine. {ref}`adr-durable-prompt-corpus-derived-search-indexes` may
contribute sorted exact-index and live-source streams.
{ref}`adr-progressive-deep-search` owns targeted cursor policy.
{ref}`adr-prompt-guided-conversation-routing` may select an explicitly
approximate conversation universe before this ADR's single collector runs;
routing scores and candidate budgets do not alter the collector's order or
result limit.

## Consequences

The engine gains a declared order it can be held to, and `--limit` starts
meaning what its help text has always claimed. Ordering, dedupe, and bounding
become one testable stage with one total-order key, so an inline driver and a
threaded driver can be asserted to produce byte-identical lists. Pagination
stops rescanning from zero. And the throughput story becomes a build choice
rather than an async rewrite.

The costs are real. `order` is a new public request parameter, so CLI, MCP, and
library surfaces each grow a field and each owe it a test. A bounded top-k
frontier is more state than a counter. Relevance requests lose pagination
outright — a deliberate loss, since the alternative is a cursor that silently
returns a different list each page. The tier barrier adds a wait that, with no
index, never triggers; it must not be allowed to rot untested in the meantime.

The chief risk is a frontend quietly re-truncating an ordered list — the very
mistake this ADR exists to correct. The mitigation is OL-2's echoed order plus
the required focused regression, which must fail loudly when the engine's order
and limit disagree.

## Final position

A limit without a declared order is a lie, and agentgrep has been telling it.
Order and limit are one stage in the collector; asyncio delivers results without
blocking a loop and computes nothing; threads carry the parallelism and the
interpreter build sets its ceiling; cursors exist where a total order exists and
nowhere else; and the live-versus-indexed split is one more sorted input into
the same merge, whose policy waits for an index worth having.
