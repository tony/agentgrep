(backend-claude)=

# Claude Code

Base path: `~/.claude`.

`observed_version`: `claude-code v2.1.143` (2026-05-15).

## Stores

| Store ID | Role | Format | Searched | Adapter ID |
|----------|------|--------|:--------:|------------|
| `claude.projects.session` | Primary Chat | JSONL | ✓ | `claude.projects_jsonl.v1` |
| `claude.projects.subagent` | Supplementary Chat | JSONL | ✓ | |
| `claude.projects.memory` | Persistent Memory | Markdown | | |
| `claude.tasks` | Todo | JSON | | |
| `claude.todos` | Todo | JSON | | |
| `claude.sessions` | App State | JSON | | |
| `claude.store_db` | App State | SQLite | | |
| `claude.paste_cache` | Cache | Opaque | | |
| `claude.plugins_cache` | Cache | Opaque | | |

## Record schemas

### claude.projects.session

JSONL with stream fragments grouped by `uuid`. Keys: `type`, `uuid`,
`parentUuid`, `timestamp`, `sessionId`, `cwd`, `gitBranch`,
`version`, `message.role`, `message.content[]`
(`text`/`thinking`/`tool_use`/`tool_result`), `message.usage`.

```json
{"type": "user", "uuid": "...", "timestamp": "2026-05-17T...",
 "message": {"role": "user", "content": [{"type": "text", "text": "..."}]}}
```

Sub-agent dispatches nest under `<session_uuid>/subagents/`.
