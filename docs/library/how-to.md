(package-agentgrep-how-to)=

# How to

These recipes cover the library-adjacent tasks people usually reach for after
their first search: discover stores, bound output, stop an interactive scan
early, and hand the same surface to an MCP client. Start with the first command
that matches your question; the examples stay small on purpose.

## Find stores before searching

Use discovery when you want to know which agent history files agentgrep can
read before you search them.

```console
$ uv run agentgrep find
```

Filter discovery to Codex session files:

```console
$ uv run agentgrep find sessions --agent codex
```

## Cap result count

Use a limit when you are checking whether a term exists and do not need every
matching record.

```console
$ uv run agentgrep grep "migration" --limit 5
```

## Answer before the scan finishes

Interactive text searches show a progress line. Press Enter on a blank line to stop scanning and print the matches collected so far.

```console
$ uv run agentgrep grep "bliss"
```

## Keep scripts quiet

Use structured output and disable progress:

```console
$ uv run agentgrep grep "release" --json --progress never
```

Progress, when enabled for JSON or NDJSON output, is written to stderr only.

## Use MCP from a client

Connect the `agentgrep` MCP server, then ask the client to search local agent history. The client can call:

- {tool}`search` for full records
- {tool}`find` for store discovery
- `agentgrep://capabilities` for server metadata
