(tui-reference)=

# API Reference

The `agentgrep.ui` subpackage holds the streaming Textual explorer.
Textual is imported lazily inside
{func}`~agentgrep.ui.app.build_streaming_ui_app` via
`importlib.import_module`, so bare `import agentgrep` does not pull
Textual into the importing process; the import error is deferred to
the moment the factory is called.

The subpackage's `__init__` re-exports {func}`~agentgrep.ui.app.run_ui`
and {func}`~agentgrep.ui.app.build_streaming_ui_app` at the
`agentgrep.ui` namespace for convenience, and the top-level
`agentgrep` package provides matching lazy wrappers — see
{func}`agentgrep.run_ui` and {func}`agentgrep.build_streaming_ui_app`
in the {ref}`library reference <package-agentgrep-reference>`.

## Argument type

```{eval-rst}
.. autoclass:: agentgrep.UIArgs
   :members:
```

## Entry points

```{eval-rst}
.. autofunction:: agentgrep.run_ui_command
.. autofunction:: agentgrep.ui.app.run_ui
.. autofunction:: agentgrep.ui.app.build_streaming_ui_app
```

## Filter and display helpers

```{eval-rst}
.. autofunction:: agentgrep.cached_haystack
.. autofunction:: agentgrep.clear_haystack_cache
.. autofunction:: agentgrep.compute_filter_matches
.. autofunction:: agentgrep.format_timestamp_tig
.. autofunction:: agentgrep.ui.app.scroll_percent
```
