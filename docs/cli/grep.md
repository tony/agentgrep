(cli-grep)=

# agentgrep grep

The `agentgrep grep` command searches normalized prompt and history
records with the flag grammar and output behavior of `ripgrep` and
`the_silver_searcher`. If you already reach for `rg -i` or `ag -F`
without thinking, the same flags work here against your AI history.

Defaults follow rg: smart-case (case-insensitive unless the pattern
contains uppercase), regex pattern interpretation, color on TTY,
and line-aware output. Each matching record emits one row per
matching line in the shape `line:col:text`, with the matched span
highlighted (red+bold). On TTY a per-record heading line opens with
agent · timestamp · path; on pipe each row is prefixed with the
path so the output stays grep-pipeline friendly. The one deliberate
divergence is session deduplication — see {ref}`cli-grep-dedupe`
below.

## Examples

A literal single-pattern search across every agent:

```console
$ agentgrep grep bliss
```

Force case-insensitive matching:

```console
$ agentgrep grep -i 'serene bliss'
```

Treat the pattern as a literal substring (not a regex):

```console
$ agentgrep grep -F --type history 'v1.2.3'
```

Stream an rg-style event stream as JSON:

```console
$ agentgrep grep --json design
```

Drop session dedup for the raw rg-faithful view:

```console
$ agentgrep grep --no-dedupe foo
```

Open the Textual explorer pre-filled with the grep query:

```console
$ agentgrep grep -i foo --ui
```

Silence the stderr spinner:

```console
$ agentgrep grep --no-progress bliss
```

## Output format

By default `grep` emits one stdout line per matching line within a
record, with the matched substring highlighted. The format mirrors
`rg`:

- **On TTY** (heading mode, default): a per-record heading line
  carries `agent · timestamp · path`, then each matching line
  follows as `line:col:text` with ANSI highlights. Records are
  separated by a blank line. Toggle off with `--no-heading`.
- **On pipe** (flat mode, default when stdout isn't a TTY): every
  match emits as `path:line:col:text` with no per-record heading,
  so `agentgrep grep foo | jq` or `... | awk` see one line per
  match. Toggle on with `--heading`.

The `--vimgrep` flag forces flat mode and emits one row per match
span (rather than one per match line), so a line with two hits
produces two rows. `--only-matching` / `-o` collapses output to
just the matched substrings, one per line. `-l` /
`--files-with-matches` emits only the deduplicated paths.

## Live streaming

`grep` consumes the {ref}`library-event-stream` directly — text and
NDJSON output emit each match as the engine finds it, then flush so
your terminal sees rows live. On a slow store the first matches
appear within milliseconds, not after the whole scan finishes.

The eager output modes (`--json`, `-c`, `-l`, `-L`, `-v`) buffer
because their output shape needs the final tally or cross-record
deduplication.

## Progress

The stderr progress spinner (when stderr is a TTY) lets you know a
search is still running on slow stores. Silence it with
`--no-progress` or the equivalent `--progress=never`:

```console
$ agentgrep grep --no-progress bliss
```

Progress always writes to stderr, so it never collides with stdout
output — `agentgrep grep foo | less` won't see the spinner in the
piped buffer.

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: grep
    :nodescription:
```

## Exit codes

`agentgrep grep` follows grep's conventions:

- `0` — at least one matching record was found
- `1` — no matches
- `2` — error during search (invalid regex, unreadable store, …)

Use these in shell scripts the same way you'd use `rg`'s exit codes.

(cli-grep-dedupe)=

## Session deduplication

By default `grep` deduplicates matches by session so a single
conversation that repeats near-identical text doesn't drown the
output. This is the one place where `agentgrep grep` deliberately
diverges from `rg`'s raw behavior — AI history stores often replay
the same message text many times across one session, which makes the
raw rg view noisier than a filesystem grep.

Pass `--no-dedupe` to disable the per-session dedup and get every
matching record back, exactly matching rg's "every line is its own
match" convention:

```console
$ agentgrep grep --no-dedupe foo
```

## JSON output

Pass `--json` to emit an rg-shaped event stream:

```console
$ agentgrep grep --json deploy
```

The output is a JSON document whose `events` array carries one
`match` event per matching record plus a final `summary` event with
the total count. Each `match` event carries the agent, store, path,
session metadata, and the matched text. The shape is the same model
`rg --json` follows, adapted for agentgrep records.

## NDJSON output

Pass `--ndjson` for one match event per line:

```console
$ agentgrep grep --ndjson foo | jq '.data.text'
```

This mode is the right pick when piping into another CLI, into `jq`,
or into a non-MCP agent that consumes results incrementally.

## Interactive UI

Pass `--ui` to launch the Textual explorer pre-filled with the grep
query — same flags, same results, different presentation. This is the
`tig`-shaped overlay: ``agentgrep grep -i foo --ui`` is to
``agentgrep grep -i foo`` as ``tig log`` is to ``git log``.

See {ref}`cli-ui` for the standalone explorer entry point.
