(api-agentgrep)=

# agentgrep

The top-level package contains the CLI parser, search pipeline, terminal
progress renderer, and serializer helpers used by the command-line interface
and MCP server.

## Core data

```{eval-rst}
.. autoclass:: agentgrep.PrivatePath
   :members:

.. autofunction:: agentgrep.format_display_path

.. autoclass:: agentgrep.BackendSelection
   :members:

.. autoclass:: agentgrep.SearchArgs
   :members:

.. autoclass:: agentgrep.FindArgs
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

## Serialization and CLI

```{eval-rst}
.. autofunction:: agentgrep.serialize_search_record
.. autofunction:: agentgrep.serialize_find_record
.. autofunction:: agentgrep.serialize_source_handle
.. autofunction:: agentgrep.build_envelope
.. autofunction:: agentgrep.run_search_command
.. autofunction:: agentgrep.run_find_command
.. autofunction:: agentgrep.main
```
