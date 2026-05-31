(backend-codex)=

# Codex

Base path: `~/.codex` (env override: `CODEX_HOME`).
SQLite path: `CODEX_SQLITE_HOME`, then `sqlite_home` from
`config.toml`, then `CODEX_HOME`.

`observed_version`: `github.com/openai/codex@4c89772` (2026-05-16).

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

### codex.history

One record per user prompt, append-only across all threads. Modern
Codex writes `history.jsonl` records with `session_id`, Unix-second
`ts`, and `text`; older installs may carry `history.json` records with
`command` and `timestamp`. agentgrep supports both shapes but reports
the JSONL shape through `codex.history_jsonl.v1`.

```json
{"session_id": "...", "ts": 1747509826, "text": "<user prompt>"}
```

Upstream type: `HistoryEntry { session_id: String, ts: u64, text: String }`
([`codex-rs/message-history/src/lib.rs:54`](https://github.com/openai/codex/blob/4c89772/codex-rs/message-history/src/lib.rs#L54)).

### codex.sessions

JSONL `RolloutItem` tagged enum (`type` + `payload`):
`session_meta` | `response_item` | `compacted` | `turn_context` | `event_msg`.

```json
{"type": "response_item", "payload": {"role": "user", "content": "<prompt>"}}
```

Upstream type: [`codex-rs/protocol/src/protocol.rs:2783`](https://github.com/openai/codex/blob/4c89772/codex-rs/protocol/src/protocol.rs#L2783).

Older installs can also have root-level
`sessions/rollout-YYYY-MM-DD-*.json` files. Those are JSON objects
with `session` metadata and an `items` array carrying message-like
records with `role`, `type`, and `content`. agentgrep treats them as
the same primary chat store through `codex.sessions_legacy_json.v1`.

### codex.session_index

`session_index.jsonl` is an append-only index with `id`,
`thread_name`, and `updated_at`. It is useful for inventory and
session selection, but the full transcript remains
`sessions/YYYY/MM/DD/rollout-*.jsonl` or the legacy root
`sessions/rollout-*.json` shape.

### SQLite Stores

Codex resolves SQLite storage from `CODEX_SQLITE_HOME`, then
`sqlite_home` in `config.toml`, then `CODEX_HOME`. The known DB files
are:

| Store | File | Notes |
|-------|------|-------|
| `codex.state_db` | `state_5.sqlite` | Threads, previews, dynamic tools, agent jobs, spawn edges, and job instructions. |
| `codex.logs_db` | `logs_2.sqlite` | Structured logs and feedback log payloads; catalog-only samples read `feedback_log_body`. |
| `codex.memories_db` | `memories_1.sqlite` | Memory pipeline outputs, rollout summaries, usage, and selection state. |
| `codex.goals_db` | `goals_1.sqlite` | Thread goal objectives, statuses, token budgets, and usage. |

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
