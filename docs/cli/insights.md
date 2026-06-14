(cli-insights)=

# agentgrep insights

`agentgrep insights` turns your local agent history into a report. Where
`search` answers *"which records match this query?"*, insights answers
*"what happened across these records, and what should I look at next?"*

The default report is deterministic, local-only, and network-free. Richer
reports are an opt-in ladder: classical ML, embeddings, a persistent
hybrid index, and a local-LLM summary. Each rung that is not installed
degrades to a precise `install` hint rather than a traceback. The base
install pulls in none of the optional backends.

## The report

Generate the builtin report over your most recent prompts:

```console
$ agentgrep insights report
```

Render it as Markdown, HTML, JSON, or NDJSON:

```console
$ agentgrep insights report --format json
```

Scope the report to one agent and a record cap:

```console
$ agentgrep insights report --agent claude --limit 500
```

The builtin report covers per-agent and per-store counts, a daily
timeline, frequent terms, repeated instructions, candidate open threads
(prompts that trail off in a question), and source coverage. Every
machine-readable payload carries a `schema_version`, a `status`, the
selected `level`, `diagnostics`, and grounded `next_actions`.

## Levels

Each level is an independent extra. Higher levels reuse the record stream
and, where relevant, the installed embedding model.

| Level | Extra | Adds |
| --- | --- | --- |
| `builtin` | none | deterministic report, JSON/Markdown/HTML |
| `html` | `agentgrep[insights-html]` | standalone HTML report |
| `ml` | `agentgrep[insights-ml]` | TF-IDF + KMeans topic clusters |
| `embeddings` | `agentgrep[insights-embeddings]` | semantic clusters, near-duplicate detection |
| `index` | `agentgrep[insights-index]` | persistent tantivy + sqlite-vec hybrid index |
| `llm` | `agentgrep[insights-llm]` or `agentgrep[insights-llm-litert]` | local narrative summary via Ollama or an in-process LiteRT-LM model |

List the rungs and what is installed:

```console
$ agentgrep insights levels
```

Pick the best installed rung (never installs or downloads):

```console
$ agentgrep insights report --level best-installed
```

Request a specific rung:

```console
$ agentgrep insights report --level embeddings --model all-MiniLM-L6-v2
```

The default embedding runtime is the torch-free `model2vec`. Install
`agentgrep[insights-embeddings-st]` to prefer `sentence-transformers`
when higher quality is worth the heavier dependency. The persistent index
defaults to `tantivy` + `sqlite-vec`; pass `--index-backend lancedb` after
installing `agentgrep[insights-index-lancedb]` for the single-store
alternative.

Stream a grounded local-LLM summary. Two runtimes are wired: Ollama over
local HTTP, and an in-process LiteRT-LM model (e.g. a Gemma `.litertlm`
artifact). Both examples are shown as `text` because they need a daemon or
a multi-gigabyte model that cannot run as a documentation test:

```text
$ agentgrep insights report --level llm --backend ollama --model llama3.2
```

```text
$ agentgrep insights report --level llm --backend litert-lm --model gemma-4-e2b
```

The LiteRT-LM artifact downloads on demand with `--auto-download-models`,
or ahead of time:

```text
$ agentgrep insights models install gemma-4-e2b --level llm --backend litert-lm --yes
```

The summary is grounded in compact facts — counts, top terms, timeline,
and open-thread titles — not raw transcripts, unless you pass
`--include-text`.

## Models

Models are listed statically and provisioned only on request. Browse the
curated embedding models:

```console
$ agentgrep insights models available --level embeddings
```

Download one into the model cache (shown as `text` because it reaches the
network):

```text
$ agentgrep insights models install all-MiniLM-L6-v2 --level embeddings --yes
```

Preview a download without writing anything:

```console
$ agentgrep insights models install potion-base-8M --level embeddings --dry-run
```

Report generation downloads a missing model only when you pass
`--auto-download-models` (and `--yes` in a non-interactive shell).

## Cache and diagnostics

The cache follows `AGENTGREP_MODEL_DIR`, then `AGENTGREP_CACHE_DIR`, then
the platform cache directory, falling back to `~/.cache/agentgrep`.

Show the resolved directories:

```console
$ agentgrep insights cache dir
```

Report cache and model sizes:

```console
$ agentgrep insights cache size
```

Reclaim regenerable index and report caches (model artifacts are kept):

```console
$ agentgrep insights cache prune
```

Diagnose dependency availability and cache state:

```console
$ agentgrep insights doctor
```
