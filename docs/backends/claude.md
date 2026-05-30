(backend-claude)=

# Claude Code

Base path: `~/.claude` (env override: `CLAUDE_CONFIG_DIR`).

Rows carry per-store observation stamps. Project transcripts were
observed against `claude-code v2.1.143` (2026-05-15); global prompt
history was observed against `claude-code v2.1.157` (2026-05-29).

## Stores

Coverage is not the same as default search. `default` stores are
searched normally; `inspectable` stores are discoverable only when an
inventory caller opts in; `catalog` stores are documented but not
parsed; `private` stores are intentionally not enumerated.

| Store ID | Role | Format | Coverage | Adapter ID |
|----------|------|--------|----------|------------|
| `claude.history` | Prompt History | JSONL | default | `claude.history_jsonl.v1` |
| `claude.projects.session` | Primary Chat | JSONL | default | `claude.projects_jsonl.v1` |
| `claude.projects.subagent` | Supplementary Chat | JSONL | default | `claude.projects_jsonl.v1` |
| `claude.store_db` | App State | SQLite | inspectable | `claude.store_sqlite.v1` |
| `claude.projects.session_memory` | Persistent Memory | Text | inspectable | `claude.session_memory_text.v1` |
| `claude.tasks` | Todo | JSON | inspectable | `claude.tasks_json.v1` |
| `claude.plans` | Plan | Text | inspectable | `claude.plans_text.v1` |
| `claude.settings` | App State | JSON | catalog | `claude.settings_json.v1` |
| `claude.projects.memory` | Persistent Memory | Markdown | catalog | |
| `claude.todos` | Todo | JSON | catalog | |
| `claude.sessions` | App State | JSON | catalog | |
| `claude.context_mode` | App State | JSON / SQLite | catalog | |
| `claude.ide` | App State | JSON | catalog | |
| `claude.skills` | Instruction | Text | catalog | |
| `claude.teams` | Instruction | Text | catalog | |
| `claude.paste_cache` | Cache | Opaque | catalog | |
| `claude.image_cache` | Cache | Opaque | catalog | |
| `claude.file_history` | Cache | Opaque | catalog | |
| `claude.plugins_cache` | Cache | Opaque | catalog | |
| `claude.shell_snapshots` | App State | Text | catalog | |
| `claude.security_state` | App State | JSON | private | |
| `claude.session_env` | App State | Opaque | private | |

## Version detection

Claude Code source discovery reports data-shape versions from concrete
record evidence. Global history records with `display`, `timestamp`,
and `project` are reported as `claude.history_jsonl.log_entry.v1`.
Project and sub-agent transcripts use embedded transcript `version`
metadata when present; otherwise their `type`, `sessionId`, and
`message` keys identify the JSONL message shape.

Catalog observation stamps remain the fallback for discovered stores
whose concrete shape cannot be sampled safely.

## Record schemas

### claude.history

Global JSONL prompt-history audit log at
`${CLAUDE_CONFIG_DIR or ~/.claude}/history.jsonl`. Each line carries
`display`, `pastedContents`, `timestamp` as Unix milliseconds,
`project`, and `sessionId`. `display` is the user-facing prompt text;
when it contains `[Pasted text #N]` placeholders, agentgrep expands
inline text from `pastedContents` or the external
`paste-cache/<contentHash>.txt` file when present. Missing or non-text
paste entries keep their original placeholder text.

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

### claude.store_db

Claude Code also keeps `__store.db` under the config root. Observed
tables include `base_messages`, `user_messages`,
`assistant_messages`, and `conversation_summaries`. The message tables
can duplicate JSONL transcript content, so agentgrep catalogs and
discovers the DB for explicit inventory but does not search it by
default.

### claude.projects.session_memory

Session memory summaries live under
`projects/<encoded_project>/<session_uuid>/session-memory/summary.md`.
They can contain prompt context and summaries, but they are derived
state rather than the primary transcript.

### Tasks, Plans, And Configuration

Tasks are JSON files under `tasks/<task_list>/<task_id>.json` with
`id`, `subject`, `description`, `status`, `blocks`, `blockedBy`, and
optional metadata fields. Plans are Markdown files under `plans/*.md`.
Settings, keybindings, context-mode files, security state, session
environment, IDE bridge state, skills, teams, plugin cache, file
history, shell snapshots, and image/paste caches are catalogued so
storage audits can identify them without treating them as default
prompt history.
