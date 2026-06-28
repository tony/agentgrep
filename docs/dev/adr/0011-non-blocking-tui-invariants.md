(adr-non-blocking-tui-invariants)=

# ADR 0011: Non-blocking TUI invariants

## Status

Accepted.

## Context

{ref}`ADR 0004 <adr-headless-query-planning-non-blocking-execution>` states, in
one unenforced sentence, that the Textual TUI must remain non-blocking. The
explorer already honors that: discovery, ripgrep, JSONL parsing, SQLite reads,
and ranking run inside `thread=True` workers; streaming results return to the
pump via `call_from_thread` rather than the message bus; batch application is
chunked and yields; cancellation is cooperative through `SearchControl`.

But none of that was written down as rules, and nothing stopped a future edit
from dropping a synchronous `open()`, `json.loads()` of an unbounded transcript,
or a multi-second scan straight into an `on_*` handler — which would freeze
keystrokes, the spinner, resize, and cancel all at once, because Textual's
message pump is single-threaded and has no built-in watchdog.

This ADR turns the one-sentence rule into an enforceable catalog plus the
mechanism that holds the line.

## Decision

A *pump-thread callable* is any method Textual invokes on the event-loop
thread: `on_*`, `action_*`, `watch_*`, `compute_*`, `render`, `compose`, the
input `_on_key` / `_watch_value` overrides, `set_interval` / `set_timer` /
`call_later` callbacks, and any function reached through `call_from_thread`.

- **NB-1 — No blocking I/O or unbounded CPU on the pump.** A pump-thread
  callable must not open files, spawn subprocesses, read SQLite, parse JSON of
  unbounded size, walk the filesystem, run ranking, or `time.sleep`.
- **NB-2 — Heavy work runs in a `thread=True` worker.** Discovery, parse, scan,
  rank, SQLite, and subprocess work runs only inside a worker or engine call.
- **NB-3 — High-frequency results bypass the message bus.** Streaming results,
  progress snapshots, and finished events return via `call_from_thread`;
  typing-speed events use the message bus and a debounce of at least 150 ms.
- **NB-4 — Every batch application is bounded and yields.** A pump-side apply of
  a worker-produced collection iterates in slices no larger than a named chunk
  cap and awaits between slices.
- **NB-5 — Watchers and `render` / `compose` are O(1) and non-blocking.** They
  do bounded constant work and may only `post_message`, set a reactive, or
  change-gate a `refresh()`.
- **NB-6 — Supersedable worker groups are `exclusive`.** Workers a newer user
  action should cancel run with `exclusive=True` and a stable `group`.
- **NB-7 — Cancellation is cooperative and polled.** Long loops poll
  `SearchControl.answer_now_requested()`; superseding a search swaps in a fresh
  control so a draining worker keeps its stale flag.
- **NB-8 — `call_from_thread` callees are themselves pump-safe.** Moving a call
  to a worker does not exempt the callee it schedules back onto the pump from
  NB-1/NB-4/NB-5.
- **NB-9 — Inline fast-path heavy work is hard-bounded.** When a pump callable
  builds a heavy renderable inline, it does so only below an explicit size
  threshold; above it the work offloads to a worker.
- **NB-10 — Stale events cannot repaint fresh chrome.** Cross-thread event
  callees carry a generation token checked on the pump before mutating chrome.

## Enforcement

Three layers, all default-off in production:

1. **Primitives** in `agentgrep.ui._runtime` make the right thing
   structural: the `pump_only` / `offload` decorators assert (in dev/test
   builds) that a callable runs on / off the bound pump thread (NB-1, NB-2,
   NB-8); `stream_apply` enforces the chunk cap and the inter-slice `await`
   (NB-4); `make_gated_emitter` centralizes the bus-bypass plus generation
   token (NB-3, NB-10).
2. **A static AST guard**, `tests/test_tui_non_blocking.py`, parses `ui/app.py`,
   classifies pump-thread methods, and fails if one contains a blocking call.
   JSON parsing is confined to the one bounded builder; a new site must be added
   to that allowlist deliberately. The same scan asserts every worker launch is
   `thread=True, exclusive=True` with a group (NB-6) and that the batch applier
   routes through `stream_apply` (NB-4).
3. **An opt-in heartbeat watchdog** (`AGENTGREP_TUI_WATCHDOG`) records a pump
   heartbeat on a timer and logs `pump heartbeat stalled` with
   `agentgrep_pump_stall_ms` when the pump goes quiet past a threshold. It is
   the oracle the hang-fuzz harness asserts on; the structured log keys are kept
   stable for that reason and are OpenTelemetry-attribute-friendly if metrics
   land later.

The decorators and watchdog are active under pytest (so violations fail CI) or
when the env var is set; otherwise the decorators reduce to one boolean check
and the watchdog thread never starts. Textual's debug `SLOW_THRESHOLD` log was
rejected as enforcement — it offers no assertion or test hook. A ruff rule was
rejected — ruff has no custom-Python-rule mechanism.

The `_runtime` module is Textual-free and imports only the standard library, so
it sits below `app.py` in the
{ref}`ADR 0010 layering <adr-module-boundaries-and-facade-re-export-contract>`
and the guard/unit tests reach it without entering the app factory's closure.

## Consequences

The invariants are now executable: a blocking call in a pump handler fails the
AST guard, a worker body run on the pump trips `@offload`, and a wedged pump is
observable through the watchdog. The chief risk is a false positive in the
static guard; it is mitigated by an explicit, reviewed allowlist and failure
messages that name the NB rule. The widgets still live inside the
`build_streaming_ui_app` closure; the guard walks into it, so lifting them to
`ui/widgets/` modules (a strangler-fig follow-up) strengthens but is not
required by this enforcement.

## Coverage limits and the runtime complement

The static guard prevents a *decidable* subset of blocking calls reachable from a
classified pump entrypoint. "Blocks the pump" is a semantic, Rice-undecidable
property, so that syntactic proxy is sound in neither direction. Three limits are
load-bearing and point to a runtime complement rather than an ever-larger
denylist:

- **Enumerate pump entrypoints; do not classify by name prefix.** Textual also
  runs user code on the pump through `@on(...)`-decorated handlers (arbitrary
  names), inline reactive `watch_` / `validate_` / `compute_` (which bypass the
  message queue *and* Textual's own `SLOW_THRESHOLD`), `render` / `__rich__` /
  `get_content_*`, and the callables passed to `set_timer` / `set_interval` /
  `call_from_thread` / `subscribe`. The guard seeds the `@on` and *named*
  scheduler / `call_from_thread` / `subscribe` targets; a `lambda` / `partial`
  target or a new helper still carries `@pump_only` so both the classifier and
  the runtime assert cover it.
- **Interprocedural and CPU blind spots.** The guard follows same-class
  `self.helper()` calls, so a denylisted call one hop below a pump method is
  caught — but cross-module or dynamic dispatch is not, and pure-CPU blocking
  (an unbounded casefold / sort / regex, `Syntax(...).highlight` on a full body)
  has no call signature to denylist at all.
- **Prevention vs. detection.** The decidable subset is *prevented* at merge; the
  undecidable residue is *detected* at runtime by the heartbeat watchdog. An
  opt-in `sys.addaudithook` scoped to the pump thread adds denylist-free
  *prevention* of CPython-instrumented blocking-I/O *initiation* (socket.connect,
  getaddrinfo, subprocess, time.sleep, sqlite3.connect): it fires
  on the acting thread and aborts the syscall regardless of how the call was
  spelled or dispatched. It is blind to CPU spin, byte-transfer on already-open
  handles, and native syscalls that skip `PySys_Audit`; the wall-clock watchdog
  is the cause-agnostic backstop for that residue.

The two heaviest sites the static guard cannot reach — the filter re-apply
(`on_filter_completed` → `set_records` → `_rebuild_options`, in a different class
than the NB-4 check) and the find-in-detail re-highlight (`_present_detail_find`,
a full-body `Syntax.highlight`) — are bounded by an id-keyed row-render cache and
a cached syntax base rather than by the guard. The `textual-non-blocking-pump`
skill carries the full pump-entrypoint catalog and the per-change review rules.

## Final position

The NB-1..NB-10 catalog is the source of truth for TUI concurrency. New TUI work
reuses the `_runtime` primitives rather than re-deriving the patterns, and the
static guard plus opt-in watchdog keep the invariants from eroding.
