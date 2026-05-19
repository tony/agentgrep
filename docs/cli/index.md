(cli)=

(cli-index)=

# CLI

The `agentgrep` CLI is the fastest path to your local AI agent prompt
and history archives from a terminal. It wraps the same read-only
discovery and parsing layer the MCP server exposes — search, find
stores, filter by agent — and lets you pipe everything through
`--json` or `--ndjson` so any script or non-MCP agent can consume the
results.

```{cli-install}
```

::::{grid} 1 2 2 2
:gutter: 2 2 3 3

:::{grid-item-card} agentgrep search
:link: search
:link-type: doc
Search prompts and history across every configured agent.
:::

:::{grid-item-card} agentgrep find
:link: find
:link-type: doc
Discover the on-disk stores agentgrep can read.
:::

::::

## Use from another agent

The CLI is a first-class consumer for any agent that doesn't speak
MCP. Two flags govern machine-readable output:

- `--json` emits a single JSON document with an `envelope` carrying
  the record list. Best when the caller wants to parse the whole
  result at once.
- `--ndjson` streams one JSON object per line. Best for piping into
  `jq`, into another CLI, or into an agent that consumes results
  incrementally.

Both flags work on `search` and `find`. See
[](#cli-search-json-output) and [](#cli-find-json-output) for the
record shapes.

Agents that already speak MCP should prefer
[`agentgrep-mcp`](../mcp/index.md) — same discovery and parsing
surface, but exposed as MCP tools with typed schemas.

## Examples

`search` is the default subcommand, so `agentgrep bliss` is equivalent
to `agentgrep search bliss`:

```console
$ agentgrep bliss
```

Combine multiple terms with an agent filter:

```console
$ agentgrep serene bliss --agent codex
```

Stream history matches as NDJSON:

```console
$ agentgrep search prompt history --type history --ndjson
```

List stores for one agent as JSON:

```console
$ agentgrep find cursor --json
```

## Command: `agentgrep`

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :nosubcommands:
    :nodescription:
```
