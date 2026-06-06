---
name: profile
description: Run the agentgrep local engine profiler for a named component or all components. Use when asked for $profile, profiling search paths, engine timings, or local bottleneck evidence.
---

# Profile agentgrep components

Use this skill for `$profile <component> [query]`.

This is a developer-local workflow. It runs `scripts/profile_engine.py` and
emits sanitized JSON: result counts, source counts, span names, durations, and
coarse subprocess metadata. Artifacts include `schema_version` and
`artifact_kind` so saved outputs can be identified later. They must not emit
prompt text, raw argv, or local absolute paths.

## Components

| Component | Command |
|---|---|
| `search-prompts` | `uv run python scripts/profile_engine.py search-prompts --agent all --limit 500 <query>` |
| `search-conversations` | `uv run python scripts/profile_engine.py search-conversations --agent all --limit 500 <query>` |
| `grep-prompts` | `uv run python scripts/profile_engine.py grep-prompts --agent all --max-count 500 <query>` |
| `grep-conversations` | `uv run python scripts/profile_engine.py grep-conversations --agent all --max-count 500 <query>` |
| `find-prompts` | `uv run python scripts/profile_engine.py find-prompts --agent all --limit 500` |
| `all` | `uv run python scripts/profile_engine.py all --agent all --limit 500 <query>` |
| `cursor-ide` | `uv run python scripts/profile_engine.py search-prompts --agent cursor-ide --limit 500 <query>` |

## Procedure

1. Pick the component from the table. If no component is provided, use `all`.
2. Use a narrow query for search and grep components. Avoid common terms unless
   the user explicitly asks for a broad profile.
3. No output flag means the default Rich slow-span table. Use `--json` for a
   single payload or `--ndjson` for one child profile run per line. The
   `--format json`, `--format ndjson`, and `--format rich` forms still work
   for templated invocations. Use `--top-spans 0` to hide the slow-span table.
4. Add `--json` before redirecting to `.tmp/profile-<component>.json` when the
   output will be reused in an issue or PR note.
5. Summarize the slowest spans and source/result counts; do not paste private
   prompt text or local paths.
6. For bottleneck diagnosis, inspect source-level spans:
   `search.discover.group`, `search.plan.decision`,
   `search.plan.strategy_group`, `search.plan.prefilter_root`,
   `search.plan.direct_source`, `search.collect.source`,
   optional `search.collect.scheduler`,
   optional `search.collect.source_scan_cache`, and
   `find.filter.source`.

Example:

```console
$ uv run python scripts/profile_engine.py grep-prompts \
    --agent all \
    --max-count 500 \
    --json \
    tmux > .tmp/profile-grep-prompts.json
```

Interactive slow-span summary:

```console
$ uv run python scripts/profile_engine.py all \
    --agent all \
    --limit 500 \
    --top-spans 20 \
    tmux
```
