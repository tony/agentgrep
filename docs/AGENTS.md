# Documentation voice

This file covers the *voice* of prose under `docs/` — how to frame a
page so a reader meets the idea before its flags. It complements the
repository-root `AGENTS.md`, which already governs code blocks,
shell-command formatting, changelog conventions, and MyST roles. When
the two overlap, the root file wins; this one only answers the
question it leaves open: how should the prose sound?

## Who you are writing for

The default reader runs the `agentgrep` CLI — `search`, `grep`,
`find`, `ui` — against their own agent history. They live at a shell
and already reach for `rg` or `ag` without thinking, and they know
their agents (Codex, Claude Code, Cursor, Gemini, …) as tools they
use daily, but you cannot assume they read Python or know agentgrep's
internals: the execution engine, the query planner, per-backend store
layouts, or how the prompts/conversations scopes are carved.

A second, smaller reader integrates rather than types: they wire the
MCP server (`agentgrep-mcp`) into a client, script the `--json` /
`--ndjson` streams, or call the Python library (`SearchQuery`,
`run_search_query`, `SearchRecord`). Serve them too, but mark their
material opt-in ("for scripts and non-MCP agents", "advanced") so the
default reader knows they can stop. Never make the common case pay a
comprehension tax for the advanced one.

## Voice

- **Second person, present tense, active.** "You raise the score
  bar", not "The threshold is applied". Address the reader who is
  doing the thing.
- **Concept before flags.** Open by saying what the command *is* and
  what question it answers ("`search` is the smart default for 'what
  did I say about X?'"). The flag grammar — `--threshold`, `--scope`,
  `--no-dedupe` — is the last detail they need, not the first. A page
  that opens with a flag table has buried the idea under its
  mechanics.
- **Say when they can stop.** Lead with the default and the
  reassurance: prompts are searched by default, conversations are
  opt-in, ranking works out of the box. Let a skimmer leave after one
  paragraph.
- **Progressive disclosure.** Order by how many readers need it: the
  bare `agentgrep search "deploy"` → the one flag a few will tune →
  the machine-readable stream → the query-language grammar. Each step
  is for a smaller audience than the last.
- **Lean on the pipeline.** The reader thinks agent → store → source
  → record: agentgrep discovers each agent's on-disk store, parses
  its sources (JSONL logs, SQLite databases) into normalized records,
  and searches those. Reinforce that chain when you explain scope,
  discovery, or why results look the way they do.
- **Name the trade-off.** If a behavior costs something — session
  dedup diverging from raw `rg`, a high `--threshold` filtering
  everything out, eager `--json` buffering where `--ndjson` streams —
  say so, and say what it buys. State it; don't sell it.
- **Frame by concept, not by mechanism.** Don't headline a feature by
  its flag or record field in prose; that names the implementation
  surface, which is the reader's last concern. Name the concept
  ("session deduplication", not `--no-dedupe`). The mechanics
  vocabulary belongs in the generated `argparse` reference block and
  the exit-code lists, and only there.

## Examples that run

Console examples under `docs/` are executed, not decorative: the
documentation suite collects every ```` ```console ```` fence (plus
`README.md` and `fastmcp.json`) and runs it as a literal shell script.
`testpaths` includes `docs`; executable examples carry the `documentation`
and `slow` markers, so `just test-docs` runs every one while the default loop
keeps them opt-in.

- Examples run in a temp-home sandbox seeded with sample stores from
  `tests/samples` (Codex history and session JSONL, Claude Code
  history, Cursor CLI prompt history and transcripts) — write
  commands against those stores, not your own live history.
- Python blocks on `README.md` and `docs/library/*.md` run as one
  page-level script per page, so a later block can use a variable an
  earlier block defined. That makes their **order load-bearing**:
  never reorder, add, or drop a code block when you reshape the prose
  around it.

## What stays precise

Warm the framing, never the facts. Exit-code lists, output-shape
descriptions, exact error strings, JSON event examples, and class or
tool cross-references carry meaning in their exact form — leave them
alone. The friendly voice belongs in the sentences *around* a precise
block, introducing it, not inside it paraphrasing it into vagueness.

## Cross-references

Point the advanced reader at the deep-dive rather than inlining it,
and put the link where their interest peaks — on the phrase that made
them curious ("the full grammar", "consume results incrementally") —
not as a standalone footnote the eye skips. Use the MyST roles listed
in the root `AGENTS.md`, including the MCP tool roles (`{tool}`,
`{tooliconl}`, `{toolref}`). A `{ref}` must match its target's anchor
exactly — anchors are hyphenated and page-prefixed (`cli-search`,
`backend-codex`, `library-query-language`). `just build-docs` catches
a broken cross-reference; the console examples do not — so build the
docs before you commit.

Link the first prose mention of any symbol that has a useful
destination on that page. This includes Python objects, agentgrep
APIs, MCP tools, CLI command pages, backend pages, and external tools
or projects. Use the most specific target available: `{class}`,
`{meth}`, `{func}`, `{mod}`, `{exc}`, or `{attr}` for API objects;
`{tool}` / `{tooliconl}` / `{toolref}` for MCP tools; `{ref}` or
`{doc}` for documentation pages and section anchors; and a Markdown
link or reference link for external projects. After the first linked
mention on a page, later mentions can stay plain unless the distance
or context makes another link useful.

Do not rely on a later reference section to satisfy the first-mention
rule. If the first occurrence would be a heading, grid-card teaser, or
introductory sentence, link that occurrence or retitle the heading so
the first prose mention can carry the link. Leave command examples,
code blocks, and literal configuration values as code; link the
surrounding prose instead.

## A page that does this

`docs/cli/search.md` is the worked example: a concept-first intro
that says what `search` answers and how it differs from `grep` before
any flag, examples ordered by shrinking audience, honest trade-offs
(a high threshold can filter everything out; `--no-rank` returns
discovery order), features named by concept with `{ref}`
cross-references, and the generated `argparse` block and exit-code
list left exact. Read it before reshaping another page.

## Before you commit

- Does the page open with what the command *is*, or with how to flag
  it?
- Can a reader who needs only the default stop after the first
  paragraph?
- Is anything framed by its flag or record field that should be named
  by concept instead?
- Are the MCP, library, and scripting parts clearly marked opt-in?
- Do the console examples pass under `just test-docs`, and did you leave
  every code block, table, error string, and cross-reference exact?
- Did `just build-docs` stay clean — no new warning, no broken
  cross-reference?
