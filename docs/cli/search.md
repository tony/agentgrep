(cli-search)=

# agentgrep search

The `agentgrep search` command returns ranked, deduplicated search
results across normalized prompt and conversation records. Use it when
you want best-first records instead of rg-shaped line output.

`search` defaults to prompt scope. Pass `--scope conversations` to
search full transcript records, or `--scope all` to include both
surfaces.

## Examples

Search prompt records:

```console
$ agentgrep search streaming parser
```

Require a minimum fuzzy score:

```console
$ agentgrep search --threshold 70 migration
```

Return discovery-order records without ranking or session grouping:

```console
$ agentgrep search --no-rank --no-group caching
```

Emit structured JSON:

```console
$ agentgrep search bliss --json
```

Open the Textual explorer pre-filled with the same query:

```console
$ agentgrep search bliss --ui
```

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: search
    :nodescription:
```

## Output

Text output is record-oriented and sorted by match quality unless
`--no-rank` is set. `--json` returns one envelope with serialized
{class}`~agentgrep.SearchRecord` entries. `--ndjson` streams one
record per line for incremental consumers.

## Cache controls

`search` supports the DB cache flags:

```console
$ agentgrep search "release" --cache require
```

Use `--no-cache` when benchmarking or checking live source freshness:

```console
$ agentgrep search "release" --no-cache
```
