(backend-grok)=

# Grok CLI

Grok CLI combines a prompt-history audit log with full session transcript
files. agentgrep searches user prompts by default. Targeted effort resolves a
prompt record's session UUID to the corresponding project transcript; full
assistant, reasoning, and tool records also require conversation or all scope.

Base path: `~/.grok` (env override: `GROK_HOME`).

`observed_version`: `grok 1.0.0` (observed 2026-08-08).

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
`tool_result`, `backend_tool_call`.

Not every `user` record is something you typed. Grok writes its own
injected turns into the same stream, tagged with a `synthetic_reason`
key naming why — `system_reminder` for tool nudges, `project_instructions`
for `AGENTS.md` content. In the sample this page was verified against,
52 of 101 `user` records carried one. agentgrep does not yet read that
key, so those injected turns are searched as if you had typed them; a
prompt result you do not recognise is most likely one of these.

Assistant tool calls live in a `tool_calls`
array on the assistant record; `reasoning` records carry a readable `summary`
array of `{type: summary_text, text}` blocks plus an opaque `encrypted_content`
blob, but agentgrep does not surface them because the adapter reads only
`content`, which reasoning records omit. `content` is either a plain string or
a content-blocks array.

```json
{"type": "user", "content": "explain the design"}
```

Transcript records carry no clock of their own — no record of any type
has a `timestamp` key — so agentgrep backfills the transcript file's
modification time. That dates a record to its session rather than to
its turn, which is enough to order results and answer a date filter but
not to distinguish two turns in the same session. The per-prompt clock
lives next door, in the prompt-history log.

An `assistant` record names the model that answered in `model_id` — Grok's
spelling of the key other agents call `model` — and agentgrep surfaces it as
the record's model, so `model:grok-*` reaches Grok transcripts.

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

A sibling `meta` table holds `session_search_schema_version` (4) and
`last_bootstrap_at`; `PRAGMA user_version` stays 0. agentgrep converts
`updated_at` to ISO-8601 for timestamp consistency with other adapters.

### Plans

{storage:storeref}`grok.plans` is per-session plan-mode Markdown at
`sessions/<project>/<session>/plan.md` — the agent's working plan. Inspectable
(opt-in), parity with {storage:storeref}`claude.plans` and
{storage:storeref}`cursor-cli.plans`; not searched by default.

## Project context

| Store | `model` | `cwd` | `branch` |
|-------|---------|-------|----------|
| {storage:storeref}`grok.sessions` | assistant record's `model_id` | `sessions/<project>/`, URL-decoded | — |
| {storage:storeref}`grok.prompt_history` | — | `sessions/<project>/`, URL-decoded | — |
| {storage:storeref}`grok.session_search` | — | `session_docs.cwd` | — |

`%2F` is a lossless escape, so the project directory key inverts exactly
— the {ref}`lossless tier <backend-cwd-tiers>`. agentgrep decodes it back
into the working directory and reports it on every prompt-history and
transcript record, which is the same absolute path
{storage:storeref}`grok.session_search` already stored literally in
`session_docs.cwd`. All three stores therefore answer `--cwd` and `cwd:`
with one working directory per session.

That encoding has one exception, and it is the reason a deeply nested
project can behave differently from a shallow one. When the encoded
name would exceed 255 bytes, Grok names the directory with a slug plus
a hash instead and writes the real path into a `.cwd` file inside the
group. A slug does not invert, so agentgrep reads the sidecar; without
it those sessions would lose `cwd` while every shallower project kept
it. A directory that neither decodes nor carries a `.cwd` file yields
no `cwd` rather than a plausible one.

`branch:` does not reach this backend, but not because Grok is unaware
of git. Each session's `summary.json` carries `head_branch`,
`head_commit`, `git_root_dir`, and `git_remotes` — no store row reads
that file, so the branch is on disk and out of reach rather than
absent.
