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
parsed; `private` stores are intentionally not enumerated.

| Store ID | Role | Format | Coverage | Adapter ID |
|----------|------|--------|----------|------------|
| `codex.history` | Prompt History | JSON / JSONL | default | `codex.history_json.v1`, `codex.history_jsonl.v1` |
| `codex.sessions` | Primary Chat | JSONL | default | `codex.sessions_jsonl.v1` |
| `codex.session_index` | App State | JSONL | inspectable | `codex.session_index_jsonl.v1` |
| `codex.state_db` | App State | SQLite | inspectable | `codex.state_sqlite.v1` |
| `codex.memories_db` | Persistent Memory | SQLite | inspectable | `codex.memories_sqlite.v1` |
| `codex.goals_db` | Plan | SQLite | inspectable | `codex.goals_sqlite.v1` |
| `codex.instructions` | Instruction | Text | inspectable | `codex.instructions_text.v1` |
| `codex.memories` | Persistent Memory | Markdown | inspectable | |
| `codex.logs_db` | App State | SQLite | catalog | `codex.logs_sqlite.v1` |
| `codex.external_agent_imports` | App State | JSON | catalog | `codex.external_imports_json.v1` |
| `codex.config` | App State | Text | catalog | |
| `codex.model_cache` | Cache | JSON | catalog | |
| `codex.internal_storage` | App State | JSON | catalog | |
| `codex.plugins` | Instruction | Opaque | catalog | |
| `codex.skills` | Instruction | Text | catalog | |
| `codex.rules` | Instruction | Text | catalog | |
| `codex.runtime_cache` | Cache | Opaque | catalog | |
| `codex.log_files` | App State | Text | catalog | |
| `codex.process_manager` | App State | JSON | catalog | |
| `codex.shell_snapshots` | App State | Opaque | catalog | |
| `codex.auth` | App State | JSON | private | |
| `codex.policy` | App State | Opaque | private | |

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

### codex.session_index

`session_index.jsonl` is an append-only index with `id`,
`thread_name`, and `updated_at`. It is useful for inventory and
session selection, but the full transcript remains
`sessions/YYYY/MM/DD/rollout-*.jsonl`.

### SQLite Stores

Codex resolves SQLite storage from `CODEX_SQLITE_HOME`, then
`sqlite_home` in `config.toml`, then `CODEX_HOME`. The known DB files
are:

| Store | File | Notes |
|-------|------|-------|
| `codex.state_db` | `state_5.sqlite` | Threads, previews, dynamic tools, agent jobs, spawn edges, and job instructions. |
| `codex.logs_db` | `logs_2.sqlite` | Structured logs and feedback log payloads. |
| `codex.memories_db` | `memories_1.sqlite` | Memory pipeline outputs, rollout summaries, usage, and selection state. |
| `codex.goals_db` | `goals_1.sqlite` | Thread goal objectives, statuses, token budgets, and usage. |

These DBs are not searched by default because they can duplicate
transcripts, contain runtime state, or mix prompt-bearing fields with
operational metadata.

### Instructions, Memory, And Runtime State

`instructions.md`, `skills/`, `rules/`, and plugin bundles are
instruction surfaces rather than chat transcripts. `memories/` and
`memories_1.sqlite` hold retained memory and rollout summaries. Auth
and policy state are private; caches, logs, process-manager files,
shell snapshots, model cache, and temp directories are catalogued for
audits but stay outside default search.
