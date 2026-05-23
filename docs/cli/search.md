(cli-search)=

# agentgrep search

The `agentgrep search` command searches normalized prompts and history
records across every configured agent backend (Codex, Claude Code,
Cursor, Gemini). Search is read-only — agentgrep never mutates the
source stores.

```{note}
Versions before 0.1.0a5 made `agentgrep <terms>` an implicit
shorthand for `agentgrep search <terms>`. That shortcut is gone.
Spell `search` out so the available subcommands stay discoverable
through `agentgrep --help`. Users who reach for raw substring or
regex matching may prefer `agentgrep grep` (rg-shaped) over
`agentgrep search` (sensible-defaults).
```

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

Silence the stderr progress spinner:

```console
$ agentgrep search --no-progress design
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

Pass `--ui` to open the {ref}`Textual explorer <tui>` pre-filled
with the search query. Mutually exclusive with `--json` and
`--ndjson`.

## Filtering by agent

`--agent` is repeatable and limits the search to specific backends:

```console
$ agentgrep search --agent claude --agent codex "deploy"
```

Pass `--agent all` (or omit the flag) to search every available
backend.

## Query language

`search` accepts Lucene-style field predicates, boolean
composition, and date ranges inline with the positional terms:

```console
$ agentgrep search agent:codex bliss
```

```console
$ agentgrep search '(agent:codex OR agent:cursor) AND deploy'
```

```console
$ agentgrep search 'timestamp:>2026-01-01 -agent:claude bliss'
```

See {ref}`library-query-language` for the full grammar, field
registry, and date literal forms. Mixing the new field syntax with
the equivalent flag (`--agent codex agent:claude`) is rejected at
parse time.
