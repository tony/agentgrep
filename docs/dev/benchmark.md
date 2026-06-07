(benchmark)=

# Benchmark harness

`scripts/benchmark.py` is a cross-commit benchmark runner that walks
hyperfine across HEAD, trunk, a range, the last N commits, an explicit
list of tags / SHAs, or just HEAD vs. trunk. It surfaces uniform
cold-start regressions that are easy to miss by eyeballing — promoting
the one-off shell-script prototype to a first-class repo tool turns
"where did `grep` slow down?" into a one-liner.

The harness is a **development-only tool** — it isn't shipped with the
agentgrep wheel and isn't installed by `pip install agentgrep`. It
lives in `scripts/` alongside `scripts/mcp_swap.py` and runs straight
from a repo checkout via `uv run`.

The script is a **PEP 723 self-contained file** — its third-party deps
(typer / rich / pydantic) resolve through `uv run`'s transient venv,
so the harness is portable across any uv-managed project. Drop in your
own `scripts/benchmark.toml` and it works.

## Invocation recipes

Every subcommand is reachable via `uv run scripts/benchmark.py`:

```console
$ uv run scripts/benchmark.py run --target HEAD
```

Single-commit bench against the configured `[bench.*]` entries.

```console
$ uv run scripts/benchmark.py run --target trunk
```

Single-commit bench against the trunk ref (default `master`, override
in `[settings].trunk`).

```console
$ uv run scripts/benchmark.py run --head-vs-trunk
```

HEAD versus trunk — the most common "did my branch regress?" shape.

```console
$ uv run scripts/benchmark.py run --range master..HEAD
```

Walk every commit on the current branch since it diverged from trunk.

```console
$ uv run scripts/benchmark.py run --lookback 10
```

The last 10 commits from HEAD, oldest first.

```console
$ uv run scripts/benchmark.py run --from-trunk-back 5
```

Trunk and the five commits before it — useful for establishing a
baseline that pre-dates a refactor.

```console
$ uv run scripts/benchmark.py run --tags
```

Every git tag, sorted by `--sort=v:refname`.

```console
$ uv run scripts/benchmark.py run --commits abc1234,def5678
```

Explicit list of refs (SHAs, tags, or branches).

```console
$ uv run scripts/benchmark.py compare HEAD master
```

Sugar for `run --commits HEAD,master`.

## Discovery subcommands

```console
$ uv run scripts/benchmark.py list-commits --lookback 10
```

Print the commits a selector would resolve to, without running any
benchmarks — handy for sanity-checking `--range` and `--lookback`
arguments before committing to a long run.

```console
$ uv run scripts/benchmark.py list-commands
```

Print every configured `[bench.X]` entry after applying the TOML
layers — useful when iterating on `benchmark.local.toml`.

```console
$ uv run scripts/benchmark.py show-config
```

Dump the post-layering config as JSON, the closest thing to a "what
will actually run" inspection.

## Output formats

`--format` selects the renderer; the default is a rich terminal table:

| Format | Use case |
|---|---|
| `rich` | Interactive terminal viewing (default) |
| `json` | Single JSON document with raw samples preserved |
| `ndjson` | One JSON object per line — pipe-friendly for `jq` |
| `md` | Markdown tables (mirrors the prototype `performance.md`) |
| `csv` | Flat row-per-measurement; raw samples joined by `;` |

`json` and `ndjson` are the artifact formats. Both include
`schema_version` and `artifact_kind` fields so local files can be
distinguished from profiler payloads, benchmark rows, and future CI
artifacts. Every measurement keeps the raw `samples`, `status`,
`error`, `dry_run`, and a sanitized `command_string`. Rendered command
strings replace local values with placeholders such as `{repo}`,
`{venv}`, `{home}`, and `{query}` so the artifact can be copied into
issues without exposing the local checkout or search term.

Dry-run rows set `dry_run: true` and keep `samples: []`. Rows for
`profile-engine-*` benchmarks also include `profile_payload` when the
post-timing profile capture succeeds, or `profile_capture_error` when
that capture fails. The timing samples still come from the configured
benchmark runs; `profile_payload` is the explainability artifact that
preserves engine span detail beside those timings.

The rich renderer shows timing tables by default and, when
`profile_payload` is present, appends a `profile payload slowest spans`
table. Use `--top-spans N` to choose how many child profiler spans to
show, or `--top-spans 0` to suppress that table.

Analyze a saved benchmark artifact when you want a repeatable
bottleneck summary without rerunning the benchmark. The analyzer
accepts benchmark `json` and `ndjson` artifacts, emits no-color rich
tables by default, and can render machine-readable `json` or `ndjson`
with `agentgrep.benchmark.analysis` artifact metadata.

```console
$ uv run scripts/benchmark.py analyze \
    .tmp/benchmark-profile-engine.json \
    --format rich \
    --top-spans 20 \
    --top-groups 10
```

Write a machine-readable analysis artifact:

```console
$ uv run scripts/benchmark.py analyze \
    .tmp/benchmark-profile-engine.json \
    --format json \
    --output .tmp/benchmark-analysis.json
```

```console
$ uv run scripts/benchmark.py run --lookback 50 --format md --output performance.md
```

`--show-percentiles` picks the subset of stat labels rendered in the
visible cells:

```console
$ uv run scripts/benchmark.py run --target HEAD --show-percentiles min,avg,p90,max
```

Accepted labels: `min`, `max`, `avg`, and any `p<N>` (e.g. `p50`,
`p90`, `p95`, `p99`).

## Config layering

Four layers compose into the effective config, in this precedence
order (each layer overlays the previous):

1. **Built-in pydantic defaults** — `runs = 3`, `warmup = 1`, `trunk =
   "master"`, etc.
2. **`scripts/benchmark.toml`** — the committed defaults shipped with
   the repo. Defines small capped benches, broad all-agent prompt,
   conversation, search, and find benches that exercise the discovery
   planner, and `profile-*` probes for deeper bottleneck runs.
3. **`scripts/benchmark.local.toml`** — per-machine overrides
   (gitignored). Copy `scripts/benchmark.local.toml.example` to start.
4. **CLI flags** — `--runs N`, `--warmup N`, `--query STR`,
   `--commands grep,find`, or `--commands profile-engine` always
   trump the TOML.

Deep-merge semantics: only the keys you set in a higher layer are
replaced. So adding `[bench.fuzzy]` in `benchmark.local.toml` extends
the bench set without disturbing the existing entries.

## Benchmark names

Treat committed `[bench.X]` keys and descriptions as the human audit
surface for performance runs. If a command is capped, the cap must be
visible before anyone reads the command string:

- `grep --limit N` benches use `limit-N` in the key and `limit N`
  in the description.
- `search --limit N` and `find --limit N` benches use `limit-N` in
  the key and `limit N` in the description.
- Committed `grep` benches use the primary `--limit` flag rather
  than the `-m` / `--max-count` aliases, so `list-commands` stays
  self-explanatory.

The ordinary benches are shaped for repeatable time-series
comparisons. `profile-*` benches are still bounded and explicit, but
they intentionally cover broader or different lookup paths so a
profiling run can expose planner, discovery, parsing, ranking, and
output bottlenecks. They are useful evidence for bottleneck work even
when their distributions are noisier across machines.

Use `--commands profile-engine` to run every committed
`profile-engine-*` benchmark. Exact benchmark keys and comma-separated
mixes still work, so `--commands grep,profile-engine` runs the `grep`
bench plus the profiler benchmark group. `list-commands` prints
available command groups after the configured `[bench.X]` entries.
Use `--commands profile-engine-cursor-ide` for the Cursor IDE SQLite
profile-engine set without expanding the all-agent profiler group.

## Engine profiler

`scripts/profile_engine.py` runs the search engine directly and emits
sanitized timings without CLI rendering overhead. Use it when you need
to explain which engine phase is expensive before changing planner,
discovery, parser, or rendering behavior.

Profile one component:

```console
$ uv run python scripts/profile_engine.py grep-prompts \
    --agent all \
    --max-count 500 \
    --json \
    tmux > .tmp/profile-grep-prompts.json
```

Profile Cursor IDE SQLite stores directly:

```console
$ uv run python scripts/profile_engine.py search-prompts \
    --agent cursor-ide \
    --limit 500 \
    --format json \
    agentgrep-cursor-db-no-match > .tmp/profile-cursor-ide.json
```

Profile every component:

```console
$ uv run python scripts/profile_engine.py all \
    --agent all \
    --limit 500 \
    --json \
    tmux > .tmp/profile-all.json
```

Choose the renderer with `--format`:

| Format | Use case |
|---|---|
| `rich` | Terminal summary plus the slowest spans; default |
| `json` | One sanitized payload |
| `ndjson` | One sanitized child profile run per line |

Show a compact terminal summary:

```console
$ uv run python scripts/profile_engine.py all \
    --agent all \
    --limit 500 \
    --top-spans 20 \
    tmux
```

`--json` and `--ndjson` are shortcuts for the machine-readable
renderers. `--format json`, `--format ndjson`, and `--format rich`
remain available when a single flag shape is easier to template.

Available components are `search-prompts`, `search-conversations`,
`grep-prompts`, `grep-conversations`, `find-prompts`, and `all`.
The JSON payload reports counts, phase timings, and coarse subprocess
metadata. Profile runs include phase spans such as `search.discover`,
`search.plan`, and `search.collect`, plus source-level spans such as
`search.discover.group`, `search.plan.decision`,
`search.plan.strategy_group`, `search.plan.prefilter_root`,
`search.plan.direct_source`, `search.collect.source`, and
`find.filter.source`. Concurrent source execution also reports
`search.collect.scheduler` with driver, worker, submitted, completed,
skipped, cancellation-requested, batch, queued-batch, queue-wait, and emitted
counts when the frontier driver is selected, and runs with an active
source-scan cache report `search.collect.source_scan_cache` lookup samples.
DB-cache consultations report one `search.cache.decision` sample per query
with the cache mode, whether the cache served the query, the served record
count, and the fallback reason when it did not.
Those spans report agent, store,
adapter, path kind, source kind, counts, and match decisions without including
prompt text, raw argv, or local absolute paths.

## Templating

Command strings in `[bench.X].command` support these placeholders:

| Token | Value |
|---|---|
| `{venv}` | `[settings].venv` (default `.venv`) |
| `{query}` | per-bench `default_query`, or `--query STR` |
| `{sha}` | full commit SHA being benchmarked |
| `{short_sha}` | first seven chars |
| `{repo}` | repo root absolute path |

Unknown placeholders raise — the harness fails loud rather than emit
a broken command.

## Safety

- **Dirty-tree refusal.** The script aborts on uncommitted changes
  unless `--allow-dirty` is passed. Per-commit benchmarks shred the
  worktree via `git checkout`; running them on top of uncommitted
  work would silently roll your edits forward into the wrong commit.
- **HEAD restore on exit.** An `atexit` hook plus SIGINT / SIGTERM
  trap restores the original ref (branch name or HEAD SHA) on every
  exit path. Pass `--keep-checkout` to disable the trap when you
  want to iterate on a checked-out commit after the run.
- **Per-commit isolation.** Each iteration runs `git checkout` + (by
  default) `uv sync` to rebuild `.venv` against the current source.
  `--no-sync` skips the sync if the wheel happens to be ABI-compatible
  across the range you're testing.

## When to skip the harness

The harness is a developer tool, not a CI gate. It expects:

- A clean worktree (or `--allow-dirty`).
- A repo where every commit in the range builds — failed `uv sync`
  marks the row `sync_fail` and moves on, but a wide swath of
  unbuildable history makes the output less useful.
- Hyperfine on PATH for the default timing path. The pure-Python
  fallback (`--no-hyperfine`, or simply not having hyperfine
  installed) is portable but produces noisier samples.
