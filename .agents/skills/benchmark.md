---
name: benchmark
description: Run the agentgrep cross-commit benchmark harness and detect performance regressions. Use when asked to benchmark, profile performance across commits, check for regressions, or compare HEAD vs trunk timing.
---

# Benchmark regression detector

Run `scripts/benchmark.py` across commits on the current branch,
parse the results, and flag any performance regressions. This skill
is project-internal — it assumes `scripts/benchmark.py` and
`scripts/benchmark.toml` exist in the repo root.

## Arguments

The user may provide:

- A **commit range** — e.g. "last 10 commits", "HEAD vs trunk",
  "since the query-language landing", a git range like `master..HEAD`
- A **bench subset** — e.g. "just grep", "import-time only",
  "all benches"
- **Run count** — e.g. "3 runs" (default: 2 runs + 1 warmup)

If no arguments, default to `--range origin/master..HEAD` with all
configured benches, `--runs 2 --warmup 1`.

## Procedure

### Step 1: Determine the benchmark parameters

Map the user's request to `scripts/benchmark.py run` flags:

| User says | Flags |
|---|---|
| "last N commits" | `--lookback N` |
| "HEAD vs trunk" | `--head-vs-trunk` |
| "since <ref>" | `--range <ref>..HEAD` |
| "all commits on this branch" | `--range origin/master..HEAD` |
| "just grep" / "grep only" | `--commands grep` |
| "import-time" | `--commands import-time` |
| "all benches" | (omit --commands; runs all configured) |
| nothing specified | `--range origin/master..HEAD` with all benches |

Always include: `--runs 2 --warmup 1 --allow-dirty --format json --output /tmp/bench-skill.json`

Add `--no-progress` unless the user explicitly asks for live output.

### Step 2: Pre-flight checks

Before running:

1. Confirm `scripts/benchmark.py` exists.
2. Confirm the branch is not trunk (`git branch --show-current`
   must not be `master` or `main`).
3. Count the commits in scope (`git rev-list --count <range>`).
   If > 100, warn the user and ask for confirmation — each commit
   takes ~3-5 seconds per bench.
4. Estimate wall-clock time: `commits × benches × 4 seconds`.
   Print: "Benchmarking N commits × M benches ≈ Xs".

### Step 3: Run the benchmark

```bash
uv run scripts/benchmark.py run \
  <target-flags> \
  --runs 2 --warmup 1 \
  --allow-dirty \
  --format json \
  --output /tmp/bench-skill.json \
  --no-progress
```

The script handles:
- `git checkout` per commit
- `uv sync` to rebuild the venv
- hyperfine (or pure-Python fallback) timing
- HEAD restoration on exit

Stream stderr to the user so they see progress
(`[1/N] <sha> <subject>`). Use `2>&1` only if `--no-progress`
was NOT passed.

### Step 4: Parse results and detect regressions

Read `/tmp/bench-skill.json`. For each command (grep, find, search,
import-time), compute:

**Per-commit averages:**
```python
avg = sum(r["samples"]) / len(r["samples"])
```

**Distribution stats** (across all ok commits for that command):
- min, max, avg, p50, p90, p95, p99
- spread (max / min)

**Regression detection** — rolling 5-commit median baseline:
```python
window = 5
for i, commit in enumerate(ok_commits):
    if i < window:
        continue
    prev_avgs = sorted(avgs[i - window : i])
    baseline = prev_avgs[len(prev_avgs) // 2]
    if avg[i] / baseline >= 1.15:  # 15% threshold
        # flagged
```

**Spike vs step classification:**
- If the NEXT commit's avg recovers to < 95% of the flagged value,
  it's a **SPIKE** (noise — filesystem cache, OS scheduling).
- Otherwise it's a **STEP** (persistent regression — new import
  cost, algorithm change, hot-path overhead).

Only STEPs are real regressions. SPIKEs are noise.

**Phase analysis** — divide commits into thirds and compare averages:
- First third (scaffold/early)
- Middle third (features)
- Last third (polish/HEAD)
- Compute the warming/cooling trend percentage.

**End-to-end delta:**
- Compare the first measurable commit's avg to HEAD's avg.
- Report per bench: `grep: 1.17s → 1.15s (-1.6%)`.

### Step 5: Report findings

Present a structured report. Use this format:

```
## Benchmark results — <bench-list> × <N> commits

### Summary

| Bench | Measured | Min | Avg | Max | First→HEAD |
|---|---|---|---|---|---|
| grep | 42/61 | 1.10s | 1.21s | 1.48s | -1.6% |
| find | 42/61 | 0.67s | 0.73s | 0.87s | +1.8% |
| ... | ... | ... | ... | ... | ... |

### Regressions

(Only if STEPs found — persistent regressions that don't recover)

| Bench | SHA | Time | vs baseline | Subject |
|---|---|---|---|---|
| find | abc1234 | 0.83s | +17% | feat: the change |

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

<one-paragraph summary: "No actionable regressions" or
"N persistent regressions found at <commits>">
```

If `bench_fail` rows exist, add a note explaining why (e.g. "19
commits predated the `--no-progress` flag and failed the bench
command").

### Step 6: Follow-up suggestions

Based on findings, suggest:

- If a STEP regression is found: "Consider profiling with
  `python -X importtime` or `py-spy` at the regressing commit
  to identify the hot path."
- If import-time trend > 20%: "The cold-start warming may be
  worth investigating — `python -X importtime` shows the
  per-module import cost."
- If no regressions: "Branch is performance-neutral. No action
  needed."

## Error handling

- If `scripts/benchmark.py` doesn't exist: "The benchmark harness
  isn't in this repo. See docs/dev/benchmark.md for setup."
- If the bench run fails mid-way: report partial results from
  whatever was written to `/tmp/bench-skill.json`.
- If `bench_fail` on ALL commits for a bench: skip that bench
  in the report, note it was skipped.

## What this skill does NOT do

- Does not commit, push, or modify any files.
- Does not install dependencies beyond what `uv sync` provides.
- Does not compare against external baselines or saved history.
- Does not run in CI — it's a developer-local workflow.
