(cli-db-explain)=

# agentgrep db explain

Print cache diagnostics for the selected database: row counts, the
sync-state breakdown (ok and error sources, most recent sync time),
which query forms the cache can answer, and the agent/scope coverage
recorded by completed syncs. Auto-mode searches serve cache hits only
for covered agent/scope combinations, so the coverage line explains
why a search did or did not use the DB cache. `not recorded` means no
completed sync has written coverage yet — run `agentgrep db sync`.

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
