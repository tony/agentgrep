(package-agentgrep-tutorial)=

# Tutorial

## Search prompts

Search user prompts across all supported stores:

```console
$ uv run agentgrep grep "draft pr"
```

Search only Codex prompts:

```console
$ uv run agentgrep grep "draft pr" --agent codex
```

## Search conversations

Search assistant, tool, event, and full conversation records:

```console
$ uv run agentgrep grep "pytest" --scope conversations
```

Search prompts and conversations together:

```console
$ uv run agentgrep grep "docs" --scope all
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
