(cli-export)=

# agentgrep export

`agentgrep export` turns records matched by the shared search engine into a
portable artifact without modifying an agent's history. There are exactly two
formats. Use `ndjson` for scripts and `markdown` for reading or sharing. The
default format is `ndjson`, and the default sink is standard output. Record
bodies are included by default because running `export` is an explicit choice.

The command accepts the same agent, scope, case, and query-language filters as
search. Terms are combined with AND semantics, and the default scope is
`prompts`. The default limit is `100`; set `--limit` to any value from `1`
through `1000`.

## TUI reviewed save

Press `e` while an exact selected record has focus in the HUD results or
detail pane. One compact dialog remembers the export directory and filename
template in TUI-private user configuration. It starts with
`{date} {time} - {title}.md`; directory completion lists existing child
directories and accepts a choice with the arrow keys and Tab.

The preview freezes local time when the dialog opens. The date and time render
as the filesystem-safe `YYYY-MM-DD HH-MM-SS`, and the title token uses a
bounded normalized form of the record title without reading its body or source
path. Submitting the draft shows the directory and exact filename separately.
The confirmation starts on **No**; No returns to editing with both values
intact.

Save writes only the reviewed explicit no-clobber destination. If that name
already exists, agentgrep returns to the same draft instead of replacing the
file or silently choosing another name. Automatic private exports requested by
the HUD slash commands keep their canonical-ID names. CLI and MCP do not
consume the TUI preference: the CLI still uses standard output or an explicit
`--output` path, and MCP still returns a bounded inline artifact without local
filesystem authority.

## Examples

Export matching prompt records as NDJSON to standard output:

```console
$ agentgrep export "release notes"
```

Omit prompt and history text while retaining portable metadata:

```console
$ agentgrep export "release notes" --no-bodies
```

Write human-readable Markdown to standard output:

```console
$ agentgrep export "release notes" --format markdown
```

Search prompts and conversation records together:

```console
$ agentgrep export "release notes" --scope all
```

Write NDJSON to a new relative file:

```console
$ agentgrep export "release notes" -o records.ndjson
```

Replace an existing regular file deliberately:

```console
$ agentgrep export "release notes" -o records.ndjson --force
```

`-o -` names standard output explicitly. `--force` is invalid with standard
output; it applies only to a file destination.

## Formats and privacy

NDJSON contains one canonical JSON object per record. Keys and record order
are stable, and a trailing newline separates every object. Markdown presents
the same allowlisted metadata as headings and lists. With bodies enabled, it
places exact valid UTF-8 text in a dynamically sized code fence so backticks in
the record cannot close the block.

Both formats include the record schema version, agent, store, kind, role,
timestamp, model, content ID, optional record ID and stability, and optional
thread ID. `--no-bodies` omits the `text` field or body section entirely.
Neither format carries source or display paths, titles, session IDs,
conversation IDs, project origin, or adapter metadata. See {ref}`ADR 0017
<adr-portable-record-export>` for the exact allowlist and ordering contract.

NDJSON represents lone surrogate code points as JSON escapes, so the artifact
remains valid UTF-8 and a JSON decoder can recover the original string.
Markdown cannot represent those values as valid UTF-8 and returns a path-free
encoding error instead of altering the text.

(cli-export-files)=

## File safety

File output refuses to overwrite an existing destination unless `--force` is
present. Even with force, the destination must be a regular file: agentgrep
rejects a symlink, a symlinked parent, and any lexical, resolved, or hard-link
alias of a source store. The CLI protects every discovered source store,
including non-default stores and stores that did not match the query, so an
export cannot replace history by choosing it as the destination.

The completed artifact is installed atomically with private file permissions.
Errors do not include the destination or source path. These rules preserve
agentgrep's read-only treatment of Codex, Claude Code, Cursor, and every other
source store; only the chosen export artifact is written.

## Exit status

The command exits `0` when at least one record is exported and `1` when the
search has no matches, producing an empty valid artifact. Invalid arguments,
source failures, encoding failures, and output failures exit `2` with a
path-free diagnostic on standard error.

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: export
    :nodescription:
```
