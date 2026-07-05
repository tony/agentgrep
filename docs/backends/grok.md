(backend-grok)=

# Grok CLI

Grok CLI combines a prompt-history audit log with full session transcript
files. agentgrep searches user prompts by default and exposes assistant,
reasoning, and tool records when the caller chooses the conversation scope.

Base path: `~/.grok` (env override: `GROK_HOME`).

`observed_version`: `grok-cli v0.2.59` (observed 2026-06-21).

Grok stores data under `~/.grok/sessions/` using URL-encoded project
paths as directory keys (e.g. `%2Fhome%2Fd%2Fwork%2Fpython%2Fproj`).
Each session is identified by a UUIDv7 (timestamp-sortable).

## Stores

```{storage:agent} grok
```

## Record schemas

### Prompt history

{storage:storeref}`grok.prompt_history` is a per-project user-prompt audit log. One
record per prompt, append-only.

```json
{"timestamp": "2026-05-25T10:00:00.000000000Z",
 "session_id": "019729a0-...", "prompt": "...", "is_bash": false}
```

Keys: `timestamp` (ISO-8601 nanosecond), `session_id` (UUIDv7),
`prompt` (user text), `is_bash` (bool — true for shell commands).

### Session transcripts

{storage:storeref}`grok.sessions` contains full session transcripts. The `type` field
discriminates record kinds: `system`, `user`, `assistant`, `reasoning`,
`tool_result`, `backend_tool_call`. Assistant tool calls live in a `tool_calls`
array on the assistant record; `reasoning` records carry a readable `summary`
array of `{type: summary_text, text}` blocks plus an opaque `encrypted_content`
blob, but agentgrep does not surface them because the adapter reads only
`content`, which reasoning records omit. `content` is either a plain string or
a content-blocks array.

```json
{"type": "user", "content": "explain the design",
 "timestamp": "2026-05-25T10:00:01.000000000Z"}
```

### Subagent delegations

{storage:storeref}`grok.subagents` is one JSON dispatch object per delegated subagent
under `sessions/<project>/<session>/subagents/<subagent>/meta.json`. The
subagent's own turns are not persisted separately, so the delegated `prompt` is
the only searchable record of the delegation.

```json
{"subagent_id": "019e6626-...", "parent_session_id": "019e660d-...",
 "subagent_type": "code-explorer", "description": "Map the auth module",
 "prompt": "Explore the auth module and summarize ...", "tool_calls": []}
```

agentgrep emits the `prompt` as one supplementary-chat record titled
with `description`; `subagent_type` and `parent_session_id` are
attached as metadata.

### Session search index

{storage:storeref}`grok.session_search` is a SQLite database with FTS5. Table
`session_docs`:

| Column | Type | Description |
|--------|------|-------------|
| `session_id` | TEXT | UUIDv7 primary key |
| `cwd` | TEXT | Working directory |
| `updated_at` | INTEGER | Unix seconds |
| `title` | TEXT | Generated session title |
| `content` | TEXT | Full-text indexed body |
| `content_hash` | TEXT | Content digest |
| `last_indexed_offset` | INTEGER | Incremental-index cursor |

A sibling `meta` table holds `session_search_schema_version` (3) and
`last_bootstrap_at`; `PRAGMA user_version` stays 0. agentgrep converts
`updated_at` to ISO-8601 for timestamp consistency with other adapters.

### Plans

{storage:storeref}`grok.plans` is per-session plan-mode Markdown at
`sessions/<project>/<session>/plan.md` — the agent's working plan. Inspectable
(opt-in), parity with {storage:storeref}`claude.plans` and
{storage:storeref}`cursor-cli.plans`; not searched by default.
