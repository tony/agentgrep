(adr-non-blocking-tui-invariants)=

# ADR 0011: Non-blocking TUI invariants

## Status

Accepted.

## Context

Textual runs input, rendering, reactive updates, timers, and callbacks on one
message-pump thread. Blocking I/O or unbounded CPU on that thread freezes
keystrokes, progress, resize, and cancellation together.

Discovery and search already run behind an off-pump execution boundary. This
ADR defines the TUI-side invariants that keep all other work responsive.

## Decision

A pump callable is any code Textual invokes on its event-loop thread, including
event handlers, actions, reactive callbacks, render/compose paths, scheduled
callbacks, and functions scheduled back from workers. Naming conventions alone
do not identify the full set.

- **NB-1 — No blocking I/O or unbounded CPU on the pump.** Pump callables do
  not perform filesystem, subprocess, database, network, wait, bulk parsing,
  ranking, filtering, or equivalent unbounded work.
- **NB-2 — Heavy work runs off the pump.** Use a Textual offload worker or
  headless execution driver, as appropriate. This ADR requires off-pump
  execution but does not prescribe threads as the permanent transport.
- **NB-3 — Cross-thread delivery is bounded and coalesced.** High-frequency
  progress or record delivery uses a bridge that cannot flood the message
  queue or create unbounded pending pump work. The concrete bridge and debounce
  values are implementation choices.
- **NB-4 — Bulk application is bounded and yields.** Pump-side adoption of
  worker output uses declared chunks or an equivalent work budget and yields
  between them.
- **NB-5 — Reactive and rendering work respects a frame budget.** Watchers,
  renderers, and composition are bounded independently of total corpus,
  transcript, or result-set size. Viewport-bounded work is allowed; unbounded
  whole-dataset work is not.
- **NB-6 — Supersedable work has stable cancellation ownership.** A newer user
  action can cancel or replace older work without cancelling unrelated work.
- **NB-7 — Cancellation is cooperative.** Long work observes the operation's
  shared cancellation signal through the mechanism appropriate to its
  transport.
- **NB-8 — Worker-to-pump callees are pump-safe.** Moving work to a worker does
  not exempt the code it schedules back onto the pump from these invariants.
- **NB-9 — Inline heavy fast paths are explicitly bounded.** Inline parsing or
  rendering is permitted only below a named size or work threshold; larger
  inputs offload.
- **NB-10 — Stale work cannot repaint current state.** Supersedable results
  carry a generation or equivalent token checked before pump-side mutation.

## Enforcement

The invariants use complementary mechanisms:

1. Runtime primitives mark pump and offload boundaries, apply streamed results
   in bounded chunks, and gate stale cross-thread events.
2. Manual review enumerates every Textual pump entrypoint and traces reachable
   helpers. The `textual-non-blocking-pump` skill is the required working
   method.
3. Runtime observation uses thread-placement assertions, an opt-in blocking-I/O
   audit, and a heartbeat watchdog to detect exercised stalls.

No static rule can prove that arbitrary code will not block. Review must include
decorated handlers with arbitrary names, reactive methods, rendering and Rich
hooks, and callbacks passed to timers, subscriptions, and cross-thread bridges.
It must also inspect unbounded CPU work, dynamic dispatch, already-open handles,
and native calls that a finite audit-event set cannot identify.

Runtime observation complements rather than replaces review: it detects only
paths that execute, while review cannot prove the absence of all dynamic or
CPU-bound stalls. Tests cover the structural primitives and deterministic
watchdog outcomes; a large-store interactive exercise remains appropriate for
changed hot paths.

## Relationships

- If adopted, ADR 0004 supplies the shared headless execution and cancellation
  contract; these pump invariants remain self-contained without it.
- ADR 0010 owns module layering for reusable runtime primitives.
- ADR 0014 allows any execution transport that preserves collector semantics;
  this ADR governs only safe delivery to the TUI pump.

## Consequences

TUI responsiveness becomes a reviewable architectural property rather than a
convention. The policy permits viewport-bounded rendering and different
execution transports while keeping total-data work off the pump.

Defense in depth adds runtime and review machinery, and none of it proves every
possible stall. The remaining uncertainty is explicit and covered through
manual tracing plus exercised watchdog observation.
