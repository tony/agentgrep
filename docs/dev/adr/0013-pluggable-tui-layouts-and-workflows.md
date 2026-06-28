(adr-pluggable-tui-layouts-and-workflows)=

# ADR 0013: Pluggable TUI layouts and workflows

## Status

Accepted. Shipped as a strangler-fig sequence (relocate the HUD into a layout →
add the workflow seam → add a second workflow → add a second layout → add the
registry, CLI, and switching), each step landing behind the {ref}`completion
gate <adr-non-blocking-tui-invariants>`.

## Context

{ref}`ADR 0012 <adr-reusable-tui-widget-architecture>` finished the reusable
leaf-widget layer behind the `SearchInvoker` engine seam and recorded the
pi/ink → Textual capability mapping. It deliberately declined a *layout*
abstraction, on the grounds that "agentgrep ships exactly one frontend" and a
plugin base class with no second consumer is speculation. That premise no longer
holds: the goal now is to **launch into — and switch between — different TUI
surfaces over the same engine and the same normalized records**. The former
`ExplorerApp` fused the App lifecycle and one fixed heads-up display into a
single ~2358-line object, so it could be neither swapped nor re-targeted.

The surface splits along two orthogonal axes:

- **Layout** — *structure*: which widgets exist and how they are arranged
  (a results-list + detail split vs. an append-only log).
- **Workflow** — *behavior*: what the primary input does (run a fresh engine
  search vs. filter the already-loaded records in-memory).

These are independent: a workflow should drive any layout, and a layout should
host any workflow. Textual already supplies the mechanisms (`Screen`, `App.MODES`
/ `switch_mode`, reactive state) — so this is an *extraction and seam* exercise,
not a new framework. This ADR records the architecture so contributors neither
re-fuse the App and the view nor invent a parallel plugin system.

## Decision

The TUI is a thin App shell that mounts one **layout** (a Textual `Screen`)
driven by one **workflow** (a plain strategy object), both resolved by name from
a registry and switchable at runtime. The following invariants govern the layer
(PL for *pluggable layout*), in the enumerated style of {ref}`ADR 0011
<adr-non-blocking-tui-invariants>`.

- **PL-1 — A layout is a `Screen` injected with a shared context.** A layout is a
  `LayoutScreen(Screen)` subclass receiving a frozen `UiContext` (home, the
  `SearchInvoker` seam, the launch query, the cooperative-cancel control) and the
  active `Workflow`. It owns `compose`, CSS, `BINDINGS`, and presentation, and
  reaches the engine only through `context.invoker` (ADR 0012 RW-1) — never
  `agentgrep._engine`, `agentgrep.query`, or `agentgrep.stores`.
- **PL-2 — A workflow is a Textual-free strategy driven through a narrow host.**
  `Workflow` is a `Protocol`: `on_attach` seeds the initial dispatch and
  `on_query` handles a submission, both by calling the `WorkflowHost` surface
  (`build_query` / `run_search` / `filter_loaded` / `reset_view` /
  `record_history` / `request_cancel`). A workflow imports no Textual and touches
  no widget, so it runs on any layout and is unit-tested against a fake host.
- **PL-3 — The App shell owns selection and switching, not presentation.**
  `ExplorerApp(App)` owns lifecycle, theme registration, the ADR-0011 pump bind /
  watchdog / audit hook, the `UiContext`, and the choice of layout × workflow.
  Layouts switch via `App.MODES` / `switch_mode` (`F2`, suspend-not-destroy);
  a layout's workflow swaps via `LayoutScreen.set_workflow` (`F3`). No rendering,
  matching, or record-detail construction lives on the shell (mirrors RW-6).
- **PL-4 — Layouts and workflows resolve through a frozen, lazy registry.**
  `agentgrep.ui.registry` is a Textual-free catalog of `LayoutSpec` /
  `WorkflowSpec` whose loaders are function-local imports, so listing names never
  imports Textual and `agentgrep --help` stays cold. A name is validated against
  the registry before launch; `--layout` / `--workflow` consume the names as
  argparse `choices`. A future `importlib.metadata` entry-point source can feed
  the same spec shape without changing consumers.
- **PL-5 — Each layout carries its own transport over the shared primitives.**
  A layout's streaming transport reuses `_runtime.make_gated_emitter` /
  `@offload` / `@pump_only` / `stream_apply` (ADR 0011 NB-1..NB-10, unchanged)
  with a layout-specific *present*. Every `run_worker` stays `thread=True,
  exclusive=True` and grouped (the `history` append group excepted), and the
  static guard scans **every** `ui/layouts/*.py`, not just the HUD. The transport
  is intentionally *not* hoisted into the base: a shared `present_*` base waits
  for a third consumer, per the defer-until-consumer rule of ADR 0012.
- **PL-6 — Orthogonality is real and proven.** Any workflow drives any layout.
  The behavior difference is the workflow's routing (`SearchWorkflow` →
  `run_search`, `BrowseWorkflow` → `filter_loaded`); the structure difference is
  the layout's `compose` + present. The product is proven by `search` × `browse`
  over `hud` × `greplog`.
- **PL-7 — The opaque `Screen` base carries the former App posture.**
  `LayoutScreen` keeps the `t.Any` base the fused App used, because `DOMNode.query`
  (the DOM query) collides with view state; the search-query state is
  `self.search_query` precisely to avoid that. Fully typing the views against
  `Screen` is a follow-up, as it was against `App`.

### Catalog

| Kind | Name | Class | Role |
| --- | --- | --- | --- |
| Layout | `hud` (default) | `HudLayout` | Search bar, streaming results list, detail pane. |
| Layout | `greplog` | `GrepLogLayout` | Append-only `grep`-style log of streamed matches. |
| Workflow | `search` (default) | `SearchWorkflow` | Each submission runs a fresh engine search. |
| Workflow | `browse` | `BrowseWorkflow` | The input filters the loaded records in-memory. |

`agentgrep ui --layout greplog --workflow browse` launches a pair; `F2` cycles
the layout and `F3` cycles the workflow at runtime, with the active pair shown in
the title bar.

## Relationship to ADR 0012

This ADR builds on, and partially supersedes, {ref}`ADR 0012
<adr-reusable-tui-widget-architecture>`. ADR 0012's reusable widget layer
(RW-1..RW-8) and the {ref}`ADR 0011 <adr-non-blocking-tui-invariants>`
non-blocking catalog are kept intact — layouts compose the same leaf widgets and
honor the same pump rules. What this ADR reverses is ADR 0012's single-frontend
*position*: the second consumer it said to wait for has arrived, so the layout
abstraction (`LayoutScreen`, the `Workflow` seam, the registry) is now
warranted. No reconciler, flexbox engine, or kill-ring editor is adopted;
Textual's `Screen` / `MODES` supply the switching primitive directly.

## Engine changes

None. Layouts and workflows reach the engine only through the existing
`SearchInvoker` seam and the already-streaming, cooperatively-cancellable engine
of {ref}`ADR 0004 <adr-headless-query-planning-non-blocking-execution>`. No
native code, no new engine entry point.

## Consequences

The explorer gains two orthogonal, registry-selected, runtime-switchable axes
behind a thin shell; the former god-object is now a layout among layouts. Each
step is independently revertable, and the non-blocking guards run at every gate.

The chief risks: `switch_mode` *suspends* rather than destroys the previous
layout, so a hidden layout's in-flight worker keeps running against the warm
`SearchRuntime` cache — accepted for now (cheap warm-resume); a cancel-on-suspend
policy is a follow-up. The opaque-base typing (PL-7) remains a debt. And the
per-layout transport (PL-5) carries a little boilerplate over the shared
primitives until a third layout justifies a `present_*` base.
