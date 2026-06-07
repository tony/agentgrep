(cli-suggestions-list)=

# agentgrep suggestions list

List persisted suggestion artifacts. By default this prints a terminal
summary; use `--json` or `--ndjson` when stdout must be
machine-readable. When `--target` is provided, agentgrep first creates
review-only suggestions from open omission findings for that target.

## Examples

List existing suggestions:

```console
$ agentgrep suggestions list
```

Create suggestions for `AGENTS.md` and emit JSON:

```console
$ agentgrep suggestions list \
    --target AGENTS.md \
    --json
```

Use a non-default agentgrep database:

```console
$ agentgrep suggestions list --db .tmp/agentgrep.sqlite
```

Return only the most confident suggestion:

```console
$ agentgrep suggestions list --limit 1
```

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: suggestions list
    :nodescription:
```
