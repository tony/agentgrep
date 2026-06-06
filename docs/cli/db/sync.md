(cli-db-sync)=

# agentgrep db sync

Sync discovered source records into the persistent DB index.
The DB index is derived state; original agent stores remain the
source of truth.

## Examples

Sync every supported agent and scope:

```console
$ agentgrep db sync
```

Sync only Codex prompt-scope records into a temporary database:

```console
$ agentgrep db sync \
    --db .tmp/agentgrep.sqlite \
    --agent codex \
    --scope prompts
```

Limit source count while profiling:

```console
$ agentgrep db sync --limit-sources 50
```

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: db sync
    :nodescription:
```
