(mcp)=

# MCP

agentgrep's MCP server exposes a read-only search surface over stdio.
Search defaults to prompt scope; full conversation records are an
explicit `scope="conversations"` opt-in. The server does not mutate
local agent stores, open SQLite in write mode, or execute arbitrary
shell commands.

## Install

Pick a client, install method, and config scope. The snippet copies directly into your terminal or config file.

```{mcp-install}
```

::::{grid} 1 1 3 3
:gutter: 2

:::{grid-item-card} Tools
:link: tools
:link-type: doc
Invoke search and discovery.
:::

:::{grid-item-card} Resources
:link: resources
:link-type: doc
Read capabilities and source inventories.
:::

:::{grid-item-card} Prompts
:link: prompts
:link-type: doc
Reusable client-side search recipes.
:::

:::{grid-item-card} API Reference
:link: reference
:link-type: doc
Payload models, server factory, and MCP helpers.
:::

::::

## Search Tool

<a class="reference internal" href="tools/#fastmcp-tool-search"><code>search</code></a>

## Discovery

<a class="reference internal" href="tools/#fastmcp-tool-find"><code>find</code></a>


```{toctree}
:hidden:

tools
resources
prompts
reference
```
