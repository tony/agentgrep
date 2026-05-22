(cli-grep)=

# agentgrep grep

The `agentgrep grep` command searches normalized prompt and history
records with the flag grammar and output behavior of `ripgrep` and
`the_silver_searcher`. If you already reach for `rg -i` or `ag -F`
without thinking, the same flags work here against your AI history.

Defaults follow rg: smart-case (case-insensitive unless the pattern
contains uppercase), regex pattern interpretation, color on TTY,
and line-aware output. Each matching record emits one row per
matching line. By default each row is just `path:text` (rg's
default pipe shape); pass `-n` / `--line-number` to add line
numbers, `--column` to add column numbers (implies `-n`), and
`--vimgrep` for the `path:line:col:text` shape with one row per
match span. On TTY a per-record heading line opens with
agent · timestamp · path. The one deliberate divergence is session
deduplication — see {ref}`cli-grep-dedupe` below.

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
record, with the matched substring highlighted. The shape mirrors
`rg`:

- **On TTY** (heading mode, default): a per-record heading line
  carries `agent · timestamp · path`, then each matching line
  follows as `text` (or `line:text` with `-n`, or
  `line:col:text` with `--column`). Records are separated by a
  blank line. Toggle off with `--no-heading`.
- **On pipe** (flat mode, default when stdout isn't a TTY): every
  match emits as `path:text` (rg's default), so `agentgrep grep
  foo | jq` or `... | awk` see one line per match. `-n` adds
  `:line:` after the path; `--column` adds `:col:` (implies `-n`).
  Toggle the heading on with `--heading`.

The `--vimgrep` flag forces flat mode and emits `path:line:col:text`
with one row per match span (rather than one per match line), so a
line with two hits produces two rows — useful for `vim` `:cfile`
and other editors that consume the
`file:line:col:message` format.

`--only-matching` / `-o` collapses output to just the matched
substrings, one per line — the per-record heading separator is
suppressed under `-o`, so the stream is exactly bare matches
back-to-back (`rg -o` parity). `-l` / `--files-with-matches` emits
only the deduplicated paths. `-c` emits `path:N` per matching
record with the count of matching lines (or just `N` when exactly
one record matched), matching `rg -c`.

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

(cli-grep-error-handling)=

## Error handling

Invalid regex patterns are caught at the argparse layer and surfaced
with the standard argparse error shape, then exit 2:

```console
$ agentgrep grep '['
usage: agentgrep grep [...]
agentgrep grep: error: invalid regex '[': unterminated character set at position 0
```

The check runs before the engine starts so a malformed pattern never
emits partial output and never escapes as a Python traceback. `-F`
(fixed-strings) skips the check — its patterns are literal substrings,
not regex.

Empty patterns are also rejected at parse time (git-grep parity):

```console
$ agentgrep grep ''
usage: agentgrep grep [...]
agentgrep grep: error: pattern cannot be empty
```

The check applies to every term — a valid pattern followed by an
empty one (`agentgrep grep foo ''`) still fails.

`-v` / `--invert-match` for plain text output is not yet
implemented and is refused at parse time:

```console
$ agentgrep grep -v bliss
usage: agentgrep grep [...]
agentgrep grep: error: --invert-match for text output is not yet
implemented (see https://github.com/tony/agentgrep/issues/8); use
-c or -L
```

The flag is still honored under `-c` (returns `0` if any record
matched, `1` if none) and `-L` (lists sources with no matches),
since both reduce to a "did anything match?" question that the
engine's current output supports. Tracking issue:
[tony/agentgrep#8](https://github.com/tony/agentgrep/issues/8).

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

Pass `--json` to emit an rg-shaped per-line event stream:

```console
$ agentgrep grep --json deploy
```

The output is a JSON document whose `events` array carries one
`begin` event opening each matching record, one `match` event per
matching line within that record, an `end` event closing the
record, and a final `summary` event with the total match count.

Each `match` event mirrors `rg`'s per-line shape:

```json
{"type":"match","data":{
  "path":{"text":"~/.codex/.../sample.jsonl"},
  "line_number":1,
  "lines":{"text":"The bliss primitive ships with serene defaults"},
  "submatches":[{"match":{"text":"bliss"},"start":4,"end":9}]}}
```

`submatches` carries byte offsets within the line so consumers can
slice the matched substring directly. Tools written against `rg`'s
JSON contract can consume agentgrep's stream with the same parser.

## NDJSON output

Pass `--ndjson` for one event per line:

```console
$ agentgrep grep --ndjson foo | jq 'select(.type == "match") | .data.lines.text'
```

This mode is the right pick when piping into another CLI, into `jq`,
or into a non-MCP agent that consumes results incrementally.

## Interactive UI

Pass `--ui` to launch the Textual explorer pre-filled with the grep
query — same flags, same results, different presentation. This is the
`tig`-shaped overlay: ``agentgrep grep -i foo --ui`` is to
``agentgrep grep -i foo`` as ``tig log`` is to ``git log``.

See {ref}`cli-ui` for the standalone explorer entry point.
