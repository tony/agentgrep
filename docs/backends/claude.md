(backend-claude)=

# Claude Code

Base path: `~/.claude` (env override: `CLAUDE_CONFIG_DIR`).

`observed_version`: `claude-code v2.1.185` (observed 2026-06-21).

## Stores

Coverage is not the same as default search. `default` stores are
searched normally; `inspectable` stores are discoverable only when an
inventory caller opts in; `catalog` stores are documented but not
searched by default; `private` stores are intentionally not
enumerated. Some catalog stores expose safe structural samples for
explicit inspection.

```{storage:agent} claude
```

## Version detection

Claude Code source discovery reports data-shape versions from concrete
record evidence. Global history records with `display`, `timestamp`,
and `project` are reported as `claude.history_jsonl.log_entry.v1`.
Project and sub-agent transcripts use embedded transcript `version`
metadata when present; otherwise their `type`, `sessionId`, and
`message` keys identify the JSONL message shape.
Task files with `id`, `subject`, `description`, and `status` are
reported as `claude.tasks.json.v1`. Todo, team, app-state, skill,
command, plugin, project instruction, user/project memory, and
auto-memory stores use path and key-shape inference for their
non-default adapters. Settings and app-state files use
medium-confidence key/type summaries because their values are
deliberately not indexed. Debug logs and shell snapshots expose
metadata-only summaries, not raw file content.

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
optional metadata fields. agentgrep emits one inspectable task sample
from the subject and description plus status/blocking metadata. Plans
are Markdown files under `plans/*.md`.

Settings and keybindings expose only top-level key summaries for
explicit inspection, so raw values such as environment variables are
not indexed. Auto-memory Markdown, `CLAUDE.md` memory files,
persistent todos, user skills, legacy commands, project-local
commands/agents/skills, plugin instructions, and team member prompts
are inspectable because they can steer future Claude Code behavior.
Project-local files are discovered only from roots already referenced
by local Claude transcript metadata; agentgrep does not recursively
scan `$HOME` for every possible project.

Context-mode files, IDE bridge state, Chrome/native integration state,
native installer metadata, jobs, stats/update cache, and session state
expose only key/type summaries. Debug logs and shell snapshots expose
file metadata such as name, suffix, byte size, and line count. Security
state, session environment, uploads, file history, backups, generic
cache, credentials, and image/paste caches stay catalogued or private
so storage audits can identify them without treating them as default
prompt history.
