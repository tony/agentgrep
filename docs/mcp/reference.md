(mcp-reference)=

# API Reference

FastMCP server factory, payload models, and MCP helpers.

## Payload models

```{eval-rst}
.. autoclass:: agentgrep.mcp.SearchRecordModel
   :members:

.. autoclass:: agentgrep.mcp.FindRecordModel
   :members:

.. autoclass:: agentgrep.mcp.SourceRecordModel
   :members:

.. autoclass:: agentgrep.mcp.SearchRequestModel
   :members:

.. autoclass:: agentgrep.mcp.SearchToolResponse
   :members:

.. autoclass:: agentgrep.mcp.FindRequestModel
   :members:

.. autoclass:: agentgrep.mcp.FindToolResponse
   :members:

.. autoclass:: agentgrep.mcp.ResultStatsModel
   :members:

.. autoclass:: agentgrep.mcp.PageInfoModel
   :members:

.. autoclass:: agentgrep.mcp.RunStatusModel
   :members:

.. autoclass:: agentgrep.mcp.DiagnosticModel
   :members:

.. autoclass:: agentgrep.mcp.InspectResultRequest
   :members:

.. autoclass:: agentgrep.mcp.InspectResultResponse
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
