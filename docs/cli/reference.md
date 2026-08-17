(cli-reference)=

# API Reference

CLI argument types, serialization helpers, and command entry points.

## Argument types

```{eval-rst}
.. autoclass:: agentgrep.SearchArgs
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.GrepArgs
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.FindArgs
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.UIArgs
   :no-index:
   :members:
   :no-undoc-members:

.. autoclass:: agentgrep.DbArgs
   :members:
   :no-undoc-members:
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
