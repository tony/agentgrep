(backend-gemini)=

# Gemini CLI

Base path: `~/.gemini` (env override: `GEMINI_CLI_HOME`).

`observed_version`: `gemini-cli v0.47.0` stable (observed 2026-06-21);
types pinned at HEAD `927170fc`.

## Stores

```{storage:agent} gemini
```

## Record schemas

### gemini.tmp.chats

JSONL with mixed record types. Line 1 is a `SessionMetadataRecord`
(`sessionId`, `projectHash`, `startTime`, `lastUpdated`, `kind`).
Subsequent lines are `MessageRecord` turns (`id`, `timestamp`,
`type`, `content`) interleaved with `MetadataUpdateRecord` updates
(`{$set: {...}}`).

For `gemini`-typed records whose `content` is empty, the assistant's
prose is drawn from `thoughts[*].subject`/`description` and the
tool-call context from `toolCalls[*].name`/`description`.

### gemini.tmp.chats\_legacy

Pre-Feb 2026 single-file `.json` format. JSON object with session
metadata at the top level and the full conversation under a
`messages` array.

### gemini.tmp.logs

Flat JSON array of `LogEntry { sessionId, messageId, timestamp,
type, message }` — user-prompt audit log.

### gemini.memory

`~/.gemini/GEMINI.md` — the global user-authored context/memory file
injected into Gemini CLI sessions, the analogue of Claude's
`CLAUDE.md`. Standing instructions rather than chat, so it is
inspectable (opt-in) rather than searched by default.

## Path hashing

Legacy `tmp/` project directories are named by the SHA-256 of the
project root; current Gemini CLI also uses timestamp-style and plain
project-basename directory names. Discovery does not depend on the
scheme — agentgrep walks `tmp/` recursively — but agentgrep still
exposes {func}`~agentgrep.store_catalog.gemini_project_hash` to
reproduce the legacy hash directories.
