(cli-insights-explain)=

# agentgrep insights explain

Show persisted insight counters for the selected agentgrep database.

## Examples

Explain insight counts:

```console
$ agentgrep insights explain
```

Emit structured JSON:

```console
$ agentgrep insights explain --json
```

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: insights explain
    :nodescription:
```
