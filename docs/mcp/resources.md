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
