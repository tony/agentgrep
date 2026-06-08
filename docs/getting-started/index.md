(getting-started)=
(quickstart)=

# Getting Started

One path from a checkout to a useful search result.

## 1. Install dependencies

From the repository root:

```console
$ uv sync --all-groups
```

## 2. Search local agent prompts

Ranked search across supported stores — deduped, newest first:

```console
$ uv run agentgrep search "release notes"
```

Search prompts and conversations together in one sweep:

```console
$ uv run agentgrep search "release notes" --scope all
```

Prefer ripgrep-shaped flags? `grep` searches prompt-scope records
across supported stores:

```console
$ uv run agentgrep grep "release notes"
```

Search one agent's prompt records:

```console
$ uv run agentgrep grep "deploy docs" --agent codex
```

Search full conversation records explicitly:

```console
$ uv run agentgrep grep "deploy docs" --agent codex --scope conversations
```

## 3. Inspect the stores

See which files and databases agentgrep can read:

```console
$ uv run agentgrep find
```

Filter discovery output:

```console
$ uv run agentgrep find sessions --agent codex
```

## 4. Use MCP

Run the local stdio server:

```console
$ uv run agentgrep-mcp
```

Or run the FastMCP config:

```console
$ uv run fastmcp run fastmcp.json
```

See {ref}`clients` for MCP client snippets.

## Next steps

- {doc}`../library/tutorial` walks through CLI search in more detail.
- {doc}`../mcp/tools` documents the MCP tool payloads.
- {doc}`configuration` explains output, progress, privacy, and source selection.

```{toctree}
:hidden:

installation
clients
configuration
```
