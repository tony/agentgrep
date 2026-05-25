(backend-codex)=

# Codex

Base path: `~/.codex` (env override: `CODEX_HOME`).

`observed_version`: `github.com/openai/codex@4c89772` (2026-05-16).

## Stores

| Store ID | Role | Format | Searched | Adapter ID |
|----------|------|--------|:--------:|------------|
| `codex.history` | Prompt History | JSONL | ✓ | `codex.history_json.v1` |
| `codex.sessions` | Primary Chat | JSONL | ✓ | `codex.sessions_jsonl.v1` |
| `codex.state_db` | App State | SQLite | | |
| `codex.logs_db` | App State | SQLite | | |
| `codex.memories` | Persistent Memory | Markdown | | |

## Record schemas

### codex.history

One record per user prompt, append-only across all threads.

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
