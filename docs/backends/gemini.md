(backend-gemini)=

# Gemini CLI

Base path: `~/.gemini` (env override: `GEMINI_CLI_HOME`).

`observed_version`: `gemini-cli v0.42.0` stable; types from
`v0.44.0-nightly` HEAD `77e65c0d`.

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

## Path hashing

Gemini hashes project roots with SHA-256 to derive directory names.
agentgrep exposes {func}`~agentgrep.store_catalog.gemini_project_hash`
to reproduce this derivation.
