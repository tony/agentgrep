(development)=

# Development

Contributing and internals references — material for people poking at agentgrep's source, writing new adapters, or running benchmarks across the commit history. None of this ships in the published wheel.

::::{grid} 1 1 2 2
:gutter: 2

:::{grid-item-card} Benchmark harness
:link: benchmark
:link-type: doc
Cross-commit `hyperfine` sweeps across HEAD, trunk, ranges, lookback, tags, or explicit commit lists. PEP 723 self-contained script in `scripts/benchmark.py`.
:::

:::{grid-item-card} Storage catalogue
:link: storage-catalog
:link-type: doc
On-disk store layouts for Codex, Claude Code, Cursor, Gemini CLI, Antigravity, Grok CLI, Pi, OpenCode, and VS Code — useful for adapter authors and anyone tracing why a record was or wasn't found.
:::

:::{grid-item-card} DB index
:link: db-index
:link-type: doc
SQLite cache and sync planner for the persistent DB index.
:::

:::{grid-item-card} Architecture decisions
:link: adr/index
:link-type: doc
Decision records for storage, parsing, and compatibility policies.
:::

::::

```{toctree}
:hidden:

benchmark
storage-catalog
db-index
adr/index
```
