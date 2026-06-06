(index)=

# agentgrep

Read-only search for local AI agent prompts and opt-in conversations across Codex, Claude Code, Cursor, Gemini, Antigravity, Grok, Pi, OpenCode, and VS Code.

```{warning}
**Pre-alpha.** APIs may change. [Feedback welcome](https://github.com/tony/agentgrep/issues).
```

```{cli-install}
:variant: compact
```

```{mcp-install}
:variant: compact
```

## Try it

Ranked search across every agent's prompts:

```console
$ agentgrep search "deploy"
```

Sweep prompts and conversations together:

```console
$ agentgrep search "deploy" --scope all
```

Browse matches in the interactive explorer:

```console
$ agentgrep ui "deploy"
```

::::{grid} 1 1 2 3
:gutter: 2 2 3 3

:::{grid-item-card} Quickstart
:link: getting-started/index
:link-type: doc
Run a first search and inspect the result shape.
:::

:::{grid-item-card} CLI
:link: cli/index
:link-type: doc
Search and find from the terminal. Pipe `--json` / `--ndjson` for scripts and agents.
:::

:::{grid-item-card} TUI
:link: tui/index
:link-type: doc
Interactive Textual explorer for browsing prompt and conversation records.
:::

:::{grid-item-card} Insights
:link: insights/index
:link-type: doc
Compare indexed records, detect omissions, and review suggestions.
:::

:::{grid-item-card} MCP
:link: mcp/index
:link-type: doc
Tools, resources, and prompts for MCP clients.
:::

:::{grid-item-card} Library
:link: library/index
:link-type: doc
Tutorial, how-to, reference, and examples for the Python library.
:::

:::{grid-item-card} Client Setup
:link: getting-started/clients
:link-type: doc
Config snippets for local MCP clients.
:::

:::{grid-item-card} Configuration
:link: getting-started/configuration
:link-type: doc
Search behavior, privacy, output, and progress controls.
:::

:::{grid-item-card} Backends
:link: backends/index
:link-type: doc
Per-agent store layouts, record schemas, and support matrix.
:::

::::

## What you can do

### Prompt Search

Find prompt records by literal term or regular expression, with explicit opt-in for conversations.

<a class="reference internal" href="mcp/tools/#fastmcp-tool-search"><code>search</code></a>

### Discovery

List the stores, session files, and SQLite databases that agentgrep can read.

<a class="reference internal" href="mcp/tools/#fastmcp-tool-find"><code>find</code></a>

### Insights

Compare indexed records, list persisted omissions, and review suggested
instruction changes.

{ref}`insights`

### MCP guidance

Use prompts for common agent workflows:

{ref}`fastmcp-prompt-search-prompts` · {ref}`fastmcp-prompt-search-conversations` · {ref}`fastmcp-prompt-inspect-stores`

```{toctree}
:hidden:

getting-started/index
cli/index
tui/index
library/index
insights/index
mcp/index
backends/index
dev/index
history
GitHub <https://github.com/tony/agentgrep>
```
