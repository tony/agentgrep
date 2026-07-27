(configuration)=

# Configuration

agentgrep is intentionally low-configuration. It reads known local agent stores under the current user's home directory and never mutates them.

## Agent selection

Use `--agent` one or more times to limit search or discovery:

```console
$ uv run agentgrep grep "cache" --agent codex
```

Supported agents are `codex`, `claude`, `cursor-cli`, `cursor-ide`,
`gemini`, `antigravity-cli`, `antigravity-ide`, `grok`, `pi`,
`opencode`, and `vscode`. Omitting `--agent` searches all supported
agents. Windsurf storage is documented but unsupported (its
conversations are encrypted); see {doc}`/backends/unsupported/index`.

## Search depth and result scope

Search and grep open only dedicated prompt-history stores by default.
This is the fast path. Agents without a dedicated prompt-history store,
including Cursor IDE, OpenCode, and Pi, contribute records only to a
full exhaustive search:

```console
$ uv run agentgrep grep "docs deploy" --exhaustive
```

Depth controls which backends may be read. `--scope` separately controls
which record kinds may be returned. Use `--deep` for bounded,
prompt-guided conversation routing; only Codex, Claude Code, Grok, and
Antigravity CLI currently provide proof-bound prompt-to-conversation
locators. Other conversation backends require `--exhaustive`.

```console
$ uv run agentgrep grep "docs deploy" --scope conversations
```

Or search both surfaces at once:

```console
$ uv run agentgrep grep "docs deploy" --scope all
```

Allowed scope values are `prompts`, `conversations`, and `all`. Existing
conversation and all scopes imply exhaustive reads when no effort flag is
present. `--deep` infers `all` when scope is omitted. `--exhaustive` keeps
an omitted CLI scope at `prompts`; add `--scope all` when you want prompt and
conversation records together.

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

Search text output shows progress by default. With active progress, a TTY for
both stdin and stderr, and no structured output, press Enter on a blank line
to return the matches collected so far. This answer-now control also applies
to `search --no-rank`; `grep` has no answer-now control.

```console
$ uv run agentgrep search "bliss" --progress always
```

Disable progress when scripting:

```console
$ uv run agentgrep grep "bliss" --progress never
```

## Privacy

Serialized paths are protected before leaving the process. Home-relative paths are displayed as `~/...`, and directory paths keep a trailing `/`, for example `~/.codex/sessions/`.

## MCP capabilities

MCP clients can read `agentgrep://capabilities` to inspect supported agents, adapters, tools, resources, prompts, and selected optional backends.
