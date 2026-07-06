(library)=

# Library

Use `agentgrep` as a Python library from your own scripts and tools.
The same search, discovery, parsing, serialization, and path-privacy
layer powers the terminal CLI and the MCP server, so anything you can do
from the command line you can drive directly in code. Search results may
carry {class}`~agentgrep.RecordOrigin`; its path fields use the same
{class}`~agentgrep.PrivatePath` display layer as source paths, and
remote URLs are serialized without credentials.

## Install

Pick an install method below — the snippet copies straight into your terminal, and the runnable quickstart mirrors the same search query you'd run from the CLI.

```{library-install}
```

::::{grid} 1 1 2 2
:gutter: 2

:::{grid-item-card} Tutorial
:link: tutorial
:link-type: doc
Run the CLI from first search to structured output.
:::

:::{grid-item-card} How to
:link: how-to
:link-type: doc
Common workflows for search, discovery, progress, and scripting.
:::

:::{grid-item-card} API Reference
:link: reference
:link-type: doc
Core data types, discovery, search pipeline, and progress APIs.
:::

:::{grid-item-card} Examples
:link: examples
:link-type: doc
CLI and MCP request examples.
:::

::::


```{toctree}
:hidden:

tutorial
how-to
event-stream
query-language
reference
examples
```
