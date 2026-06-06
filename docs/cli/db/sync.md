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

Force a full refresh even when sources look unchanged:

```console
$ agentgrep db sync --force
```

Show progress even when writing structured output:

```console
$ agentgrep db sync \
    --json \
    --progress always
```

Disable progress for quiet scripts:

```console
$ agentgrep db sync --progress never
```

## Progress

Text-mode sync shows stderr progress by default. In an interactive
terminal, press Enter on a blank line to stop before the next source
transaction and return the partial sync counters. The active source
finishes before the command exits.

Progress output always goes to stderr. JSON and NDJSON stdout stay
machine-readable even when progress is forced with `--progress always`.

## Freshness

By default, sync uses the persisted source ledger and `source_state`
fingerprints to skip sources whose size and mtime still match the last
successful sync. Use `--force` when you need to rebuild records from
unchanged source files.

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: db sync
    :nodescription:
```
