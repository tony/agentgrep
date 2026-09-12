(backend-opencode)=

# OpenCode

OpenCode is a SQLite-backed conversation backend. agentgrep reconstructs turns
from the relational session/message/part tables, and reads OpenCode's
prompt-history log for the fast prompt path, so plain `agentgrep search`
reaches OpenCode prompts without `--exhaustive`. Assistant and reasoning parts
require conversation scope. Targeted effort cannot route this backend.

Base path: `~/.local/share/opencode` (env overrides: `XDG_DATA_HOME`,
`OPENCODE_DB`); prompt history under `~/.local/state/opencode` (env override:
`XDG_STATE_HOME`).

`observed_version`: `opencode v1.18.30` (observed 2026-09-11).

OpenCode (anomalyco/opencode) stores conversations in a single SQLite
database, `opencode.db`, under its XDG data directory
(`${XDG_DATA_HOME:-~/.local/share}/opencode`). Non-stable install
channels use `opencode-<channel>.db`, and `OPENCODE_DB` can relocate the
database (an absolute path is used directly).

## Stores

```{storage:agent} opencode
```

## Record schema

### Conversation database

{storage:storeref}`opencode.db` is a relational `session → message → part` schema
(Drizzle). A conversation turn is reconstructed by joining a `part` row up to
its `message` (for the role) and `session` (for the title and working
directory). User text parts participate in prompt scope under `--exhaustive`;
assistant and reasoning parts require `--scope conversations` or
`--scope all`.

`session` table — one row per session:

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT | Session id (primary key) |
| `project_id` | TEXT | Git remote/root hash, or `global` |
| `directory` | TEXT | Working directory, stored verbatim |
| `title` | TEXT | Session title |
| `time_created` / `time_updated` | INTEGER | Unix milliseconds |

`message` table — `id`, `session_id` (FK), and a `data` JSON column.
Assistant messages carry a top-level `modelID`/`providerID`/`path.cwd`;
user messages instead nest the selected model under `model.modelID`, so
agentgrep reads `modelID` and falls back to `model.modelID`:

```json
{"role": "assistant", "modelID": "...", "providerID": "...",
 "time": {"created": 1779999665000}, "path": {"cwd": "..."}}
```

`part` table — `id`, `message_id` (FK), `session_id`, and a `data` JSON
column holding one content part. The searchable text lives here:

| Part `type` | Searchable field |
|-------------|------------------|
| `text` | `text` (user prompts and assistant replies) |
| `reasoning` | `text` (model thinking) |
| `subtask` | `prompt` |

A part's `kind` is derived from the joined message `role` (`user` →
prompt, otherwise history). Tool, file, snapshot, patch, and step-marker
parts are metadata and stay outside default search. Message timestamps
are unix-milliseconds and are normalized to ISO-8601.

The same `opencode.db` file also carries OpenCode's v2 event-sourced
tables — `session_input`, `session_message`, `event`/`event_sequence`,
and `todo`. The `event` table is now populated on stable installs and
mirrors the `part` transcript text, so agentgrep leaves it unsearched to
avoid duplicate hits; the canonical transcript stays in
`session`/`message`/`part`. The secret-bearing `account`,
`account_state`, `control_account`, and `credential` tables are present
but never enumerated — the adapter reads only text-bearing `part` rows.

The legacy pre-migration layout (one JSON file per session, message, and
part under `storage/`) is documented but no longer searched — current
installs migrate it into `opencode.db` on startup.

### Prompt history

{storage:storeref}`opencode.prompt_history` is OpenCode's recall log, one JSON
object per line:

```json
{"input": "Review [Pasted ~43 lines]", "mode": "normal",
 "parts": [{"type": "text", "text": "...",
            "source": {"text": {"start": 7, "end": 25,
                                "value": "[Pasted ~43 lines]"}}}]}
```

`input` is the prompt as typed. A pasted block shows there as a placeholder,
and its `parts` entry holds the pasted `text` with the placeholder's span;
agentgrep splices the paste back in, so pasted text is searchable. `mode` is
kept as metadata.

The log has no timestamp and no session id, and agentgrep invents neither. Its
records sort after dated ones, a date filter excludes them, and
`has:timestamp` is false for them. Plain search reads this log alone, so a
prompt appears once. Under `--exhaustive` or `--scope all`,
{storage:storeref}`opencode.db` is read too, and a prompt present in both
appears once per store, the same way Codex's prompt history stands beside its
session transcripts and Claude Code's `history.jsonl` beside its project
transcripts. The log is not a copy of the database: prompts recalled there can
be absent from `opencode.db` entirely.

## Changes by version

Each entry brackets a change between the observation that first saw it and
the last one that did not; see {ref}`storage-observations`.

### 1.18.30

Observed 2026-09-11: no store changes since 1.18.15 (2026-08-08).
