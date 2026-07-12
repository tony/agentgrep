(backends)=

# Backends

agentgrep reads on-disk stores from multiple AI coding assistants.
Each backend page documents the agent's path layout, environment
overrides, store descriptors, and record schemas.

## Backend pages

::::{grid} 1 1 2 3
:gutter: 2 2 3 3

:::{grid-item-card} Codex
:link: codex
:link-type: doc
OpenAI Codex CLI history, sessions, instructions, memory, goals, and SQLite state.
:::

:::{grid-item-card} Claude Code
:link: claude
:link-type: doc
Claude Code history, project transcripts, tasks, memory, settings, and plugin surfaces.
:::

:::{grid-item-card} Cursor CLI
:link: cursor-cli
:link-type: doc
`cursor-agent` transcripts, prompt history, chat blobs, and AI-tracking summaries.
:::

:::{grid-item-card} Cursor IDE
:link: cursor-ide
:link-type: doc
Cursor desktop app `state.vscdb` SQLite — global and per-workspace chat history.
:::

:::{grid-item-card} Gemini CLI
:link: gemini
:link-type: doc
Gemini CLI chat sessions, prompt logs, checkpoints, settings, and skills.
:::

:::{grid-item-card} Antigravity
:link: antigravity
:link-type: doc
Google Antigravity overview, split into CLI prompt recall and IDE-local stores.
:::

:::{grid-item-card} Antigravity CLI
:link: antigravity-cli
:link-type: doc
Antigravity CLI prompt history, protobuf conversation databases, and local cache state.
:::

:::{grid-item-card} Antigravity IDE
:link: antigravity-ide
:link-type: doc
Antigravity IDE protobuf transcripts, Markdown brain artifacts, skills, and settings.
:::

:::{grid-item-card} Grok CLI
:link: grok
:link-type: doc
Grok CLI prompt history, session transcripts, memory, logs, and config.
:::

:::{grid-item-card} Pi
:link: pi
:link-type: doc
Pi (earendil-works) session transcripts, settings, prompts, and managed extensions.
:::

:::{grid-item-card} OpenCode
:link: opencode
:link-type: doc
OpenCode (anomalyco) SQLite session store, config, snapshots, and caches.
:::

:::{grid-item-card} VS Code
:link: vscode
:link-type: doc
VS Code GitHub Copilot Chat JSON transcripts and inline-edit history, including WSL cross-host stores.
:::

::::

## Unsupported backends

Some agents store their conversations in an obfuscated or encrypted form
agentgrep cannot read. Their storage is catalogued for inventory, but
they are excluded from search — see {doc}`unsupported/index`
(currently {doc}`Windsurf <unsupported/windsurf>`).

## Coverage levels

The backend pages distinguish search support from storage coverage.
Default-search stores are opened by normal search and find commands.
Inspectable stores are known and can be inventoried explicitly, but
are not searched by default. Catalog-only stores are documented so
future adapters do not mistake them for prompt history; some catalog
stores expose safe structural samples for `inspect_record_sample`, but
they still stay outside default search. Private stores are documented
but intentionally not enumerated from disk.

Search scope is record-level. `--scope prompts` is the default and
includes dedicated prompt-history logs plus user turns projected from
transcript-only backends. Full conversation, assistant, tool, and event
records require `--scope conversations` or `--scope all`.

(backend-project-context)=

## Project context availability

Project context is best-effort and store-dependent. When a backend
records working directories, repository roots, branches, workspace
hashes, or sibling workspace metadata, agentgrep attaches that data as
{class}`~agentgrep.RecordOrigin` on search results. Those origins power
{ref}`current-project search <cli-search-project-context>` and the
origin fields in {ref}`library-query-language-origin-fields`.

Backends without project context still remain searchable; they simply do
not match hard origin filters. Some SQLite-backed workspace stores, such
as {doc}`cursor-ide` and {doc}`vscode`, expose enough source-level
origin facts for agentgrep to skip mismatched workspace databases before
parsing. Global stores that do not know their project stay
conservative.

Each backend page carries a `Project context` section naming, per store,
which of `model`, `cwd`, and `branch` a record can carry and where the
value comes from — a SQLite column, a path segment, a sibling file, or a
nested key.

(backend-cwd-tiers)=

### How agentgrep learns a working directory

Agents do not agree on how to write down where a session ran, so a `cwd`
reaches a record through one of three tiers. The tier decides what you
can filter on, and it is a property of the store, not of your query.

**Lossless.** The store wrote the path, or an encoding that inverts
exactly: a `cwd` column ({storage:storeref}`codex.state_db`,
{storage:storeref}`pi.context_mode_db`), a nested key
({storage:storeref}`cursor-ide.state_vscdb`), a sibling file
({storage:storeref}`gemini.tmp.chats`), or a `%2F`-escaped directory name
({storage:storeref}`grok.sessions`). Records carry `origin.cwd` and answer
`--cwd` and `cwd:` with the real path.

**Lossy.** The store folded the path into a name that cannot be inverted
on its own. Cursor CLI's `projects/<name>/` segment replaced every
separator with `-` and escaped nothing, so `foo-bar` is equally
consistent with `/foo/bar` and `/foo-bar`. agentgrep reconstructs the
name against the filesystem and keeps the answer only when exactly one
reconstruction resolves to a directory that exists. Ambiguity, a
directory that has since moved, and a pathological name that exhausts the
probe budget all leave `origin.cwd` **unset**: a fabricated path does not
merely omit a result, it makes a repo-scoped filter silently skip your
own project, so a known-unknown is the safer answer.

**Digest.** The store only ever knew a hash of the path
({storage:storeref}`cursor-cli.chats`). Records carry `origin.cwd_hash`
and nothing else, so they answer `cwd_hash:` and not `cwd:`. A digest
does not invert, so agentgrep never reverses one into a `cwd` — and it
never runs the hash the other way either: a `cwd_hash` is always read
from the name the store chose, never computed from a `cwd` recovered
somewhere else. A path segment is admitted as a `cwd_hash` only when it
has a digest's shape, so a `backup.db` sitting beside a real database
does not publish its own file name as a searchable project identity.

The tiers stack. A store that hashes its directory name *and* repeats the
literal path inside — {storage:storeref}`pi.context_mode_db`,
{storage:storeref}`gemini.tmp.chats` — gives a record both `cwd` and
`cwd_hash`.

Only `cwd_hash` is a fact about where a source *lives*, so it is the only
origin field agentgrep trusts to skip a store before opening it. A `cwd`
learned from a sibling `workspace.json` or a project directory name
describes the source, not a promise about every record inside it — a
Cursor composer bubble can name its own worktree — so those stores are
still opened and filtered record by record.

## Version detection

Source discovery reports version metadata separately from record
content. agentgrep prefers concrete source evidence over app freshness:
embedded metadata, file/record shape, and SQLite suffixes identify the
data version; local version files provide app-version context only
when they can be read without spawning an upstream CLI. If neither is
available, the catalog observation stamp is reported as a
low-confidence fallback.

## Support matrix

```{storage:coverage-grid}
```

```{toctree}
:hidden:

codex
claude
cursor-cli
cursor-ide
gemini
antigravity
antigravity-cli
antigravity-ide
grok
pi
opencode
vscode
unsupported/index
```
