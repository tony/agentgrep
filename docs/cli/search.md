(cli-search)=

# agentgrep search

The `agentgrep search` command is the smart default for "what did I say
about X?" — it ranks matches by relevance and groups them by session, so
the best answer rises to the top instead of scrolling past in discovery
order. Where {ref}`grep
<cli-grep>` is `rg`-shaped (every matching line, newest-first), `search`
is results-shaped: fewer, better rows.

Like `grep`, it searches normalized prompt records by default and takes
explicit `--scope` controls for conversations. Scoring uses rapidfuzz's
`WRatio` — a token-aware 0-100 similarity — against the space-joined
terms.

## Examples

Rank prompts by relevance to a multi-term query (terms are AND-matched):

```console
$ agentgrep search streaming parser
```

Search prompts and conversations together in one sweep:

```console
$ agentgrep search "deploy" --scope all
```

Prefer records from the project you are standing in:

```console
$ agentgrep search --here deploy
```

Hard-filter by recorded working directory:

```console
$ agentgrep search --cwd ~/work/django-project deploy
```

Keep only strong matches by raising the score bar:

```console
$ agentgrep search --threshold 70 migration
```

Skip ranking and grouping for a flat, discovery-order list:

```console
$ agentgrep search --no-rank --no-group caching
```

Take just the top results:

```console
$ agentgrep search bliss --limit 5
```

Hand the same query to the {ref}`Textual explorer <tui>`:

```console
$ agentgrep search bliss --ui
```

Stream machine-readable results for a script or non-MCP agent:

```console
$ agentgrep search bliss --ndjson
```

## Ranking and relevance

By default `search` scores every matched record against your query with
rapidfuzz's `WRatio` and sorts best-first. The default `--threshold 0`
shows every match; raise it to drop weak ones:

```console
$ agentgrep search --threshold 70 release
```

A high threshold can filter everything out — `search` then exits `1`
(no matches) with no rows, the same way `grep` reports an empty result.
Pass `--no-rank` to bypass scoring entirely and return records in
discovery order (newest-first), the ordering `grep` uses:

```console
$ agentgrep search --no-rank release
```

## Deduplication and grouping

AI conversation stores replay the same text across a session. To keep
one chatty session from dominating, `search` deduplicates repeated
record text per session before ranking — the same
{ref}`session deduplication <cli-grep-dedupe>` `grep` applies, minus the
`--no-dedupe` escape hatch. Survivors are then grouped by session, with
the best match opening each group. Pass `--no-group` for a flat ranked
list with no session headings:

```console
$ agentgrep search --no-group caching
```

## Search scope

`search` searches `--scope prompts` by default — user-authored prompts,
including dedicated prompt-history logs and user turns projected from
transcript-only stores. Pass `--scope conversations` for full
conversation, session, assistant, tool, and event records, or
`--scope all` to search both surfaces together:

```console
$ agentgrep search "docs deploy" --scope all
```

(cli-search-project-context)=

## Project context

When a backend records where a prompt came from, `search` can use that
{class}`~agentgrep.RecordOrigin` to find the right project history.
`--here` keeps global results visible but boosts records from the
current checkout; `--only-here` hard-filters to that checkout and can be
used without text terms. The detector preserves the logical `$PWD`, so a
symlinked checkout is treated the same way the shell sees it.

```console
$ agentgrep search --here deploy
```

Use explicit filters when you know the project path or branch you want:

```console
$ agentgrep search --cwd ~/work/django-project deploy
```

```console
$ agentgrep search --branch main agent:codex
```

`--cwd` and `--repo` accept relative paths and `~`; blank values are
ignored. Origin filters compose with the user query without changing the
meaning of punctuation-heavy literals, URLs, or phrases. The same fields
are available inline as `cwd:`, `repo:`, `worktree:`, `branch:`,
`project:`, and `cwd_hash:`; see {ref}`library-query-language-origin-fields`.

`--here` affects ranking only, so it is rejected with `--no-rank` and
with `--ui`. Use `--only-here` when you want the Textual explorer to
open on current-project results.

## Output

The default output is ranked, grouped text for terminal reading. For
scripts and non-MCP agents, two machine-readable modes mirror `grep`:

- `--json` emits one JSON document with an `envelope` carrying the
  ranked record list. Best when the caller parses the whole result at
  once.
- `--ndjson` streams one JSON object per line. Best for piping into
  `jq`, another CLI, or an agent that consumes results incrementally.

```console
$ agentgrep search bliss --json
```

### Identity handles

Ranked terminal detail places three full, fixed-width handle rows immediately
after each result's provenance: `Record:`, `Content:`, and `Thread:`. Content is
always available. When a store cannot support logical occurrence or thread
identity, the corresponding human row uses an em dash (`—`) instead of
inventing a value. The handles compare records; they are not resolvers.

Every result in both JSON and NDJSON includes `content_id`, `record_id`,
`record_id_stability`, and `thread_id`. The latter three keys remain present
with JSON null values when the source cannot support them, so consumers do not
have to infer identity availability from a missing key. See the
{ref}`deterministic record identity contract
<adr-deterministic-record-identity>` for the distinction between content,
logical occurrence, thread, and physical location.

When a backend exposes project context, each serialized
{class}`~agentgrep.SearchRecord` can include an `origin` object with
`cwd`, `repo`, `worktree`, `branch`, `remote`, and `cwd_hash` fields.
Path values use the same display-safe layer as `path`; credentialed
remotes are stripped before output, and local `file://` remotes are
omitted. Filters compare against the recorded origin before display
rewriting, so privacy formatting does not change match behavior.

## Interactive UI

Pass `--ui` to open the {ref}`Textual explorer <tui>` pre-filled with
the search query — the `tig`-shaped overlay model, where
`agentgrep search bliss --ui` is to `agentgrep search bliss` what
`tig log` is to `git log`.

```console
$ agentgrep search bliss --ui
```

## Query language

`search` accepts the same Lucene-style field syntax as `grep` and
`find` — mix field predicates with text inline:

```console
$ agentgrep search agent:codex bliss
```

The predicates (`agent:`, `path:`, `timestamp:`, `cwd:`, `repo:`,
`branch:`, and more) prune and filter sources around the text terms.
See {ref}`library-query-language` for the full grammar.

## Progress

A stderr progress spinner (when stderr is a TTY) signals a search is
still running on slow stores. Silence it with `--no-progress` or the
equivalent `--progress=never`:

```console
$ agentgrep search --no-progress bliss
```

Progress always writes to stderr, so it never collides with stdout —
`agentgrep search bliss | jq` won't see the spinner in the piped buffer.

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: search
    :nodescription:
```

## Exit codes

`agentgrep search` returns:

- `0` — at least one ranked result survived
- `1` — no matches, including when `--threshold` filtered them all out

`search` has no separate runtime-error exit code — unlike {ref}`grep
<cli-grep>`, whose `2` covers invalid-regex and unreadable-store errors.
Malformed flags are still rejected by argparse before the search starts.
