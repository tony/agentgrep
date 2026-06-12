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

Write Markdown or HTML document output:

```console
$ agentgrep insights report --format markdown --output report.md
$ agentgrep insights report --level html --format html --output report.html
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

`--level` selects the insight level the user asked for. The default is
always `builtin`, even if optional packages are installed. Explicit
optional levels import only their own backend modules through the lazy
loader. If a requested backend is unavailable, the command exits with a
configuration error and a setup hint instead of falling back silently.
Use `best-installed` when you want an executable fallback that stays
offline:

```console
$ agentgrep insights report --level best-installed
```

`best-installed` is the explicit fallback mode. It checks installed
optional backends under the current offline policy and uses the richest
usable level. If no optional backend is usable, it returns the builtin
report.

The five optional dependency levels are independent package extras:

| Level | Extra | Adds | Model behavior |
| --- | --- | --- | --- |
| `html` | `agentgrep[insights-html]` | Template rendering and reusable report profiles | no models |
| `ml` | `agentgrep[insights-ml]` | TF-IDF features and classical clustering | no model downloads |
| `embeddings` | `agentgrep[insights-embeddings]` | Dense and sparse semantic grouping | explicit model install only |
| `index` | `agentgrep[insights-index]` | Persistent local indexes for report refreshes | reuses installed embedding models only |
| `llm` | backend-specific `agentgrep[insights-llm-*]` extras | Local narrative synthesis through embedded or HTTP backends | explicit local model or endpoint only |

`agentgrep[insights-all]` installs levels 1 through 4. The local LLM
level stays separate because it has a heavier runtime and model story.

Report generation is offline by default. `--allow-download` is an
explicit opt-in for model loaders that support local-only switches, and
non-loopback LLM endpoints require `--allow-network`.
Install one LLM adapter extra at a time:

| Backend | Extra | Use when |
| --- | --- | --- |
| `ollama` | `agentgrep[insights-llm-ollama]` | an Ollama-compatible local HTTP daemon already owns model management |
| `llama-cpp` | `agentgrep[insights-llm-llama-cpp]` | you have a local GGUF model file |
| `litert-lm` | `agentgrep[insights-llm-litert-lm]` | you have or want to install a local `.litertlm` model file |

The compatibility extra `agentgrep[insights-llm]` installs all stable
LLM adapters, but `agentgrep insights setup llm` asks for a specific
backend before mutating the environment. A report still needs a real local
model path or a local backend model name. For example, pass
`--llm-backend llama-cpp --model /path/to/model.gguf`,
`--llm-backend litert-lm --model /path/to/model.litertlm`, or
`--llm-backend ollama --model llama3`.

Use `insights models list` to print the curated local LLM model allowlist
without searching local history, importing optional LLM packages,
contacting Ollama, or downloading model files:

```console
$ agentgrep insights models list --llm-backend litert-lm
```

The older report-scoped spelling remains available for compatibility:

```console
$ agentgrep insights report --llm-backend litert-lm --list
```

The same allowlist is available for local Ollama model names:

```console
$ agentgrep insights report --llm-backend ollama --list
```

Machine-readable output uses the same `--json` and `--ndjson` flags as
other insights commands:

```console
$ agentgrep insights report --llm-backend litert-lm --list --json
```

```console
$ agentgrep insights report --llm-backend ollama --list --ndjson
```

## Models

`insights models install` is the explicit model-management surface. It
requires `--yes` before downloading a LiteRT-LM artifact or running
`ollama pull`.

Preview the LiteRT-LM download plan without mutating local state:

```console
$ agentgrep insights models install \
    --llm-backend litert-lm \
    litert-community/gemma-4-E2B-it-litert-lm \
    --dry-run
```

Install the default curated LiteRT-LM Gemma artifact into agentgrep's
model cache:

```{code-block} console
$ agentgrep insights models install \
    --llm-backend litert-lm \
    litert-community/gemma-4-E2B-it-litert-lm \
    --yes
```

Then run the report with the printed local `.litertlm` path:

```{code-block} console
$ agentgrep insights report \
    --level llm \
    --llm-backend litert-lm \
    --model ~/.cache/agentgrep/models/litert-lm/litert-community--gemma-4-E2B-it-litert-lm/gemma-4-E2B-it.litertlm
```

Pull a curated Ollama model through the local Ollama CLI:

```{code-block} console
$ agentgrep insights models install \
    --llm-backend ollama \
    gemma3n:e2b \
    --yes
```

LiteRT-LM downloads use `AGENTGREP_MODEL_DIR` first, then
`AGENTGREP_CACHE_DIR`, then platform cache locations. Gated Hugging Face
models require accepting the model terms and setting `HF_TOKEN` before
installing.

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

LLM setup requires an explicit adapter:

```console
$ agentgrep insights setup \
    llm \
    --llm-backend litert-lm \
    --install \
    --yes
```

`insights doctor` uses `importlib.util.find_spec` probes. It does not
import `sklearn`, `torch`, `sentence_transformers`, `sqlite_vec`,
`tantivy`, `llama_cpp`, `httpx`, or `litert_lm`.

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
