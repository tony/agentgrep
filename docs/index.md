(index)=

# agentgrep

Read-only search for local AI agent prompts and history across Codex, Claude Code, and Cursor.

agentgrep has two entry points: a terminal CLI for direct search, and a FastMCP server for clients that want structured tools, resources, and prompts. Both surfaces use the same read-only discovery and parsing layer.

```{warning}
**Pre-alpha.** APIs may change. [Feedback welcome](https://github.com/tony/agentgrep/issues).
```

::::{grid} 1 1 2 3
:gutter: 2 2 3 3

:::{grid-item-card} Quickstart
:link: quickstart
:link-type: doc
Run a first search and inspect the result shape.
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

:::{grid-item-card} API Reference
:link: reference/api/index
:link-type: doc
Curated Python and MCP API documentation.
:::

:::{grid-item-card} Client Setup
:link: clients
:link-type: doc
Config snippets for local MCP clients.
:::

:::{grid-item-card} Configuration
:link: configuration
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
:caption: Get started

quickstart
installation
clients
configuration
storage-catalog
```

```{toctree}
:hidden:
:caption: Library

library/index
```

```{toctree}
:hidden:
:caption: MCP

mcp/index
```

```{toctree}
:hidden:
:caption: Reference

reference/api/index
```

```{toctree}
:hidden:
:caption: Project

history
GitHub <https://github.com/tony/agentgrep>
```
