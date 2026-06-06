---
name: benchmark
description: Run the agentgrep cross-commit benchmark harness and detect performance regressions. Use when asked for $benchmark, benchmark timing, profile-engine benchmark selectors, performance comparisons, or benchmark evidence for agentgrep.
---

# Benchmark regression detector

Run `scripts/benchmark.py` across commits on the current branch,
parse the results, and flag any performance regressions. This skill
is project-internal — it assumes `scripts/benchmark.py` and
`scripts/benchmark.toml` exist in the repo root.

Use `$benchmark <component> [query]` for the local engine profiler
benchmark selectors. Component names keep profiling caps visible in
the benchmark name and report.

## Arguments

The user may provide:

- A **component** via `$benchmark <component>` — one of
  `search-prompts`, `search-conversations`, `grep-prompts`,
  `grep-conversations`, `find-prompts`, or `all`
- A **commit range** — e.g. "last 10 commits", "HEAD vs trunk",
  "since the query-language landing", a git range like `master..HEAD`
- A **bench subset** — e.g. "just grep", "import-time only",
  "all benches"
- **Run count** — e.g. "10 runs" or "thorough"

If no arguments are given, default to ALL configured benches across
`--range origin/master..HEAD`. **Never silently drop to a single
bench (e.g. import-time only)** — the user wants the full picture
unless they explicitly ask for a subset.

## Component shortcuts

`$benchmark <component>` maps to committed profile-engine benchmark
selectors:

| Component | Benchmark selector |
|---|---|
| `search-prompts` | `profile-engine-search-all-prompts-limit-500` |
| `search-conversations` | `profile-engine-search-all-conversations-limit-500` |
| `grep-prompts` | `profile-engine-grep-all-prompts-max-count-500` |
| `grep-conversations` | `profile-engine-grep-all-conversations-max-count-500` |
| `find-prompts` | `profile-engine-find-all-prompts-limit-500` |
| `all` | `profile-engine` |
| `cursor-ide` | `profile-engine-cursor-ide` |

If a component is supplied, use its selector as `--commands`. For
`all`, pass `--commands profile-engine`; the benchmark harness expands
that command group into every committed `profile-engine-*` benchmark.
For Cursor IDE SQLite profiling, pass `--commands profile-engine-cursor-ide`;
that group runs the Cursor-only search, grep, and find profile-engine
benchmarks without expanding the all-agent profiler group.
Keep the cap visible in reports: if a selector includes `limit-500` or
`max-count-500`, say `limit 500` or `max-count 500`.

## Procedure

### Step 1: Prompt the user for scope and depth

**Always prompt before running** (via `AskUserQuestion`) unless the
user already specified every parameter. Present estimated wall-clock
time for each option so the user can make an informed choice.

**Bench subset prompt** (skip if user specified `--commands`):

> Which benchmarks should I run?

| Option | Description |
|---|---|
| All benches (Recommended) | grep + find + search + import-time — the full picture |
| Workload only | grep + find + search — skips the cold-start bench |
| Import-time only | Cold-start `--help` timing — useful if you only changed imports |

**Run depth prompt** (skip if user specified `--runs`):

> How many samples per commit?

| Option | Runs | Warmup | Use case | Est. time (N commits × 4 benches) |
|---|---|---|---|---|
| Quick | 3 | 1 | Fast iteration, ±15% noise | N × 16s |
| Standard (Recommended) | 5 | 2 | Stable medians, catches 10%+ regressions | N × 28s |
| Thorough | 10 | 3 | Gold-standard p50/p90/p99, publishable results | N × 52s |

Calculate estimated time: `commits × benches × seconds_per_bench`.
Display it in the prompt: e.g. "61 commits × 4 benches × Standard
≈ 28 minutes".

**Commit range** (skip if user specified `--range` / `--lookback` / etc.):

Default to `--range origin/master..HEAD`. Count commits first:

```bash
git rev-list --count origin/master..HEAD
```

Report: "N commits on this branch since trunk."

### Step 2: Pre-flight checks

1. Confirm `scripts/benchmark.py` exists.
2. Confirm the branch is not trunk.
3. Report: "Benchmarking N commits × M benches at <depth> depth ≈ Xs."

### Step 3: Run the benchmark

Map the user's choices to flags:

```bash
uv run scripts/benchmark.py run \
  <target-flags> \
  --runs <N> --warmup <W> \
  --allow-dirty \
  --format json \
  --output /tmp/bench-<branch>.json \
  --no-progress
```

Always include `--allow-dirty` (the working tree may have unstaged
changes). Always `--format json` for analysis. Always
`--no-progress` (progress goes to stderr and clutters the output).

JSON and NDJSON rows include `dry_run`, `profile_payload`, and
`profile_capture_error`, plus `schema_version` and `artifact_kind` for
machine parsing. Treat `samples` as the timing evidence. For
`profile-engine-*` rows, `profile_payload` is a post-timing profile
capture that explains where the engine spent time; it is not another
timing sample. `command_string` is sanitized with `{repo}`, `{venv}`,
`{home}`, and `{query}` placeholders before serialization.

Use `--format rich --top-spans N` for a local terminal report that
includes nested `profile_payload` slow spans. Use `--top-spans 0` when
you want the rich timing table without nested profile detail.

For long runs (>5 minutes estimated), launch in background via
`run_in_background: true` so the user can continue working.

The script handles:
- `git checkout` per commit
- `uv sync` to rebuild the venv
- hyperfine (or pure-Python fallback) timing
- HEAD restoration on exit (atexit + signal trap)

### Step 4: Analyze saved benchmark artifacts

Use the benchmark analyzer before hand-written summaries:

```bash
uv run scripts/benchmark.py analyze \
  /tmp/bench-<branch>.json \
  --format rich \
  --top-spans 20 \
  --top-groups 10
```

For machine-readable evidence, write a sanitized analysis artifact:

```bash
uv run scripts/benchmark.py analyze \
  /tmp/bench-<branch>.json \
  --format json \
  --output /tmp/bench-<branch>-analysis.json
```

Analysis artifacts use `artifact_kind:
agentgrep.benchmark.analysis`. Their child rows/objects summarize
command timings, slow profile spans, profile span groups, and warnings.
Use them as the first-pass bottleneck summary; only drop to custom
Python snippets when the analyzer output is missing a specific view.

### Step 5: Parse results and detect regressions

Read the JSON output. For each command, compute:

**Per-commit stats:**
```python
avg = sum(r["samples"]) / len(r["samples"])
median = statistics.median(r["samples"])
```

**Distribution across all commits** (per bench):
- min, max, avg, p50, p90, p95, p99
- spread (max / min)

**Regression detection** — rolling 5-commit median baseline:
```python
window = 5
threshold = 1.15  # 15%
for i, commit in enumerate(ok_commits):
    if i < window: continue
    prev = sorted(avgs[i - window : i])
    baseline = prev[len(prev) // 2]
    if avgs[i] / baseline >= threshold:
        # flagged
```

**Spike vs step classification:**
- If the NEXT commit's avg < 95% of the flagged value → **SPIKE**
  (noise — filesystem cache, OS scheduling)
- Otherwise → **STEP** (persistent regression)

Only STEPs are actionable. SPIKEs are noise.

**Phase analysis** — divide commits into thirds:
- Early / Middle / Late averages
- Warming/cooling trend percentage

**Nested profile analysis** for `profile-engine-*` rows:
- Inspect `profile_payload.profile.samples`
- Start with `search.plan.decision`, `search.plan.strategy_group`,
  `search.plan.prefilter_root`,
  `search.plan.direct_source`,
  `search.collect.source`, optional `search.collect.scheduler`,
  optional `search.collect.source_scan_cache`,
  `search.discover.group`, and
  `find.filter.source`
- Compare agent/store/adapter/count attributes, not local paths or
  prompt text
- Prefer the analyzer's profile span groups for the first pass; they
  aggregate repeated source-level spans without leaking local paths or
  prompt text.

**End-to-end delta:**
- First measurable commit → HEAD, per bench
- Report: `grep: 1.17s → 1.15s (-1.6%)`

### Step 6: Report findings

```
## Benchmark results — <benches> × <N> commits (<depth> depth)

### Summary

| Bench | OK | Min | Avg | Max | p50 | p90 | First→HEAD |
|---|---|---|---|---|---|---|---|
| grep | 42 | 1.10s | 1.21s | 1.48s | 1.19s | 1.32s | -1.6% |
| ... | ... | ... | ... | ... | ... | ... | ... |

### Regressions (persistent steps)

| Bench | SHA | Time | vs baseline | Subject |
|---|---|---|---|---|
| (none found — or list here) |

### Spikes (noise — recovered on next commit)

| Bench | SHA | Time | vs baseline | Subject |
|---|---|---|---|---|
| grep | def5678 | 1.48s | +32% | feat: something |

### Trend

| Phase | grep | find | search | import-time |
|---|---|---|---|---|
| Early | 1.14s | 0.69s | 1.11s | 0.20s |
| Mid | 1.22s | 0.73s | 1.15s | 0.22s |
| Late | 1.20s | 0.73s | 1.17s | 0.23s |

### Verdict

<one-paragraph: "No actionable regressions" or
"N persistent regressions at <commits>">
```

### Step 7: Follow-up suggestions

- STEP found → "Profile with `python -X importtime` or `py-spy`
  at the regressing commit."
- Import-time trend > 20% → "Cold-start warming — check for new
  top-level imports that violate the lazy-import convention
  (AGENTS.md § Lazy imports)."
- No regressions → "Branch is performance-neutral. No action needed."

## Error handling

- `scripts/benchmark.py` missing → "The benchmark harness isn't
  in this repo. See docs/dev/benchmark.md for setup."
- Run fails mid-way → report partial results from whatever JSON
  was written.
- All `bench_fail` for a bench → skip it, note: "N commits
  predated the `--no-progress` flag."

## What this skill does NOT do

- Does not commit, push, or modify any files.
- Does not install dependencies beyond what `uv sync` provides.
- Does not compare against external baselines or saved history.
- Does not run in CI — it's a developer-local workflow.
