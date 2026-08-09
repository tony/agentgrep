(backend-gemini)=

# Gemini CLI

Gemini CLI keeps a prompt-history log alongside active JSONL and legacy JSON
chat files. Normal search reads the prompt log; `--exhaustive` also projects prompts
from the chat files. The prompt-log adapter has no proof-bound targeted route,
so `--deep` does not select Gemini conversations.

Base path: `~/.gemini` (env override: `GEMINI_CLI_HOME`).

`observed_version`: `gemini-cli v0.54.4` (observed 2026-08-08);
types pinned at HEAD `927170fc`.

## Stores

```{storage:agent} gemini
```

## Record schemas

### Active chat JSONL

{storage:storeref}`gemini.tmp.chats` is JSONL with mixed record types. Line 1 is a
`SessionMetadataRecord` (`sessionId`, `projectHash`, `startTime`,
`lastUpdated`, `kind`). Subsequent lines are `MessageRecord` turns (`id`,
`timestamp`, `type`, `content`) interleaved with `MetadataUpdateRecord` updates
(`{$set: {...}}`). Some user records also carry `displayContent` (the UI-echo
variant); `content` is the expanded form agentgrep searches.

For `gemini`-typed records whose `content` is empty, the assistant's
prose is drawn from `thoughts[*].subject`/`description` and the
tool-call context from `toolCalls[*].name`/`description`.

### Legacy chat JSON

{storage:storeref}`gemini.tmp.chats_legacy` is the pre-Feb 2026 single-file `.json`
format. It is a JSON object with session metadata at the top level and the full
conversation under a `messages` array.

### Prompt logs

{storage:storeref}`gemini.tmp.logs` is a flat JSON array of
`LogEntry { sessionId, messageId, timestamp, type, message }` — a user-prompt
audit log.

### Memory file

{storage:storeref}`gemini.memory` is `~/.gemini/GEMINI.md` — the global user-authored
context/memory file injected into Gemini CLI sessions, the analogue of
Claude's `CLAUDE.md`. Standing instructions rather than chat, so it is
inspectable (opt-in) rather than searched by default.

## How a project directory is named

Gemini has named `tmp/` project directories three different ways over
time, and a long-lived home holds all three at once. One tree here
carries 38 SHA-256 directories, 59 run-scoped names shaped like
`20260424-214247z-3714694-63ea`, and 45 plain project-basename slugs.

You do not have to care for search: agentgrep walks `tmp/` recursively,
so every scheme is found. You do have to care for two narrower things.

{func}`~agentgrep.store_catalog.gemini_project_hash` reproduces the
SHA-256 scheme only. It still answers "which directory holds this
repo?" for a hash-named tree, and answers nothing for the other two —
so treat it as a reader for older layouts, not as a general lookup.
The reverse index that does cover every scheme is `projects.json`,
which maps each absolute project root to the directory name Gemini
chose for it.

A `cwd_hash` is published only when the directory name really is a
digest. A basename slug is not one, and labelling it as such would
answer `cwd_hash:` with a value no agent ever wrote, so agentgrep
leaves the field unset instead — see {ref}`backend-cwd-tiers`. The
literal working directory is unaffected; it comes from the sibling
`.project_root` file either way.

## Project context

| Store | `model` | `cwd` | `branch` |
|-------|---------|-------|----------|
| {storage:storeref}`gemini.tmp.chats` | assistant turn's `model` | metadata line's `directories[0]`, else sibling `.project_root` | — |
| {storage:storeref}`gemini.tmp.chats_legacy` | — | sibling `.project_root` | — |
| {storage:storeref}`gemini.tmp.logs` | — | sibling `.project_root` | — |

All three prompt stores live under one `tmp/<project>/` directory, and
the literal path is on disk in two places: the session metadata line
names it in a plural `directories` array — where every other agent
writes a scalar `cwd` — and Gemini drops a `.project_root` file beside
the directory. agentgrep reads both, so Gemini records answer `--cwd`,
`cwd:`, `repo:`, and `project:`. Both sources are
{ref}`lossless <backend-cwd-tiers>`; a missing `.project_root` is
ordinary on older trees and simply leaves the record without a `cwd`.

`cwd_hash` is the narrower field. A record carries one only when its
project directory is genuinely a digest, so a hash-named tree answers
`cwd_hash:` and a slug-named tree does not. Filtering by `cwd:` reaches
both, which is why it is the better habit for this backend.

Gemini records no git branch in any of its prompt stores, so `branch:`
does not reach this backend.
