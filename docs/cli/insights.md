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

List the optional capability levels and whether their extras are installed:

```console
$ agentgrep insights levels
```

Print a dry-run install command for a richer local backend:

```console
$ agentgrep insights setup embeddings
```

Run optional dependency diagnostics:

```console
$ agentgrep insights doctor
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

`--level` records the optional insight level the user asked for. The
default is always `builtin`, even if optional packages are installed.
In this first concept slice, only `builtin` executes analysis. Other
levels report that optional enrichers were skipped instead of importing
or installing heavy dependencies:

```console
$ agentgrep insights report --level embeddings
```

Future slices will add setup, model management, and richer enrichers
behind optional extras while keeping `builtin` as the default.

The five optional dependency levels are independent package extras:

| Level | Extra | Adds | Model behavior |
| --- | --- | --- | --- |
| `html` | `agentgrep[insights-html]` | Template rendering and reusable report profiles | no models |
| `ml` | `agentgrep[insights-ml]` | TF-IDF features and classical clustering | no model downloads |
| `embeddings` | `agentgrep[insights-embeddings]` | Dense and sparse semantic grouping | explicit model install only |
| `index` | `agentgrep[insights-index]` | Persistent local indexes for report refreshes | reuses installed embedding models only |
| `llm` | `agentgrep[insights-llm]` | Local narrative synthesis through embedded or HTTP backends | explicit local model or endpoint only |

`agentgrep[insights-all]` installs levels 1 through 4. The local LLM
level stays separate because it has a heavier runtime and model story.

## Setup

`insights setup` is explicit and dry-run by default. It prints the
installer command it would run, preferring uv when available and falling
back to the current Python executable:

```console
$ agentgrep insights setup ml
```

To mutate the current environment, pass both `--install` and `--yes`:

```console
$ agentgrep insights setup ml --install --yes
```

You can force a command shape when documenting or debugging an
environment:

```console
$ agentgrep insights setup ml --manager pip
```

`insights doctor` uses `importlib.util.find_spec` probes. It does not
import `sklearn`, `torch`, `sentence_transformers`, `sqlite_vec`,
`tantivy`, `llama_cpp`, or `httpx`.

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: insights report
    :nodescription:
```

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: insights levels
    :nodescription:
```

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: insights doctor
    :nodescription:
```

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: insights setup
    :nodescription:
```
