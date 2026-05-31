(backends)=

# Backends

agentgrep reads on-disk stores from multiple AI coding assistants.
Each backend page documents the agent's path layout, environment
overrides, store descriptors, and record schemas.

## Backend pages

::::{grid} 1 1 2 3
:gutter: 2 2 3 3

:::{grid-item-card} Codex
:link: codex
:link-type: doc
OpenAI Codex CLI history, sessions, instructions, memory, goals, and SQLite state.
:::

:::{grid-item-card} Claude Code
:link: claude
:link-type: doc
Claude Code history, project transcripts, tasks, memory, settings, and plugin surfaces.
:::

:::{grid-item-card} Cursor CLI
:link: cursor-cli
:link-type: doc
`cursor-agent` transcripts, prompt history, chat blobs, and AI-tracking summaries.
:::

:::{grid-item-card} Cursor IDE
:link: cursor-ide
:link-type: doc
Cursor desktop app `state.vscdb` SQLite — global and per-workspace chat history.
:::

:::{grid-item-card} Gemini CLI
:link: gemini
:link-type: doc
Gemini CLI chat sessions, prompt logs, checkpoints, settings, and skills.
:::

:::{grid-item-card} Grok CLI
:link: grok
:link-type: doc
Grok CLI prompt history, session transcripts, memory, logs, and config.
:::

:::{grid-item-card} Pi
:link: pi
:link-type: doc
Pi (earendil-works) session transcripts, settings, prompts, and managed extensions.
:::

:::{grid-item-card} OpenCode
:link: opencode
:link-type: doc
OpenCode (anomalyco) SQLite session store, config, snapshots, and caches.
:::

::::

## Coverage levels

The backend pages distinguish search support from storage coverage.
Default-search stores are opened by normal search and find commands.
Inspectable stores are known and can be inventoried explicitly, but
are not searched by default. Catalog-only stores are documented so
future adapters do not mistake them for prompt history; some catalog
stores expose safe structural samples for `inspect_record_sample`, but
they still stay outside default search. Private stores are documented
but intentionally not enumerated from disk.

## Version detection

Source discovery reports version metadata separately from record
content. agentgrep prefers concrete source evidence over app freshness:
embedded metadata, file/record shape, and SQLite suffixes identify the
data version; local version files provide app-version context only
when they can be read without spawning an upstream CLI. If neither is
available, the catalog observation stamp is reported as a
low-confidence fallback.

## Support matrix

```{storage:coverage-grid}
```

```{toctree}
:hidden:

codex
claude
cursor-cli
cursor-ide
gemini
grok
pi
opencode
```
