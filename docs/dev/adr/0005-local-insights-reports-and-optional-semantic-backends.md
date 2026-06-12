(adr-local-insights-reports-optional-semantic-backends)=

# ADR 0005: Local insights reports and optional semantic backends

## Status

Accepted.

## Context

agentgrep already reads assistant storage directly from Python. Discovery,
source descriptors, and normalized `SearchRecord` payloads give the library a
storage-level view of prompt history and conversations without spawning the
upstream assistant CLI:
[record and discovery contracts](https://github.com/tony/agentgrep/blob/508d50c/src/agentgrep/__init__.py),
[event-stream search engine](https://github.com/tony/agentgrep/blob/508d50c/src/agentgrep/_engine/search.py),
and [current dependency surface](https://github.com/tony/agentgrep/blob/508d50c/pyproject.toml).
ADR 0001 already requires version detection to come from concrete data evidence
instead of `codex`, `claude`, or another agent subprocess.

An insights feature asks a different question than search. Search finds matching
records. Insights should summarize local activity into reports: source coverage,
time ranges, session shape, repeated topics, project/domain clusters, unanswered
threads, tool-use patterns when adapters expose them, and privacy-safe examples
when explicitly requested. That can be done from the same local records. It does
not require an agent to parse the storage.

The design has to preserve agentgrep's current installation ergonomics. A user
who installs `agentgrep` should not receive PyTorch, ONNX, vector databases, or
LLM runtimes by accident. The default report path must remain the lightest
available implementation. Richer ML and local-LLM behavior should be opt-in,
cross-platform, free, permissively licensed, and discoverable through clear
commands.

The implementation should also stay clean-room. It should derive report
behavior from local storage contracts, public source references, and
permissively licensed Python dependencies, not from copied proprietary report
logic or by running an assistant as an oracle.

Prior systems suggest useful boundaries:

- uv keeps environment mutation as an explicit command surface:
  [pip install command](https://github.com/astral-sh/uv/blob/0cdd500/crates/uv/src/commands/pip/install.rs)
  and [managed Python installation command](https://github.com/astral-sh/uv/blob/0cdd500/crates/uv/src/commands/python/install.rs).
- scikit-learn shows the classical ML layer for text features and clustering:
  [text vectorizers](https://github.com/scikit-learn/scikit-learn/blob/d564524/sklearn/feature_extraction/text.py),
  [KMeans](https://github.com/scikit-learn/scikit-learn/blob/d564524/sklearn/cluster/_kmeans.py),
  and [HDBSCAN](https://github.com/scikit-learn/scikit-learn/blob/d564524/sklearn/cluster/_hdbscan/hdbscan.py).
- sentence-transformers shows model loading with explicit cache folders,
  revisions, local-only loading, dense backends, and sparse encoders:
  [base model loader](https://github.com/huggingface/sentence-transformers/blob/82ea2dc/sentence_transformers/base/model.py)
  and [sparse encoder](https://github.com/huggingface/sentence-transformers/blob/82ea2dc/sentence_transformers/sparse_encoder/model.py).
- Local index stores belong behind optional backends. Chroma exposes a
  collection and nearest-neighbor DB contract:
  [DB interface](https://github.com/chroma-core/chroma/blob/43171c5/chromadb/db/__init__.py).
  sqlite-vec and tantivy-py are lighter candidates for embedded vector or
  full-text indexing:
  [sqlite-vec](https://github.com/asg017/sqlite-vec/tree/04d28bd)
  and [tantivy-py](https://github.com/quickwit-oss/tantivy-py/tree/8e8fb05).
- Local LLM execution should be a backend, not a parser requirement. Ollama
  exposes local model, embedding, and compatibility routes:
  [routes](https://github.com/ollama/ollama/blob/12e0437/server/routes.go).
  llama-cpp-python is an embedded Python-facing local runtime candidate:
  [llama-cpp-python](https://github.com/abetlen/llama-cpp-python/tree/19ea70c).

## Decision

agentgrep will define insights as a report pipeline over local storage records,
not as an agent-driven analysis loop.

The base implementation is always selected by default:

```console
agentgrep insights report
agentgrep insights report --scope conversations --format markdown --output report.md
agentgrep insights report --json
agentgrep insights report --level html --format html --output report.html
```

The base report uses only the normal `agentgrep` installation. It may use the
standard library plus dependencies already required by agentgrep, but it must
not import optional ML, vector, template, or LLM packages. It produces a stable
report model and renders terminal, JSON, and Markdown outputs without optional
packages. Template-based HTML output belongs to the optional HTML level.

The pipeline has six contracts:

1. **Record source**: existing discovery and adapters yield `SearchRecord`
   objects from prompt and conversation storage.
2. **Session model**: records are grouped into conversations, sessions, agents,
   stores, projects, time buckets, and coarse text surfaces.
3. **Base analysis**: deterministic counters, timelines, top terms, repeated
   phrases, source coverage, empty/error states, and representative record IDs.
4. **Optional enrichers**: installed backends may add ML clusters, embeddings,
   persistent indexes, or local-LLM summaries to the same report model.
5. **Evidence policy**: reports include aggregate facts by default. Raw prompt
   or transcript text appears only with an explicit `--include-text` or
   `--sample-text` option.
6. **Renderers**: terminal, JSON, Markdown, and optional static HTML consume the
   same report model. Renderers do not discover stores, parse records, install
   packages, or call models.

No live upstream agent is required. Insights code may understand storage written
by Codex, Claude, Cursor, Gemini, Grok, Pi, OpenCode, or another supported tool,
but it must not run those tools to interpret local files.

## Optional dependency ladder

The base level is level 0 and has no extra. The five enhancement levels are
purely optional extras. Each level may be installed independently, and each one
must degrade to a clear "not installed" message instead of a traceback.

| Level | Extra | Candidate dependencies | Adds | Model behavior |
| --- | --- | --- | --- | --- |
| 0 | none | none beyond `agentgrep` | deterministic local reports, JSON, Markdown, terminal text | no models |
| 1 | `agentgrep[insights-html]` | `jinja2`, optional `platformdirs` if not promoted to core | custom report templates, reusable report profiles, cross-platform cache/report directories | no models |
| 2 | `agentgrep[insights-ml]` | `scikit-learn` | TF-IDF terms, classical clustering, outlier sessions, topic candidate labels | no model downloads |
| 3 | `agentgrep[insights-embeddings]` | `sentence-transformers` | dense/sparse embeddings, semantic clustering, semantic dedupe, nearest-session examples | explicit model install only |
| 4 | `agentgrep[insights-index]` | `sqlite-vec` or `tantivy`; Chroma remains an experimental heavier option | persistent local indexes, incremental embedding reuse, fast nearest-neighbor or full-text report refreshes | reuses installed embedding models only |
| 5 | backend-specific `agentgrep[insights-llm-*]` extras | `llama-cpp-python` for GGUF inference, `httpx` for local HTTP backends such as Ollama, `litert-lm-api` for in-process `.litertlm` inference | local narrative synthesis, cluster naming, unanswered-question extraction, report refinement from evidence | explicit local model or endpoint only |

`agentgrep[insights-all]` may install levels 1 through 4. Level 5 should remain
separate unless its dependency set has reliable wheels and a manageable install
experience across Linux, macOS, and Windows. A dependency that is not
permissively licensed, not available on supported platforms, or too fragile for
normal users must stay behind an experimental extra such as
`agentgrep[insights-llm-experimental]`.

LLM adapters may be split further by backend, for example
`agentgrep[insights-llm-ollama]`, `agentgrep[insights-llm-llama-cpp]`, and
`agentgrep[insights-llm-litert-lm]`. A compatibility umbrella extra may keep the
older `agentgrep[insights-llm]` name, but setup commands should prefer a
backend-specific extra so one local LLM backend does not require installing
every other adapter.

The default command does not auto-upgrade to the highest installed level. If a
user wants richer behavior, they must ask for it:

```console
$ agentgrep insights report --level builtin
```

```console
$ agentgrep insights report --level ml
```

For embeddings and LLM reports, the user passes a real local model path, such
as `agentgrep insights report --level embeddings --model /path/to/all-MiniLM-L6-v2`
or `agentgrep insights report --level llm --model /path/to/model.gguf`.

```console
$ agentgrep insights report --level best-installed
```

`builtin` is the default. `best-installed` is an explicit opt-in that selects the
highest usable installed backend without installing packages or downloading
models. Explicit optional levels fail with a configuration error when their
dependency or local model requirement is not met.

## Installation and model management

agentgrep may provide setup commands that install optional extras on request:

```console
$ agentgrep insights setup html
```

```console
$ agentgrep insights setup ml
```

```console
$ agentgrep insights setup embeddings
```

```console
$ agentgrep insights setup index
```

```console
$ agentgrep insights setup llm --llm-backend litert-lm
```

Setup must be explicit. By default, it prints the exact command it would run,
preferring uv when available and falling back to the current Python executable:

```console
$ uv pip install "agentgrep[insights-embeddings]"
```

The fallback installer shape is
`python -m pip install "agentgrep[insights-embeddings]"`.

It may execute the install only with an affirmative flag such as `--install` or
`--yes`. After changing the environment, the command should either re-exec or
ask the user to rerun the requested report command. It must not silently mutate
the environment as a side effect of `agentgrep insights report`.

Model management is a separate future surface. The intended command family is
`agentgrep insights models list`,
`agentgrep insights models available --level embeddings`,
`agentgrep insights models install all-MiniLM-L6-v2 --level embeddings`,
`agentgrep insights models install tinyllama --level llm --backend llama-cpp`,
`agentgrep insights models remove all-MiniLM-L6-v2`, and
`agentgrep insights doctor`.

The model registry is local and inspectable. It records model ID, backend,
source URL or local path, revision or content hash, license, file sizes,
installed path, install time, and whether the model can run offline. Model
downloads require an explicit install command. Report generation may use only
already-installed models unless the user passes an explicit install or download
flag.

Model and index locations follow a cross-platform order:

1. `AGENTGREP_MODEL_DIR` or `AGENTGREP_CACHE_DIR`.
2. Platform cache directories.
3. `XDG_CACHE_HOME` on Unix-like systems.
4. `LOCALAPPDATA` on Windows.
5. `~/Library/Caches` on macOS.
6. `~/.cache/agentgrep` as a final fallback.

Reports must redact local absolute paths by default. Diagnostic commands may
show paths only when the user asks for local troubleshooting output.

## Developer experience

Missing extras should explain the next step:

```text
Embeddings are not installed.
Run: agentgrep insights setup embeddings --install
Or use the default report: agentgrep insights report --level builtin
```

The implementation should use lazy backend loading:

- Importing `agentgrep` must not import `sklearn`, `torch`,
  `sentence_transformers`, `sqlite_vec`, `tantivy`, `llama_cpp`, `httpx`, or
  `litert_lm`.
- Each optional backend has a small capability probe and a typed failure result.
- The report command prints the selected level, whether models are installed,
  and which enrichers were skipped.
- `agentgrep insights doctor` checks optional packages, model registry entries,
  index health, local endpoint reachability, disk usage, and privacy defaults.

Tests must keep the base package honest:

- A no-extra environment must import `agentgrep` and run the builtin report.
- Missing optional packages must produce actionable setup messages.
- Each optional level gets focused smoke tests behind markers.
- Golden report fixtures should assert stable JSON keys and privacy-safe text
  defaults.
- Model tests should use fake registries or tiny local fixtures unless a job is
  explicitly marked as a model-download integration test.

## Consequences

The base insights feature remains small, deterministic, and safe for users who
only want local reports. It can answer useful questions without ML: what stores
exist, which agents are represented, which sessions were active, which terms or
projects recur, where parsing failed, and which records are worth opening.

Richer analysis is available without making every user pay for it. Classical ML
adds topic and cluster structure. Embeddings add semantic grouping. Index
extras make repeated reports fast. Local LLMs can turn evidence into readable
narratives, but only after records have already been parsed and reduced by the
local report pipeline.

The tradeoff is a larger compatibility matrix. Optional extras, model registry
state, platform-specific caches, and local runtime diagnostics need careful
tests and clear error messages. The cost is acceptable because each level is
isolated behind an explicit extra and the default behavior stays at level 0.

## Rejected and deferred

Running a live assistant agent to parse storage is rejected. It is slower, less
private, harder to reproduce, and unnecessary when agentgrep already has
storage adapters.

Remote hosted LLM providers are out of scope for this ADR. They can be discussed
later as a separate explicit integration, but the local report and local model
contracts should not depend on them.

Automatically downloading models during report generation is rejected. Users
must choose when to install packages and models.

Making Chroma or another full vector database the default index is deferred.
The first persistent index should be embedded, local, and easy to remove.
