(backends)=

# Backends

agentgrep reads on-disk stores from multiple AI coding assistants.
Each backend page documents the agent's path layout, environment
overrides, store descriptors, and record schemas.

## Coverage levels

The backend pages distinguish search support from storage coverage.
Default-search stores are opened by normal search and find commands.
Inspectable stores are known and can be inventoried explicitly, but
are not searched by default. Catalog-only stores are documented so
future adapters do not mistake them for prompt history; some catalog
stores expose safe structural samples for `inspect_record_sample`, but
they still stay outside default search. Private stores are documented
but intentionally not enumerated from disk.

## Version detection

Source discovery reports version metadata separately from record
content. agentgrep prefers concrete source evidence over app freshness:
embedded metadata, file/record shape, and SQLite suffixes identify the
data version; local version files provide app-version context only
when they can be read without spawning an upstream CLI. If neither is
available, the catalog observation stamp is reported as a
low-confidence fallback.

## Support matrix

| Agent | Default search | Opt-in parsers | Safe catalog samples | Memory | Plans / todos / goals | Instructions / plugins / skills | Indexes / summaries | App state / config | Runtime / cache / private |
|-------|----------------|----------------|----------------------|--------|-----------------------|----------------------------------|---------------------|--------------------|---------------------------|
| Codex | {doc}`codex` history and modern/legacy sessions | {doc}`codex` state DB previews, instructions, memory workspace, memory DB, goals DB, session index | {doc}`codex` logs DB feedback bodies, external import ledger | {doc}`codex` memory workspace and DB | {doc}`codex` goals DB | {doc}`codex` instructions, plugins, skills, rules | {doc}`codex` `session_index.jsonl` | {doc}`codex` config, config backups, model/internal/update/version state | {doc}`codex` logs, process manager, cache, SQLite sidecars, shell snapshots, auth/policy/installation id |
| Claude | {doc}`claude` history, project sessions, subagents | {doc}`claude` `__store.db`, session memory, tasks, plans | {doc}`claude` settings/keybindings key summaries | {doc}`claude` project/session memory | {doc}`claude` tasks and plans | {doc}`claude` skills, teams, plugin cache | | {doc}`claude` settings, sessions, update state, context/security, IDE state | {doc}`claude` paste/image cache, file history, shell snapshots, debug, backups, credentials/session env |
| Cursor | {doc}`cursor` CLI transcripts, IDE state, AI tracking | | | | {doc}`cursor` plans | | {doc}`cursor` AI tracking summaries | {doc}`cursor` CLI/IDE state | {doc}`cursor` worktrees and terminal/cache state |
| Gemini | {doc}`gemini` prompt logs and chat sessions | {doc}`gemini` checkpoints | | | | | | {doc}`gemini` config / auth state | |
| Grok | {doc}`grok` prompt history, sessions, FTS index | | | {doc}`grok` memory | | | {doc}`grok` session search index and summaries | {doc}`grok` config/state | {doc}`grok` logs, events, worktrees |

```{toctree}
:hidden:

codex
claude
cursor
gemini
grok
```
