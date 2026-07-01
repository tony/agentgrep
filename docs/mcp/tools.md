(mcp-tools)=

# Tools

agentgrep's tools are read-only. They return structured Pydantic models and protect private paths before serialization.

## Prompt and Conversation Search

```{fastmcp-tool} search
```

**Use when** you need prompt records matching one or more terms. Pass
`scope="conversations"` for full conversation, assistant, tool, and
event records, or `scope="all"` for both surfaces.

**Returns:** request metadata, run status, result stats, page metadata, and normalized records with `ref`, agent, store, adapter, path, text, title, role, timestamp, model, session ID, conversation ID, and metadata. When `page.next_cursor` is present, pass it back as `cursor` to continue the same search without rebuilding the request.

**Example:**

```json
{
  "tool": "search",
  "arguments": {
    "terms": ["release notes"],
    "agent": "all",
    "scope": "prompts",
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

**Returns:** request metadata, run status, result stats, page metadata, and source records with `ref`, agent, store, adapter, protected path, path kind, and metadata. When `page.next_cursor` is present, pass it back as `cursor` to continue the same discovery scan.

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

**Use when** you want a structured listing of discovered sources with
optional path-kind, source-kind, and coverage filters. By default this
matches the default-search surface; pass `include_non_default=true` or
set `coverage_filter` to inspect inventory-only stores such as Codex
SQLite DBs or Claude session memory. Each returned source includes
`searchable`, `search_by_default`, `searchable_reason`, `inspectable`,
and `version_detection`, which records the strategy and evidence
agentgrep used to identify the app/data version for that concrete file
or DB.

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

```{fastmcp-tool} inspect_result
```

**Use when** you have a `ref` returned by `search` or `find` and need to inspect the matching result or sample records from that source without reconstructing local paths.

```{fastmcp-tool-input} inspect_result
```

## Diagnostics

```{fastmcp-tool} validate_query
```

**Use when** you want to dry-run a literal pattern against sample text before issuing a broad cross-agent search.

```{fastmcp-tool-input} validate_query
```
