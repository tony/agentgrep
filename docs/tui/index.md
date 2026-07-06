(tui)=

# TUI

The `agentgrep ui` command launches the interactive Textual explorer
over the same Codex, Claude Code, Cursor, Gemini, Antigravity, Grok,
Pi, OpenCode, and VS Code stores the rest of the CLI walks. It is read-only —
agentgrep never mutates the source stores. Bare `agentgrep` prints the
directory of choices, so
the explorer always needs the explicit `ui` subcommand.

```{note}
Versions before 0.1.0a5 made bare `agentgrep` equivalent to
`agentgrep ui`. That shortcut is gone. Reach the explorer through
the explicit `ui` subcommand, or use the `--ui` overlay on
`agentgrep grep` / `find` to open it
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

Hand a one-shot `search` straight to the explorer with `--ui`:

```console
$ agentgrep search bliss --ui
```

Open the explorer on current-project results:

```console
$ agentgrep search --only-here deploy --ui
```

Open the explorer over prompts and conversations at once:

```console
$ agentgrep grep tmux --scope all --ui
```

## Layouts and workflows

The explorer is a thin shell over two pluggable axes — a **layout** (how records
are arranged on screen) and a **workflow** (how the primary input queries them).
Both are selectable at launch and switchable at runtime, so the same engine and
the same records can drive very different surfaces.

Two layouts ship:

- **`hud`** (default) — the search bar, streaming results list, and detail pane.
- **`greplog`** — an append-only `grep`-style log of matches as they stream in.

Two workflows ship:

- **`search`** (default) — the input runs a fresh engine search on each submit.
- **`browse`** — the input filters the already-loaded records in-memory.

Launch straight into a specific pair:

```console
$ agentgrep ui --layout greplog --workflow browse
```

Switch at runtime with `F2` (cycle the layout) and `F3` (cycle the workflow);
the active `layout · workflow` pair shows in the title bar.

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
When the selected record carries {class}`~agentgrep.RecordOrigin`, the
detail header also shows available cwd, repo, worktree, branch, and cwd
hash values.

## Completion

Both the search bar and the in-list filter offer
{ref}`query-language <library-query-language>` completion as you type.
The completion is **keyword-only** — field names and aliases (`age` →
`agent:`) and enum values (`agent:co` → `agent:codex`); it never
suggests text pulled from your records, so no prompt content or IDs
leak into the dropdown.

Two surfaces drive it:

- **Inline ghost text** previews the single best completion of the
  trailing token. Press `→` (right arrow) at the end of the input to
  accept it.
- A **keyword dropdown** lists every candidate (field keywords for a
  bare token, enum values for a `field:` token). Press `↓` to step into
  the list, `Enter` to accept the highlighted entry, and `Esc` or
  `Ctrl-C` to dismiss it without changing your text. Accepting an entry
  rewrites only the trailing token and leaves the cursor in place — the
  rest of the query is untouched.

::::{grid} 1 1 2 2
:gutter: 2

:::{grid-item-card} API Reference
:link: reference
:link-type: doc
UIArgs, entry points, filter and display helpers.
:::

::::

## See also

- {ref}`cli` — the `--ui` flag on any search-shaped subcommand opens
  the same explorer pre-seeded with that subcommand's query (e.g.
  `agentgrep grep bliss --agent codex --ui`).

```{toctree}
:hidden:

reference
```
