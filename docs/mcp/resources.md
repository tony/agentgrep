(mcp-resources)=

# Resources

MCP resources expose passive read-only data at `agentgrep://` URIs. Clients read them with `resources/read`.

## Capability summary

```{fastmcp-resource} agentgrep_capabilities
```

Read `agentgrep://capabilities` to see supported agents, adapters,
search scopes, tools, resources, prompts, and optional backend
selections.

## Sources

```{fastmcp-resource} agentgrep_sources
```

Read `agentgrep://sources` to list every discovered source.
Each source includes a `version_detection` object with the detected
app version, detected data version, strategy, confidence, and evidence
used to interpret that source.

## Sources by agent

```{fastmcp-resource-template} agentgrep_sources_by_agent
```

Read `agentgrep://sources/codex`, `agentgrep://sources/claude`,
`agentgrep://sources/cursor-cli`, `agentgrep://sources/cursor-ide`,
`agentgrep://sources/gemini`, `agentgrep://sources/antigravity-cli`,
`agentgrep://sources/antigravity-ide`, `agentgrep://sources/grok`,
`agentgrep://sources/pi`, or `agentgrep://sources/opencode` to filter
discovery by agent.

## Store catalog

```{fastmcp-resource} agentgrep_catalog
```

Read `agentgrep://catalog` for the canonical catalog of every store agentgrep knows about — role, format, upstream reference, and schema notes per entry.

(mcp-resource-query-language)=

## Query language

```{fastmcp-resource} agentgrep_query_language
```

Read `agentgrep://query-language` for the field and operator catalog the
{tool}`search` tool accepts: every queryable field with its kind, layer,
aliases, and enum values, plus the boolean / phrase / wildcard / range
operators with copy-pasteable examples. The catalog is generated from the
same registry the compiler uses, so it never drifts from what the tools
actually accept. {tool}`search` honors this query language inline; call
{tool}`validate_query` to dry-run a query's syntax (parse + compile) without
running a search. See {ref}`library-query-language` for the full prose
reference.

## Store roles

```{fastmcp-resource} agentgrep_store_roles
```

Read `agentgrep://store-roles` for the enumeration of role values (`primary_chat`, `prompt_history`, `app_state`, …) with one-line descriptions.

## Store formats

```{fastmcp-resource} agentgrep_store_formats
```

Read `agentgrep://store-formats` for the enumeration of on-disk format values (`jsonl`, `sqlite`, `md_frontmatter`, …) with one-line descriptions.
