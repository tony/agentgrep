(storage-catalog)=

# Storage catalogue

agentgrep keeps an explicit catalogue of every on-disk store it knows
about, modelled as Pydantic
{class}`~agentgrep.stores.StoreDescriptor` rows aggregated under one
{class}`~agentgrep.stores.StoreCatalog`. The catalogue is **descriptive**:
it documents *where* each agent's data lives and *what* the records look
like. Search-policy decisions — whether agentgrep actually opens a
particular store by default — are captured per-row and may be deferred
(`search_by_default=None`) when no adapter consumes them yet.

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
root (Codex respects `CODEX_HOME`; Gemini respects `GEMINI_CLI_HOME`).

## Stores by agent

### Claude Code

`observed_version`: ``claude-code v2.1.143`` (2026-05-15).

Claude's primary chat record lives at
`${HOME}/.claude/projects/<encoded_project>/<session_uuid>.jsonl`. The
file format is JSONL with multiple record types per line —
`type: "user"`, `type: "assistant"`, `type: "attachment"`,
`type: "permission-mode"`. Sub-agent dispatches nest under
`<session_uuid>/subagents/`. The auto-memory feature stores markdown
notes under `<encoded_project>/memory/`.

### Cursor

Two distinct surfaces, both catalogued and both searched:

- **Cursor CLI agent** (`cursor-agent`): transcripts live at
  `${HOME}/.cursor/projects/<id>/agent-transcripts/<session_uuid>/<session_uuid>.jsonl`
  and are parsed by `cursor.cli_jsonl.v1`. Records are
  Anthropic-style `{role, message.content[]}` with `text` and
  `tool_use` content blocks; tool outputs are sometimes `[REDACTED]`
  in older `cursor-agent` builds. There is no native per-turn
  timestamp, so agentgrep backfills the file's mtime.
- **Cursor IDE**: parsed by `cursor.state_vscdb_modern.v1` /
  `cursor.state_vscdb_legacy.v1` via `state.vscdb` (SQLite). The
  catalogue keeps the IDE path separate from the CLI agent so the
  two never collide.

`cursor.cli.worktrees` is catalogued explicitly with
`role=SOURCE_TREE` and `search_by_default=False` so the adapter
does not index multi-gigabyte git working trees as chat history.

### Codex

`observed_version`: ``github.com/openai/codex@4c89772`` (2026-05-16).

Schemas are pinned directly to the upstream Rust types:

- {attr}`~agentgrep.stores.StoreFormat.JSONL` `history.jsonl` →
  `HistoryEntry { session_id: String, ts: u64, text: String }`
  ([`codex-rs/message-history/src/lib.rs:54-58`](https://github.com/openai/codex/blob/4c89772/codex-rs/message-history/src/lib.rs#L54)).
- Per-thread `sessions/YYYY/MM/DD/rollout-…jsonl` → tagged enum
  `RolloutItem` with variants `SessionMeta`, `ResponseItem`,
  `Compacted`, `TurnContext`, `EventMsg`
  ([`codex-rs/protocol/src/protocol.rs:2783`](https://github.com/openai/codex/blob/4c89772/codex-rs/protocol/src/protocol.rs#L2783)).

The two `_N.sqlite` files at the Codex root — `state_5.sqlite` and
`logs_2.sqlite` — belong to the Codex CLI. Their filenames come from
`STATE_DB_FILENAME` and `LOGS_DB_FILENAME` in
[`codex-rs/state/src/lib.rs`](https://github.com/openai/codex/blob/4c89772/codex-rs/state/src/lib.rs#L70-L71).

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
