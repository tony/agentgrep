(cli)=

(cli-index)=

# CLI

The `agentgrep` CLI is the fastest path to your local AI agent prompt
and history archives from a terminal. It wraps the same read-only
discovery and parsing layer the MCP server exposes — search, find
stores, filter by agent — and lets you pipe everything through
`--json` or `--ndjson` so any script or non-MCP agent can consume the
results. Bare `agentgrep` (no subcommand) lands in an interactive
Textual explorer for browsing without piping.

```{cli-install}
```

::::{grid} 1 2 2 3
:gutter: 2 2 3 3

:::{grid-item-card} agentgrep ui
:link: ui
:link-type: doc
Browse prompts and history interactively in the Textual explorer.
:::

:::{grid-item-card} agentgrep search
:link: search
:link-type: doc
Search prompts and history with sensible serene-DX defaults.
:::

:::{grid-item-card} agentgrep grep
:link: grep
:link-type: doc
Content search with rg/ag-shaped flags, output, and exit codes.
:::

:::{grid-item-card} agentgrep find
:link: find
:link-type: doc
Enumerate on-disk stores with fd-shaped flag grammar.
:::

:::{grid-item-card} agentgrep fuzzy
:link: fuzzy
:link-type: doc
Non-interactive fuzzy match on stdin, shaped like `fzf --filter`.
:::

::::

## --ui overlay

Every search-shaped subcommand accepts `--ui`: pass it to open the
Textual explorer pre-filled with the same query you'd otherwise run
as a one-shot. This is the `tig`-shaped overlay model — `agentgrep
grep -i foo --ui` is to `agentgrep grep -i foo` what `tig log` is to
`git log`. Same args, same query semantics, different presentation.

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
