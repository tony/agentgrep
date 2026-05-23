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
   the repo. Defines `[bench.grep]`, `[bench.find]`, `[bench.search]`,
   `[bench.import-time]` for agentgrep against `libtmux`.
3. **`scripts/benchmark.local.toml`** — per-machine overrides
   (gitignored). Copy `scripts/benchmark.local.toml.example` to start.
4. **CLI flags** — `--runs N`, `--warmup N`, `--query STR`,
   `--commands grep,find` always trump the TOML.

Deep-merge semantics: only the keys you set in a higher layer are
replaced. So adding `[bench.fuzzy]` in `benchmark.local.toml` extends
the bench set without disturbing the existing entries.

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
