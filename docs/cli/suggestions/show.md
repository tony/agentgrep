(cli-suggestions-show)=

# agentgrep suggestions show

Show one persisted suggestion artifact. Use this for structured
review data, especially with `--json`.

## Examples

Show one suggestion:

```console
$ agentgrep suggestions show demo-id
```

Emit one suggestion as JSON:

```console
$ agentgrep suggestions show demo-id --json
```

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: suggestions show
    :nodescription:
```
