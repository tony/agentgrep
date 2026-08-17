(cli-db)=

# agentgrep db

The `agentgrep db` command group manages the persistent
SQLite DB index. The CLI pages here document the command
surface; implementation details belong in
{ref}`dev-db-index`.

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: db
    :nosubcommands:
    :nodescription:
```

Choose a subcommand for details:

- {ref}`cli-db-sync` - sync discovered sources into the index
- {ref}`cli-db-status` - show index row counts
- {ref}`cli-db-explain` - show planner and status details

```{toctree}
:maxdepth: 1
:hidden:

sync
status
explain
```
