(backend-codex)=

# Codex

Codex stores prompt recall, JSONL session transcripts, rollout summaries, and
optional SQLite state under one home directory. agentgrep searches prompt and
session records by default, then leaves metadata caches and database state for
explicit inventory or sample inspection.

Base path: `~/.codex` (env override: `CODEX_HOME`).
SQLite path: `CODEX_SQLITE_HOME`, then `sqlite_home` from
`config.toml`, then `CODEX_HOME`.

`observed_version`: `github.com/openai/codex@3fb81667` (2026-06-21).

## Stores

Coverage is not the same as default search. `default` stores are
searched normally; `inspectable` stores are discoverable only when an
inventory caller opts in; `catalog` stores are documented but not
searched by default; `private` stores are intentionally not
enumerated. Some catalog stores have safe sample parsers for
`inspect_record_sample`, but they do not join normal search.

```{storage:agent} codex
```

## Version detection

Codex exposes both app-version context and concrete data-shape
versions in source discovery. `models_cache.json.client_version`
provides app-version context when present; `version.json.latest_version`
is not treated as the installed version. Session transcripts can carry
`session_meta.payload.cli_version`, which is stronger evidence for
that transcript than the global cache.

Metadata-rich discovery reads the root client-version cache once per
discovery pass and reuses it for every Codex source. Normal search and
find paths skip version evidence entirely, so broad all-agent lookups do
not reread root metadata before the query planner narrows the source set.

Data-shape detection is based on the source itself. `history.jsonl`
records with `session_id`, `ts`, and `text` are reported as
`codex.history_jsonl.current`; legacy `history.json` array records with
`command` and `timestamp` are reported as
`codex.history_json.legacy`. Legacy root `sessions/rollout-*.json`
objects with `session` and `items` are reported as
`codex.sessions.legacy_json.v1`. SQLite stores derive data versions
from their migration suffixes, such as `state_5.sqlite` →
`codex.state.sqlite.v5`. Config, app-state, skill, rule, and plugin
adapters infer shape from TOML keys, JSON keys, manifest keys, hook
event names, marketplace keys, file metadata, or instruction paths
while keeping those sources outside default search.

## Record schemas

### Prompt history

{storage:storeref}`codex.history` is one record per user prompt, append-only across
all threads. Modern Codex writes `history.jsonl` records with `session_id`,
Unix-second `ts`, and `text`; older installs may carry `history.json` records
with `command` and `timestamp`. agentgrep supports both shapes but reports the
JSONL shape through `codex.history_jsonl.v1`.

```json
{"session_id": "...", "ts": 1747509826, "text": "<user prompt>"}
```

Upstream type: `HistoryEntry { session_id: String, ts: u64, text: String }`
([`codex-rs/message-history/src/lib.rs:56`](https://github.com/openai/codex/blob/3fb81667/codex-rs/message-history/src/lib.rs#L56)).

### Session transcripts

{storage:storeref}`codex.sessions` is a JSONL `RolloutItem` tagged enum (`type` +
`payload`): `session_meta` | `response_item` | `compacted` | `turn_context` |
`event_msg`.

```json
{"type": "response_item", "payload": {"role": "user", "content": "<prompt>"}}
```

Upstream type: [`codex-rs/protocol/src/protocol.rs:2929`](https://github.com/openai/codex/blob/3fb81667/codex-rs/protocol/src/protocol.rs#L2929).

Older installs can also have root-level
`sessions/rollout-YYYY-MM-DD-*.json` files. Those are JSON objects
with `session` metadata and an `items` array carrying message-like
records with `role`, `type`, and `content`. agentgrep treats them as
the same primary chat store through `codex.sessions_legacy_json.v1`.

### Session index

{storage:storeref}`codex.session_index` is an append-only index with `id`,
`thread_name`, and `updated_at`. It is useful for inventory and session
selection, but the full transcript remains `sessions/YYYY/MM/DD/rollout-*.jsonl`
or the legacy root `sessions/rollout-*.json` shape.

### SQLite Stores

Codex resolves SQLite storage from `CODEX_SQLITE_HOME`, then
`sqlite_home` in `config.toml`, then `CODEX_HOME`. The known DB files
are:

| Store | File | Notes |
|-------|------|-------|
| {storage:storeref}`codex.state_db` | `state_5.sqlite` | Threads, previews, dynamic tools, agent jobs, spawn edges, and job instructions. |
| {storage:storeref}`codex.logs_db` | `logs_2.sqlite` | Structured logs and feedback log payloads; catalog-only samples read `feedback_log_body`. |
| {storage:storeref}`codex.memories_db` | `memories_1.sqlite` | Memory pipeline outputs, rollout summaries, usage, and selection state. |
| {storage:storeref}`codex.goals_db` | `goals_1.sqlite` | Thread goal objectives, statuses, token budgets, and usage. |

These DBs are not searched by default because they can duplicate
transcripts, contain runtime state, or mix prompt-bearing fields with
operational metadata.

### Instructions, Memory, And Runtime State

`instructions.md`, `skills/`, `rules/`, project `.codex/skills/`, and
plugin bundles are instruction surfaces rather than chat transcripts.
The root instructions file, user skills, project skills, rules, plugin
manifests, plugin marketplace metadata, plugin hooks, and plugin
command/agent/skill/custom-skill Markdown are inspectable but stay
outside default search. Project-local files are discovered only from
roots already referenced by local Codex session metadata; agentgrep
does not recursively scan `$HOME` for arbitrary `.codex` directories.

`memories/` and `memories_1.sqlite` hold retained memory and rollout
summaries; the Markdown workspace is inspectable through
`codex.memories_text.v1`. The external-agent import ledger exposes
imported thread ids and source file names for explicit inspection
without indexing full imported content. Config TOML, managed config,
environment TOML, config backups, project config,
update/version/model/internal JSON, hooks, arg0 runtime state, and
process-manager state expose only key/type summaries. Raw logs, shell
snapshots, and personality-migration markers expose metadata-only file
summaries.
Auth, installation id, secrets, `.env`, and policy state are private;
caches, SQLite sidecars, and temp directories are catalogued for audits
but stay outside default search.

## Project context

| Store | `model` | `cwd` | `branch` |
|-------|---------|-------|----------|
| {storage:storeref}`codex.sessions` | first `turn_context` payload's `model` | `session_meta` `cwd` | `session_meta` `git.branch` |
| {storage:storeref}`codex.state_db` | `threads.model` | `threads.cwd` | `threads.git_branch` |
| {storage:storeref}`codex.history` | — | — | — |

The two stores that know where a session ran write the path literally, so
they land in the {ref}`lossless tier <backend-cwd-tiers>` and answer
`--cwd`, `cwd:`, and `branch:` with the real values.

A rollout's `session_meta` header names `model_provider` — the provider
id, not a model slug — so the session model is read from the first valid
per-turn `turn_context` record instead, which is where Codex writes the
slug the session actually ran as. The reader scans complete JSONL records,
discarding unrelated large records in bounded chunks rather than imposing a
fixed byte cutoff. `session_meta` is the fallback only when the rollout has
no valid model-bearing `turn_context` record.

A {storage:storeref}`codex.state_db` `threads` row keeps the model, the
working directory, the git branch, and the remote URL beside the prompt
text, so those records carry an origin without re-reading the rollout
file. `git_origin_url` lands on `origin.remote`. The `agent_jobs` rows in
the same database stay origin-less: the shipped table has no `thread_id`
column to reach a `threads` row through. The database is reachable at
`--scope conversations`, not at the default prompt scope.

The state database is an index and fallback, not a second canonical
transcript. When a matching `first_user_message` row and a matching rollout
prompt have the same text and identify the same conversation by exact rollout
path or thread id, agentgrep keeps the rollout result. Resolution happens
across matching candidates before ranking and limits, so index-only matches
such as a state-database title, model, or working directory remain searchable.
Explicit store, path, and adapter filters still select the physical state row;
`--no-dedupe` and genuinely different state previews remain unchanged.

{storage:storeref}`codex.history` carries no project context in any shape
Codex has shipped: the JSONL rows are `session_id`, `ts`, and `text`, and
the legacy JSON rows are `command` and `timestamp`. That store is
searchable by text, agent, and time, and it does not satisfy an origin
filter.
