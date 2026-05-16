(mcp-tools)=

# Tools

agentgrep's tools are read-only. They return structured Pydantic models and protect private paths before serialization.

## Prompt and History Search

```{fastmcp-tool} search
:no-index:
```

**Use when** you need full prompt or history records matching one or more terms.

**Returns:** query metadata plus normalized records with agent, store, adapter, path, text, title, role, timestamp, model, session ID, conversation ID, and metadata.

**Example:**

```json
{
  "tool": "search",
  "arguments": {
    "terms": ["release notes"],
    "agent": "all",
    "search_type": "prompts",
    "limit": 20
  }
}
```

```{fastmcp-tool-input} search
```

## Store Discovery

```{fastmcp-tool} find
```

**Use when** you need to inspect which stores, session files, and databases agentgrep can read.

**Returns:** query metadata plus source records with agent, store, adapter, protected path, path kind, and metadata.

**Example:**

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

```{fastmcp-tool-input} find
```
