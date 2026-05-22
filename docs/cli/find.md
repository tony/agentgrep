(cli-find)=

# agentgrep find

The `agentgrep find` command enumerates the on-disk prompt and history
stores agentgrep can read — Codex session files, Claude Code JSONL
transcripts, Cursor SQLite databases, Gemini history. Use it to
inspect what agentgrep sees before running a search, or to feed a
catalog into another tool.

The flag grammar mirrors `fd`: the positional PATTERN is treated as a
regex by default, with `-F` (literal), `-g` (glob), and `--exact`
modifiers; `-t` filters by record kind; `-e` filters by file
extension; `-l` switches to a long-format output; `-0` separates
output with NUL for `xargs -0` consumers.

The default output is **one path per line** — the fd-faithful
shape. Use `-l/--list-details` to add metadata (agent, kind, store,
adapter_id) as tab-separated columns.

## Examples

List every store agentgrep can read for one agent:

```console
$ agentgrep find codex
```

Filter by literal substring (the legacy default before fd alignment):

```console
$ agentgrep find -F sessions
```

Restrict to one record kind and one file extension:

```console
$ agentgrep find -t prompts -e jsonl
```

Long format for column-aware downstream tools:

```console
$ agentgrep find -l
```

NUL-separated output for `xargs -0`:

```console
$ agentgrep find -0 | xargs -0 -n1 ls -l
```

Open the Textual explorer pre-filled with the find query:

```console
$ agentgrep find -t prompts --ui
```

Silence the source-discovery spinner:

```console
$ agentgrep find --no-progress codex
```

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: find
    :nodescription:
```

(cli-find-json-output)=

## JSON output

Pass `--json` to emit a single JSON document containing every
discovered store:

```console
$ agentgrep find --json
```

The envelope carries a list of {class}`~agentgrep.FindRecord` entries.
Each record carries an `agent` tag, the absolute path, the store
kind, and discovery metadata.

`--json` is the right mode when the caller wants to parse the entire
catalog at once — for example a wrapping agent that decides which
stores to read before issuing a `search` call.

## NDJSON output

Pass `--ndjson` to stream one JSON object per line:

```console
$ agentgrep find --ndjson | jq -r '.path'
```

Each line is a single {class}`~agentgrep.FindRecord`. Use this mode
when piping into `jq`, into another CLI, or into a non-MCP agent that
consumes the catalog incrementally.

## Filtering by agent

`--agent` is repeatable and limits discovery to specific backends:

```console
$ agentgrep find --agent claude --agent codex
```

Pass `--agent all` (or omit the flag) to enumerate every available
backend.
