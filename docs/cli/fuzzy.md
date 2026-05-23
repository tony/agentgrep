(cli-fuzzy)=

# agentgrep fuzzy

The `agentgrep fuzzy` command is a non-interactive fuzzy filter
shaped like `fzf --filter`. It reads candidate lines from stdin,
scores them against your query, and emits the matches in descending
score order. Use it as the narrowing stage at the tail of a pipeline.

Defaults follow fzf: fuzzy matching with the v2 algorithm, smart-case
(case-insensitive unless the query has uppercase), extended-search
syntax (`foo !bar`, `^foo`, `bar$`, `'foo`), and score-descending
sort.

## Examples

Narrow grep output to lines that fuzzy-match a phrase:

```console
$ agentgrep grep -F . | agentgrep fuzzy 'config bliss'
```

Exact substring matching instead of fuzzy:

```console
$ agentgrep fuzzy --exact -i 'design notes' < transcript.txt
```

Match in a specific tab-separated column (1-indexed):

```console
$ agentgrep find -l | agentgrep fuzzy --delimiter $'\t' --nth 4 jsonl
```

Print the query as the first output line (fzf's `--print-query`):

```console
$ agentgrep fuzzy --print-query design < transcript.txt
```

Open the Textual explorer pre-filled with the fuzzy query:

```console
$ agentgrep fuzzy design --ui
```

## No-input behavior

When `agentgrep fuzzy` is invoked with no QUERY positional, no `-f`
filter, AND no piped stdin (i.e. you typed `agentgrep fuzzy` into an
interactive shell), the subcommand prints its usage and exits with
status `2`. There is no interactive fzf-style TUI fallback — for
interactive browsing reach for `agentgrep ui` or the `--ui` overlay
on any other subcommand.

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: fuzzy
    :nodescription:
```

## Exit codes

- `0` — at least one input line matched the filter
- `1` — no lines matched
- `2` — invalid usage (no QUERY and no piped stdin)

## Extended-search syntax

The default `--no-extended` flag toggles fzf's extended-search
grammar:

- A bare token must appear in the line (substring match)
- `!token` excludes lines containing `token`
- `^token` anchors to the line prefix
- `token$` anchors to the line suffix
- `'token` forces an exact substring match (no fuzzy fallback)

Tokens are whitespace-separated. A line matches when every positive
token's predicate is satisfied and no negative token's predicate is.
Pass `--no-extended` to treat the query as a single literal pattern.

## Field selection

`--delimiter`, `--nth`, and `--with-nth` mirror fzf's field-selection
model. With `--delimiter $'\t'` and `--nth 2`, only the second
tab-separated field is scored. With `--with-nth 3`, only the third
field is displayed even though scoring happens against the full line.
Together they let you fuzzy-narrow tabular pipelines (e.g. agentgrep
find `--list-details`) by column.

## NUL-delimited I/O

Pass `--read0` to treat stdin as NUL-delimited (for input from
`agentgrep find --print0`, `xargs -0`, etc.). Pass `--print0` to
separate output records with NUL — useful when piping back into
another `xargs -0` consumer or any tool that needs paths with
embedded spaces.

## Interactive UI

`--ui` opens the Textual explorer pre-filled with the fuzzy query.
Same `tig`-shaped overlay as the other subcommands.

See {ref}`cli-ui`.
