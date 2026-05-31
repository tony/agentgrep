(package-agentgrep-tutorial)=

# Tutorial

## Search prompts

Search user prompts across all supported stores:

```console
$ uv run agentgrep grep "draft pr"
```

Search only Codex prompts:

```console
$ uv run agentgrep grep "draft pr" --agent codex --type prompts
```

## Search history

Search assistant and command history:

```console
$ uv run agentgrep grep "pytest" --type history
```

Search prompts and history together:

```console
$ uv run agentgrep grep "docs" --type all
```

## Combine terms

Require every term:

```console
$ uv run agentgrep grep docs deploy
```

Use regular expressions (regex is the default):

```console
$ uv run agentgrep grep "docs?.*deploy"
```

## Return structured output

Pretty JSON:

```console
$ uv run agentgrep grep "release" --json
```

Line-delimited JSON:

```console
$ uv run agentgrep grep "release" --ndjson
```
