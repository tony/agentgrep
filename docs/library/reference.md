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

.. autoclass:: agentgrep.SourceHandle
   :members:

.. autoclass:: agentgrep.SearchRecord
   :members:

.. autoclass:: agentgrep.FindRecord
   :members:
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

## Discovery and search

```{eval-rst}
.. autofunction:: agentgrep.select_backends
.. autofunction:: agentgrep.discover_sources
.. autofunction:: agentgrep.run_search_query
.. autofunction:: agentgrep.search_sources
.. autofunction:: agentgrep.run_find_query
.. autofunction:: agentgrep.find_sources
```
