(backend-opencode)=

# OpenCode

Base path: `~/.local/share/opencode` (env overrides: `XDG_DATA_HOME`, `OPENCODE_DB`).

`observed_version`: `opencode v1.17.9` (observed 2026-06-21).

OpenCode (anomalyco/opencode) stores conversations in a single SQLite
database, `opencode.db`, under its XDG data directory
(`${XDG_DATA_HOME:-~/.local/share}/opencode`). Non-stable install
channels use `opencode-<channel>.db`, and `OPENCODE_DB` can relocate the
database (an absolute path is used directly). This makes OpenCode a
SQLite backend, like Grok's `session_search` and Cursor's `state.vscdb`,
rather than a JSONL-transcript backend.

## Stores

```{storage:agent} opencode
```

## Record schema

### opencode.db

A relational `session → message → part` schema (Drizzle). A conversation
turn is reconstructed by joining a `part` row up to its `message` (for
the role) and `session` (for the title and working directory).
User text parts participate in the default prompt scope; assistant and
reasoning parts require `--scope conversations` or `--scope all`.

`session` table — one row per session:

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT | Session id (primary key) |
| `project_id` | TEXT | Git remote/root hash, or `global` |
| `directory` | TEXT | Working directory, stored verbatim |
| `title` | TEXT | Session title |
| `time_created` / `time_updated` | INTEGER | Unix milliseconds |

`message` table — `id`, `session_id` (FK), and a `data` JSON column:

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

The same `opencode.db` file also carries OpenCode's unreleased v2
event-sourced tables — `session_input`, `session_message`,
`event`/`event_sequence`, and `todo`. On stable installs these are empty
beta state; the canonical transcript stays in `session`/`message`/`part`,
so agentgrep does not search them. The secret-bearing `account`,
`account_state`, `control_account`, and `credential` tables are present
but never enumerated — the adapter reads only text-bearing `part` rows.

The legacy pre-migration layout (one JSON file per session, message, and
part under `storage/`) is documented but no longer searched — current
installs migrate it into `opencode.db` on startup.
