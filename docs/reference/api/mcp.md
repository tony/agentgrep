(api-agentgrep-mcp)=

# agentgrep.mcp

The MCP module exposes the FastMCP server factory, payload models, prompt
helpers, resources, and tool adapters for agentgrep.

## Payload models

```{eval-rst}
.. autoclass:: agentgrep.mcp.SearchRecordModel
   :members:

.. autoclass:: agentgrep.mcp.FindRecordModel
   :members:

.. autoclass:: agentgrep.mcp.SourceRecordModel
   :members:

.. autoclass:: agentgrep.mcp.SearchToolQuery
   :members:

.. autoclass:: agentgrep.mcp.SearchToolResponse
   :members:

.. autoclass:: agentgrep.mcp.FindToolQuery
   :members:

.. autoclass:: agentgrep.mcp.FindToolResponse
   :members:

.. autoclass:: agentgrep.mcp.BackendAvailabilityModel
   :members:

.. autoclass:: agentgrep.mcp.CapabilitiesModel
   :members:
```

## Server helpers

```{eval-rst}
.. autofunction:: agentgrep.mcp.normalize_agent_selection
.. autofunction:: agentgrep.mcp.list_source_models
.. autofunction:: agentgrep.mcp.build_capabilities
.. autofunction:: agentgrep.mcp.build_mcp_server
.. autofunction:: agentgrep.mcp.main
```
