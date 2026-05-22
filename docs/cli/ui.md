(cli-ui)=

# agentgrep ui

The `agentgrep ui` command launches the interactive Textual explorer
over the same Codex, Claude Code, Cursor, and Gemini stores the rest
of the CLI walks. It is read-only — agentgrep never mutates the
source stores. Bare `agentgrep` prints the directory of choices, so
the explorer always needs the explicit `ui` subcommand.

```{note}
Versions before 0.1.0a5 made bare `agentgrep` equivalent to
`agentgrep ui`. That shortcut is gone. Reach the explorer through
the explicit `ui` subcommand, or use the `--ui` overlay on
`agentgrep grep` / `find` / `fuzzy` / `search` to open it
pre-filled with that subcommand's query.
```

## Examples

Open the explorer with no seed query:

```console
$ agentgrep ui
```

Seed the search bar with an initial query so the explorer dispatches
a backend search immediately:

```console
$ agentgrep ui bliss
```

## Command

```{eval-rst}
.. argparse::
    :module: agentgrep
    :func: build_docs_parser
    :prog: agentgrep
    :path: ui
    :nodescription:
```

## Key interactions

The top input is the **search bar**. Pressing `Enter` dispatches a
fresh backend search; pressing `Enter` again while a search is in
flight signals the previous worker to wrap up before the next one
starts, so re-querying mid-stream does not pile up cancellations.
Empty / whitespace-only input parks the explorer in an idle state
instead of issuing a no-op backend search.

Below the results list sits a **sticky in-list filter**. Every
keystroke narrows the already-loaded records without re-running the
backend search, so refining a large result set is instant. Plain
`up` on the filter returns focus to the search bar; plain `right` on
an empty filter releases focus to the detail pane, so the full
arrow-key perimeter walks the three columns without reaching for
`Ctrl-L`. A non-empty `right` keeps cursor-in-input semantics.

Each pane carries a footer **status line**. The results footer shows
match count, cursor position, and a tig-style scroll percent that
reads `100%` when the view fits; the detail footer shows the compact
source path and the same scroll percent. Result-row timestamps
render in the viewer's local timezone with offset
(`YYYY-MM-DD HH:MM ±HHMM`), formatted via
{func}`~agentgrep.format_timestamp_tig`.

## See also

- {ref}`cli-search` — the `--ui` flag opens the same explorer
  pre-seeded with a search-flag invocation (e.g.
  `agentgrep search bliss --agent codex --ui`).
- {ref}`api-agentgrep-ui` — Python API for the
  {mod}`agentgrep.ui` subpackage, including
  {func}`~agentgrep.ui.app.run_ui` and
  {func}`~agentgrep.ui.app.build_streaming_ui_app`.
