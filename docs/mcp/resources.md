(mcp-resources)=

# Resources

MCP resources expose passive read-only data at `agentgrep://` URIs. Clients read them with `resources/read`.

## Capability summary

```{fastmcp-resource} agentgrep_capabilities
```

Read `agentgrep://capabilities` to see supported agents, adapters, tools, resources, prompts, and optional backend selections.

## Sources

```{fastmcp-resource} agentgrep_sources
```

Read `agentgrep://sources` to list every discovered source.

## Sources by agent

```{fastmcp-resource-template} agentgrep_sources_by_agent
```

Read `agentgrep://sources/codex`, `agentgrep://sources/claude`, or `agentgrep://sources/cursor` to filter discovery by agent.

## Store catalog

```{fastmcp-resource} agentgrep_catalog
```

Read `agentgrep://catalog` for the canonical catalog of every store agentgrep knows about — role, format, upstream reference, and schema notes per entry.

## Store roles

```{fastmcp-resource} agentgrep_store_roles
```

Read `agentgrep://store-roles` for the enumeration of role values (`primary_chat`, `prompt_history`, `app_state`, …) with one-line descriptions.

## Store formats

```{fastmcp-resource} agentgrep_store_formats
```

Read `agentgrep://store-formats` for the enumeration of on-disk format values (`jsonl`, `sqlite`, `md_frontmatter`, …) with one-line descriptions.
