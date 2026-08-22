# `src/agentgrep/ui/` — non-blocking TUI rules

These rules apply to every change under `src/agentgrep/ui/`, in addition to
the repo-wide policy in the root [AGENTS.md](../../../AGENTS.md).

The Textual message pump is single-threaded: any callable it invokes that
runs past a frame budget — or never returns — freezes keystrokes, the
spinner, resize, and cancel at once. ADR 0011 (NB-1..NB-10) is the contract;
the `textual-non-blocking-pump` skill is the working method. On every change
here:

- **Enumerate pump entrypoints, do not prefix-guess.** Textual runs your code
  on the pump through `on_*`/`_on_*` and **any `@on(...)`-decorated
  handler**, inline reactive `watch_*`/`validate_*`/`compute_*`,
  `render`/`__rich__`/`get_content_*`, `action_*`, and **the callables passed
  to `set_timer`/`set_interval`/`call_later`/`call_from_thread`/`subscribe`**.
  Decorate any new pump entrypoint `@pump_only`; decorate every `run_worker`
  target `@offload` (`thread=True`, `exclusive=True` except
  `group="history"`, stable `group=`).
- **No blocking work reachable from a pump callable — even one helper hop
  down.** No file open, subprocess, sqlite3, network, filesystem walk,
  lock/queue wait, `concurrent.futures` `.result()`, `json.load(s)`/
  `dump(s)`, `.read()`, or **unbounded CPU** (full-result casefold/sort/regex,
  `Syntax(...).highlight` on a full body). Route bulk UI updates through
  `stream_apply`; route large/uncached detail builds through an `@offload`
  worker. Never evade review by aliasing or using a `from` import — move the
  call off the pump.
- **Static review cannot prove completeness.** "Blocks the pump" is a
  semantic (Rice-undecidable) property, and no retained automated static gate
  walks this graph. Apply the skill's entrypoint catalog and helper tracing
  by hand. The decorators assert thread placement under pytest or an
  explicitly truthy `AGENTGREP_TUI_WATCHDOG`; the audit hook also requires
  that explicit opt-in. The log-only heartbeat defaults on for an interactive
  TTY, a falsey override disables it, and pytest does not auto-start it.
  Exercise a change once with the explicit watchdog setting against a large
  real store before calling its path non-blocking.
