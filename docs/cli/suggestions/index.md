(cli-suggestions)=

# agentgrep suggestions

The `agentgrep suggestions` command group lists and renders
review-only instruction suggestions derived from omission findings.
The CLI pages document command flags; the suggestion workflow is
explained in {ref}`insights-suggestions`.

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: suggestions
    :nosubcommands:
    :nodescription:
```

Choose a subcommand for details:

- {ref}`cli-suggestions-list` - list or create persisted suggestions
- {ref}`cli-suggestions-show` - inspect one suggestion as structured output
- {ref}`cli-suggestions-render` - render one suggestion as review text

```{toctree}
:maxdepth: 1
:hidden:

list
show
render
```
