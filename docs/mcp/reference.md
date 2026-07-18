(mcp-reference)=

# API Reference

FastMCP server factory, payload models, and MCP helpers.

## Payload models

```{eval-rst}
.. autoclass:: agentgrep.mcp.AgentGrepModel

.. autoclass:: agentgrep.mcp.SearchRecordModel

.. autoclass:: agentgrep.mcp.RecordOriginModel

.. autoclass:: agentgrep.mcp.FindRecordModel

.. autoclass:: agentgrep.mcp.SourceRecordModel

.. autoclass:: agentgrep.mcp.SourceVersionDetectionModel

.. autoclass:: agentgrep.mcp.SearchRequestModel

.. autoclass:: agentgrep.mcp.SearchToolResponse

.. autoclass:: agentgrep.mcp.FindRequestModel

.. autoclass:: agentgrep.mcp.FindToolResponse

.. autoclass:: agentgrep.mcp.ResultStatsModel

.. autoclass:: agentgrep.mcp.PageInfoModel

.. autoclass:: agentgrep.mcp.RunStatusModel

.. autoclass:: agentgrep.mcp.DiagnosticModel

.. autoclass:: agentgrep.mcp.InspectResultRequest

.. autoclass:: agentgrep.mcp.InspectResultResponse

.. autoclass:: agentgrep.mcp.InsightsSkillsRequest

.. autoclass:: agentgrep.mcp.InsightsSkillsResponse

.. autoclass:: agentgrep.mcp.StoreDescriptorModel

.. autoclass:: agentgrep.mcp.ListStoresRequest

.. autoclass:: agentgrep.mcp.ListStoresResponse

.. autoclass:: agentgrep.mcp.GetStoreDescriptorRequest

.. autoclass:: agentgrep.mcp.ListSourcesRequest

.. autoclass:: agentgrep.mcp.ListSourcesResponse

.. autoclass:: agentgrep.mcp.FilterSourcesRequest

.. autoclass:: agentgrep.mcp.DiscoverySummaryRequest

.. autoclass:: agentgrep.mcp.DiscoverySummaryResponse

.. autoclass:: agentgrep.mcp.ValidateQueryRequest

.. autoclass:: agentgrep.mcp.ValidateQueryResponse

.. autoclass:: agentgrep.mcp.RecentSessionsRequest

.. autoclass:: agentgrep.mcp.RecentSessionsResponse

.. autoclass:: agentgrep.mcp.InspectSampleRequest

.. autoclass:: agentgrep.mcp.InspectSampleResponse

.. autoclass:: agentgrep.mcp.BackendAvailabilityModel

.. autoclass:: agentgrep.mcp.CapabilitiesModel
```

## Server helpers

```{eval-rst}
.. autofunction:: agentgrep.mcp.normalize_agent_selection
.. autofunction:: agentgrep.mcp.list_source_models
.. autofunction:: agentgrep.mcp.build_capabilities
.. autofunction:: agentgrep.mcp.build_mcp_server
.. autofunction:: agentgrep.mcp.main
```
