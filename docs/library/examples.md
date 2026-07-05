(package-agentgrep-examples)=

# Examples

These examples show the same read-only search surface from the two common
entry points: CLI commands for humans at a shell, and MCP payloads for clients
that call agentgrep as a tool.

## CLI search

Search only Codex prompt records and stop after ten matches:

```console
$ uv run agentgrep grep "database migration" --agent codex --limit 10
```

## CLI discovery

List Cursor CLI sources as structured JSON when you want to script the result:

```console
$ uv run agentgrep find cursor-cli --agent cursor-cli --json
```

## MCP search call

Call {tool}`search` when a client needs normalized result records rather than
terminal text:

```json
{
  "tool": "search",
  "arguments": {
    "terms": ["database migration"],
    "agent": "codex",
    "scope": "prompts",
    "limit": 10
  }
}
```

## MCP discovery call

Call {tool}`find` when a client needs source metadata before choosing a search:

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

Read resources when the client needs passive metadata without running a tool:

```text
agentgrep://capabilities
agentgrep://sources
agentgrep://sources/codex
```
