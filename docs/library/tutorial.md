(package-agentgrep-tutorial)=

# Tutorial

## Search prompts

Search user prompts across all supported stores:

```console
$ uv run agentgrep search "draft pr"
```

Search only Codex prompts:

```console
$ uv run agentgrep search "draft pr" --agent codex --type prompts
```

## Search history

Search assistant and command history:

```console
$ uv run agentgrep search "pytest" --type history
```

Search prompts and history together:

```console
$ uv run agentgrep search "docs" --type all
```

## Combine terms

Require every term:

```console
$ uv run agentgrep search docs deploy
```

Match any term:

```console
$ uv run agentgrep search docs deploy --any
```

Use regular expressions:

```console
$ uv run agentgrep search "docs?.*deploy" --regex
```

## Return structured output

Pretty JSON:

```console
$ uv run agentgrep search "release" --json
```

Line-delimited JSON:

```console
$ uv run agentgrep search "release" --ndjson
```
