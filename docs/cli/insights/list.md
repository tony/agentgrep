(cli-insights-list)=

# agentgrep insights list

List a bounded page of persisted similarity edges and omission
findings. By default this prints a terminal summary with sampled rows;
use `--json` or `--ndjson` when stdout must be machine-readable. This
command does not run new analysis. Use `agentgrep insights explain` for
cheap counts without returning row samples.

## Examples

List a small persisted-insight sample:

```console
$ agentgrep insights list
```

Change the per-kind row limit:

```console
$ agentgrep insights list --limit 10
```

List only omission findings as JSON:

```console
$ agentgrep insights list --kind omissions --json
```

Read from a non-default agentgrep database:

```console
$ agentgrep insights list --db .tmp/agentgrep.sqlite
```

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: insights list
    :nodescription:
```
