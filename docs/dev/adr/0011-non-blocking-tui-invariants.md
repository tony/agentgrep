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
chunked and yields; cancellation is cooperative through
{class}`~agentgrep.SearchControl`.

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
  {meth}`agentgrep.SearchControl.answer_now_requested`; superseding a search
  swaps in a fresh control so a draining worker keeps its stale flag.
- **NB-8 — `call_from_thread` callees are themselves pump-safe.** Moving a call
  to a worker does not exempt the callee it schedules back onto the pump from
  NB-1/NB-4/NB-5.
- **NB-9 — Inline fast-path heavy work is hard-bounded.** When a pump callable
  builds a heavy renderable inline, it does so only below an explicit size
  threshold; above it the work offloads to a worker.
- **NB-10 — Stale events cannot repaint fresh chrome.** Cross-thread event
  callees carry a generation token checked on the pump before mutating chrome.

## Enforcement

Three complementary layers have different activation rules:

1. **Primitives** in `agentgrep.ui._runtime` make the right thing
   structural: the `pump_only` / `offload` decorators assert, when guards are
   enabled, that a callable runs on / off the bound pump thread (NB-1, NB-2,
   NB-8); `stream_apply` enforces the chunk cap and the inter-slice `await`
   (NB-4); `make_gated_emitter` centralizes the bus-bypass plus generation
   token (NB-3, NB-10).
2. **Manual static review** uses the `textual-non-blocking-pump` skill to
   enumerate every pump entrypoint, follow its helper calls, inspect worker
   flags and groups, and confirm bounded apply and generation-token seams. This
   is a required review method, not an automated static CI gate; it cannot prove
   the semantic absence of blocking work.
3. **Runtime observation** combines an audit hook and heartbeat watchdog. A
   truthy `AGENTGREP_TUI_WATCHDOG` enables the audit hook, decorator assertions,
   and heartbeat. With the variable unset, the log-only heartbeat defaults on
   for interactive stdout TTYs; a falsey value forces it off, and pytest never
   auto-starts it. The heartbeat logs `pump heartbeat stalled` with
   `agentgrep_pump_stall_ms` after a threshold. The deterministic oracle in
   `tests/test_tui_runtime_oracles.py` pins that warning and its structured
   threshold fields.

The decorators are enabled under pytest or an explicitly truthy watchdog
variable; the audit hook requires the explicit truthy variable because it can
raise. Textual's debug `SLOW_THRESHOLD` log was rejected as enforcement — it
offers no assertion or test hook. A ruff rule was rejected — ruff has no
custom-Python-rule mechanism.

The `_runtime` module is Textual-free and imports only the standard library, so
it sits below `app.py` in the
{ref}`ADR 0010 layering <adr-module-boundaries-and-facade-re-export-contract>`
and focused runtime tests reach it without entering the application shell.

## Consequences

The invariants have structural runtime seams plus a required manual review: a
worker body run on the pump trips `@offload` when guards are enabled, the
explicit audit mode aborts covered blocking-I/O initiation, and a wedged pump
is observable through the watchdog. Manual review remains necessary for
unbounded CPU, dynamic dispatch, stale repaint, teardown, and other behavior
the runtime mechanisms cannot prove before it executes. Moving code between UI
modules does not change that review obligation.

## Coverage limits and the runtime complement

Manual static review can prevent a *decidable* subset of blocking calls
reachable from a recognized pump entrypoint. "Blocks the pump" is a semantic,
Rice-undecidable property, so source inspection is sound in neither direction.
Three limits are load-bearing and require runtime complements:

- **Enumerate pump entrypoints; do not classify by name prefix.** Textual also
  runs user code on the pump through `@on(...)`-decorated handlers (arbitrary
  names), inline reactive `watch_` / `validate_` / `compute_` (which bypass the
  message queue *and* Textual's own `SLOW_THRESHOLD`), `render` / `__rich__` /
  `get_content_*`, and the callables passed to `set_timer` / `set_interval` /
  `call_from_thread` / `subscribe`. Reviewers enumerate those sites and trace
  their named, `lambda`, and `partial` targets; every new pump entrypoint still
  carries `@pump_only` so enabled runtime assertions check its thread placement.
- **Interprocedural and CPU blind spots.** Reviewers follow same-class helpers
  and cross-module calls by hand, but dynamic dispatch remains easy to miss and
  pure-CPU blocking (an unbounded casefold / sort / regex,
  `Syntax(...).highlight` on a full body) has no call signature to search for.
- **Prevention vs. detection.** Manual review reduces known hazards before
  merge; the heartbeat watchdog detects super-threshold stalls only on exercised
  runtime paths. An explicit-env `sys.addaudithook` scoped to the pump thread
  checks a finite eight-event allowlist: `socket.connect`, `socket.getaddrinfo`,
  `subprocess.Popen`, `os.system`, `os.exec`, `os.spawn`, `time.sleep`, and
  `sqlite3.connect`. For those covered events it fires on the acting thread and
  can abort initiation regardless of how the operation was spelled, aliased, or
  dynamically dispatched. `open` and `import` are deliberately excluded because
  Textual/Rich perform legitimate pump-side theme and syntax reads and lazy
  imports. The hook is also blind to CPU spin, byte-transfer on already-open
  handles, and native syscalls that skip `PySys_Audit`; the wall-clock watchdog
  is the cause-agnostic backstop for that residue.

The two heaviest sites are bounded by design rather than by static enforcement.
Filter projection runs in `_run_filter_worker`; the pump adopts its prepared
model in `on_filter_completed`, and `SearchResultsList` renders only the
requested `ScrollView` lines through bounded row and final-strip caches.
Find-in-detail re-highlight (`_present_detail_find`, a full-body
`Syntax.highlight`) reuses a cached syntax base. The `textual-non-blocking-pump`
skill carries the full pump-entrypoint catalog and the per-change review rules.

## Final position

The NB-1..NB-10 catalog is the source of truth for TUI concurrency. New TUI work
reuses the `_runtime` primitives rather than re-deriving the patterns. Manual
skill-guided review, the explicit-env audit mode, and the default-on interactive
heartbeat provide defense in depth without claiming complete static proof.
