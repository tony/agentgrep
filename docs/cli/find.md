(cli-find)=

# agentgrep find

The `agentgrep find` command enumerates the on-disk prompt and history
stores agentgrep can read — Codex session files, Claude Code JSONL
transcripts, Cursor SQLite databases, Gemini history. Use it to
inspect what agentgrep sees before running a search, or to feed a
catalog into another tool.

## Examples

List every store agentgrep can read for one agent:

```console
$ agentgrep find codex
```

Filter by a path substring within an agent:

```console
$ agentgrep find sessions --agent codex
```

Emit the catalog as JSON for downstream tools:

```console
$ agentgrep find cursor --json
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
