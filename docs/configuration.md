(configuration)=

# Configuration

agentgrep is intentionally low-configuration. It reads known local agent stores under the current user's home directory and never mutates them.

## Agent selection

Use `--agent` one or more times to limit search or discovery:

```console
$ uv run agentgrep search "cache" --agent codex
```

Supported agents are `codex`, `claude`, and `cursor`. Omitting `--agent` searches all supported agents.

## Search type

Use `--type` to choose records:

```console
$ uv run agentgrep search "docs deploy" --type prompts
```

Allowed values are `prompts`, `history`, and `all`.

## Output

Text output is optimized for terminal reading:

```console
$ uv run agentgrep search "release"
```

Use JSON or NDJSON for scripts:

```console
$ uv run agentgrep search "release" --json
```

```console
$ uv run agentgrep search "release" --ndjson
```

## Progress and early answers

Human text searches show progress by default. Press Enter on a blank line to return the matches collected so far.

```console
$ uv run agentgrep search "bliss" --progress always
```

Disable progress when scripting:

```console
$ uv run agentgrep search "bliss" --progress never
```

## Privacy

Serialized paths are protected before leaving the process. Home-relative paths are displayed as `~/...`, and directory paths keep a trailing `/`, for example `~/.codex/sessions/`.

## MCP capabilities

MCP clients can read `agentgrep://capabilities` to inspect supported agents, adapters, tools, resources, prompts, and selected optional backends.
