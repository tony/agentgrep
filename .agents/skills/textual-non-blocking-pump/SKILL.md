---
name: textual-non-blocking-pump
description: Use when writing, reviewing, or auditing agentgrep TUI code (src/agentgrep/ui/) for anything that could freeze the Textual message pump — keystrokes/spinner/cancel hang, a handler that blocks, a slow filter/find/detail render, "the UI froze", a new pump method/timer/worker, or extending the ADR 0011 non-blocking guard. Covers the full pump-entrypoint catalog, the static-vs-runtime defense split, and the per-change review rules.
---

# Keeping the Textual pump non-blocking

## Overview

agentgrep's TUI runs on a **single-threaded message pump**. Any callable Textual
invokes on that thread that runs longer than a frame budget (~16 ms) — or never
returns — freezes *everything at once*: keystrokes, the spinner, resize, and
cancel. ADR 0011 (NB-1..NB-10) is the contract; `ui/_runtime.py` has the
primitives; `tests/test_tui_non_blocking.py` is the static guard.

**The one idea to internalize:** "blocks the pump" is a *semantic* property of
runtime behavior, not a *syntactic* property of source text. By Rice's theorem
no static analyzer can flag *all and only* blocking code (it reduces to
halting). So the goal is **never 100% static prevention** — it is a **two-gate
defense**: static analysis *prevents* the decidable, enumerable cases at merge,
and a cause-agnostic runtime oracle *detects* the undecidable residue. Treat any
"we'll just lint for it" instinct as the trap this skill exists to correct.

## When to use

- Adding/editing any method on a Textual `App` / `Screen` / `Widget` in `ui/`.
- A user reports a freeze/hang/jank; the spinner stalls; cancel stops responding.
- Reviewing a PR that touches `ui/` — especially filter, find-in-detail, detail
  rendering, streaming apply, or anything reading a store.
- Extending the ADR 0011 guard, the `_runtime` primitives, or the fuzz harness.

When NOT to use: non-UI engine/MCP work (those run off the pump by construction);
pure styling/`.tcss` edits with no Python.

## The trap: why a denylist lint feels like enough and isn't

The static guard now follows same-class `self.helper()` calls, classifies `@on`
handlers and the callables named at `set_timer`/`set_interval`/
`call_from_thread`/`subscribe` sites, and resolves import aliases against an
expanded denylist. But it still cannot be *complete* — by Rice's theorem no
denylist can. Four holes remain, each the shape of a real hang this repo has
shipped:

1. **Closure is same-class only.** It follows `self.helper()` within a class, but
   a blocking call reached through *cross-module* dispatch, a stored callable, or
   `getattr` is still invisible. (The `os.write` history-write hang — commit
   `8b26d8a3` — was the intra-class version this now catches.)
2. **Classification needs a name or a seed.** It sees `@on` handlers and the
   callables *named* at scheduler / `call_from_thread` / `subscribe` sites, but a
   `lambda` or `functools.partial` handed to a scheduler — and inline reactive
   `validate_`/`compute_` reached dynamically — still slip.
3. **The denylist is import-aware but finite.** It resolves `import subprocess as
   sp` / `from time import sleep` and covers network / fs-walk / `json.load` /
   `input`, but generic-attr blocking (`Lock.acquire` / `Queue.get` /
   `Future.result` — no type to match on) is unrepresentable.
4. **Pure-CPU blocking has no call signature at all.** An unbounded
   `casefold`/`sort`/`regex` over the result set, or `Syntax(...).highlight` on a
   full body, cannot be denylisted. This is the undecidable core.

**ruff and ty cannot close this.** ruff's `ASYNC2xx` rules fire *only inside
`async def`* (pump methods are mostly sync) and ruff has no custom-Python-rule
API. ty has no plugin system; `Annotated` metadata is dropped; the only working
type pattern is a `PumpView`/`WorkerIO` capability seam, and even that leaks
through `Any`-erasure and direct `builtins.open()`. Type coloring is the
*lowest*-leverage option.

## Pump-entrypoint catalog (enumerate, don't prefix-guess)

Every callable below runs on the event-loop thread. A new one that does heavy
work MUST be `@pump_only` (so both the static classifier and runtime assert
cover it) and route its heavy work off the pump.

| Family | Entrypoints | Classifier sees it? |
|---|---|---|
| Message handlers | `on_*`, `_on_*`, **any `@on(...)`-decorated method (any name)** | prefix: partial; `@on`: **seeded** |
| Reactivity (inline, sync) | `watch_*`, `validate_*`, `compute_*` — bypass the message queue *and* Textual's own SLOW_THRESHOLD | partial |
| Render/layout (compositor) | `render`, `render_line`, `__rich__`, `get_content_width/height`, `pre_layout` | partial |
| Actions | `action_*`, `_action_*` (key bindings, links) | partial |
| Scheduled callbacks | `set_timer`, `set_interval`, `call_later`, `call_next`, `call_after_refresh` targets — **arbitrary names** | **seeded** (named targets) |
| Cross-thread callees (NB-8) | anything passed to `call_from_thread(fn, …)` — **arbitrary names** | **seeded** (named targets) |
| Signals | `subscribe(self, fn)` callbacks (e.g. theme-changed) | **seeded** |
| Startup | `compose`, `on_mount` (run before the loop, on the pump) | exact/prefix: yes |
| Async `@work` **without** `thread=True` | the coroutine body runs *on the loop* — CPU without `await` freezes it | n/a |

Off the pump (safe place for blocking work): `@work(thread=True)` /
`run_worker(..., thread=True)` bodies, decorated `@offload`.

## The two-gate defense (the actual answer)

No single mechanism both prevents and detects. Build the **pair**, organized by
the I/O-vs-CPU asymmetry:

```
                    PREVENT (merge / dev)            DETECT (CI / runtime)
  I/O initiation  │ static denylist (decidable)  │ sys.addaudithook (denylist-free,
  (open/connect/  │ + AST guard                  │   aborts on the acting thread)
   Popen/sleep)   │                              │
  ────────────────┼──────────────────────────────┼───────────────────────────────
  CPU spin /      │ (no call signature —         │ wall-clock watchdog
  recv on open fd │  NOT statically detectable)  │   (faulthandler / heartbeat /
  / native I/O    │                              │   sys._current_frames sampling)
```

- **`sys.addaudithook`** (PEP 578) is denylist-FREE: the hook fires synchronously
  on the *acting* thread, so `if get_ident()==pump_id and event in IO_INITIATORS:
  raise` **aborts the syscall before it runs** — immune to alias / `Any`-erasure
  / dynamic dispatch. Reproduced on CPython 3.14: `open()` via `getattr` alias
  and `sqlite3.connect()` abort; a CPU sort+regex fires **zero** events.
- **It is blind to** CPU spin, byte-transfer on *already-open* handles
  (`recv`/`send`, `cursor.execute` on a live connection — the canonical slow-query
  hang carries no audit event), and native Rust/C syscalls that skip `PySys_Audit`.
- **The wall-clock watchdog is the cause-agnostic backstop** for exactly that
  residue: it catches anything that stalls past a threshold, of any cause. Ceiling:
  only above threshold, only on exercised inputs, only *after* the freeze (it
  cannot abort a thread mid-C-syscall — only kill the process). This is the
  asyncio `slow_callback_duration` model.

**The `@pump_only`/`@offload` asserts and the audit hook stay opt-in**
(`PYTEST_CURRENT_TEST` or `AGENTGREP_TUI_WATCHDOG`), since both can raise. The
log-only heartbeat watchdog **defaults on for an interactive TTY**, so real users
get the cause-agnostic backstop without risking a crash on a latent violation.

## Review rules (apply on EVERY ui/ change)

Static analysis provably cannot self-discover new pump entrypoints, CPU blocking,
or stale-event paths. These are the judgment calls a reviewer/agent must make:

1. **Decorate every new pump entrypoint `@pump_only`** (catalog above), every
   `run_worker` target `@offload` (`thread=True, exclusive=True` except
   `group="history"`, stable `group=`).
2. **No blocking work reachable from a pump callable**, even one hop down a
   helper: no file open, subprocess, sqlite3, network, fs-walk
   (`glob`/`scandir`/`walk`/`iterdir`/`stat`), lock/queue/`Event.wait`,
   `futures.result`, `json.load(s)`/`dump(s)`, `.read()`/`.readlines()`,
   `time.sleep`, or **unbounded CPU** (full-result `casefold`/`sort`/`regex`,
   `Syntax(...).highlight` on a full body). Route bulk UI updates through
   `stream_apply`; route large/uncached detail builds through an `@offload` worker.
3. **Never satisfy the guard by aliasing/`from`-import** — that's evasion, not a
   fix. Move the call off the pump.
4. **Worker bodies must not read/mutate pump-owned state** (widget/theme vars);
   snapshot on the pump and pass it in (cf. `b1bc2f48`).
5. **Cross-thread chrome repaint carries a generation token** (`make_gated_emitter`,
   NB-10); recompute find/match state from live values; cancel pending debounce
   timers on record-switch/close. *Stale-event repaint is the most recurrent
   historical hang (~8 fixes) and no oracle detects it.*
6. **Teardown-reachable handlers tolerate an empty screen stack** (early-return);
   teardown exceptions are invisible to every blocking oracle (cf. `4895d2a2`).
7. **Don't raise runtime bounds** (`_DETAIL_ASYNC_BODY_THRESHOLD=20000`,
   `stream_apply` chunk cap `200`) without re-proving the per-frame budget — the
   static guard cannot verify these constants still gate the inline path.
8. **Exercise the change once under `AGENTGREP_TUI_WATCHDOG=1` against a large
   real store** before claiming a path is non-blocking; the merge gate cannot
   prove the absence of CPU/data-dependent blocking.

## Audit procedure (when hunting an existing hang)

1. Reproduce; note which interaction freezes (filter widen, find keystroke,
   detail open on a big record, resize, theme switch).
2. Map the interaction to its pump entrypoint via the catalog — **include the
   arbitrary-named ones** (`@on`, timer, `call_from_thread`, `subscribe`).
3. Follow the call graph by hand one+ hops (the guard won't). Look for the four
   holes: helper-extracted I/O, unclassified entrypoint, aliased call, unbounded
   CPU loop.
4. Classify the cause: I/O-on-pump → offload + audit hook; unbounded CPU →
   bound it (`stream_apply`) or offload + watchdog; stale-event → generation
   token; teardown → empty-stack guard; worker-touches-pump-state → snapshot.
5. Lock it with a Pilot regression test; if it's a stall, add a fuzz/watchdog
   assertion.

## What the static guard still can't catch

Two historical CPU stalls here — the filter rebuild
(`on_filter_completed → set_records → _rebuild_options`) and the find-in-detail
re-highlight (`_present_detail_find`) — are now bounded by an id-keyed row-render
cache and a cached syntax base. The *static* residue is the four holes above plus
pure-CPU spin: cross-module/dynamic dispatch, `lambda`/`partial` scheduler
targets, generic-attr blocking, and unbounded loops. The watchdog is the runtime
backstop for all of them; profile a suspected stall (synthetic records, timings
only) before reaching for a structural fix.

## Residual risks (state these honestly; do not claim 100%)

Pure-CPU/data-dependent blocking, native-extension syscalls, `recv`/`execute` on
open handles, fast-in-CI/slow-in-prod, sub-threshold jank, stale-event repaint,
and teardown exceptions all slip the static gate. The watchdog catches the
super-threshold subset on exercised inputs, after the fact. The honest guarantee
is "high coverage via defense-in-depth," never literal completeness.

## Reference

- Contract: `docs/dev/adr/0011-non-blocking-tui-invariants.md` (NB-1..NB-10).
- Primitives: `src/agentgrep/ui/_runtime.py` (`pump_only`/`offload`/`stream_apply`/
  `make_gated_emitter`/`start_pump_watchdog`).
- Static guard: `tests/test_tui_non_blocking.py`.
- Fuzz harness: `tests/test_ui_fuzz.py`.
