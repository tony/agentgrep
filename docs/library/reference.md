(package-agentgrep-reference)=

# API Reference

Core data types, discovery functions, and the search pipeline used by
every surface (CLI, TUI, MCP).

## Core data

```{eval-rst}
.. autoclass:: agentgrep.PrivatePath
   :members:

.. autofunction:: agentgrep.format_display_path

.. autoclass:: agentgrep.BackendSelection
   :members:

.. autoclass:: agentgrep.SearchQuery
   :members:

.. autoclass:: agentgrep.RecordOrigin
   :members:

.. autoclass:: agentgrep.SourceHandle
   :members:

.. autoclass:: agentgrep.SearchRecord
   :members:

.. autoclass:: agentgrep.FindRecord
   :members:

.. autoclass:: agentgrep.ProjectContext
   :members:

.. autofunction:: agentgrep.detect_project_context
```

## Search control and progress

```{eval-rst}
.. autoclass:: agentgrep.SearchControl
   :members:

.. autoclass:: agentgrep.SearchProgress
   :members:

.. autoclass:: agentgrep.NoopSearchProgress
   :members:

.. autoclass:: agentgrep.ConsoleSearchProgress
   :members:

.. autoclass:: agentgrep.SearchRuntime
   :members:

.. autoclass:: agentgrep.SourceScanCache
   :members:

.. autoclass:: agentgrep.SourceScanCacheStats
   :members:
```

## Event streams

```{eval-rst}
.. autoclass:: agentgrep.events.SearchStarted

.. autoclass:: agentgrep.events.SourceStarted

.. autoclass:: agentgrep.events.RecordEmitted

.. autoclass:: agentgrep.events.SourceFinished

.. autoclass:: agentgrep.events.SearchFinished

.. autodata:: agentgrep.events.SearchEvent
   :no-value:

.. autoclass:: agentgrep.events.FindStarted

.. autoclass:: agentgrep.events.FindRecordEmitted

.. autoclass:: agentgrep.events.FindFinished

.. autodata:: agentgrep.events.FindEvent
   :no-value:

.. autofunction:: agentgrep.iter_search_events
.. autofunction:: agentgrep.aiter_search_events
.. autofunction:: agentgrep.iter_find_events
```

## Query language helpers

```{eval-rst}
.. automodule:: agentgrep.query.help
   :no-members:

.. autofunction:: agentgrep.query.help.query_language_fields
.. autofunction:: agentgrep.query.help.query_language_operators
```

## Discovery and search

```{eval-rst}
.. autofunction:: agentgrep.select_backends
.. autofunction:: agentgrep.discover_sources
.. autofunction:: agentgrep.run_search_query
.. autofunction:: agentgrep.search_sources
.. autofunction:: agentgrep.run_find_query
.. autofunction:: agentgrep.find_sources
```

## Store catalog

```{eval-rst}
.. autodata:: agentgrep.stores.AgentName
   :no-value:

.. autodata:: agentgrep.stores.PathKind
   :no-value:

.. autodata:: agentgrep.stores.SourceKind
   :no-value:

.. autoclass:: agentgrep.stores.StoreFormat

.. autoclass:: agentgrep.stores.StoreRole

.. autoclass:: agentgrep.stores.StoreCoverage

.. autoclass:: agentgrep.stores.VersionDetectionStrategy

.. autoclass:: agentgrep.stores.VersionDetectionConfidence

.. autoclass:: agentgrep.stores.DiscoverySpec

.. autoclass:: agentgrep.stores.StoreDescriptor

.. autoclass:: agentgrep.stores.StoreCatalog

.. autofunction:: agentgrep.store_catalog.gemini_project_hash
```
