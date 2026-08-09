(cli-insights-analyze)=

# agentgrep insights analyze

Analyze deterministic insight jobs against the DB index. Insight
analysis persists evidence artifacts for later listing and review.

## Examples

Analyze every insight family:

```console
$ agentgrep insights analyze
```

Analyze only similarity evidence:

```console
$ agentgrep insights analyze --kind similarity
```

Analyze omissions for an instruction file:

```console
$ agentgrep insights analyze \
    --kind omissions \
    --target AGENTS.md
```

## Progress

Text-mode analysis shows stderr progress by default. In an interactive
terminal, press Enter on a blank line to stop before the next insight
step and return partial analysis counters. The active insight step
finishes before the command exits.

Progress output always goes to stderr. JSON and NDJSON stdout stay
machine-readable even when progress is forced with `--progress always`.

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: insights analyze
    :nodescription:
```
