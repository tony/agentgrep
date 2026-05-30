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

## Stores by agent

### Claude Code

Claude rows carry per-store observation stamps. Project transcript
schemas were observed against ``claude-code v2.1.143`` (2026-05-15);
global prompt history was observed against ``claude-code v2.1.157``
(2026-05-29).

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

### Cursor

Two distinct surfaces, both catalogued and both searched:

- **Cursor CLI agent** (`cursor-agent`): transcripts live at
  `${HOME}/.cursor/projects/<id>/agent-transcripts/<session_uuid>/<session_uuid>.jsonl`
  and are parsed by `cursor.cli_jsonl.v1`. Records are
  Anthropic-style `{role, message.content[]}` with `text` and
  `tool_use` content blocks; tool outputs are sometimes `[REDACTED]`
  in older `cursor-agent` builds. There is no native per-turn
  timestamp, so agentgrep backfills the file's mtime. Sub-agent
  transcripts nested under `subagents/` share the parser but surface as
  the distinct runtime store `cursor.cli_subagents`.
- **Cursor IDE**: parsed by `cursor.state_vscdb_modern.v1` /
  `cursor.state_vscdb_legacy.v1` via `state.vscdb` (SQLite). The
  catalogue keeps the IDE path separate from the CLI agent so the
  two never collide.

`cursor.cli.worktrees` is catalogued explicitly with
`role=SOURCE_TREE` and `search_by_default=False` so the adapter
does not index multi-gigabyte git working trees as chat history.

### Codex

`observed_version`: ``github.com/openai/codex@4c89772`` (2026-05-16).
Codex honours `CODEX_HOME` for primary files. SQLite files resolve
through `CODEX_SQLITE_HOME`, then `sqlite_home` in `config.toml`, then
`CODEX_HOME`.

Schemas are pinned directly to the upstream Rust types:

- {attr}`~agentgrep.stores.StoreFormat.JSONL` `history.jsonl` →
  `HistoryEntry { session_id: String, ts: u64, text: String }`
  ([`codex-rs/message-history/src/lib.rs:54-58`](https://github.com/openai/codex/blob/4c89772/codex-rs/message-history/src/lib.rs#L54)).
- Per-thread `sessions/YYYY/MM/DD/rollout-…jsonl` → tagged enum
  `RolloutItem` with variants `SessionMeta`, `ResponseItem`,
  `Compacted`, `TurnContext`, `EventMsg`
  ([`codex-rs/protocol/src/protocol.rs:2783`](https://github.com/openai/codex/blob/4c89772/codex-rs/protocol/src/protocol.rs#L2783)).
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

`observed_version`: ``gemini-cli v0.42.0`` stable (2026-05-12); types
from `v0.44.0-nightly` HEAD `77e65c0d`. Three adapters cover the
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
  [`chatRecordingService.ts:941`](https://github.com/google-gemini/gemini-cli/blob/77e65c0d/packages/core/src/services/chatRecordingService.ts#L941);
  the legacy file holds session metadata at the top level and the
  full conversation under a `messages` array.
- `gemini.tmp_logs_json.v1` parses
  `tmp/<project_hash>/logs.json` — a flat JSON array of
  `LogEntry` records (user-prompt audit log).

Gemini's
[`sessionCleanup.ts`](https://github.com/google-gemini/gemini-cli/blob/77e65c0d/packages/cli/src/utils/sessionCleanup.ts)
hard-deletes expired sessions via `fs.unlink()` — there is no
`history/` archive. The Antigravity files some installs carry under
`~/.gemini/antigravity/conversations/` are written by the
[Antigravity IDE](https://github.com/google-gemini/gemini-cli/blob/77e65c0d/packages/core/src/ide/detect-ide.ts),
a separate Google product — Gemini CLI only detects Antigravity as
an IDE launcher and does not read or write the protobuf
conversation files. Both stores are out of scope for the Gemini
adapters.

The `project_hash` is `sha256(absolute_project_root)`. agentgrep
exposes a Python mirror via
{func}`~agentgrep.store_catalog.gemini_project_hash` so the CLI can
answer "which Gemini sessions belong to *this* repo?".

### Grok CLI

`observed_version`: ``grok-cli v0.1.219`` (observed 2026-05-25).

Grok stores data under `${GROK_HOME or ${HOME}/.grok}/sessions/`
using URL-encoded absolute project paths as directory keys
(e.g. `%2Fhome%2Fd%2Fwork%2Fpython%2Fproj`). Three adapters cover
the three searchable store shapes:

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

Documentary-only entries cover events, summaries, memory, logs,
worktrees, and config — all catalogued with `search_by_default=False`
or deferred.

## Adding or updating a store

1. Edit `src/agentgrep/store_catalog.py`. Stamp `observed_version`
   and `observed_at` against the version you actually inspected.
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
