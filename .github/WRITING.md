# Writing

How this project writes prose, for humans and agents alike. It governs
`README.md`, `CHANGES`, release notes, commit messages, CLI help and error
text, MCP tool descriptions, docstrings, and source comments — every surface a
reader reaches.

For environment setup, the gates, and pull request workflow, see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Voice

Three surfaces, one voice. A docstring says what a caller may rely on; a
`CHANGES` entry says what changed; prose says what happens. All three are
present tense, lead with the thing being described, and stop. Why it was built
that way belongs in the commit message, which is timestamped and attached to
the diff.

The most useful editing operation is deleting the introductory sentence.

Lead with verbs and name concrete things. Put identifiers in backticks. Prefer
short declarative sentences, one operational fact each. Do not explain Python
to Python developers; do explain this project's semantics.

Type annotations describe shape. Documentation describes meaning. A sentence
that restates a signature has said nothing.

Use MUST, SHOULD, and MAY only where the normative sense is meant. Say what
actually happens rather than that something is "supported".

| Instead of                       | Prefer                            |
| --------------------------------- | ---------------------------------- |
| "We added…"                      | "`agentgrep search` now accepts…" |
| "New and improved"               | "`agentgrep grep` now…"           |
| "powerful", "seamless"           | state the capability              |
| "easily", "simply", "just"       | omit                              |
| "simple", "obvious", "intuitive" | omit                              |
| "robust"                         | name the failure that is handled  |
| "comprehensive"                  | name what is covered              |
| "production-ready"               | state the guarantee               |
| "optimized", "blazingly fast"    | give the magnitude                |
| "various fixes"                  | name the components               |
| "under the hood"                 | omit unless observable            |
| "please note that", "note that"  | state the fact                    |
| "leverage", "utilize"            | "use"                             |
| "delve into"                     | "read", or omit                   |
| "best practices"                 | name the practice                 |
| "in order to"                    | "to"                              |

## Who you are writing for

The default reader runs the `agentgrep` CLI — `search`, `grep`, `find`, `ui` —
against their own agent history. They live at a shell and already reach for
`rg` or `ag` without thinking, and they know their agents (Codex, Claude Code,
Cursor, Gemini, …) as tools they use daily, but you cannot assume they read
Python or know agentgrep's internals: the execution engine, the query
planner, per-backend store layouts, or how the prompts/conversations scopes
are carved.

A second, smaller reader integrates rather than types: they wire the MCP
server (`agentgrep-mcp`) into a client, script the `--json` / `--ndjson`
streams, or call the Python library (`SearchQuery`, `run_search_query`,
`SearchRecord`). Serve them too, but mark their material opt-in ("for scripts
and non-MCP agents", "advanced") so the default reader knows they can stop.
Never make the common case pay a comprehension tax for the advanced one.

Rules that follow:

- **Second person, present tense, active.** "You raise the score bar", not
  "The threshold is applied". Address the reader who is doing the thing.
- **Concept before flags.** Open by saying what the command *is* and what
  question it answers ("`search` is the smart default for 'what did I say
  about X?'"). The flag grammar — `--threshold`, `--scope`, `--no-dedupe` — is
  the last detail they need, not the first. A page that opens with a flag
  table has buried the idea under its mechanics.
- **Say when they can stop.** Lead with the default and the reassurance:
  prompts are searched by default, conversations are opt-in, ranking works out
  of the box. Let a skimmer leave after one paragraph.
- **Progressive disclosure.** Order by how many readers need it: the bare
  `agentgrep search "deploy"` → the one flag a few will tune → the
  machine-readable stream → the query-language grammar. Each step is for a
  smaller audience than the last.
- **Lean on the pipeline.** The reader thinks agent → store → source →
  record: agentgrep discovers each agent's on-disk store, parses its sources
  (JSONL logs, SQLite databases) into normalized records, and searches those.
  Reinforce that chain when you explain scope, discovery, or why results look
  the way they do.
- **Name the trade-off.** If a behavior costs something — session dedup
  diverging from raw `rg`, a high `--threshold` filtering everything out,
  eager `--json` buffering where `--ndjson` streams — say so, and say what it
  buys. State it; don't sell it.
- **Frame by concept, not by mechanism.** Don't headline a feature by its flag
  or record field in prose; that names the implementation surface, which is
  the reader's last concern. Name the concept ("session deduplication", not
  `--no-dedupe`). The mechanics vocabulary belongs in the generated `argparse`
  reference block and the exit-code lists, and only there.

`docs/cli/search.md` is the worked example: a concept-first intro that says
what `search` answers and how it differs from `grep` before any flag, examples
ordered by shrinking audience, honest trade-offs, features named by concept
with `{ref}` cross-references, and the generated `argparse` block and exit-code
list left exact.

## README

A README is the shortest path from "what is this?" to competent use, not the
project's autobiography.

The first sentence is a contract. It says what abstraction the reader has been
handed, concretely enough to tell this package apart from the neighbouring
one.

Get to a runnable command before anything the reader can skip. A logo, a
mission statement, a comparison matrix and three paragraphs of history in
front of the install line all cost the same thing.

State the minimum Python version and platform constraints in prose, not only
in badges. `requires-python` in `pyproject.toml` is the authority; the README
must agree with it.

Name the distribution, the import, and the executables separately wherever
they differ. `agentgrep` names the PyPI distribution and the `import
agentgrep` package identically, but ships two executables —
`agentgrep` (CLI) and `agentgrep-mcp` (MCP server) — naming both prevents a
reader from assuming there is only one entry point.

Examples are executable, not illustrative fiction. Never
`your-command <some-options>`. See
[Documented examples that run](#documented-examples-that-run) for which
blocks are executed and how to write one that qualifies.

Document the semantic model, not the flag list. `--help` already enumerates
flags; what it cannot say is precedence, filesystem effects, what goes to
stdout versus stderr, and what a non-zero exit means.

State defaults explicitly — defaults are API. State negative guarantees where
they exist: "read-only", "no network access outside an MCP client's own
transport", "never writes outside the destination". They establish boundaries
faster than any amount of description.

Headings stay conventional and stable, because people deep-link them. Badges
are few and load-bearing.

## Documented examples that run

Examples in this repository are tests, but through two different mechanisms.
Get both wrong and an edit can look like prose while it silently deletes or
disables a test.

### The doctest layer

`--doctest-modules` is in `addopts`, and `doctest_optionflags` sets
`ELLIPSIS` and `NORMALIZE_WHITESPACE`. There is no `--doctest-glob`, so this
layer collects `>>> ` prompts from Python docstrings only — under
`testpaths` (`src/agentgrep`, `src/pytest_documentation`, `docs`,
`fastmcp.json`, `tests`), any `.py` module's docstring, never a `.md` or
`.rst` file. A fence tag alone (` ```python ` with no prompts) collects
nothing. This is the single most expensive mistake available when editing a
docstring: removing the prompts leaves a green test suite and a silently
deleted test. When editing a docstring that contains `>>> ` examples, count
the prompts before and after. A `>>> ` prompt written into a Markdown page
does nothing under this layer — see the executable-documentation layer
below for what actually runs a Markdown example.

No `doctest_namespace` fixture is registered anywhere in this repository, so a
pytest-collected doctest gets nothing for free — import every name it uses.

### The executable-documentation layer

This repository also ships `src/pytest_documentation`, a repo-local pytest
plugin loaded explicitly via `-p pytest_documentation.plugin` in `addopts`
(it ships no `pytest11` entry point, so it never auto-loads in a consumer's
own pytest run). Root `conftest.py` wires four more shapes of executable
example, all collected only from files pytest's own walk already visits under
`testpaths` — a `.md` page under `docs/`, `fastmcp.json`, or `docs/justfile`:

- **` ```console ` fences under `docs/`** run as literal shell transcripts in
  a temp-`$HOME` sandbox seeded with sample Codex/Claude/Cursor/Cursor-CLI
  stores from `tests/samples`. Lines starting `$ ` or `> ` (a `\`
  continuation) are the command; any other non-blank line is expected output,
  matched as a whitespace-normalized substring of the command's combined
  stdout/stderr. A line containing `[...]` is skipped from that check — this
  plugin's own ellipsis marker, written literally as `[...]` in the fence, and
  distinct from doctest's `ELLIPSIS` flag. See the `usage: agentgrep grep
  [...]` lines in `docs/cli/grep.md` for a working example. If no expected
  line survives that filter, the check falls back to exit code 0.
- **Unprompted ` ```python ` fences on `README.md` and `docs/library/*.md`**
  are concatenated per page into one script and executed for a zero exit
  code — no output assertion, and no `>>> ` prompt required; a plain fenced
  Python block on one of those two page groups is itself the test. This makes
  block **order load-bearing** on those pages: a later block may use a name an
  earlier block defined. Never reorder, add, or drop a code block on
  `README.md` or a `docs/library/*.md` page without checking what a
  surrounding block depends on.
- **`fastmcp.json`** is validated structurally (valid JSON, a filesystem
  `source` with a real entrypoint file) rather than executed as a script.
- **The `doctest` recipe in `docs/justfile`** runs Sphinx's own
  `sphinx.ext.doctest` builder as one pytest item. It re-executes every
  `>>> ` docstring example that autodoc renders onto a reference page —
  currently `docs/library/reference.md` and `docs/tui/reference.md` — the
  same source `>>> ` examples the doctest layer above already collects, run
  a second time against the *rendered* page. `docs/conf.py` sets
  `doctest_global_setup` to pre-import `pathlib`, `format_timestamp_tig`,
  and `gemini_project_hash` for this pass only; `uv run pytest`'s
  `--doctest-modules` run of the same docstring gets none of that. Write
  every docstring example to pass under plain `uv run pytest` — the real
  gate — never only under `docs/justfile:doctest`.

Console and Python-page examples carry the `documentation` and `slow`
pytest markers, so the default local loop (`uv run pytest`, `just test`)
skips them; `just test-docs` (`pytest -m documentation`) and the exhaustive
lane (`just test-all`, and CI's `pytest -m ""`) run them.

**`README.md` is not itself in `testpaths`.** `conftest.py` lists it in the
suite's `include_paths`, and its console/Python-page examples collect and run
when `README.md` is passed to pytest explicitly (`pytest README.md`), but
`testpaths` never includes it, so today none of `uv run pytest`, `just test`,
`just test-docs`, `just test-all`, or CI's `pytest -m ""` reach it. Treat its
console and Python blocks as if they were tested anyway — every rule above
still applies — but do not assume a green gate proves a README example still
works.

**`# doctest: +SKIP` is not permitted** on a pytest-collected doctest. Use a
regular unit test instead of a skipped doctest — a skipped test is noise.

### Where a `>>> ` doctest belongs, and where it does not

Most of this repository's surface reads the user's home directory, parses a
Codex/Claude/Cursor store, or talks to an MCP client, so a blanket doctest
mandate does not fit its shape. Scope doctests to functions that actually run
offline:

- **Use a doctest** for a pure helper — a parser, formatter, serializer,
  redaction routine, small utility — and for a module-level example that
  illustrates a concept without touching the filesystem, a subprocess, or the
  network.
- **Use a unit test with fixtures instead** for anything that reads `$HOME`,
  opens a Codex/Claude/Cursor store, spawns `ripgrep`, opens a SQLite
  database, starts the Textual TUI, or implements an MCP tool (MCP tools need
  a FastMCP context; add a focused MCP contract and run `just test-mcp`).

When output varies, use `ELLIPSIS` inline:

```python
>>> record.path  # doctest: +ELLIPSIS
PosixPath('.../codex/...')
```

## The changelog

`CHANGES` is the changelog, rendered at `docs/history.md` via a plain
`{include}`. Modeled on Django's release-notes shape — deliverables get titles
and prose, not bullets.

A ledger, not a narrative. It is scanned, and the question a reader is asking
is whether an entry affects them.

**Release entry boilerplate.** Every release header is
`## agentgrep X.Y.Z (YYYY-MM-DD)`. The file opens with a
`## agentgrep X.Y.Z (Yet to be released)` placeholder block fenced by
`<!-- KEEP THIS PLACEHOLDER ... -->` and
`<!-- END PLACEHOLDER ... -->` HTML comments — new entries land immediately
below the END marker, never above it.

**Open with a multi-sentence lead paragraph.** Plain prose, no italic. Open
with the version as sentence subject ("agentgrep X.Y.Z ships …") so the lead
is self-contained when excerpted. Two to four sentences telling the reader
what shipped and who cares — user-visible takeaways, not internal mechanism.
Cross-reference detail docs with `{ref}` to keep the lead compact.

**Lead paragraphs are release-time material — off-limits to branches and
PRs.** The unreleased entry carries no lead paragraph and no version summary:
sections only (`### Breaking changes`, `### What's new` deliverables,
`### Fixes`, …). Speaking for the release — what the version "is", "ships", or
"focuses on" — is presumptuous before its scope is final; only the person
cutting the release writes that, and only when the user explicitly asks to
release. Never write or edit a lead from a feature branch, and never ask or
imply that a release should happen.

**Each deliverable is a section, not a bullet.** Inside `### What's new`,
every distinct deliverable gets a `#### Deliverable title (#NN)` heading
naming it in user vocabulary, followed by 1-3 prose paragraphs explaining what
shipped. Don't wrap a paragraph in `- ` — bullets are for enumerable lists, not
paragraph containers. Cross-link detail docs (`` See {ref}`foo` for
details. ``) so prose stays focused.

**The deliverable test.** Before writing an entry, ask: "What's the
deliverable, in user vocabulary?" If you can't answer in one sentence, the
entry isn't ready. Mechanism (helper internals, byte counters,
schema-validation locations) belongs in PR descriptions and code comments, not
the changelog.

**Fixed subheadings**, in this order when present: `### Breaking changes`,
`### Dependencies`, `### What's new`, `### Fixes`, `### Documentation`,
`### Development`. Dev tooling (helper scripts, internal automation) lives
under `### Development`. For breaking changes, show the migration path with
concrete inline code (a `# Before` / `# After` fenced block). Dependency
floor bumps use the form ``Minimum `pkg>=X.Y.Z` (was `>=X.Y.W`)``.

**PR refs `(#NN)`** sit in each deliverable's `####` heading.

**When bullets are appropriate.** Catch-all sections (`### Fixes`,
occasionally `### Documentation`) with 3+ genuinely small items use bullets —
one line each, never paragraphs. If a bullet swells past two lines, promote it
to a `#### Title (#NN)` heading with prose body.

**Anti-patterns.**

- Fragile metrics: token ceilings, third-party version pins, percent
  benchmarks, exact byte counts. Describe the *capability*, not the math.
- Internal jargon: private symbols (leading-underscore identifiers), algorithm
  names exposed for the first time, backend scaffolding.
- Walls of text dressed up as bullets.
- Buried breaking changes — they get their own subheading at the top of the
  entry.

**Always link autodoc'd APIs.** Any class, method, function, exception,
attribute, or MCP tool slug that has its own rendered page must be cited via
the appropriate role (`{class}`, `{meth}`, `{func}`, `{exc}`, `{attr}`,
`{tooliconl}`) — never plain backticks. Doc pages without explicit ref labels
use `{doc}`. Plain backticks are correct for code syntax, env vars, parameter
names, and file paths that aren't doc pages.

**Versions are PEP 440 identifiers.** Semantic-versioning meaning applies to
the *documented public API*, which includes command names, options, exit
statuses, configuration keys, environment variables, and serialized formats —
not only imported Python symbols. See
[CLI, MCP, and error-message conventions](#cli-mcp-and-error-message-conventions).

**Summarization style.** When asked "what changed in the latest version?",
lead with the entry's lead paragraph (paraphrased if needed), followed by
each `####` deliverable heading under `### What's new` with a one-sentence
summary. Cite `(#NN)` only if asked for source links. Don't invent versions,
dates, or numbers not present in `CHANGES`. Don't quote line numbers or file
offsets — those shift as the file evolves.

## CLI, MCP, and error-message conventions

`agentgrep` and `agentgrep-mcp` are the two console scripts this package
ships. ADR 0006 treats the public CLI/MCP surface as compatibility-sensitive:
command names, flags, exit statuses, JSON/NDJSON keys, and MCP tool schemas
change with the same discipline as a Python API, and a break gets its own
`CHANGES` subheading.

**Exit statuses are part of the documented surface, and they differ per
command.** State the real ones rather than a blanket convention:

- `agentgrep search` returns `0` when at least one ranked result survived,
  `1` for no matches (including every match filtered out by `--threshold`).
  It has no separate runtime-error exit code; malformed flags are still
  rejected by argparse before the search starts.
- `agentgrep grep` follows `rg`'s convention: `0` (a match), `1` (no
  matches), `2` (an error during search — invalid regex, an unreadable
  store).

Document a command's exit statuses next to its `argparse` reference block, not
folded into prose above it — see `docs/cli/search.md` and `docs/cli/grep.md`
for the pattern.

**stdout carries results; stderr carries everything else.** Progress spinners,
interrupt notices, and diagnostics write to stderr so
`agentgrep search deploy | jq` never sees them in the piped buffer. `--json`
and `--ndjson` write the payload to stdout only.

**A parse-time error uses argparse's own shape and exits `2`**:
`agentgrep grep '['` fails with
`agentgrep grep: error: invalid regex '[': unterminated character set at
position 0`, caught before the engine starts so a malformed pattern never
emits partial output or an unhandled traceback. **A runtime usage error** —
`search` called with no term and no `--ui` — is a single-line message on
stderr with no traceback, distinct from the argparse shape above.

**MCP diagnostics and next-action hints never carry prompt text, secret
values, raw argv, or local absolute paths.** The same privacy boundary that
governs profiler and benchmark artifacts (see
[CONTRIBUTING.md](CONTRIBUTING.md)) governs anything an MCP tool returns.

## Docstrings

The prime directive: never restate the type. The annotation is the source of
truth; the docstring carries what the annotation cannot.

This is documentation debt wearing a docstring:

```python
def get_id(pane: Pane) -> str:
    """Get the pane's identifier.

    Parameters
    ----------
    pane : Pane
        The pane.

    Returns
    -------
    str
        The identifier.
    """
```

Document instead the dimensions the type system cannot encode: mutation
(what it changes in place), ownership (what the caller must close or keep
alive), ordering, timing (what has finished by the time an awaitable
resolves), failure (which exceptions, triggered by what), idempotence,
concurrency (coalesced, queued, independent; thread/process/fork safety),
units and ranges, boundary behaviour (zero, empty, maximum), platform
differences, and the security boundary (what is executed versus only read).

Follow NumPy style for every function and method:

```python
"""Short description of the function or class.

Detailed description using reStructuredText format.

Parameters
----------
param1 : type
    Description of param1
param2 : type
    Description of param2

Returns
-------
type
    Description of return value
"""
```

**Classes with fields** — `NamedTuple`, dataclasses — document every field in
an `Attributes` section:

```python
class HistoryEntry(t.NamedTuple):
    """One recalled search query: the raw text, its unix ts, and launch scope.

    Attributes
    ----------
    text : str
        Query text exactly as the user typed it.
    ts : float
        Unix timestamp of when the query ran.
    scope : str
        Scope the query launched under, or ``""`` when it had none.
    """
```

Autodoc renders every field whether or not you describe it, so an
undocumented `NamedTuple` field ships to the API docs as "Alias for field
number 0" and a dataclass field ships bare. Document all of them — a class
with three fields and two documented still ships a stub for the third.

The first sentence stands alone; tooling truncates there. PEP 257 applies:
triple double quotes, an imperative one-line summary ending in a period, a
blank line before any extended description. Do not repeat an introspectable
signature.

The ambiguity worth resolving by example: whether "retry three times" means
three attempts or four. State it.

## Source comments

A comment ships only if it passes all three gates. Fail any: delete or
rewrite. Borderline: delete — borderline means the information is
reconstructible, which is what makes deletion cheap.

**Loss.** Three years from now, would losing this cost a maintainer real time
rediscovering intent, an invariant, a constraint, or a failure mode the code
and tests do not already make obvious?

**Elite.** Would SQLite, Redis, the Go standard library, or CPython write this
comment, at this length? Those projects state the constraint and stop. They do
not argue with an imagined objector.

**Upkeep.** Will it stay true without maintenance? A comment that hand-syncs a
value the code owns — a count, an offset, a line reference, a duplicated
constant — is false the first time that value moves.

### Ceiling

One or two lines. A comment reaching four is either carrying several facts, in
which case split it, or arguing, in which case cut it to the fact.

Rationale, alternatives weighed, and the story of how the code got here belong
in the commit message: timestamped, attached to the exact diff, and free to
maintain.

A comment often holds both a constraint and the deliberation that found it.
Keep the constraint, cut the deliberation. "Runs at most once per second"
survives; "this is the right trade for now" does not.

### Keep

- Why over how: upstream quirks, protocol and compatibility constraints,
  performance tradeoffs still part of the contract.
- Invariants, preconditions, ordering, lifetime, and concurrency requirements
  that types and tests cannot express.
- Code that looks wrong but is not, so a later cleanup does not reintroduce
  the bug.
- A high-level sketch of an algorithm whose local operations do not reveal
  the whole.

### Delete

- Narration of the next lines; code translated into English.
- Restated names, types, defaults, or control flow.
- Values duplicated from the code and hand-synced.
- Justification, hedging, or apology for a choice.
- Speculation about future requirements.
- History version control already holds, including commented-out code.
- Ticket and issue numbers. They say nothing to a reader without tracker
  access, and they rot when the tracker moves. Unfinished work goes in the
  tracker, not the source.
- Transient observations — "currently", "for now", "the latest release" —
  that go stale with no nearby edit.

### The upkeep gate in practice

It reaches values that track our own code. It does not reach frozen external
facts.

Bad (Delete):

```python
# There are 321 tests to complete for servers.
```

Good (Keep):

```python
# CPython < 3.11 has no ExceptionGroup, so this branch stays.
```

### Documentation exception

Doctests, minimal usage examples, and `Parameters`/`Returns`/`Attributes`
entries on public API are exempt from the loss gate — they serve the caller,
not the maintainer. They are exempt from nothing else. Ceiling: a good man
page entry. Autodoc ships every field whether or not you describe it, and a
doctest that runs is also a test.

## Terminology and capitalization

Pick the domain noun and keep it. If the code calls something a store, don't
call it a backend in one paragraph and a data source in the next. If the
method is `run_search_query`, write "search" everywhere rather than
alternating with "look up", "query", and "find" for the same operation.

Stable vocabulary is what makes search, deep links, and an agent's retrieval
work at all.

Python and PyPI keep their own capitalisation. Distribution names are written
as they are published.

Do not write counts into prose — how many tests exist, how many source files
there are. They go stale silently and no reader needs them. Counts that pin a
fixture or guard an invariant are different, and belong in code.

## Markdown

Prose wraps at 80 columns. Table rows, badge lines, and long links are
exempt, because breaking them harms rendering. A pull request or issue body
does not wrap at all: GitHub renders a single newline as a space in a file and
as a line break in a comment, so a wrapped comment body arrives as ragged
stubs.

GitHub alert blocks — `> [!NOTE]`, `> [!WARNING]` — render as literal text
outside GitHub, so reserve them for at most one load-bearing warning per
document. Write the sentence so it carries the fact on its own, and a renderer
that drops the marker loses nothing.

Do not use a local absolute path or an email address in anything published.

**MyST roles for API and MCP tool references.** `{class}`, `{meth}`,
`{func}`, `{mod}`, `{exc}`, `{attr}` for Python API objects; `{ref}` or
`{doc}` for documentation pages and section anchors. For MCP tools
specifically (`docs/conf.py`'s `sphinx-autodoc-fastmcp` integration):

- `{tool}` — code chip + full safety badge. Use in headers, bulleted lists,
  and tables where the badge gives scannable context.
- `{tooliconl}` — code chip + small colored icon (left). Use in inline
  paragraph text where the full badge is too heavy.
- `{toolref}` — code chip only, no badge. Use for dense inline sequences or
  where the safety tier is already established.
- `{tooliconil}` / `{tooliconir}` — bare emoji inside a code chip. Use for
  compact lists and scan-heavy surfaces.

Link the first prose mention of any symbol that has a rendered destination on
that page — Python objects, agentgrep APIs, MCP tools, CLI command pages,
backend pages, and external tools. After the first linked mention on a page,
later mentions can stay plain unless distance or context makes another link
useful. Don't rely on a later reference section to satisfy the first-mention
rule; if the first occurrence would be a heading or teaser, link that
occurrence or retitle it so the first prose mention can carry the link.

A `{ref}` must match its target's anchor exactly — anchors in this
documentation are hyphenated and page-prefixed (`cli-search`,
`backend-codex`, `library-query-language`). `just build-docs` catches a
broken cross-reference; the console examples do not, so build the docs
before you commit a page that adds one.

**Warm the framing, never the facts.** Exit-code lists, output-shape
descriptions, exact error strings, JSON event examples, and cross-references
carry meaning in their exact form — leave them alone. The friendly voice
belongs in the sentences *around* a precise block, introducing it, not inside
it paraphrasing it into vagueness.

## Code blocks

Code blocks are paste-and-run units: pasting one block runs exactly one
intended action. Doctests and the executable-documentation examples above are
exempt — the test suite runs them, nobody pastes them.

- **One command per block.** Multiple steps may share a block only when
  explicitly chained with `&&`, `;`, or `\` continuations — the chain is then
  one logical command.
- **Explanations go in prose above the block**, never as `#` comments inside
  it.
- **Command menus are per-command blocks with prose lead-ins**, not tables.
- **Shell commands use the `console` tag with a `$ ` prefix.** This separates
  interactive commands from scripts and enables prompt-aware copy — and under
  `docs/`, it is what makes a block executable at all (see
  [Documented examples that run](#documented-examples-that-run)).
- **Split long commands with `\`** — one flag or flag+value pair per indented
  continuation line, positional arguments last.

Good — show the last ten commits as a graph:

```console
$ git log \
    --max-count=10 \
    --graph \
    --oneline
```

Bad:

```console
# Show the last ten commits as a graph
$ git log --max-count=10 --graph --oneline
```

## Commits

```
Scope(type[detail]): concise description

why: Explanation of necessity or impact.

what:
- Specific technical changes made
- Focused on a single topic
```

Keep the subject to 50 characters or fewer, excluding any trailing `(#NN)`
pull request reference, and wrap body lines at 72. Separate the `why:` and
`what:` blocks with a blank line.

Routine maintenance commits drop the colon and take a capitalised
description, which is what distinguishes them at a glance in
`git log --oneline`:

```
py(deps[dev]) Bump dev packages
ai(rules[AGENTS]) Judge comments by three gates
```

Everything that changes behaviour keeps the colon.

Common types:

- **feat**: New features or enhancements
- **fix**: Bug fixes
- **refactor**: Code restructuring without functional change
- **docs**: Documentation updates
- **chore**: Maintenance (dependencies, tooling, config)
- **test**: Test-related updates
- **style**: Code style and formatting
- **ci**: Workflow and pipeline changes
- **py(deps)**: Dependencies
- **py(deps[dev])**: Dev dependencies
- **ai(rules[AGENTS])**: AI rule updates (`AGENTS.md` / `CLAUDE.md`)
- **ai(claude[rules])**: Claude Code-specific rule changes
- **ai(claude[command])**: Claude Code command changes (`.claude/`)

Example:

```
agentgrep(refactor[typecheck]): Fix ty diagnostics

why: ty reports a few stricter diagnostics around TypedDict payloads,
dynamic class bases, and monkeypatched imports. Making those cases
explicit keeps the runtime behavior unchanged while letting the new
ty gate run without suppressing broad categories of checks.

what:
- Cast JSON TypedDict payloads only at untyped JSON container
  boundaries.
- Mark the dynamic Textual App base with the targeted ty
  unsupported-base suppression.
- Use pytest monkeypatch for import substitution tests instead of
  assigning over imported functions directly.
```

For a multi-line message, use a heredoc so the formatting survives:

```console
$ git commit -m "$(cat <<'EOF'
Scope(feat[detail]): Concise description

why: Explanation of the change.

what:
- First change
- Second change
EOF
)"
```

### Release commits

Never create tags. Never push tags. The owner handles tagging and tag pushes,
because a tag triggers the publish workflow.

A release commit subject is plain and short: `Tag v<version>`. The detailed
why and what go in the body. Do not use the `Scope(type[detail]):` format for
a release — it buries the lede.

## Slop prevention

Treat AI slop as review-hostile noise, not as proof that text or code is
wrong. The goal is to maximise information density.

- **AI signatures.** No "Generated by", no conversational filler, no
  unexplained emoji, no tool metadata.
- **Brittle references.** No hard-coded line numbers, fragile file counts,
  dated "as of" claims, bare SHAs, or local absolute paths — unless they are
  strict evidentiary artefacts such as a benchmark log.
- **Diff narration.** Do not restate what moved, was renamed, or was removed
  in anything the reader holds alongside the diff: code, docstrings, README,
  `CHANGES`, or a pull request description. The diff and the commit message
  already carry it.
- **Branch-internal narrative.** Do not mention intermediate states,
  abandoned approaches, or "no longer" behaviour unless users of a published
  release actually experienced the old state — the Published-Release Test
  below.
- **Low-value scaffolding.** No ownerless TODOs, unused future-proofing,
  debug artefacts, or defensive wrappers around failure modes nothing can
  reach.
- **Prose inflation.** The diction table under [Voice](#voice) governs;
  replace an inflated word with a concrete description of behaviour,
  constraints, or trade-offs.
- **Coded labels.** Write rules and findings as plain imperatives. No `[R1]`,
  `Option B`, or any index a reader has to decode in text a human reads.

Preserve the "why". Never delete a comment documenting an invariant, a
protocol constraint, a platform quirk, or an upstream workaround — those are
the facts [Source comments](#source-comments) keeps, and every other comment
is judged by it.

### Durable source links

Link to a pinned revision, never to trunk. A pinned permalink is not a
brittle reference; an unlinked SHA dropped into prose is. `blob/master/…`
links rot silently — the file moves, lines shift, and the anchor lands on
unrelated code while still resolving.

- Prefer a release tag (`blob/v0.1.0a50/…`). Most durable, and it tells the
  reader which released version the claim held for.
- Otherwise use a 7-char commit ref (`blob/9a29b1a/…`) reachable from `master`.
  Use when there is no tag or the claim is about unreleased code. Never a
  PR-head SHA — it can be rebased or garbage-collected.
- Reserve `blob/master/…` for living documents meant to always show the
  latest state, such as a contributing guide.
- Line anchors (`#L120-L145`) are only safe on a pinned ref.

### The Published-Release Test

Long-running branches accumulate tactical decisions — renames, refactors,
attempts-then-reverts. When deciding what counts as branch-internal, use
`master` or the parent branch as the baseline, not intermediate states inside
the current branch. Ask:

> Did users of the most recently published release ever experience this old
> name, old behavior, or bug?

If the answer is no, it is branch-internal narrative. Move it to the commit
message and describe only the final state in the artifact. Deprecations and
migration guides for symbols that actually shipped, `### Fixes` entries for
bugs that affected a published release, and comments explaining why the
current code looks this way all stay — a reader who never saw the previous
version still needs them.

### Cleaning up slop found in hindsight

Applying this section retroactively inside a feature branch, first diff
against `master` (or the parent branch) to scope which commits the branch
actually introduced. For a commit the branch introduced, prefer a `fixup!`
commit squashed in with `git rebase --autosquash` over a single catch-all
cleanup commit at the tip — it credits the fix to the commit it corrects. For
a commit from `master` or the parent branch, leave it alone unless the user
explicitly asks otherwise; never rewrite shared history to chase slop that
predates the branch.
