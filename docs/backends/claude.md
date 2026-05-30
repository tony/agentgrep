(backend-claude)=

# Claude Code

Base path: `~/.claude`.

Rows carry per-store observation stamps. Project transcripts were
observed against `claude-code v2.1.143` (2026-05-15); global prompt
history was observed against `claude-code v2.1.157` (2026-05-29).

## Stores

| Store ID | Role | Format | Searched | Adapter ID |
|----------|------|--------|:--------:|------------|
| `claude.history` | Prompt History | JSONL | ✓ | `claude.history_jsonl.v1` |
| `claude.projects.session` | Primary Chat | JSONL | ✓ | `claude.projects_jsonl.v1` |
| `claude.projects.subagent` | Supplementary Chat | JSONL | ✓ | `claude.projects_jsonl.v1` |
| `claude.projects.memory` | Persistent Memory | Markdown | | |
| `claude.tasks` | Todo | JSON | | |
| `claude.todos` | Todo | JSON | | |
| `claude.sessions` | App State | JSON | | |
| `claude.store_db` | App State | SQLite | | |
| `claude.paste_cache` | Cache | Opaque | | |
| `claude.plugins_cache` | Cache | Opaque | | |

## Record schemas

### claude.history

Global JSONL prompt-history audit log at `~/.claude/history.jsonl`.
Each line carries `display`, `pastedContents`, `timestamp` as Unix
milliseconds, `project`, and `sessionId`. `display` is the user-facing
prompt text; when it contains `[Pasted text #N]` placeholders,
agentgrep expands inline text from `pastedContents` or the external
`~/.claude/paste-cache/<contentHash>.txt` file when present. Missing
or non-text paste entries keep their original placeholder text.

```json
{"display": "Review [Pasted text #1]",
 "pastedContents": {"1": {"type": "text", "content": "..."}},
 "timestamp": 1700000000000,
 "project": "/repo",
 "sessionId": "..."}
```

### claude.projects.session

JSONL with stream fragments grouped by `uuid`. Keys: `type`, `uuid`,
`parentUuid`, `timestamp`, `sessionId`, `cwd`, `gitBranch`,
`version`, `message.role`, `message.content[]`
(`text`/`thinking`/`tool_use`/`tool_result`), `message.usage`.

```json
{"type": "user", "uuid": "...", "timestamp": "2026-05-17T...",
 "message": {"role": "user", "content": [{"type": "text", "text": "..."}]}}
```

Sub-agent dispatches nest under `<session_uuid>/subagents/` and use
the same record parser. agentgrep reports them as the distinct runtime
store `claude.projects_subagents` so main session files and nested
sub-agent files do not collapse into one source.
