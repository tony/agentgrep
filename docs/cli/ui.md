(cli-ui)=

# agentgrep ui

The `agentgrep ui` command opens the Textual explorer directly. It is
the CLI command reference for the parser surface; the full feature
guide lives in {ref}`tui`.

## Examples

Open the explorer with an empty search:

```console
$ agentgrep ui
```

Open the explorer with an initial query:

```console
$ agentgrep ui bliss
```

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: ui
    :nodescription:
```

## See also

See {ref}`tui` for keyboard behavior, streaming updates, and the
interactive record viewer.
