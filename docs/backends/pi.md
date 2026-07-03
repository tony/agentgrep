(backend-pi)=

# Pi

Base path: `~/.pi/agent` (env override: `PI_CODING_AGENT_DIR`).

`observed_version`: `pi v0.79.9` (observed 2026-06-21).

pi (the earendil-works "Pi Agent Harness") stores each conversation as
one append-only JSONL file under `~/.pi/agent/sessions/`, grouped by
working directory. The directory key is the cwd with its leading slash
stripped and `/`, `\`, and `:` replaced by `-`, wrapped in double
dashes (e.g. `--home-d-work-python-agentgrep--`). Each session file is
named `<iso-timestamp>_<session-uuid>.jsonl`.

Unlike Codex or Grok, pi keeps no separate prompt-history log and no
SQLite session index — the session transcript is the entire searchable
surface, which makes pi the structural twin of the Claude Code backend.
agentgrep projects user turns from that transcript into the default
prompt scope; assistant, tool, summary, and branch records require
`--scope conversations` or `--scope all`.

The optional `PI_CODING_AGENT_SESSION_DIR` override points at the
sessions directory directly. When it is set, pi writes session files
flat into that directory with no per-working-directory subdirectory;
agentgrep then recovers the cwd from each session's header rather than
the directory name.

## Stores

```{storage:agent} pi
```

## Record schemas

### pi.sessions

The first line is a session header; `version` is `3` and may be absent
in older (v1) files.

```json
{"type": "session", "version": 3, "id": "019e5691-...",
 "timestamp": "2026-05-23T20:41:01.417Z",
 "cwd": "/home/d/work/python/agentgrep"}
```

Every later line is a `SessionEntry` sharing `id` / `parentId` /
`timestamp` (an append-only tree, not a flat list). A `message` entry
wraps an LLM message; `role` is `user`, `assistant`, or `toolResult`,
and `content` is a string or a content-blocks array. Assistant turns
carry `model` and `provider` inline. A `bashExecution` role has no
`content`; agentgrep joins its shell `command` and `output` as the
searchable text.

```json
{"type": "message", "id": "...", "parentId": "...",
 "timestamp": "2026-05-23T20:41:05.000Z",
 "message": {"role": "user",
             "content": [{"type": "text", "text": "..."}],
             "timestamp": 1779999665000}}
```

User turns surface as prompts and assistant / tool turns as history via
the shared role-to-kind mapping. `compaction` and `branch_summary`
entries contribute their `summary` text, and `session_info` contributes
its user-set `name`; `model_change`, `thinking_level_change`, `custom`,
and `label` entries are metadata only. Entry-level timestamps are
ISO-8601; the inner `message.timestamp` is unix-milliseconds and is used
only as a fallback.

### pi.context_mode_db

`~/.pi/context-mode/sessions/<project_hash>.db` is a per-project SQLite
database rooted outside the agent dir; the stem is
`sha256(project_dir)[:16]`, so it is a hashed `cwd` grouping holding
multiple sessions (each row carries its own `session_id`). Its
`session_events` table holds events (`type` = role/intent/decision/
tool_call/file_read/blocker_resolved/data) with a JSON `data` payload,
emitted as inspectable records; sibling `session_meta`/`session_resume`/
`tool_calls` tables exist but only `session_events` is parsed.

## Documentary stores

The remaining `pi.*` rows are catalogued for completeness but not
searched: `pi.settings`, `pi.models`, `pi.themes`, `pi.tools`,
`pi.bin`, `pi.prompts`, `pi.debug_log`, and `pi.extensions_npm` (the
managed npm extension install root). `pi.auth` holds provider
credentials and is documented but never enumerated from disk.
