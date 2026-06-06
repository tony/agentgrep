(configuration)=

# Configuration

agentgrep is intentionally low-configuration. It reads known local agent stores under the current user's home directory and never mutates them.

## Agent selection

Use `--agent` one or more times to limit search or discovery:

```console
$ uv run agentgrep grep "cache" --agent codex
```

Supported agents are `codex`, `claude`, `cursor-cli`, `cursor-ide`, `gemini`, `grok`, `pi`, and `opencode`. Omitting `--agent` searches all supported agents.

## Search scope

Search and grep default to prompt scope: user-authored prompts,
including dedicated prompt-history logs and user turns projected from
transcript-only stores. Use `--scope` to opt into broader records:

```console
$ uv run agentgrep grep "docs deploy" --scope conversations
```

Allowed values are `prompts`, `conversations`, and `all`.

## DB cache

Search-shaped commands default to `--cache auto`. When an agentgrep
database already exists and can answer the query, agentgrep can use the
SQLite index; otherwise it falls back to the live scanner.

Force a fresh live scan for cold-path checks and benchmarks:

```console
$ uv run agentgrep grep "release" --no-cache
```

Require the DB path:

```console
$ uv run agentgrep search "release" --cache require
```

## Output

Text output is optimized for terminal reading:

```console
$ uv run agentgrep grep "release"
```

Use JSON or NDJSON for scripts:

```console
$ uv run agentgrep grep "release" --json
```

```console
$ uv run agentgrep grep "release" --ndjson
```

## Progress and early answers

Human text searches show progress by default. Press Enter on a blank line to return the matches collected so far.

```console
$ uv run agentgrep grep "bliss" --progress always
```

Disable progress when scripting:

```console
$ uv run agentgrep grep "bliss" --progress never
```

## Privacy

Serialized paths are protected before leaving the process. Home-relative paths are displayed as `~/...`, and directory paths keep a trailing `/`, for example `~/.codex/sessions/`.

## MCP capabilities

MCP clients can read `agentgrep://capabilities` to inspect supported agents, adapters, tools, resources, prompts, and selected optional backends.
