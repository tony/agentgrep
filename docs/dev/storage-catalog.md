(storage-catalog)=

# Storage catalogue

agentgrep keeps an explicit catalogue of every on-disk store it knows
about, modelled as Pydantic
{class}`~agentgrep.stores.StoreDescriptor` rows aggregated under one
{class}`~agentgrep.stores.StoreCatalog`. The catalogue is **descriptive**:
it documents *where* each agent's data lives and *what* the records look
like. Coverage decisions — whether agentgrep searches, inventories, or
only documents a store — are captured per-row so adding storage
knowledge does not automatically expand default prompt search.

The catalogue is the single source of truth that downstream adapters
consume. When upstream renames a path or changes a record shape, the
fix is to update one
{class}`~agentgrep.stores.StoreDescriptor` and bump the
catalogue version; adapters pick the new metadata up automatically.

```{contents}
:local:
:depth: 2
```

## Catalog summary

```{storage:catalog-summary}
```

## Why a catalogue

Three reasons we did not bake paths into the adapters:

1. **Provenance.** Each row carries an `observed_version` and
   `observed_at` stamp. A reader can tell at a glance whether the
   schema notes are still current or stale.
2. **Drift.** Codex renames `history.jsonl`, Cursor adds a CLI agent
   layout, Gemini reorganises its `tmp/` tree. With paths catalogued
   centrally, those changes diff cleanly in code review.
3. **Overlap.** Several stores live in adjacent paths but play
   different roles — Codex `history.jsonl` (user prompts only) vs.
   `sessions/*.jsonl` (full per-thread transcripts); Gemini
   `tmp/<hash>/chats/` (live) vs. `history/<timestamp>/` (post-retention
   archive). The `distinguishes_from` field on each descriptor names
   the sibling and explains the difference.

## Reading a descriptor

```python
from agentgrep.store_catalog import CATALOG

claude_session = CATALOG.by_id("claude.projects.session")
claude_session.path_pattern
# '${HOME}/.claude/projects/<encoded_project>/<session_uuid>.jsonl'
```

Path patterns use `${HOME}` and `${<ENV>}` tokens; resolving them
against a concrete environment is the consumer's job, so the catalogue
stays portable. `env_overrides` lists the env vars that change the
root (Claude respects `CLAUDE_CONFIG_DIR`; Codex respects
`CODEX_HOME` and `CODEX_SQLITE_HOME`; Gemini respects
`GEMINI_CLI_HOME`).

## Coverage levels

Every descriptor has an effective coverage level:

| Coverage | Meaning |
|----------|---------|
| `default_search` | Normal search and find commands discover and parse this store. |
| `inspectable` | Inventory tools can discover it when explicitly requested; default search skips it. |
| `catalog_only` | The path and schema are documented, and default search skips it. A row may still expose a conservative structural sample for explicit inspection. |
| `private` | The store is documented but intentionally not enumerated from disk. |

This distinction lets the catalogue describe auth files, runtime logs,
shell snapshots, and file-history caches without making them part of
ordinary prompt search.

## Version detection strategies

Discovery payloads include a `version_detection` object for each source
agentgrep can enumerate. The object records the app version when local
metadata exposes one, the data-shape version, the strategy used, the
confidence level, and a short evidence string.

The strategies are:

| Strategy | Meaning |
|----------|---------|
| `embedded_metadata` | The source itself carries a version field, such as a session metadata record. |
| `shape_inference` | The file name, record keys, table names, or SQLite suffix identify the data shape. |
| `version_check` | A local version file provides app-version context without spawning the upstream CLI. |
| `catalog_observation` | No concrete source evidence was available, so the catalog observation stamp is reported as a low-confidence fallback. |

The concrete data shape is authoritative. If a modern app-version hint
coexists with an old unmigrated file, agentgrep parses the file by its
own shape. See {ref}`adr-storage-version-detection` for the full
decision.

Discovery callers choose how much version evidence they need. Normal
`grep`, `search`, and `find` paths use metadata-free discovery so the
planner can prune source handles before opening files for evidence.
Inventory and MCP source-listing surfaces keep shape detection enabled,
while catalog-only detail remains available for callers that want a
cheap, low-confidence metadata stamp.

Search callers also narrow discovery by descriptor role before walking
the filesystem. Prompt scope first enumerates `prompt_history` rows and
then falls back, per agent, to `primary_chat` and `supplementary_chat`
rows only when no prompt-history source exists for that agent.
Conversation scope enumerates the chat roles directly, and all scope
keeps the full default-search catalogue.

## Stores by agent

### Claude Code

`observed_version`: ``claude-code v2.1.185`` (observed 2026-06-21).

Claude honours `CLAUDE_CONFIG_DIR`, falling back to `${HOME}/.claude`.
Its global prompt-history audit log lives at
`${CLAUDE_CONFIG_DIR or ${HOME}/.claude}/history.jsonl` and is parsed by
`claude.history_jsonl.v1`. It stores the user-facing `display` text,
Unix-millisecond `timestamp`, `project`, `sessionId`, and
`pastedContents`; content-addressed text pastes resolve through
`paste-cache/<contentHash>.txt` when present.

Claude's primary chat record lives at
`${HOME}/.claude/projects/<encoded_project>/<session_uuid>.jsonl`. The
file format is JSONL with multiple record types per line —
`type: "user"`, `type: "assistant"`, `type: "attachment"`,
`type: "permission-mode"`. Sub-agent dispatches nest under
`<session_uuid>/subagents/`, share the same parser, and are exposed as
the distinct runtime store `claude.projects_subagents`. `__store.db`,
session memory, project auto-memory, `CLAUDE.md`, tasks, todos, plans,
skills, legacy commands, project-local `.claude` instructions, plugin
instructions, and teams are inspectable but remain outside default
search because they either duplicate transcripts, represent derived
state, or steer future sessions. Tasks and todos emit their subject,
content, and description fields; teams emit team descriptions and
member prompts. Settings and app-state JSON expose only top-level key
and type summaries, so raw values such as env vars are not indexed.
Debug output and shell snapshots expose metadata-only file summaries.
Backups, uploads, file history, context/security state, credentials,
session environment, and cache payloads are catalogued or private
according to sensitivity.
Claude source version detection uses `embedded_metadata` for transcript
`version` fields, `shape_inference` for history records with `display`,
`timestamp`, and `project`, task/todo/team JSON keys, settings and
app-state key summaries, plugin manifests and hook event names,
instruction Markdown paths, file metadata summaries, and
`catalog_observation` as the fallback. Project-local discovery is
bounded to roots already present in Claude transcript metadata.

### Cursor CLI and Cursor IDE

Cursor is modelled as two separate agents — `cursor-cli` (the
`cursor-agent` terminal binary) and `cursor-ide` (the desktop app) —
because they have disjoint data homes and on-disk formats.

- **`cursor-cli`** spans two home directories. The original
  `${HOME}/.cursor/` tree holds the JSONL transcripts
  (`cursor_cli.transcripts_jsonl.v1`, Anthropic-style
  `{role, message.content[]}` with no native timestamp, so agentgrep
  backfills the file mtime) and the AI-tracking SQLite summaries. The
  newer lowercase `${HOME}/.config/cursor/` home holds
  `prompt_history.json` — a flat JSON array of typed prompts parsed by
  `cursor_cli.prompt_history_json.v1`, Cursor's prompt-history store —
  and the per-session chat `chats/<hash>/<uuid>/store.db`. The chat
  store holds content-addressed protobuf blobs with no published
  schema; `cursor_cli.chats_protobuf.v1` walks the wire format
  best-effort and is inspectable (opt-in) rather than searched by
  default, since it overlaps the cleaner transcripts.
  `cursor-cli.worktrees` is catalogued with `role=SOURCE_TREE` and
  `search_by_default=False` so the adapter never indexes multi-gigabyte
  git working trees as chat history. `cursor-cli.skills` covers the
  `SKILL.md` definitions under `~/.cursor/skills/` and
  `~/.cursor/skills-cursor/` as inspectable instruction text.
- **`cursor-ide`** is parsed by `cursor_ide.state_vscdb_modern.v1` /
  `cursor_ide.state_vscdb_legacy.v1` via VS Code-style `state.vscdb`
  SQLite. `cursor-ide.state_vscdb` covers the global database and
  `cursor-ide.workspace_state` covers the per-workspace
  `workspaceStorage/<hash>/state.vscdb`; both surface the
  `aiService.prompts` history alongside composer/chat keys. On WSL,
  discovery also probes the Windows-host mount under `/mnt/c/Users`
  (overridable via `AGENTGREP_WSL_USERS_ROOT`) for a Windows-side Cursor
  editing a WSL project, mirroring the VS Code backend; see
  {ref}`adr-cross-host-discovery`.

### Codex

`observed_version`: ``github.com/openai/codex@3fb81667`` (2026-06-21).
Codex honours `CODEX_HOME` for primary files. SQLite files resolve
through `CODEX_SQLITE_HOME`, then `sqlite_home` in `config.toml`, then
`CODEX_HOME`.

Schemas are pinned directly to the upstream Rust types:

- {attr}`~agentgrep.stores.StoreFormat.JSONL` `history.jsonl` →
  `HistoryEntry { session_id: String, ts: u64, text: String }`
  ([`codex-rs/message-history/src/lib.rs:56-60`](https://github.com/openai/codex/blob/3fb81667/codex-rs/message-history/src/lib.rs#L56)).
- Per-thread `sessions/YYYY/MM/DD/rollout-…jsonl` → tagged enum
  `RolloutItem` with variants `SessionMeta`, `ResponseItem`,
  `Compacted`, `TurnContext`, `EventMsg`
  ([`codex-rs/protocol/src/protocol.rs:2929`](https://github.com/openai/codex/blob/3fb81667/codex-rs/protocol/src/protocol.rs#L2929)).
- Legacy root `sessions/rollout-*.json` → JSON object with `session`
  metadata and an `items` array carrying message-like records.

The `_N.sqlite` files belong to the Codex CLI, not Cursor. Known
SQLite stores are `state_5.sqlite`, `logs_2.sqlite`,
`memories_1.sqlite`, and `goals_1.sqlite`. Prompt-bearing fields such
as `threads.first_user_message`, `threads.preview`, memory summaries,
goal objectives, and job instructions are inspectable storage rather
than default search.
Codex source version detection uses `shape_inference` for
`history.jsonl`, legacy `history.json`, legacy root rollout JSON,
`session_index.jsonl`, external import ledgers, memory Markdown,
config TOML, project config TOML, app-state JSON summaries, plugin
manifests, plugin marketplace metadata, hook event names, instruction
Markdown/rule paths, file metadata summaries, and SQLite suffixes. It
uses `embedded_metadata` for session `cli_version`, and
`version_check` for `models_cache.json.client_version` app-version
context. Project-local `.codex` discovery is bounded to roots already
present in Codex session metadata.

### Gemini CLI

`observed_version`: ``gemini-cli v0.47.0`` stable (observed
2026-06-21); types pinned at HEAD `927170fc`. Three adapters cover the
three on-disk shapes:

- `gemini.tmp_chats_jsonl.v1` parses
  `tmp/<project_hash>/chats/session-*.jsonl`. Each file opens with
  a `SessionMetadataRecord` (`sessionId`, `projectHash`,
  `startTime`, `lastUpdated`, `kind`); subsequent lines are
  `MessageRecord` turns interleaved with
  `MetadataUpdateRecord` updates (`{$set: {…}}`). Real files
  surface `type` values `user` and `gemini`; upstream types also
  declare `info`/`error`/`warning` plus `RewindRecord` and
  `PartialMetadataRecord`, but those records did not appear in
  sampling. `gemini`-typed turns whose `content` is empty have
  their searchable text drawn from `thoughts[*].subject`/
  `description` and `toolCalls[*].name`/`description`, joined
  into one record per turn.
- `gemini.tmp_chats_legacy_json.v1` parses pre-Feb 2026
  `tmp/<project_hash>/chats/session-*.json` single-file sessions.
  Upstream still reads this shape via the `isLegacyRecord`
  discriminator at
  [`chatRecordingService.ts:1041`](https://github.com/google-gemini/gemini-cli/blob/927170fc/packages/core/src/services/chatRecordingService.ts#L1041);
  the legacy file holds session metadata at the top level and the
  full conversation under a `messages` array.
- `gemini.tmp_logs_json.v1` parses
  `tmp/<project_hash>/logs.json` — a flat JSON array of
  `LogEntry` records (user-prompt audit log).

The `gemini.memory` row covers `~/.gemini/GEMINI.md`, the global
user-authored context file injected into sessions — the Gemini
analogue of Claude's `CLAUDE.md`, parsed by `gemini.memory_text.v1` as
an inspectable (opt-in) store rather than searched by default.

Gemini's
[`sessionCleanup.ts`](https://github.com/google-gemini/gemini-cli/blob/927170fc/packages/cli/src/utils/sessionCleanup.ts)
hard-deletes expired sessions via `fs.unlink()` — there is no
`history/` archive. The Antigravity files some installs carry under
`~/.gemini/antigravity/conversations/` are written by the
[Antigravity IDE](https://github.com/google-gemini/gemini-cli/blob/927170fc/packages/core/src/ide/detect-ide.ts),
a separate Google product — Gemini CLI only detects Antigravity as
an IDE launcher and does not read or write the protobuf
conversation files. They are documented as the separate
{doc}`/backends/antigravity-ide` and {doc}`/backends/antigravity-cli`
backends, not as Gemini adapters.

The `project_hash` is `sha256(absolute_project_root)`. agentgrep
exposes a Python mirror via
{func}`~agentgrep.store_catalog.gemini_project_hash` so the CLI can
answer "which Gemini sessions belong to *this* repo?".

### Grok CLI

`observed_version`: ``grok-cli v0.2.59`` (observed 2026-06-21).

Grok stores data under `${GROK_HOME or ${HOME}/.grok}/sessions/`
using URL-encoded absolute project paths as directory keys
(e.g. `%2Fhome%2Fd%2Fwork%2Fpython%2Fproj`). Four adapters cover
the searchable store shapes:

- `grok.prompt_history_jsonl.v1` parses per-project
  `sessions/<project>/prompt_history.jsonl`. Each line is a
  `{ timestamp, session_id, prompt, is_bash }` audit record — one
  entry per user prompt, analogous to Codex's `history.jsonl`.
- `grok.sessions_jsonl.v1` parses per-session
  `sessions/<project>/<uuid>/chat_history.jsonl`. Lines carry a
  `type` field (`system` / `user` / `assistant` / `tool_use` /
  `tool_result`) and a `content` field (string or content-blocks
  array). All record types are emitted.
- `grok.session_search_sqlite.v1` parses the global
  `sessions/session_search.sqlite` FTS5 index. Table `session_docs`
  has `session_id`, `cwd`, `updated_at` (unix seconds), `title`
  (generated), and `content` (full-text indexed body).
- `grok.subagents_json.v1` parses per-subagent
  `sessions/<project>/<uuid>/subagents/<subagent>/meta.json`. The
  delegated `prompt` is the only persisted record of the subagent, so
  it is emitted as supplementary-chat content, parity with the Claude
  and Cursor CLI subagent stores.

The `grok.plans` row covers per-session `plan.md` plan-mode Markdown,
and `grok.memory` covers the flat and per-project
`memory/**/MEMORY.md` subtree — both inspectable (opt-in) like
`claude.plans`. Documentary-only entries cover per-session
`system_prompt.txt`, `prompt_context.json`, `hunk_records.jsonl`
(edit attribution), `updates.jsonl` (ACP stream), `terminal/*.log`
(tool stdout), plus events, summaries, logs, worktrees, and config —
all carrying no user prompt payload and catalogued with
`search_by_default=False` or deferred.

### Pi

`observed_version`: ``pi v0.79.9`` (observed 2026-06-21).

Pi (earendil-works) stores each conversation as one append-only JSONL
file under `${PI_CODING_AGENT_DIR or ${HOME}/.pi/agent}/sessions/`,
grouped by working directory (`--<encoded_cwd>--`, leading slash
stripped and `/ \ :` replaced by `-`). It keeps no separate
prompt-history log and no SQLite index, so a single adapter covers the
whole searchable surface:

- `pi.sessions_jsonl.v1` parses `sessions/--<cwd>--/<ts>_<uuid>.jsonl`.
  Line one is a `type:"session"` header (`version` may be absent in v1
  files); each later line is a `SessionEntry` tagged union. `message`
  entries carry an LLM message (`role` user / assistant / toolResult,
  `content` string or content-blocks; assistant turns carry `model`),
  while `compaction` / `branch_summary` summaries and `session_info`
  names are emitted as history text. User turns surface as prompts via
  the shared role-to-kind mapping.

Discovery resolves two roots: `PI_CODING_AGENT_DIR` (the agent dir,
default `~/.pi/agent`) and the optional `PI_CODING_AGENT_SESSION_DIR`,
which holds session files flat with the cwd recovered from the header.

Documentary-only entries cover settings, auth (private credentials),
models, themes, tools, managed binaries, prompt templates, the debug
log, and the npm extension install root.

### OpenCode

`observed_version`: ``opencode v1.17.9`` (observed 2026-06-21).

OpenCode (anomalyco/opencode) stores conversations in a single SQLite
database under `${XDG_DATA_HOME or ${HOME}/.local/share}/opencode/`,
unlike the JSONL-transcript backends:

- `opencode.db_sqlite.v1` parses the `opencode.db` SQLite database. It
  joins the relational `part → message → session` tables: each
  text-bearing `part` row becomes a record whose `kind` comes from the
  joined message `role` (`user` → prompt, else history). Searchable text
  is `part.data` of type `text`/`reasoning` (the `text` field) and
  `subtask` (the `prompt`); the session `title`, `directory`, and the
  message `model`/timestamp are attached. Message times are
  unix-milliseconds, normalized to ISO-8601.

Discovery resolves the data root via `XDG_DATA_HOME` (default
`~/.local/share`) plus the `opencode` segment and finds `opencode.db` by
filename — not a glob, so the binary SQLite file bypasses the text
prefilter. An absolute `OPENCODE_DB` value is discovered as that exact
file, so channel installs are reachable by pointing `OPENCODE_DB` at
their `opencode-<channel>.db`.

OpenCode's unreleased v2 event-sourced tables (`session_input`,
`session_message`, `event`/`event_sequence`, `todo`) share the same
`opencode.db` file but are empty beta state on stable installs — the
canonical transcript stays in `session`/`message`/`part` — so they are
not searched. The secret-bearing `account`/`credential` tables are
present but never enumerated.

Documentary-only entries cover the legacy per-file JSON layout, config,
auth (private credentials), snapshots, the repo cache, logs, and tool
output.

### VS Code (GitHub Copilot Chat)

`observed_version`: ``VS Code GitHub Copilot Chat (chatSessions v3)``
(observed 2026-06-21).

VS Code's built-in Copilot Chat stores readable JSON transcripts under
the workbench `User/` directory, covered across the `Code`,
`Code - Insiders`, `VSCodium`, and `Code - OSS` editions:

- `vscode.chat_sessions_json.v1` parses one session object per
  `workspaceStorage/<hash>/chatSessions/<uuid>.json` (and the windowless
  `globalStorage/emptyWindowChatSessions/*.json`). Each `requests[]` turn
  yields a user prompt from `message.text` and an assistant record from
  the no-`kind` `MarkdownString` response parts joined; tool names come
  from `result.metadata.toolCallRounds[].toolCalls[].name`, and the
  epoch-millisecond `timestamp` is normalized to ISO-8601. The sibling
  `workspace.json` `folder` URI resolves the project `cwd`, mapping a
  `vscode-remote://wsl+<distro>/<path>` remote to its Linux path.
- `vscode.inline_history_sqlite.v1` reads the `inline-chat-history` key of
  the global `state.vscdb` `ItemTable` — a JSON array of Ctrl+I
  inline-edit prompts. Only that key is read, so the `secret://…` auth
  keys in the same database are never enumerated.

Discovery resolves `User/` directories per OS (Linux `~/.config/Code`,
macOS `~/Library/Application Support/Code`, Windows `%APPDATA%/Code`) and,
on WSL, also probes the Windows host mount under `/mnt/c/Users` because a
WSL-remote workspace stores its chat client-side on Windows.
`VSCODE_APPDATA` pins one `Roaming` directory and `AGENTGREP_WSL_USERS_ROOT`
overrides the Windows users mount. See
{doc}`adr/0009-cross-host-discovery` for the cross-host design.

Documentary-only entries cover the per-chat `chatEditingSessions/` edit
snapshots (a byproduct of the transcripts, keyed by the same session
UUID) and the `secret://…` auth keys in `state.vscdb` (private
credentials, never enumerated).

## Adding or updating a store

1. Edit the per-agent module under `src/agentgrep/store_catalog/`
   (e.g. `vscode.py`). Stamp `observed_version` and `observed_at`
   against the version you actually inspected.
2. Add an `upstream_ref` (preferred) or a `sample_record` so future
   readers can verify the schema.
3. If the new store overlaps a sibling, name it in
   `distinguishes_from` and explain the difference in
   `schema_notes`.
4. Capture a redacted fixture under
   `tests/samples/<agent>/<store_id>/`.
5. Bump `catalog_version` in the same commit that changes
   descriptor shape.
6. Run `uv run pytest tests/test_stores.py`.

## See also

- {mod}`agentgrep.stores` — model definitions
- {mod}`agentgrep.store_catalog` — concrete registry
