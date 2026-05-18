(package-agentgrep-examples)=

# Examples

## CLI search

```console
$ uv run agentgrep search "database migration" --agent codex --limit 10
```

## CLI discovery

```console
$ uv run agentgrep find cursor --agent cursor --json
```

## MCP search call

```json
{
  "tool": "search",
  "arguments": {
    "terms": ["database migration"],
    "agent": "codex",
    "search_type": "prompts",
    "limit": 10
  }
}
```

## MCP discovery call

```json
{
  "tool": "find",
  "arguments": {
    "pattern": "sessions",
    "agent": "codex",
    "limit": 50
  }
}
```

## MCP resource reads

```text
agentgrep://capabilities
agentgrep://sources
agentgrep://sources/codex
```
