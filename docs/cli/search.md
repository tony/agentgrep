(cli-search)=

# agentgrep search

The `agentgrep search` command searches normalized prompts and history
records across every configured agent backend (Codex, Claude Code,
Cursor, Gemini). Search is read-only — agentgrep never mutates the
source stores.

## Examples

A literal single-term search:

```console
$ agentgrep search bliss
```

Combine multiple terms with an agent filter:

```console
$ agentgrep search serene bliss --agent codex
```

Stream history matches as NDJSON for piping:

```console
$ agentgrep search prompt history --type history --ndjson
```

Emit a single JSON document for one-shot consumers:

```console
$ agentgrep search serenity --json
```

Browse interactively in the Textual TUI:

```console
$ agentgrep search design --ui
```

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: search
    :nodescription:
```

(cli-search-json-output)=

## JSON output

Pass `--json` to emit a single JSON document containing the matching
records:

```console
$ agentgrep search --json "deploy"
```

The envelope carries a list of normalized
{class}`~agentgrep.SearchRecord` entries. Each record carries an
`agent` tag (`codex` / `claude` / `cursor` / `gemini`), the source
path, the matching text excerpt, and session metadata when available.

`--json` is the right mode when the caller wants to parse the entire
result at once — for example a wrapping agent that scores or
post-processes records before presenting them.

## NDJSON output

Pass `--ndjson` to stream one JSON object per line:

```console
$ agentgrep search --ndjson "deploy" | jq '.agent'
```

Each line is a single {class}`~agentgrep.SearchRecord`. This mode is
the right pick for piping into `jq`, into another CLI, or into a
non-MCP agent that consumes results incrementally.

## Interactive UI

Pass `--ui` to launch the Textual TUI instead of streaming records to
stdout:

```console
$ agentgrep search --ui "deploy"
```

The TUI is read-only — it renders the same records the JSON modes
emit, but lets you scroll, filter, and inspect record bodies
interactively. `--ui` is mutually exclusive with `--json` and
`--ndjson`.

See {ref}`cli-ui` for the standalone explorer entry point. Bare
`agentgrep` is equivalent to `agentgrep ui`, and `agentgrep ui
<query>` seeds the search bar without leaving the explorer to run a
one-shot CLI query.

## Filtering by agent

`--agent` is repeatable and limits the search to specific backends:

```console
$ agentgrep search --agent claude --agent codex "deploy"
```

Pass `--agent all` (or omit the flag) to search every available
backend.
