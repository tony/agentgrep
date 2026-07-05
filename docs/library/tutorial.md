(package-agentgrep-tutorial)=

# Tutorial

This tutorial starts with the default prompt-search path and then opens the
smaller doors: ranked results, full conversation records, multi-term matching,
and structured output. You can stop after the first section if all you need is
"what did I ask my agents about this?"

## Search prompts

Search user prompts across all supported stores:

```console
$ uv run agentgrep grep "draft pr"
```

Search only Codex prompts:

```console
$ uv run agentgrep grep "draft pr" --agent codex
```

## Ranked search

`search` ranks, dedupes, and groups results by session — the smart
default when you want the most relevant matches first:

```console
$ uv run agentgrep search "draft pr"
```

Sweep prompts and conversations together:

```console
$ uv run agentgrep search "draft pr" --scope all
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
