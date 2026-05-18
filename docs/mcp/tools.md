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

## Time-Windowed Activity

```{fastmcp-tool} recent_sessions
```

**Use when** you want the most-recently modified sources for an agent — newest-first, optionally bounded by a time window.

**Returns:** the cutoff timestamp plus source records ordered by ``mtime_ns`` descending.

```{fastmcp-tool-input} recent_sessions
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

## Structured Source Listing

```{fastmcp-tool} list_sources
```

**Use when** you want a structured listing of discovered sources with optional path-kind / source-kind filters.

```{fastmcp-tool-input} list_sources
```

## Required-Pattern Filtering

```{fastmcp-tool} filter_sources
```

**Use when** you want to narrow discovered sources by required substring pattern (a stricter ``find``).

```{fastmcp-tool-input} filter_sources
```

## Discovery Counts

```{fastmcp-tool} summarize_discovery
```

**Use when** you want aggregate counts of discovered sources by agent, format, and path-kind.

```{fastmcp-tool-input} summarize_discovery
```

## Catalog

```{fastmcp-tool} list_stores
```

**Use when** you want the canonical catalog of on-disk stores agentgrep knows about — including stores that are not searched by default.

```{fastmcp-tool-input} list_stores
```

```{fastmcp-tool} get_store_descriptor
```

**Use when** you need the full descriptor (role, format, upstream reference, schema notes) for a single store id.

```{fastmcp-tool-input} get_store_descriptor
```

```{fastmcp-tool} inspect_record_sample
```

**Use when** you want a few raw records from one adapter+path to validate parser output or discover schema variations.

```{fastmcp-tool-input} inspect_record_sample
```

## Diagnostics

```{fastmcp-tool} validate_query
```

**Use when** you want to dry-run a regex or literal pattern against sample text before issuing a broad cross-agent search.

```{fastmcp-tool-input} validate_query
```
