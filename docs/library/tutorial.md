(package-agentgrep-tutorial)=

# Tutorial

This tutorial starts with the default prompt-search path and then opens the
smaller doors: ranked results, full conversation records, multi-term matching,
and structured output. You can stop after the first section if all you need is
"what did I ask my agents about this?"

## Search prompts

Search user prompts in fast prompt-history stores:

```console
$ uv run agentgrep grep "draft pr"
```

Search only Codex prompts:

```console
$ uv run agentgrep grep "draft pr" --agent codex
```

Use matching prompts to select and search a bounded set of conversations:

```console
$ uv run agentgrep grep "draft pr" --deep
```

Targeted search attempts at most 25 conversations by default and reports
approximate coverage. The unmeasured work bound is independent of the result
limit, and unresolved selected conversations are not backfilled.

Search prompt records across every readable conversation backend:

```console
$ uv run agentgrep grep "draft pr" --exhaustive
```

## Ranked search

`search` ranks, dedupes, and groups results by session — the smart
default when you want the most relevant matches first:

```console
$ uv run agentgrep search "draft pr"
```

Sweep prompts and conversations together exhaustively:

```console
$ uv run agentgrep search "draft pr" --exhaustive --scope all
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
