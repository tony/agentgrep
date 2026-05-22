(api-agentgrep-ui)=

# agentgrep.ui

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
in the top-level API reference.

## Entry points

```{eval-rst}
.. autofunction:: agentgrep.ui.app.run_ui
.. autofunction:: agentgrep.ui.app.build_streaming_ui_app
```

## Internal helpers

```{eval-rst}
.. autofunction:: agentgrep.ui.app.scroll_percent
```
