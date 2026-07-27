(library-event-stream)=

# Event-stream engine

agentgrep's search and find engines produce **typed event streams** —
sync generators that yield pydantic discriminated-union events as
they walk the user's stores. The same producer feeds the CLI's live
output path, the Textual TUI's worker, and the MCP server's response
collector. Three frontends, one engine.

## Why a stream

A short scan completes before the user notices. A long one — broad
patterns, deep history, slow stores — can take seconds. The legacy
list-return path ({func}`~agentgrep.run_search_query`) buffers every
match until the scan finishes, then returns the list. That hides the
engine's progress from the consumer and forces a "wait, then dump" UX
in the CLI.

The event stream solves both:

- **Per-record delivery.** Scan-ordered grep can emit
  {class}`~agentgrep.events.RecordEmitted` as the collector accepts each
  match. Relevance and global-newest requests may buffer records until the
  global frontier is known; their ordering guarantee takes priority over early
  display.
- **Single source of truth.** Search progress (which source is
  active, how many records seen / matched) and the matches
  themselves are the same event stream, not two parallel side
  channels.
- **Decoupling.** The engine doesn't know about stdout, Textual, or
  fastmcp. It yields events. Consumers translate.

## Architecture

::::{mermaid}
:caption: The engine yields one typed stream for search and find.
:alt: agentgrep engine yielding typed search and find events
:name: event-stream-engine-diagram
:responsive: fit

flowchart TD
    engine["agentgrep._engine"]:::cmd
    iterator["iter_search_events"]:::cmd
    events["SearchEvent / FindEvent"]:::cmd

    started["Started envelope"]
    source["Source progress"]
    record["Record emitted"]
    finished["Finished envelope"]

    engine --> iterator
    iterator --> events
    events --> started
    events --> source
    events --> record
    events --> finished
::::

The stream is the contract. Each frontend consumes the same events and
chooses how to present records, progress, and completion.

::::{mermaid}
:caption: Frontends consume the same event stream.
:alt: typed event stream feeding CLI, Textual, and MCP consumers
:name: event-stream-consumers
:responsive: fit

flowchart TD
    events["SearchEvent / FindEvent"]:::cmd
    source["Source progress"]
    record["Record emitted"]
    finished["Finished envelope"]
    cli["CLI live output"]
    tui["Textual worker"]
    mcp["MCP collector"]

    events --> source
    events --> record
    events --> finished
    record --> cli
    record --> tui
    record --> mcp
    source --> cli
    source --> tui
    finished --> cli
    finished --> tui
    finished --> mcp
::::

### Sync producer

The engine is a synchronous generator. Sync consumers iterate
{func}`~agentgrep.iter_search_events` directly. Async consumers use
{func}`~agentgrep.aiter_search_events`, which runs the producer in a worker
thread and transfers events through a bounded queue.

### Pydantic events

Events are frozen {class}`pydantic.BaseModel` subclasses tagged with a
`Literal["..."]` discriminator field. The union types
{data}`~agentgrep.events.SearchEvent` and
{data}`~agentgrep.events.FindEvent` carry
{func}`pydantic.Field` ``(discriminator="type")`` so runtime
validation routes each payload to the correct variant and `isinstance`
narrowing works in consumer loops.

Events embed agentgrep's existing
{class}`~agentgrep.SearchRecord` / {class}`~agentgrep.FindRecord`
dataclasses directly via `arbitrary_types_allowed=True`. Consumers
read record attributes without an extra conversion step. Transport-
layer consumers (a future HTTP SSE endpoint, for example) should
serialise records through
{class}`~agentgrep.mcp.models.SearchRecordModel` /
{class}`~agentgrep.mcp.models.FindRecordModel` at the boundary so
the dataclass-typed field doesn't block
{meth}`pydantic.BaseModel.model_dump_json`.

## Search events

The {data}`~agentgrep.events.SearchEvent` union has five members.
Their guaranteed sequence:

::::{mermaid}
:caption: Search starts once, pairs attempted sources, and finishes once.
:alt: search event partial order with source pairs and ordered record delivery
:name: search-event-sequence
:responsive: fit

flowchart TD
    search_started["SearchStarted"]:::cmd
    source_started["SourceStarted (each attempted source)"]:::cmd
    record_emitted["RecordEmitted"]:::cmd
    source_finished["SourceFinished"]:::cmd
    search_finished["SearchFinished"]:::cmd

    search_started --> source_started
    source_started --> source_finished
    source_started -. scan-order release while active .-> record_emitted
    source_finished -. ranked/newest release after finish .-> record_emitted
    record_emitted -->|next match| record_emitted
    source_finished --> search_finished
    record_emitted --> search_finished
::::

The graph is a partial order, not a serial source timeline:

- `SearchStarted` is first and `SearchFinished` is last.
- Every attempted source has one `SourceStarted` / `SourceFinished` pair;
  pairs can overlap and do not establish a global source sequence.
- Scan-ordered records can arrive while their source is active. Relevance and
  newest records can arrive after that source finishes.

- {class}`~agentgrep.events.SearchStarted` — exactly once at the
  head. Carries `source_count` (the number of candidate sources
  after prefiltering).
- {class}`~agentgrep.events.SourceStarted` — once per attempted source, in
  planned source priority. Carries `adapter_id`, `index`, `total`.
- {class}`~agentgrep.events.RecordEmitted` — the hot-path event.
  Fires only after deduplication and the requested ordering contract permit
  release. An ordered record need not appear inside the start/finish pair for
  the source that produced it.
- {class}`~agentgrep.events.SourceFinished` — once per source,
  paired with its `SourceStarted`. Carries `records_seen` (every
  record parsed) and `matches_seen` (the subset that matched
  before dedup).
- {class}`~agentgrep.events.SearchFinished` — exactly once at the
  tail. Carries `match_count` (total emitted) and
  `elapsed_seconds` plus the engine-owned {class}`~agentgrep.RunSummary`.
  The summary records the normalized effort, status, distinguishable empty
  outcome, source and conversation coverage, diagnostics, and next actions.

Even on empty input the `Started` / `Finished` envelope fires so
cleanup code is uniform.

## Find events

The {data}`~agentgrep.events.FindEvent` union has three members.
Find has no per-source scan loop — each discovered source produces
exactly one record — so the sequence simplifies:

::::{mermaid}
:caption: Find emits one record per discovered source, then finishes.
:alt: find event order from started through records to finished
:name: find-event-sequence
:responsive: fit

flowchart TD
    find_started["FindStarted"]:::cmd
    find_record["FindRecordEmitted"]:::cmd
    find_finished["FindFinished"]:::cmd

    find_started -->|record| find_record
    find_started -->|no records| find_finished
    find_record -->|next record| find_record
    find_record --> find_finished
::::

- {class}`~agentgrep.events.FindStarted`
- {class}`~agentgrep.events.FindRecordEmitted`
- {class}`~agentgrep.events.FindFinished`

## Consumer recipes

### Print records as they arrive (the CLI pattern)

```python
import sys
import agentgrep
from agentgrep import events


def stream_to_stdout(home, query) -> int:
    is_tty = sys.stdout.isatty()
    count = 0
    for event in agentgrep.iter_search_events(home, query):
        if isinstance(event, events.RecordEmitted):
            print(event.record.text)
            if is_tty:
                sys.stdout.flush()
            count += 1
    return 0 if count > 0 else 1
```

### Collect records and terminal evidence

```python
import agentgrep


def collect_search(home, query):
    result = agentgrep.run_search_result(home, query)
    return result.records, result.summary
```

Consumers must retain the terminal summary. An empty record tuple alone cannot
say whether prompt search found nothing, targeted routing selected no
conversation, selected conversations contained no match, exhaustive coverage
found nothing, or the run ended incompletely.

The summary is the completion evidence: `requested_effort` and
`completed_effort`, `outcome`, `coverage`, diagnostics, and engine-authored
next actions all describe facts that records cannot. Its primary status follows
the precedence `failed`, `cancelled`, `truncated`, `approximate`, `bounded`,
then `complete`; `status.conditions` retains every independent condition rather
than discarding lower-precedence facts. Apply a next-action patch only after
checking `requires_confirmation`.

Structured sinks serialize this evidence as `request`, `effort`, `status`,
`outcome`, `coverage`, `stats`, diagnostics, and next actions. The serialized
`stats` object contains matched count, elapsed time, applied order, and limit;
`RunSummary` has no `statistics` attribute.

### Consume events asynchronously

```python
import contextlib
import agentgrep
from agentgrep import events


async def collect_events(home, query) -> list[events.SearchEvent]:
    events_seen = []
    async with contextlib.aclosing(agentgrep.aiter_search_events(home, query)) as stream:
        async for event in stream:
            events_seen.append(event)
    return events_seen
```

The async wrapper applies queue backpressure. If a consumer may stop before
`SearchFinished`, `aclosing` requests cooperative cancellation and waits for
the worker to stop.

This is not a Textual rendering recipe. The explorer runs search work in a
threaded, exclusive worker in its stable `search` group and gives it a shared
{class}`~agentgrep.SearchControl` for cooperative stopping. The worker returns
events through a generation-gated `call_from_thread` callback; the pump drops
stale generations and applies record batches in bounded chunks. Keep parsing,
collection, and bulk rendering off the pump rather than putting an async event
loop directly in a handler.

### Cancel mid-scan

Pass a {class}`~agentgrep.SearchControl` and flip its
{meth}`~agentgrep.SearchControl.request_answer_now` flag to break out
at the next per-record boundary:

```python
control = agentgrep.SearchControl()

# … on a keypress / timeout / user action:
control.request_answer_now()
```

The generator still emits `SearchFinished` so cleanup runs. The CLI exposes
answer-now for `search` text output, including globally newest-first `--no-rank`,
when progress is active and both stdin and stderr are TTYs. Grep uses scan
order and its result limit instead.

## Async delivery and source scheduling

{func}`~agentgrep.aiter_search_events` is the public async API. It uses a
bounded {class}`asyncio.Queue` between the synchronous worker and async
consumer. Closing it signals the shared {class}`~agentgrep.SearchControl`,
stops delivery, and joins the worker.

Source scans may use OS threads, but the collector owns final emission.
Relevance and newest order drain enough work to prove the global frontier.
Scan order serializes source priority so a faster lower-priority worker cannot
overtake the requested sequence.

## Reference

The events module's full API is documented at
{mod}`agentgrep.events`. The iterators are at
{func}`agentgrep.iter_search_events` and
{func}`agentgrep.iter_find_events`.
