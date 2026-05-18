(package-agentgrep-how-to)=

# How to

## Find stores before searching

```console
$ uv run agentgrep find
```

Filter discovery to Codex session files:

```console
$ uv run agentgrep find sessions --agent codex
```

## Cap result count

```console
$ uv run agentgrep search "migration" --limit 5
```

## Answer before the scan finishes

Interactive text searches show a progress line. Press Enter on a blank line to stop scanning and print the matches collected so far.

```console
$ uv run agentgrep search "bliss"
```

## Keep scripts quiet

Use structured output and disable progress:

```console
$ uv run agentgrep search "release" --json --progress never
```

Progress, when enabled for JSON or NDJSON output, is written to stderr only.

## Use MCP from a client

Connect the `agentgrep` MCP server, then ask the client to search local agent history. The client can call:

- {tool}`search` for full records
- {tool}`find` for store discovery
- `agentgrep://capabilities` for server metadata
