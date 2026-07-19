(cli-db-status)=

# agentgrep db status

Show row counts for the persistent DB without
running a sync.

## Examples

Print human-readable status:

```console
$ agentgrep db status
```

Emit structured JSON:

```console
$ agentgrep db status --json
```

Inspect a non-default database:

```console
$ agentgrep db status --db .tmp/agentgrep.sqlite
```

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: db status
    :nodescription:
```
