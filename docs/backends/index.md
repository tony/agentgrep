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
future adapters do not mistake them for prompt history. Private stores
are documented but intentionally not enumerated from disk.

## Support matrix

| Agent | Default search | Opt-in / inspectable prompts | Memory | Plans / todos / goals | Instructions / plugins / skills | Indexes / summaries | App state / config | Runtime / cache |
|-------|----------------|------------------------------|--------|-----------------------|----------------------------------|---------------------|--------------------|-----------------|
| Codex | {doc}`codex` history and sessions | {doc}`codex` state DB previews, instructions, session index | {doc}`codex` memory workspace and DB | {doc}`codex` goals DB | {doc}`codex` instructions, plugins, skills, rules | {doc}`codex` `session_index.jsonl` | {doc}`codex` config, import ledger, model/internal state | {doc}`codex` logs, process manager, cache, shell snapshots |
| Claude | {doc}`claude` history, project sessions, subagents | {doc}`claude` `__store.db`, session memory | {doc}`claude` project/session memory | {doc}`claude` tasks and plans | {doc}`claude` skills, teams, plugin cache | | {doc}`claude` settings, sessions, context/security, IDE state | {doc}`claude` paste/image cache, file history, shell snapshots |
| Cursor | {doc}`cursor` CLI transcripts, IDE state, AI tracking | | | {doc}`cursor` plans | | {doc}`cursor` AI tracking summaries | {doc}`cursor` CLI/IDE state | {doc}`cursor` worktrees and terminal/cache state |
| Gemini | {doc}`gemini` prompt logs and chat sessions | {doc}`gemini` checkpoints | | | | | {doc}`gemini` config / auth state | |
| Grok | {doc}`grok` prompt history, sessions, FTS index | | {doc}`grok` memory | | | {doc}`grok` session search index and summaries | {doc}`grok` config/state | {doc}`grok` logs, events, worktrees |

```{toctree}
:hidden:

codex
claude
cursor
gemini
grok
```
