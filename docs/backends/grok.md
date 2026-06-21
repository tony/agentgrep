(backend-grok)=

# Grok CLI

Base path: `~/.grok` (env override: `GROK_HOME`).

`observed_version`: `grok-cli v0.2.59` (observed 2026-06-21).

Grok stores data under `~/.grok/sessions/` using URL-encoded project
paths as directory keys (e.g. `%2Fhome%2Fd%2Fwork%2Fpython%2Fproj`).
Each session is identified by a UUIDv7 (timestamp-sortable).

## Stores

```{storage:agent} grok
```

## Record schemas

### grok.prompt\_history

Per-project user-prompt audit log. One record per prompt, append-only.

```json
{"timestamp": "2026-05-25T10:00:00.000000000Z",
 "session_id": "019729a0-...", "prompt": "...", "is_bash": false}
```

Keys: `timestamp` (ISO-8601 nanosecond), `session_id` (UUIDv7),
`prompt` (user text), `is_bash` (bool — true for shell commands).

### grok.sessions

Full session transcripts. The `type` field discriminates record
kinds: `system`, `user`, `assistant`, `tool_use`, `tool_result`.
`content` is either a plain string or a content-blocks array.

```json
{"type": "user", "content": "explain the design",
 "timestamp": "2026-05-25T10:00:01.000000000Z"}
```

### grok.session\_search

SQLite with FTS5. Table `session_docs`:

| Column | Type | Description |
|--------|------|-------------|
| `session_id` | TEXT | UUIDv7 primary key |
| `cwd` | TEXT | Working directory |
| `updated_at` | INTEGER | Unix seconds |
| `title` | TEXT | Generated session title |
| `content` | TEXT | Full-text indexed body |
| `content_hash` | TEXT | Content digest |

agentgrep converts `updated_at` to ISO-8601 for timestamp
consistency with other adapters.
