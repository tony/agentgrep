(index)=

# agentgrep

Read-only search for local AI agent prompts and history across Codex, Claude Code, Cursor, and Gemini.

```{warning}
**Pre-alpha.** APIs may change. [Feedback welcome](https://github.com/tony/agentgrep/issues).
```

```{cli-install}
:variant: compact
```

```{mcp-install}
:variant: compact
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
Interactive Textual explorer for browsing prompts and history.
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

::::

## What you can do

### Prompt Search

Find full prompt and history records by literal term or regular expression.

<a class="reference internal" href="mcp/tools/#fastmcp-tool-search"><code>search</code></a>

### Discovery

List the stores, session files, and SQLite databases that agentgrep can read.

<a class="reference internal" href="mcp/tools/#fastmcp-tool-find"><code>find</code></a>

### MCP guidance

Use prompts for common agent workflows:

{ref}`fastmcp-prompt-search-prompts` · {ref}`fastmcp-prompt-search-history` · {ref}`fastmcp-prompt-inspect-stores`

```{toctree}
:hidden:

getting-started/index
cli/index
tui/index
library/index
mcp/index
dev/index
history
GitHub <https://github.com/tony/agentgrep>
```
