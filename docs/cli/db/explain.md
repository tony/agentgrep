(cli-db-explain)=

# agentgrep db explain

Print db planner and status details for the selected
database. Use this when debugging why a search did or did not use the
DB cache.

## Examples

Explain the default database:

```console
$ agentgrep db explain
```

Emit one JSON object per line:

```console
$ agentgrep db explain --ndjson
```

Explain a temporary database:

```console
$ agentgrep db explain --db .tmp/agentgrep.sqlite
```

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: db explain
    :nodescription:
```
