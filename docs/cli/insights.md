(cli-insights)=

# agentgrep insights

The `agentgrep insights` command creates local reports from the same
read-only records used by `search`, `grep`, and `find`. The default
path is deliberately light: it uses pure Python, analyzes a bounded
sample, and does not install packages, download models, or import
optional ML/LLM libraries.

## Examples

Create a builtin report from the newest prompt records:

```console
$ agentgrep insights report
```

Emit one JSON document for scripts:

```console
$ agentgrep insights report --json
```

Analyze prompts and conversations together:

```console
$ agentgrep insights report --scope all
```

Analyze every selected record instead of the bounded sample:

```console
$ agentgrep insights report --all
```

## Bounded default

`insights report` analyzes the newest 500 prompt records by default.
That keeps the command responsive on large local histories. Pass
`--limit N` to choose a different sample size, or `--all` for exact
full-corpus counts.

The builtin report includes aggregate facts only: record count, sampled
status, selected agents, stores, record kinds, timestamp range, and top
simple terms. It does not include raw prompt text.

## Optional levels

`--level` records the optional insight level the user asked for. In
this first concept slice, only `builtin` executes analysis. Other
levels report that optional enrichers were skipped instead of importing
or installing heavy dependencies:

```console
$ agentgrep insights report --level embeddings
```

Future slices will add setup, model management, and richer enrichers
behind optional extras while keeping `builtin` as the default.

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: insights report
    :nodescription:
```
