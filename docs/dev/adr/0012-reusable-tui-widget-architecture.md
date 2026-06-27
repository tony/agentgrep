(adr-reusable-tui-widget-architecture)=

# ADR 0012: Reusable TUI widget architecture

## Status

Accepted. Implementation proceeds as the strangler-fig sequence in [§ Implementation sequence](#implementation-sequence); each step lands behind the {ref}`completion gate <adr-non-blocking-tui-invariants>` and keeps `app.py` a thin facade.

## Context

The interactive explorer is assembled by `build_streaming_ui_app`, which lazily imports Textual ([`textual.app`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/app.py), [`textual.containers`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/containers.py), [`textual.widgets`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/widgets/__init__.py)) and then defines `class AgentGrepApp(app_type)` *inside the factory closure*. That closure-defined class is a single god-object: search and filter worker dispatch, the detail-pane rendering pipeline (header build, JSON/Markdown/plain body, LRU caches, wrap-aware find-in-detail), responsive layout, pane focus routing, staged Ctrl-C exit, slash-command dispatch, and every cross-widget message handler all live in one body. It can only be constructed through the factory, so it is neither independently unit-testable nor reusable, and it is the single largest obstacle to evolving the UI. The dynamic base is also why the tree carries `# ty: ignore[unsupported-base]`.

The leaf widgets, by contrast, are already extracted: `agentgrep.ui.widgets` holds plain Textual subclasses — `SearchResultsList` over [`OptionList`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/widgets/_option_list.py), `DetailScroll` over [`VerticalScroll`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/containers.py), the [`Input`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/widgets/_input.py)-derived `SearchInput`/`FilterInput`/`DetailFindInput`, `HistoryRecall` over [`ModalScreen`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/screen.py), and the [`Static`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/widgets/_static.py)-based status chrome — each imported only inside the factory per {ref}`ADR 0010 <adr-module-boundaries-and-facade-re-export-contract>` and each unit-tested without a live App. The non-blocking spine is already codified by {ref}`ADR 0011 <adr-non-blocking-tui-invariants>` (NB-1..NB-10) and the `agentgrep.ui._runtime` primitives. So the remaining work is *finishing the strangler extraction of the App object*, not building a framework.

A second motivation is a recurring question: should agentgrep adopt the widget surface of richer terminal UIs such as [pi](https://github.com/earendil-works/pi) (its from-scratch differential-render library [`@earendil-works/pi-tui`](https://github.com/earendil-works/pi/blob/v0.80.2/packages/tui/src/tui.ts)) and [ink](https://github.com/vadimdemedes/ink) (React-reconciler-over-terminal)? This ADR records the comparative analysis and the answer: most of that capability is either already present or a small generalization on top of Textual, a handful of items are genuine but out-of-scope for a read-only search tool, and **Textual lacks no architectural component that agentgrep needs**. The capability mapping is recorded so future contributors do not re-open the question or import an unneeded abstraction.

This ADR does not introduce a generic widget framework. agentgrep ships exactly one frontend, so "reusable" here means *independently typed, testable, and ADR-0011-guarded leaf widgets behind a narrow engine seam* — not a plugin base class with no second consumer.

## Decision

The TUI is organized as five layers with a one-directional dependency flow (engine seam ← view widgets ← app shell; theme and message contracts are shared leaves). The App shell owns composition and dispatch; it owns no rendering or matching logic.

| Layer | Responsibility | Key types |
| --- | --- | --- |
| App shell | Screen composition, global `BINDINGS`, lifecycle, worker dispatch, cooperative cancellation, stale-event generation tokens. | `ExplorerApp(App)`; `SearchControl`; generation token (`int`); `run_worker(thread=True, exclusive=True, group=...)` |
| View widgets | Reusable leaf Textual subclasses that render normalized records and emit typed messages; pump-thread behavior only. | `SearchResultsList(OptionList)`; `DetailScroll(VerticalScroll)`; `DebouncedQueryInput(Input)`; `FuzzySelectorModal(ModalScreen)`; `CompletionDropdown(OptionList)`; status chrome over `Static` |
| Message / view-model contracts | Typed `Message` subclasses carrying pre-shaped dataclass / `NamedTuple` payloads, so widgets never reach into the app. | `SearchRequested`; `FilterRequested`; `FilterCompleted`; `ResultsScrollChanged`; `DetailScrollChanged`; `ResultsStatusSnapshot` |
| Engine seam (Protocols) | Narrow `Protocol`s the app/widgets call instead of importing engine internals; faked in tests. | `SearchInvoker(Protocol)`; `PreviewProvider(Protocol)`; `SearchRecord`; `FindRecord` |
| Theme / styles | pi-lite palette tokens, terminal transparency, docked layout — centralized, not per-widget. | `theme.py` token maps; `styles.tcss` |

The following invariants govern the layer (RW for *reusable widget*), in the enumerated style of {ref}`ADR 0011 <adr-non-blocking-tui-invariants>`:

- **RW-1 — Widgets consume normalized records, never engine internals.** A view widget imports `agentgrep.records` (`SearchRecord` / `FindRecord`) and the engine-seam `Protocol`s only; it must not import `agentgrep._engine`, `agentgrep.query`, or `agentgrep.stores`. Search and preview are reached through `SearchInvoker` / `PreviewProvider`.
- **RW-2 — State leaves a widget as a typed `Message`, not a back-reference.** A widget posts a [`Message`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/message.py) subclass carrying a pre-shaped dataclass / `NamedTuple`; it does not mutate sibling widgets through `self.app`. This matches the existing `agentgrep.ui.widgets.messages` module.
- **RW-3 — Every widget is constructable and testable without a live App.** Pure construction plus [`App.run_test()`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/app.py) + [`Pilot`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/pilot.py) driving, with syrupy snapshots of rendered `Content`. No tty, filesystem, subprocess, or live engine in widget tests; the engine seam is faked. This is the pattern already in `tests/test_ui_widgets.py`, `tests/test_ui_history_modal.py`, and `tests/test_tui_non_blocking.py`.
- **RW-4 — Widget state is typed reactive.** Every [`reactive`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/reactive.py) attribute is annotated `reactive[Concrete]`; no bare `Any`; every handler names its precise `Message` subtype. The only permitted ty suppressions are the two already in the tree (`unsupported-base` for the dynamic App base, the `ModalScreen[T]` runtime-subscript `noqa`).
- **RW-5 — Widgets honor the non-blocking catalog.** Engine work runs in `thread=True, exclusive=True` workers behind a stable group; high-frequency results return via [`call_from_thread`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/app.py); pump callables stay O(1). This is {ref}`ADR 0011 <adr-non-blocking-tui-invariants>` NB-1..NB-10, unchanged — RW-5 binds the widgets to it rather than restating it.
- **RW-6 — The App shell owns composition and dispatch only.** No rendering, matching, ranking, or record-detail construction logic lives on the App; those belong to view widgets or the engine behind the seam.
- **RW-7 — Optional pi-parity widgets are gated.** Any widget marked OPTIONAL below ships only behind its own issue/ADR with a measured baseline first, per the measurement-first rule of {ref}`ADR 0003 <adr-native-boundary-execution-architecture>`. No differential-render or frame-time performance claim is made without a named baseline; the explorer relies on Textual's [compositor](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/_compositor.py) and `OptionList` line caching, and measures with the hang-fuzz harness and `scripts/profile_engine.py` before optimizing.
- **RW-8 — `app.py` stays a thin importing facade during extraction.** Each strangler step keeps `app.py` re-exporting the moved symbol so the step is independently revertable, per {ref}`ADR 0010 <adr-module-boundaries-and-facade-re-export-contract>`.

### Reusable widget catalog

CORE widgets are required for search now and mostly already exist; OPTIONAL widgets are pi-parity nice-to-haves gated by RW-7.

| Widget | Tier | Base | Role |
| --- | --- | --- | --- |
| `SearchResultsList` | CORE | [`OptionList`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/widgets/_option_list.py) | Append-only streaming result rows; `append_records` is O(batch), no prior-row relayout. |
| `DetailScroll` | CORE | [`VerticalScroll`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/containers.py) | Record detail with per-record scroll memory; heavy renderables built off-thread (NB-9). |
| `DebouncedQueryInput` family | CORE | [`Input`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/widgets/_input.py) | `SearchInput` / `FilterInput` / `DetailFindInput`; debounced typed-message emit (NB-3). |
| `FuzzySelectorModal` | CORE | [`ModalScreen`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/screen.py) | Generalizes `HistoryRecall`: in-memory fuzzy filter + worker-backed preview + focus trap. |
| `CompletionDropdown` + `QuerySuggester` | CORE | `OptionList` / [`Suggester`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/suggester.py) | Slash/field completion over the in-process `FieldRegistry`. |
| Status chrome | CORE | [`Static`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/widgets/_static.py) | `PaneHeader` / `ResultsHeader` / `SearchingPanel` / `SpinnerWidget` / `MeterWidget`; O(1) updates from pre-shaped snapshots. |
| `MarkdownRecordDetail` | OPTIONAL | `DetailScroll` + [`Markdown`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/widgets/_markdown.py) | Static render of an already-persisted record; **no** token-stream reflow path. |
| `ConversationScrollbackLog` | OPTIONAL | [`RichLog`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/widgets/_rich_log.py) | `scope=conversations` transcript browsing; append-only with a retention cap. |
| `KillRingTextArea` | OPTIONAL/CUT | [`TextArea`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/widgets/_text_area.py) | Emacs kill-ring multiline editor; cut — a single-line search box does not need it. |

## Capability mapping

### What ink has that Textual does not

The honest finding: nothing architectural that agentgrep needs. ink's declarative-React machinery is replaced by Textual's reactive descriptors, `compose()`, and the compositor; the one genuine *model* difference is layout (Yoga flexbox vs. Textual CSS), and for a fixed docked shell Textual's model is the better fit.

| ink concept | ink source (v7.1.0) | Textual counterpart (v8.2.6) | Verdict |
| --- | --- | --- | --- |
| React reconciler / vDOM diff | [`reconciler.ts`](https://github.com/vadimdemedes/ink/blob/v7.1.0/src/reconciler.ts), [`dom.ts`](https://github.com/vadimdemedes/ink/blob/v7.1.0/src/dom.ts) | [`_compositor.py`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/_compositor.py) + per-region invalidation | Parity / superior — no reconciler to port; keep watchers O(1) (NB-5). |
| Hooks (`useState`/`useEffect`) | [`hooks/`](https://github.com/vadimdemedes/ink/blob/v7.1.0/src/hooks/use-input.ts) | [`reactive.py`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/reactive.py) + `watch_*`/`compute_*` | Parity — map each hook to a typed `reactive[Concrete]`. |
| `<Static>` append-only output | [`components/Static.tsx`](https://github.com/vadimdemedes/ink/blob/v7.1.0/src/components/Static.tsx) | `OptionList.add_option` / [`RichLog.write`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/widgets/_rich_log.py) | Parity — `SearchResultsList.append_records` already maps this. |
| `<Box>`/`<Text>` + Yoga flexbox | [`components/Box.tsx`](https://github.com/vadimdemedes/ink/blob/v7.1.0/src/components/Box.tsx), [`styles.ts`](https://github.com/vadimdemedes/ink/blob/v7.1.0/src/styles.ts) | TCSS dock/grid/`fr` + [`containers.py`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/containers.py) | Gap in *model* — flexbox idioms do not port 1:1; CSS+dock is superior for the docked shell. Layout lives in `styles.tcss`. |
| `useInput` keyboard | [`hooks/use-input.ts`](https://github.com/vadimdemedes/ink/blob/v7.1.0/src/hooks/use-input.ts) | `BINDINGS` + `action_*` + `on_key` | Parity — declarative and typed. |
| `useFocus` / `useFocusManager` | [`use-focus.ts`](https://github.com/vadimdemedes/ink/blob/v7.1.0/src/hooks/use-focus.ts), [`use-focus-manager.ts`](https://github.com/vadimdemedes/ink/blob/v7.1.0/src/hooks/use-focus-manager.ts) | Focus chain on [`widget.py`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/widget.py) + `ModalScreen` trap/restore | Superior — tab-order and modal focus restore are free. |
| Alt-screen / raw-mode lifecycle | [`ink.tsx`](https://github.com/vadimdemedes/ink/blob/v7.1.0/src/ink.tsx) | `App` manages it | Superior — delete the concern entirely. |
| Streaming-token markdown reflow | ink re-reflows on each render | [`Markdown.update`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/widgets/_markdown.py) / `RichLog.write` | Gap, out of scope — agentgrep has no live token producer; records are static, so partial-fence handling never arises. |

### pi capability → Textual

Every notable capability of [pi-tui](https://github.com/earendil-works/pi/blob/v0.80.2/packages/tui/src/tui.ts) and pi's [interactive components](https://github.com/earendil-works/pi/blob/v0.80.2/packages/coding-agent/src/modes/interactive/components/session-selector.ts), mapped to the Textual path. pi's differential renderer is a from-scratch cell diff; Textual's compositor already provides the equivalent, so it is not reproduced.

| pi capability | pi source (v0.80.2) | Textual path | Effort | Tier |
| --- | --- | --- | --- | --- |
| Incremental result streaming | engine-driven | `SearchResultsList.append_records` fed by a `thread=True` worker via `call_from_thread`, chunked apply (NB-3/NB-4) | builtin | CORE — already implemented |
| Append-only scrollback | [`tui.ts`](https://github.com/earendil-works/pi/blob/v0.80.2/packages/tui/src/tui.ts) | `OptionList.add_option`; `RichLog.write` for the optional transcript | builtin | CORE |
| Fuzzy selector + live preview | [`select-list.ts`](https://github.com/earendil-works/pi/blob/v0.80.2/packages/tui/src/components/select-list.ts), [`session-selector.ts`](https://github.com/earendil-works/pi/blob/v0.80.2/packages/coding-agent/src/modes/interactive/components/session-selector.ts) | `FuzzySelectorModal(ModalScreen)` generalizing `HistoryRecall`; `rapidfuzz` filter + worker-backed `PreviewProvider` | small | CORE |
| Slash / field completion | pi autocomplete | `CompletionDropdown` + `QuerySuggester(Suggester)` over `FieldRegistry` | builtin | CORE |
| Focus traversal + modal trap | pi overlay model | Textual focus chain + `ModalScreen` auto trap/restore | small | CORE — deliverable is the documented focus graph + Pilot tests |
| Spinner / progress chrome | pi [`loader.ts`](https://github.com/earendil-works/pi/blob/v0.80.2/packages/tui/src/components/loader.ts) | `SpinnerWidget(Static)` driven by `set_interval`; snapshots into `SearchingPanel`/`MeterWidget` | builtin | CORE |
| Cancellation / supersede | pi keybindings | `SearchControl` polled in worker loops; `run_worker(exclusive=True)` (NB-6/NB-7) | builtin | CORE |
| Terminal-transparent aesthetic | pi themes | `App.ansi_color=True` + ansi-default tokens in `theme.py`; pi-lite rules in `styles.tcss` | builtin | CORE |
| Streaming-markdown transcript | [`markdown.ts`](https://github.com/earendil-works/pi/blob/v0.80.2/packages/tui/src/components/markdown.ts) | Static `MarkdownRecordDetail`; full parse off-thread, no per-token path | medium | OPTIONAL — no live token producer |
| Emacs kill-ring editor | [`editor.ts`](https://github.com/earendil-works/pi/blob/v0.80.2/packages/tui/src/components/editor.ts) | `KillRingTextArea(TextArea)` | medium | OPTIONAL/CUT — single-line box; `Input` already covers editing |
| Full conversation browsing | pi session view | `ConversationScrollbackLog(RichLog)` for `scope=conversations` | medium | OPTIONAL — needs an engine incremental-detail fetch behind its own ADR |

### Why no native code

pi ships native C ([`darwin-modifiers.c`](https://github.com/earendil-works/pi/blob/v0.80.2/packages/tui/native/darwin/src/darwin-modifiers.c), [`win32-console-mode.c`](https://github.com/earendil-works/pi/blob/v0.80.2/packages/tui/native/win32/src/win32-console-mode.c)) only for key-modifier and console-mode detection, not rendering. Textual already handles raw mode, the alternate screen, and key parsing across platforms, so there is no native boundary to open here; this stays within the no-native-by-default rule of {ref}`ADR 0003 <adr-native-boundary-execution-architecture>`. The non-blocking offload that makes streaming search safe rests on CPython's [`concurrent.futures.ThreadPoolExecutor`](https://github.com/python/cpython/blob/v3.14.5/Lib/concurrent/futures/thread.py) (under Textual's thread workers) handing results back through the loop via [`call_from_thread`](https://github.com/Textualize/textual/blob/v8.2.6/src/textual/app.py), itself built on [`asyncio`](https://github.com/python/cpython/blob/v3.14.5/Lib/asyncio/base_events.py) — all standard library, no accelerator.

## Engine changes

CORE widgets need **no execution-engine change**. The engine already streams results incrementally with cooperative `SearchControl` cancellation and chunked emission per {ref}`ADR 0004 <adr-headless-query-planning-non-blocking-execution>`, and the TUI already consumes it inside `thread=True` workers per {ref}`ADR 0011 <adr-non-blocking-tui-invariants>`. The extraction must preserve, not modify, that boundary. The only addition is a UI-layer import seam — `SearchInvoker` and `PreviewProvider` `Protocol`s in `agentgrep.ui` — so widgets call narrow interfaces instead of importing engine internals; it changes no search semantics and adds no native code. The OPTIONAL `ConversationScrollbackLog` would require an engine record-detail fetch that yields a `scope=conversations` transcript incrementally; the `scope` parameter already exists, but that fetch is deferred behind its own issue/ADR with a measured baseline.

(implementation-sequence)=

## Implementation sequence

Strangler-fig, one concern per gate-green commit, `app.py` a thin facade throughout (RW-8). The bite-sized, test-first task breakdown lives in the working plan referenced by the resumable loop prompt; the durable order is:

0. **Pin behavior.** Confirm characterization tests — pure widget tests plus Pilot/syrupy snapshots for each existing leaf widget, plus the ADR 0011 guard tests. No production code moves.
1. **Introduce the engine seam.** Define `SearchInvoker` / `PreviewProvider` `Protocol`s in `agentgrep.ui` and route existing widgets through them; add a fake-Protocol test fixture. No behavior change.
2. **De-closure the App.** Lift the App subclass out of `build_streaming_ui_app` into a module (`agentgrep.ui.app_screen`), keeping `build_streaming_ui_app` a thin assembling facade.
3. **Normalize CORE widget contracts, one widget per commit** (results, detail, the `Input` family, status chrome, dropdown): typed `reactive[Concrete]`, `Message` payload dataclasses, NumPy docstrings, a per-widget Pilot+syrupy test.
4. **Generalize the fuzzy selector.** Extract `FuzzySelectorModal`; re-express `HistoryRecall` as a thin subclass; snapshot-pin recall behavior; fake `PreviewProvider` in tests.
5. **Document and test the focus graph.** Tab order, modal trap/restore, and global-vs-focused key precedence, with Pilot focus-traversal tests.
6. **Stop for CORE; gate OPTIONAL work.** File `MarkdownRecordDetail`, `ConversationScrollbackLog`, and `KillRingTextArea` as separate issues/ADRs, each requiring a measured baseline before any code (RW-7).

Per-step exit criterion (every step): `rm -rf docs/_build; uv run ruff check . --fix --show-fixes; uv run ruff format .; uv run ty check; uv run py.test --reruns 0 -vvv; just build-docs` exits 0, and `app.py` still imports every extracted symbol.

## Consequences

The UI gains a typed, testable widget layer with a documented engine seam, and the closure god-object shrinks step by step into an App shell that only composes and dispatches. Each step is independently revertable because `app.py` keeps re-exporting moved symbols, and each is provable because the characterization snapshots and the ADR 0011 guards run at every gate. The capability question is settled in writing, so contributors neither re-derive the pi/ink comparison nor import an unneeded reconciler, flexbox engine, or kill-ring editor.

The chief risks are extraction drift (the closure captures many locals — mitigated by characterization-tests-first and the facade re-export), ty `Any`-leaks from untyped reactives or dynamic handlers (mitigated by RW-4 and the two existing suppressions), and scope creep from the OPTIONAL pi-parity widgets (mitigated by RW-7 gating them behind their own baselines). No performance claim is made without a named baseline.

## Final position

The reusable-widget layer is finished by extraction, not invention: CORE widgets ship now behind the `SearchInvoker` / `PreviewProvider` seam under RW-1..RW-8 and the {ref}`ADR 0011 <adr-non-blocking-tui-invariants>` non-blocking catalog; OPTIONAL pi-parity widgets stay gated behind their own ADRs with measured baselines. Textual supplies every architectural primitive agentgrep needs, so no native code, reconciler, or flexbox engine is adopted.
