(adr-local-insights-reports-model-backed-enrichment)=

# ADR 0005: Local insights reports and model-backed enrichment

## Status

Proposed.

## Context

agentgrep reads assistant storage directly from local files and databases. The
core search path already normalizes records without spawning Codex, Claude,
Cursor, or another upstream assistant process. Insights should build on that
same local record stream. Search answers "which records match this query?";
insights answers "what happened across these local records, and what should a
human or agent inspect next?"

The default user experience must stay small and predictable. A normal
`agentgrep` install should not pull in PyTorch, vector databases, native LLM
runtimes, model files, or daemon clients just because a report command exists.
At the same time, higher-quality reports need a path to richer local analysis:
classical text features, embeddings, persistent local indexes, and local model
summaries. The architecture needs a clean ladder from vanilla deterministic
Python to model-backed enrichment.

The useful patterns from adjacent systems are about boundaries and ergonomics,
not feature copying:

- [uv 0.11.20](https://github.com/astral-sh/uv/tree/0.11.20) keeps command
  dispatch, cache policy, and environment mutation explicit; its cache surface
  has named user operations such as
  [`cache clean`](https://github.com/astral-sh/uv/blob/0.11.20/crates/uv/src/commands/cache_clean.rs),
  [`cache prune`](https://github.com/astral-sh/uv/blob/0.11.20/crates/uv/src/commands/cache_prune.rs),
  and
  [`cache dir`](https://github.com/astral-sh/uv/blob/0.11.20/crates/uv/src/commands/cache_dir.rs).
- [scikit-learn](https://github.com/scikit-learn/scikit-learn/tree/d564524)
  shows the lightest useful ML layer for text features and clustering, such as
  [text vectorizers](https://github.com/scikit-learn/scikit-learn/blob/d564524/sklearn/feature_extraction/text.py)
  and
  [KMeans](https://github.com/scikit-learn/scikit-learn/blob/d564524/sklearn/cluster/_kmeans.py).
- [sentence-transformers](https://github.com/huggingface/sentence-transformers/tree/82ea2dc)
  shows explicit model loading, revisions, local-only loading, and cache folder
  control in its
  [base model loader](https://github.com/huggingface/sentence-transformers/blob/82ea2dc/sentence_transformers/base/model.py).
- [Tantivy 0.26.1](https://github.com/quickwit-oss/tantivy/tree/0.26.1)
  separates query parsing from execution through `Query`, `Weight`, and
  `Scorer` types; this is the right shape for optional full-text indexes that
  should not leak into report semantics.
- [SQLite](https://github.com/sqlite/sqlite/tree/002d33d) is the baseline for
  local, inspectable, removable state. Schema and pragma surfaces are explicit
  engine metadata, not hidden side effects.
- [Chroma](https://github.com/chroma-core/chroma/tree/latest) is useful as a
  heavier reference for local vector segments and cached HNSW indexes, but that
  is a later backend shape rather than a default.
- [Ollama v0.30.0-rc31](https://github.com/ollama/ollama/tree/v0.30.0-rc31)
  treats models as a local control-plane concern with native routes for
  [`/api/pull`, `/api/tags`, `/api/ps`, `/api/chat`, and `/api/embed`](https://github.com/ollama/ollama/blob/v0.30.0-rc31/server/routes.go).
- [LiteRT-LM v0.13.1](https://github.com/google-ai-edge/LiteRT-LM/tree/v0.13.1)
  keeps model package access behind typed resources such as
  [`ModelResources`](https://github.com/google-ai-edge/LiteRT-LM/blob/v0.13.1/runtime/components/model_resources.h),
  [`ModelResourcesLitertLm`](https://github.com/google-ai-edge/LiteRT-LM/blob/v0.13.1/runtime/components/model_resources_litert_lm.h),
  and
  [`LitertLmLoader`](https://github.com/google-ai-edge/LiteRT-LM/blob/v0.13.1/runtime/util/litert_lm_loader.h).

The agentic loop also needs first-class treatment. An agent should be able to
run a report, see which evidence was used, understand which optional backends
were skipped, and choose the next bounded command. It should not receive an
opaque prose blob that cannot be traced back to local records.

ADR 0004 defines the shared lifecycle vocabulary for run status, result
payloads, diagnostics, pagination, and `RecordRef` drilldown handles. ADR 0006
defines the public CLI/MCP surface vocabulary and loop. Insights must reuse
those result types rather than creating report-only names for status, source
coverage, diagnostics, next actions, or record inspection.

## Decision

agentgrep will define insights as a staged report pipeline over local records.
The pipeline is headless and deterministic first; optional enrichers attach to
the same report model.

The pipeline has these pieces:

1. **Record source**: existing discovery and adapters yield normalized prompt
   and conversation records.
2. **Activity model**: records are grouped into agents, stores, sessions,
   conversations, projects, time buckets, and coarse text surfaces.
3. **Builtin analysis**: deterministic counters, timelines, term summaries,
   repeated phrases, ADR 0006 source coverage, empty/error states, and
   representative `RecordRef` handles.
4. **Optional enrichers**: installed backends may add classical ML clusters,
   embeddings, persistent indexes, or local-model summaries to the report
   model.
5. **Evidence policy**: the report distinguishes aggregate facts,
   `RecordRef` evidence, sampled snippets, generated summaries, and
   diagnostics.
6. **Renderers**: terminal, JSON, Markdown, HTML, MCP, and future TUI views
   consume the same report model and expose ADR 0004 result state where the
   sink is machine-readable.

No renderer discovers stores, parses records, installs packages, downloads
models, or calls a model. Renderers only present the report payload.

The command shapes below are intentionally non-executable `text` examples until
the feature exists. Once implemented, user-facing examples should move to
`console` fences and become documentation-test cases.

The default command remains vanilla:

```text
$ agentgrep insights report
```

Richer modes are explicit:

```text
$ agentgrep insights report --level builtin
```

```text
$ agentgrep insights report --level ml
```

```text
$ agentgrep insights report --level embeddings --model all-MiniLM-L6-v2
```

```text
$ agentgrep insights report --level llm --backend ollama --model llama3.2
```

```text
$ agentgrep insights report --level llm --backend litert-lm --model gemma-3n
```

```text
$ agentgrep insights report --level best-installed
```

`builtin` is the default. `best-installed` may select the highest usable
installed backend, but it must not install packages or download models.

## Dependency Levels

Optional dependencies are a ladder, not a blob. Each level must degrade to a
clear capability message instead of a traceback.

| Level | Extra | Candidate dependencies | Adds | Model behavior |
| --- | --- | --- | --- | --- |
| 0 | none | none beyond core `agentgrep` | deterministic reports, JSON, Markdown, terminal output, simple HTML | no models |
| 1 | `agentgrep[insights-html]` | `jinja2`, `platformdirs` if not promoted to core | report templates, report profiles, platform cache/report directories | no models |
| 2 | `agentgrep[insights-ml]` | `scikit-learn` | TF-IDF terms, classical clustering, topic candidates, outlier sessions | no model downloads |
| 3 | `agentgrep[insights-embeddings]` | `sentence-transformers` | dense or sparse embeddings, semantic clusters, semantic dedupe, nearest-session examples | installed or explicitly provisioned embedding models |
| 4 | `agentgrep[insights-index]` | SQLite registry tables, `sqlite-vec`, `tantivy-py`; Chroma remains experimental | persistent local indexes, incremental refreshes, nearest-neighbor or full-text report reuse | reuses installed embedding models |
| 5 | `agentgrep[insights-llm]` | Ollama over local HTTP, LiteRT-LM, later `llama-cpp-python` if wheels and install UX are acceptable | local narrative synthesis, cluster naming, unanswered-thread extraction, report refinement from evidence | installed or explicitly provisioned local LLMs |

The base import path must stay lazy:

- Importing `agentgrep` must not import `sklearn`, `torch`,
  `sentence_transformers`, `tantivy`, `sqlite_vec`, `httpx`, Ollama clients,
  LiteRT-LM bindings, or LLM runtimes.
- Each optional backend exposes a small capability probe with a typed failure
  reason.
- A report records which level was selected, which enrichers ran, which
  enrichers were skipped, the ADR 0004 `RunStatus`, and grounded next
  actions the user or MCP client can run next.

`agentgrep[insights-all]` may install levels 1 through 4 once those levels are
stable. Level 5 remains separate until its platform and wheel story is good
enough for normal users.

## Model Provisioning

Model provisioning is allowed only as an explicit user-facing operation or an
explicit report flag. The default report path must never surprise the user with
network traffic, large downloads, daemon startup, or model cache growth.

Setup commands describe the action before they mutate anything:

```text
$ agentgrep insights setup embeddings
```

```text
$ agentgrep insights setup llm --backend ollama
```

Model commands manage local model state:

```text
$ agentgrep insights models available --level embeddings
```

```text
$ agentgrep insights models install all-MiniLM-L6-v2 --level embeddings
```

```text
$ agentgrep insights models install llama3.2 --backend ollama
```

```text
$ agentgrep insights models install gemma-3n --backend litert-lm
```

```text
$ agentgrep insights models list
```

```text
$ agentgrep insights models remove llama3.2 --backend ollama
```

Report generation may provision a missing model only when the user passes an
explicit flag such as `--auto-download-models`. In an interactive terminal it
must show the backend, model identifier, approximate size when known, license or
terms hint when known, cache target, and whether the download is resumable. In
non-interactive contexts it must also require `--yes`.

Ollama provisioning delegates to Ollama's local model control plane, such as
`/api/pull` for downloads and `/api/tags` for local inventory. The cache is
owned by Ollama, and agentgrep records the model name, digest when available,
daemon URL, and reachability status.

LiteRT-LM provisioning stores model artifacts in agentgrep's model cache unless
the user supplies an existing path. Cache keys include backend, model ID,
revision or digest, file format, license or terms marker, and artifact size.
The runtime receives a resolved local path or asset bundle; report code never
passes an unresolved model name into the executor.

Cache directories follow explicit precedence:

1. `AGENTGREP_MODEL_DIR` for model artifacts.
2. `AGENTGREP_CACHE_DIR` for indexes and report caches.
3. Platform cache directories.
4. `XDG_CACHE_HOME` on Unix-like systems.
5. `LOCALAPPDATA` on Windows when native Windows support exists.
6. `~/Library/Caches` on macOS.
7. `~/.cache/agentgrep` as the final fallback.

Every cache has a diagnostic surface:

```text
$ agentgrep insights doctor
```

```text
$ agentgrep insights cache dir
```

```text
$ agentgrep insights cache size
```

```text
$ agentgrep insights cache prune
```

## CLI and Agentic UX

The human CLI should optimize for copy-pasteable, bounded commands:

- One command produces a useful builtin report.
- A richer report explains the missing extra or model with a precise next
  command.
- Long-running enrichment prints progress by phase: collect, analyze, index,
  provision model, summarize, render.
- Cancellation leaves cache state either unchanged or inspectably partial.
- Errors name the failing backend and the fallback report level when available.

Machine output is stable. JSON and MCP report responses use a `ReportResult`
shape adapted from the ADR 0004 result vocabulary; NDJSON streams emit
lifecycle events and finish with an equivalent summary:

```text
$ agentgrep insights report --format json
```

```text
$ agentgrep insights report --format ndjson
```

The JSON payload includes:

- `schema_version`
- normalized report request, selected level, and backend capabilities
- ADR 0004 `RunStatus`, `PageInfo`, and `next_cursor` when report rows or
  evidence lists are paginated
- ADR 0004 `Diagnostic` entries for skipped sources, missing optional
  dependencies, model/cache issues, malformed stores, cancellation, and
  approximation notes
- ADR 0006 source coverage and skipped-source reasons
- statistics for records scanned, sessions grouped, evidence items emitted,
  enrichers attempted, enrichers skipped, elapsed time, and active limits
- deterministic builtin facts
- optional enrichment facts
- evidence references back to records or sessions using `RecordRef` handles
- privacy settings
- model provenance when a model runs
- grounded next actions

Report-specific field names are allowed when they clarify the report domain,
but they must map into these result concepts rather than forming a second
result vocabulary.

Local model summaries are always grounded in reduced evidence. The LLM prompt
receives compact facts, session IDs, timestamps, labels, and opt-in snippets,
not unbounded raw transcripts by default. The model output is stored as an
enrichment with provenance, not as the source of truth.

For MCP and agent use, the report should make the loop obvious:

1. Inspect report summary.
2. Read `completion`, `diagnostics`, source coverage, and grounded next actions.
3. Request the next page when `next_cursor` is present.
4. Open referenced sessions or records through `RecordRef` handles.
5. Rerun with a narrower query, time range, agent, project, or backend.
6. Escalate to embeddings or local LLM only when the builtin result is too
   shallow for the question.

## Privacy and Safety

The builtin path is local-only and network-free. Optional packages may be
installed explicitly. Model downloads are opt-in. Remote hosted LLM providers
are out of scope for this decision.

Reports include aggregate facts by default. Raw prompt or transcript text
requires an explicit option such as `--include-text` or `--sample-text`.
Generated summaries must identify their backend and model. Diagnostic output
uses ADR 0004 `Diagnostic` records and redacts local absolute paths unless the
user asks for local troubleshooting details.

agentgrep must not run live upstream assistant CLIs to interpret storage. It may
understand storage written by supported tools, but it does not ask those tools
to analyze themselves.

## Testing

The base package must be tested without optional extras. A no-extra environment
must import `agentgrep`, run the builtin report, and render JSON.

Each optional level gets focused tests:

- missing dependency produces the intended setup guidance
- capability probes classify unavailable, installed, misconfigured, and stale
  cache states
- report JSON and MCP responses keep stable result keys, `RunStatus`,
  diagnostics, source coverage, `RecordRef` evidence, next actions, and privacy
  defaults
- model provisioning uses fake registries or tiny fixtures by default
- real model downloads run only in explicitly marked integration jobs
- LiteRT-LM and Ollama backends test cache/provenance behavior without requiring
  normal unit tests to download a model

The docs should treat examples as executable behavior. Console examples for setup,
doctor, model install, and report generation need either executable fixtures or
explicit non-executed annotations with a reason.

## Relationship to ADR 0004 and ADR 0006

ADR 0004 owns event streams, result payloads, run status, pagination,
diagnostics, and `RecordRef`. Insights is a specialized producer of report
facts and enrichment evidence inside that lifecycle vocabulary.

ADR 0006 owns public CLI/MCP surface vocabulary, source catalog terminology,
and the MCP loop shape. Insights report commands, MCP tools, source coverage,
and next actions must use those public names so agents can move between search
and insights without learning a second vocabulary.

## Consequences

The builtin report stays fast, portable, and useful. Users can get immediate
activity summaries without learning about embeddings or local model runtimes.

Power users get a clear upgrade path. They can add one level at a time, inspect
what changed, cache expensive work, and remove model artifacts or indexes when
needed.

The cost is a larger compatibility matrix. Optional extras, model registries,
daemon reachability, platform caches, and integration tests all need careful
ownership. The levelled architecture keeps that cost isolated from the default
install and from the core search types.

## Rejected and Deferred

Running a live assistant agent to parse local storage is rejected. It is slower,
less private, harder to reproduce, and unnecessary when agentgrep has storage
adapters.

Silent auto-upgrade to the richest installed backend is rejected. Installed
dependencies may change report quality, runtime, privacy, and cost, so backend
selection must be visible in the report.

Automatic model download on the default report path is rejected. On-demand
downloads are acceptable only through explicit model commands or explicit report
flags with confirmation and cache provenance.

Remote hosted LLM providers are deferred. A future decision can define a remote
provider API, but local reports and local model enrichment must stand on
their own.

Making Chroma or another full vector database the default index is deferred.
The first persistent index should be inspectable, embedded, and easy to remove.
