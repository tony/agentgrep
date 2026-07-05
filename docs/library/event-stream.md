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

- **Per-record liveness.** Each match emits as
  {class}`~agentgrep.events.RecordEmitted` the moment the engine
  decides "unique-and-included." The CLI grep / find text paths
  consume the stream and print + flush per record; users see the
  first matches within milliseconds.
- **Single source of truth.** Search progress (which source is
  active, how many records seen / matched) and the matches
  themselves are the same event stream, not two parallel side
  channels.
- **Decoupling.** The engine doesn't know about stdout, Textual, or
  fastmcp. It yields events. Consumers translate.

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                   PRODUCER  (agentgrep._engine)                │
│                                                                │
│   def iter_search_events(home, query, *, control=None)         │
│       -> Iterator[SearchEvent]:                                │
│                                                                │
│     yield SearchStarted(source_count=...)                      │
│     for source in discovered:                                  │
│         yield SourceStarted(adapter_id, index, total)          │
│         for record in iter_source_records(source):             │
│             if matches(record, query) and unique:              │
│                 yield RecordEmitted(record=record)             │
│         yield SourceFinished(adapter_id, records_seen, ...)    │
│     yield SearchFinished(match_count, elapsed_seconds)         │
└──────────────────────────┬─────────────────────────────────────┘
                           │
       ┌───────────────────┼───────────────────┐
       ▼                   ▼                   ▼
┌──────────────┐  ┌──────────────────┐  ┌──────────────────┐
│  CLI (sync)  │  │  TUI (Textual)   │  │   MCP (sync)     │
│              │  │                  │  │                  │
│ for ev in    │  │ @work(thread=    │  │ list(records     │
│   stream:    │  │  True) consumes  │  │   for ev in      │
│   if Record  │  │  via to_thread   │  │   stream if      │
│      print() │  │                  │  │   isinstance     │
│      flush() │  │                  │  │   RecordEmitted) │
└──────────────┘  └──────────────────┘  └──────────────────┘
```

### Sync producer

The engine is a synchronous generator. Async consumers wrap it in
{func}`asyncio.to_thread` with one line; sync consumers iterate
directly. Tests exercise the producer without an event loop, which
keeps the test surface small.

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

```text
SearchStarted → (SourceStarted → RecordEmitted* → SourceFinished)* → SearchFinished
```

- {class}`~agentgrep.events.SearchStarted` — exactly once at the
  head. Carries `source_count` (the number of candidate sources
  after prefiltering).
- {class}`~agentgrep.events.SourceStarted` — once per source, in
  source-discovery order (mtime descending). Carries `adapter_id`,
  `index`, `total`.
- {class}`~agentgrep.events.RecordEmitted` — the hot-path event.
  Fires only after the per-session dedup decided unique-and-included.
- {class}`~agentgrep.events.SourceFinished` — once per source,
  paired with its `SourceStarted`. Carries `records_seen` (every
  record parsed) and `matches_seen` (the subset that matched
  before dedup).
- {class}`~agentgrep.events.SearchFinished` — exactly once at the
  tail. Carries `match_count` (total emitted) and
  `elapsed_seconds`.

Even on empty input the `Started` / `Finished` envelope fires so
cleanup code is uniform.

## Find events

The {data}`~agentgrep.events.FindEvent` union has three members.
Find has no per-source scan loop — each discovered source produces
exactly one record — so the sequence simplifies:

```text
FindStarted → FindRecordEmitted* → FindFinished
```

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

### Collect to a list (the MCP / TUI snapshot pattern)

```python
import agentgrep
from agentgrep import events


def collect_records(home, query):
    return [
        event.record
        for event in agentgrep.iter_search_events(home, query)
        if isinstance(event, events.RecordEmitted)
    ]
```

### Update a UI as events arrive (the Textual TUI pattern)

```python
import asyncio
import agentgrep
from agentgrep import events


async def update_ui(home, query, render_record):
    def _drain() -> list[events.SearchEvent]:
        return list(agentgrep.iter_search_events(home, query))
    for event in await asyncio.to_thread(_drain):
        if isinstance(event, events.RecordEmitted):
            render_record(event.record)
```

For finer-grained live updates inside Textual, run the generator
on a `@work(thread=True)`-decorated method and post a message per
event rather than draining first.

### Cancel mid-scan

Pass a {class}`~agentgrep.SearchControl` and flip its
{meth}`~agentgrep.SearchControl.request_answer_now` flag to break out
at the next per-record boundary:

```python
control = agentgrep.SearchControl()

# … on a keypress / timeout / user action:
control.request_answer_now()
```

The generator still emits `SearchFinished` so cleanup runs.

## Slice boundaries

This page documents Slice 1 — the sync iterator surface used by the
CLI's live streaming. Two follow-up slices are planned:

- **Slice 2**: an {func}`~agentgrep.aiter_search_events` async wrapper
  that bridges the sync producer via a bounded {class}`asyncio.Queue`
  and a thread-backed producer task. Cancellation propagates through
  `CancelledError`. The TUI moves to the async surface; the CLI
  keeps using the sync iterator.
- **Slice 3**: source-level parallelism via {class}`asyncio.TaskGroup`
  over {func}`asyncio.to_thread` ``(parse_source, src)``. Each source's
  events merge into a single output stream via the queue. Cancellation
  propagates through task cancel.

Both slices preserve the public event surface — consumers written
today continue to work without changes.

## Reference

The events module's full API is documented at
{mod}`agentgrep.events`. The iterators are at
{func}`agentgrep.iter_search_events` and
{func}`agentgrep.iter_find_events`.
