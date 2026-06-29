(adr-deductive-narrowing-and-conversation-layout)=

# ADR 0015: Deductive narrowing and conversation layout

## Status

Accepted. Shipped as a sequence behind the {ref}`completion gate
<adr-non-blocking-tui-invariants>`: wire the dead workflow bindings + the
action-routing seam → add the chat layout (with the existing workflows) → add the
deductive workflow + breadcrumb chrome → record this ADR.

## Context

{ref}`ADR 0013 <adr-pluggable-tui-layouts-and-workflows>` split the TUI into two
orthogonal axes — a **layout** (structure) and a **workflow** (behavior). This
ADR adds the next pair: a Claude-Code/pi-style **conversation layout** and a
**deductive narrowing workflow** where the first prompt fixes a result set and
each later prompt narrows *within* it, getting more precise, with a key to widen
back out.

Two findings shaped the design:

- The engine seam (`SearchInvoker.run`) only accepts a `SearchQuery`, not a set
  of record ids — so a true "re-search within these results" is unavailable.
  In-memory filtering of the already-loaded set is both the correct deductive
  semantics ("the haystack is fixed") and the cheaper {ref}`ADR 0011
  <adr-non-blocking-tui-invariants>` choice (one disk hit, then bounded off-pump
  scans).
- `Workflow.BINDINGS` had been declared since ADR 0013 (PL-2) but was **never
  installed** on the hosting screen — a workflow's own keys could not fire. A
  deductive workflow needs widen/clear keys, so the seam had to be completed
  before the feature could exist.

## Decision

A deductive search is *load a fixed haystack once, then narrow it in-memory*,
rendered most legibly as a conversation. The following invariants govern the
layer (DN for *deductive narrowing*), in the enumerated style of {ref}`ADR 0011
<adr-non-blocking-tui-invariants>`.

- **DN-1 — Narrowing is in-memory over a fixed haystack, never a re-query.** The
  first non-empty submission (or a non-empty launch query) runs one engine
  `run_search` that fixes the haystack; each later submission narrows the loaded
  set with a composed-`AND` `filter_loaded`. Widen re-filters with the top level
  removed; an empty submit / clear resets. The narrowing path routes through
  `filter_loaded` *only*, so a future "re-grep from disk" escape hatch is a
  drop-in — compose every frame (including the base) and call `run_search`
  instead, with no data-model change. The one accepted gap: in-memory narrowing
  can only see records that survived the haystack's initial limit (the re-grep
  hatch is the eventual mitigation).
- **DN-2 — The refinement stack is an immutable value held on the workflow.** The
  `DeductiveWorkflow` holds a `tuple[RefinementFrame, ...]` (frozen, slotted),
  pushing/popping by tuple slicing. It imports no Textual and reaches the layout
  only through the host surface, so the whole narrowing policy is unit-tested
  against a recording fake host.
- **DN-3 — The transcript is append-only and frozen; turns build once.** The chat
  layout's `ConversationLog` is a `VerticalScroll(layout: stream)` that mounts
  turn bubbles and is **never** recomposed (Textual's `recompose()` removes and
  remounts all children with no keyed diff). A finished turn is built once and
  frozen — the ink `<Static>` discipline restated as the ADR 0011 law. Turn data
  is a frozen+slots value object (`QueryTurn` / `ResultTurn` / `SystemTurn`); the
  rendering lives on a separate, non-slotted `TurnRenderer` dispatched by type
  via `singledispatchmethod` (a frozen+slots object has no `__dict__`, so derived
  state cannot live on it). Renders stay bounded (first line + compact path,
  never `Syntax`/`Markdown` on a full body); a per-block result cap bounds the
  widget count, and the detail modal builds heavy bodies off the pump. A widen
  *appends* a wider block rather than unmounting, preserving the freeze.
- **DN-4 — Workflow keys are installed and routed; the host surface grew two
  hooks.** `LayoutScreen` now installs the active `Workflow.BINDINGS` on attach
  (and removes them on swap, by identity), and `Workflow.on_action(host,
  action_id)` routes a parameterized key action (`workflow("widen")`) back into
  the strategy object via `LayoutScreen.action_workflow` — so a workflow still
  imports no Textual. The `WorkflowHost` surface (ADR 0013 PL-2) gains
  `set_input_text` (re-seed the prompt after a pop) and `update_breadcrumb`
  (repaint the path); these are the only genuinely-missing affordances.
- **DN-5 — Deductive composes with every layout (PL-6 holds).** The chat layout
  renders the breadcrumb and opens detail on a focused result turn; HUD and
  grep-log re-seed their input on `set_input_text` and treat `update_breadcrumb`
  as a no-op, so deductive narrows their views too without a crash. The breadcrumb
  flows through one host hook, so HUD can later grow count-pills from the same
  signal without touching the workflow.

### Catalog

| Kind | Name | Class | Role |
| --- | --- | --- | --- |
| Layout | `chat` | `ChatLayout` | Conversation transcript of query turns + streamed result bubbles. |
| Workflow | `deductive` | `DeductiveWorkflow` | First submit fixes the haystack; later submits narrow in-memory; widen pops. |

The pair is available through the internal programmatic composition surface;
the normal `agentgrep ui` CLI remains fixed to its public layout and workflow.
Within a deductive composition, `ctrl+up` widens and `ctrl+l` clears while
narrowing, with the path shown in the breadcrumb.

## Relationship to ADRs 0011 / 0012 / 0013

This ADR *extends*, and does not replace, its predecessors. The {ref}`ADR 0011
<adr-non-blocking-tui-invariants>` non-blocking catalog is unchanged (the new
pump entrypoints are `@pump_only`/`@offload` and the static guard scans the new
modules). It adds three reusable leaf widgets (`MessageTurn` / `ConversationLog`
/ `RefinementBreadcrumb` and the `TurnRenderer` controller) to the {ref}`ADR 0012
<adr-reusable-tui-widget-architecture>` catalog, keeping the single concrete
renderer rather than a premature `ChatTurnRenderer` protocol (defer-until-consumer).
And it completes the {ref}`ADR 0013 <adr-pluggable-tui-layouts-and-workflows>`
seam: PL-2's `WorkflowHost` surface grows two members, the dead `Workflow.BINDINGS`
are finally installed, and `Workflow.on_action` is added — a contract change
future workflows depend on, which is why it is recorded here rather than as a new
registry instance.

## Engine changes

None. The chat layout and deductive workflow reach the engine only through the
existing `SearchInvoker` seam and the in-memory matcher helpers
(`compile_record_matcher`); no native code, no new engine entry point.

## Consequences

The explorer gains a conversation lens and a deductive search that reads the
narrowing story straight down the transcript (`1240 → 88 → 12`). Each step landed
behind the gate and is independently revertable.

The chief risks: the initial-limit truncation of DN-1 (a capped haystack can hide
records a from-disk re-grep would find — the `^R` hatch is the planned mitigation);
the transcript's turn budget unmounts the oldest bubbles, which a focused/open
detail must tolerate; and a widen appends rather than unmounts, so the transcript
grows on repeated narrow/widen cycles (bounded by the budget). The opaque-base
typing debt (PL-7) and the deferred `present_*` transport base (PL-5) carry over
unchanged.
