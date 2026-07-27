(package-agentgrep-reference)=

# API Reference

Core data types, discovery functions, and the search pipeline used by
every surface (CLI, TUI, MCP).

## Core data

```{eval-rst}
.. autodata:: agentgrep.SearchEffort
   :no-value:

.. autodata:: agentgrep.SearchScopeProvenance
   :no-value:

.. autodata:: agentgrep.SourceScanOutcome
   :no-value:

.. autoclass:: agentgrep.PrivatePath
   :members:

.. autofunction:: agentgrep.format_display_path

.. autoclass:: agentgrep.BackendSelection
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.SearchQuery
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.RecordOrigin
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.SourceHandle
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.SearchRecord
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.FindRecord
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.ProjectContext
   :members:
   :no-undoc-members:

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
   :no-undoc-members:

.. autoclass:: agentgrep.SourceScanCache
   :members:

.. autoclass:: agentgrep.SourceScanCacheStats
   :members:
   :no-undoc-members:
```

## Search result contract

Use {func}`~agentgrep.run_search_result` when completion semantics matter. Its
{class}`~agentgrep.RunSummary` owns the normalized request, requested and
completed effort, primary status and conditions, outcome, coverage,
diagnostics, and next actions. Structured serializers place counts and timing
in `stats`; they do not read a `RunSummary.statistics` attribute.

```{eval-rst}
.. autodata:: agentgrep.RunState
   :no-value:

.. autodata:: agentgrep.SearchOutcome
   :no-value:

.. autodata:: agentgrep.results.DiagnosticSeverity
   :no-value:

.. autoclass:: agentgrep.NormalizedSearchRequest
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.RunStatus
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.RunCoverage
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.RunDiagnostic
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.SearchRequestPatch
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.NextAction
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.RunSummary
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.SearchResult
   :members:
   :no-undoc-members:

.. autofunction:: agentgrep.apply_search_request_patch
```

## Event streams

{func}`~agentgrep.iter_search_events` is the synchronous producer.
{func}`~agentgrep.aiter_search_events` delivers the same events through a
bounded async queue. A consumer that may stop before `SearchFinished` must
explicitly close the async generator, for example with
{func}`contextlib.aclosing`.

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
.. autofunction:: agentgrep.run_search_result
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
