(cli)=

(cli-index)=

# CLI

The `agentgrep` CLI is the fastest path to your local AI agent prompt
and history archives from a terminal. It wraps the same read-only
discovery and parsing layer the MCP server exposes — grep, find
stores, filter by agent — and lets you pipe everything through
`--json` or `--ndjson` so any script or non-MCP agent can consume the
results. Bare `agentgrep` (no subcommand) prints a colorized
directory of choices listing every subcommand with example
invocations — the same `tmuxp` / `vcspull` pattern. See {ref}`tui`
for the interactive Textual explorer.

```{note}
Versions before 0.1.0a5 silently rewrote `agentgrep <terms>` as
`agentgrep search <terms>` and bare `agentgrep` as `agentgrep ui`.
Both shortcuts are gone — every subcommand must be named
explicitly. `agentgrep bliss` is now an `invalid choice` error
rather than a grep; reach for `agentgrep grep bliss`.
```

```{cli-install}
```

::::{grid} 1 2 2 3
:gutter: 2 2 3 3

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

:::{grid-item-card} API Reference
:link: reference
:link-type: doc
CLI argument types, serialization helpers, and command entry points.
:::

::::

## --ui overlay

Every search-shaped subcommand accepts `--ui`: pass it to open the
{ref}`Textual explorer <tui>` pre-filled with the same query you'd
otherwise run as a one-shot. This is the `tig`-shaped overlay model —
`agentgrep grep -i foo --ui` is to `agentgrep grep -i foo` what
`tig log` is to `git log`.

## Use from another agent

The CLI is a first-class consumer for any agent that doesn't speak
MCP. Two flags govern machine-readable output:

- `--json` emits a single JSON document with an `envelope` carrying
  the record list. Best when the caller wants to parse the whole
  result at once.
- `--ndjson` streams one JSON object per line. Best for piping into
  `jq`, into another CLI, or into an agent that consumes results
  incrementally.

Both flags work on `grep` and `find`. See
[](#cli-find-json-output) for the record shapes.

Agents that already speak MCP should prefer
[`agentgrep-mcp`](../mcp/index.md) — same discovery and parsing
surface, but exposed as MCP tools with typed schemas.

## Examples

Search prompts with rg-shaped flags:

```console
$ agentgrep grep bliss
```

Combine multiple patterns with an agent filter:

```console
$ agentgrep grep serene bliss --agent codex
```

Stream history matches as NDJSON:

```console
$ agentgrep grep prompt history --type history --ndjson
```

List stores for one agent as JSON:

```console
$ agentgrep find cursor --json
```

Open the directory of choices:

```console
$ agentgrep
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

```{toctree}
:hidden:

grep
find
fuzzy
reference
```
