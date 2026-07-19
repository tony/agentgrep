(adr-pluggable-tui-layouts-and-workflows)=

# ADR 0013: Pluggable TUI layouts and workflows

## Status

Accepted. The layout/workflow architecture is an internal composition seam.
The shipped `agentgrep ui` surface is fixed to one layout/workflow pair; alternate
registered components remain available to Python factories, tests, and
embedders.

## Context

{ref}`ADR 0012 <adr-reusable-tui-widget-architecture>` finished the reusable
leaf-widget layer behind the `SearchInvoker` engine seam and recorded the
pi/ink → Textual capability mapping. It deliberately declined a *layout*
abstraction until a second concrete consumer existed. The subsequent HUD,
greplog, search, and browse implementations proved that two internal axes are
useful for separating shell lifecycle, structure, and interaction policy. The
former `ExplorerApp` had fused those concerns into a single ~2358-line object.

The surface splits along two orthogonal axes:

- **Layout** — *structure*: which widgets exist and how they are arranged
  (a results-list + detail split vs. an append-only log).
- **Workflow** — *behavior*: what the primary input does (run a fresh engine
  search vs. filter the already-loaded records in-memory).

These are independent: a workflow should drive any layout, and a layout should
host any workflow. That orthogonality is valuable for implementation tests and
embedding, but it does not require a normal user-facing selector or live
switcher. This ADR records the internal architecture so contributors neither
re-fuse the App and the view nor turn an implementation seam into product
surface without a separate decision.

## Decision

The TUI is a thin App shell that mounts exactly one **layout** (a Textual
`Screen`) driven by exactly one **workflow** (a plain strategy object), both
resolved by name from an internal registry. The normal CLI always uses the
registry defaults. Python app factories keep keyword injection for tests and
embedding, validate the names, and pass one frozen internal composition to
`ExplorerApp`. The shell never replaces that composition; the lower-level
`LayoutScreen.set_workflow` strategy seam remains available to component code.
The following invariants govern the layer (PL for *pluggable layout*), in the
enumerated style of {ref}`ADR 0011 <adr-non-blocking-tui-invariants>`.

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
- **PL-3 — The App shell owns initial composition, not switching or presentation.**
  `ExplorerApp(App)` owns lifecycle, theme registration, the ADR-0011 pump bind /
  watchdog / audit hook, the `UiContext`, and construction of the one typed
  layout × workflow composition it receives. It registers no Textual modes,
  exposes no layout or workflow cycling bindings/actions, and does not display
  the internal pair in chrome. No rendering, matching, or record-detail
  construction lives on the shell (mirrors RW-6).
- **PL-4 — Layouts and workflows resolve through a frozen, lazy registry.**
  `agentgrep.ui.registry` is a Textual-free catalog of `LayoutSpec` /
  `WorkflowSpec` whose loaders are function-local imports, so listing names never
  imports Textual. Programmatically injected names are validated against the
  registry before launch, resolved before Textual starts its message pump, and
  paired in one frozen value; the shell never handles lazy loaders, unresolved
  names, or fallback selection. Layout-specific startup state such as query
  history is likewise loaded at this pre-pump factory boundary. The CLI does not
  expose those names. A future `importlib.metadata` entry-point source can feed
  the same spec shape without changing internal consumers.
- **PL-5 — Each layout carries its own transport over the shared primitives.**
  A layout's streaming transport reuses `_runtime.make_gated_emitter` /
  `@offload` / `@pump_only` / `stream_apply` (ADR 0011 NB-1..NB-10, unchanged)
  with a layout-specific *present*. Every `run_worker` stays `thread=True,
  exclusive=True` and grouped (the `history` append group excepted), and manual
  pump-entrypoint review covers **every** `ui/layouts/*.py`, not just the HUD.
  The transport is intentionally *not* hoisted into the base: a shared
  `present_*` base waits for a third consumer, per the defer-until-consumer rule
  of ADR 0012.
- **PL-6 — Orthogonality is an internal contract and is proven.** Any workflow
  drives any layout.
  The behavior difference is the workflow's routing (`SearchWorkflow` →
  `run_search`, `BrowseWorkflow` → `filter_loaded`); the structure difference is
  the layout's `compose` + present. Direct component tests and injected app
  construction prove `search` × `browse` over `hud` × `greplog`; normal users do
  not choose among those combinations.
- **PL-7 — The opaque `Screen` base carries the former App posture.**
  `LayoutScreen` keeps the `t.Any` base the fused App used, because `DOMNode.query`
  (the DOM query) collides with view state; the search-query state is
  `self.search_query` precisely to avoid that. Fully typing the views against
  `Screen` is a follow-up, as it was against `App`.

### Internal catalog

| Kind | Name | Class | Role |
| --- | --- | --- | --- |
| Layout | `hud` (default) | `HudLayout` | Search bar, streaming results list, detail pane. |
| Layout | `greplog` | `GrepLogLayout` | Append-only `grep`-style log of streamed matches. |
| Workflow | `search` (default) | `SearchWorkflow` | Each submission runs a fresh engine search. |
| Workflow | `browse` | `BrowseWorkflow` | The input filters the loaded records in-memory. |

`agentgrep ui` launches the fixed `hud` × `search` pair. There are no
`--layout` / `--workflow` options, runtime cycling keys, Textual mode stacks, or
active-pair subtitle. Tests and embedders may pass `layout=` and `workflow=` to
the Python app factories; direct shell tests inject a validated composition.

## Relationship to ADR 0012

This ADR builds on, and partially supersedes, {ref}`ADR 0012
<adr-reusable-tui-widget-architecture>`. ADR 0012's reusable widget layer
(RW-1..RW-8) and the {ref}`ADR 0011 <adr-non-blocking-tui-invariants>`
non-blocking catalog are kept intact — layouts compose the same leaf widgets and
honor the same pump rules. What this ADR reverses is ADR 0012's single-frontend
*position*: the second consumer it said to wait for has arrived, so the layout
abstraction (`LayoutScreen`, the `Workflow` seam, the registry) is now
warranted internally. It does not create multiple shipped frontends or a
user-facing plugin contract. No reconciler, flexbox engine, or kill-ring editor
is adopted; Textual's `Screen` supplies the composition boundary directly.

## Engine changes

None. Layouts and workflows reach the engine only through the existing
`SearchInvoker` seam and the already-streaming, cooperatively-cancellable engine
of {ref}`ADR 0004 <adr-headless-query-planning-non-blocking-execution>`. No
native code, no new engine entry point.

## Consequences

The former god-object is now one layout behind a thin lifecycle shell, while the
shipped explorer keeps one stable interaction model. Removing live switching
also removes suspended-screen state, hidden layout workers, cross-layout workflow
reattachment, and user-facing key/chrome complexity.

The internal registry and alternate pair injection still need direct coverage so
they do not drift while absent from the CLI. `LayoutScreen.set_workflow` remains
a programmatic component operation, but `ExplorerApp` never calls it. The
opaque-base typing (PL-7) remains a debt, and the per-layout transport (PL-5)
carries a little boilerplate over the shared primitives until a third layout
justifies a `present_*` base.
