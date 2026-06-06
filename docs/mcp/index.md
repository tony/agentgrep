(mcp)=

# MCP

agentgrep's MCP server exposes a read-only search surface over stdio.
Search opens fast prompt-history stores by default. Set
`effort="targeted"` to search a bounded set of conversations selected
from prompt evidence, or `effort="exhaustive"` to search every readable
conversation. `scope` separately controls returned record kinds. The
server does not mutate local agent stores, open SQLite in write mode, or
execute arbitrary shell commands.

With targeted or exhaustive effort and omitted scope, MCP infers
`scope="all"`. It does not broaden an explicit prompt scope: targeted effort
with `scope="prompts"` is rejected. Search is cursorless and keeps its
omitted-result-limit default of 20.

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

{tool}`search`

## Discovery

{tool}`find`

## DB and insights

<a class="reference internal" href="tools/#fastmcp-tool-db_status"><code>db_status</code></a>
·
<a class="reference internal" href="tools/#fastmcp-tool-insights_list"><code>insights_list</code></a>
·
<a class="reference internal" href="tools/#fastmcp-tool-suggestions_list"><code>suggestions_list</code></a>


```{toctree}
:hidden:

tools
resources
prompts
reference
```
