(cli-reference)=

# API Reference

CLI argument types, serialization helpers, and command entry points.

## Argument types

```{eval-rst}
.. autoclass:: agentgrep.GrepArgs
   :members:

.. autoclass:: agentgrep.SearchArgs
   :members:

.. autoclass:: agentgrep.FindArgs
   :members:

.. autoclass:: agentgrep.UIArgs
   :no-index:
   :members:

.. autoclass:: agentgrep.DbArgs
   :members:

```

## Serialization

```{eval-rst}
.. autofunction:: agentgrep.serialize_search_record
.. autofunction:: agentgrep.serialize_find_record
.. autofunction:: agentgrep.serialize_source_handle
.. autofunction:: agentgrep.build_envelope
```

## Entry points

```{eval-rst}
.. autofunction:: agentgrep.run_grep_command
.. autofunction:: agentgrep.run_search_command
.. autofunction:: agentgrep.run_find_command
.. autofunction:: agentgrep.run_ui_command
.. autofunction:: agentgrep.run_db_command
.. autofunction:: agentgrep.main
```
