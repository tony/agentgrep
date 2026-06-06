(cli-insights)=

# agentgrep insights

The `agentgrep insights` command group analyzes and inspects deterministic
similarity and omission analysis over a DB index. The CLI
pages document command flags; the feature guide lives in
{ref}`insights`.

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: insights
    :nosubcommands:
    :nodescription:
```

Choose a subcommand for details:

- {ref}`cli-insights-analyze` - analyze similarity and omission evidence
- {ref}`cli-insights-list` - list persisted insight artifacts
- {ref}`cli-insights-explain` - show persisted insight counters

```{toctree}
:maxdepth: 1
:hidden:

analyze
list
explain
```
