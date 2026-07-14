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

## Slash commands

Type `/` in the primary input to open the same compact, pi-like command menu in
the explorer. Keep typing to filter it, or use `/help` to see the whole active
command set. `Ctrl-P` is intentionally inert; the slash menu replaces the larger
Textual command palette without covering your results.

The shared commands are:

- `/clear` clears the current search and results.
- `/exit` or `/quit` closes agentgrep.
- `/help` lists the active slash commands, and `/keys` toggles the active key
  bindings panel.
- `/theme` toggles the theme; `/theme dark` and `/theme light` select one
  directly.
- `/maximize` gives a content pane the available body space while keeping the
  primary input and footer reachable. It follows the last-used results or detail
  pane; use `/maximize results` or `/maximize detail` to be explicit.
- `/minimize` restores the normal results/detail split.
- `/screenshot` captures the current screen as an automatically named SVG.

`/screenshot` first clears the command text and menu, then captures the explorer
without cancelling the search or changing its results, theme, or zoom.
It accepts no path argument. In a terminal, Textual saves the SVG to your
downloads directory; in a browser session, it initiates a download.

## Slash commands

Type `/` in the primary input to open the same compact, pi-like command menu in
the HUD and greplog layouts. Keep typing to filter it, or use `/help` to see the
whole active command set. `Ctrl-P` is intentionally inert; the slash menu
replaces the larger Textual command palette without covering your results.

The shared commands are:

- `/clear` clears the current search and results.
- `/exit` or `/quit` closes agentgrep.
- `/help` lists the active slash commands, and `/keys` toggles the active key
  bindings panel.
- `/theme` toggles the theme; `/theme dark` and `/theme light` select one
  directly.
- `/maximize` gives a content pane the available body space while keeping the
  primary input and footer reachable. In the HUD, it follows the last-used
  results or detail pane; use `/maximize results` or `/maximize detail` to be
  explicit. In greplog, use `/maximize` or `/maximize log`.
- `/minimize` restores the normal split or greplog status area.
- `/screenshot` captures the current screen as an automatically named SVG.

`/screenshot` first clears the command text and menu, then captures the active
layout without cancelling the search or changing its results, theme, or zoom.
It accepts no path argument. In a terminal, Textual saves the SVG to your
downloads directory; in a browser session, it initiates a download.

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

The HUD detail header places full `Record:`, `Content:`, and `Thread:` handles
immediately after `Adapter:`. In a narrow pane, those labels compact to `R:`,
`C:`, and `T:` so each complete fixed-width handle stays on one line. Metadata
ellipsizes instead of word-wrapping when the pane is too narrow to contain its
value. On the first selection, dim `…` values reserve the identity rows while
identity is prepared away from the Textual message pump. A missing logical
occurrence or thread renders as `—`; the content handle is always available
once preparation finishes. The handles are comparison vocabulary, not copy
shortcuts or resolvers. See the {ref}`deterministic record identity contract
<adr-deterministic-record-identity>` for the exact boundary.

This is a HUD detail feature only. Compact result rows, the greplog layout, and
pane status lines keep their existing shapes.

### Bounded detail view

The HUD caps the displayed detail body at 1,000 lines and 65,536 characters so
formatting and find-in-detail cannot stall the interface on a very large
record. An overflow marker reports that more lines or characters remain. This
only bounds the TUI render; agentgrep does not change the source record.

To inspect the full body, rerun the same query with the CLI's `--json` or
`--ndjson` output and read the result's `text` field. From an MCP client, pass
the result's opaque `ref` to {tooliconl}`inspect_result` as
`inspect_result(ref=...)`.

(tui-export)=

## Export

The HUD offers two pi-like, one-shot slash commands:

- `/export [PATH]` exports exactly the selected record.
- `/export-thread [PATH]` exports the selected record's observed thread from
  the current result set after the in-list filter. A record without a canonical
  thread handle cannot be exported as a thread.

Press `e` with the results list or detail pane focused to review the exact
selected record before saving it. The dialog starts from the remembered
explicit directory and filename template, previews the exact filename, and
keeps both values when No returns to editing. Save writes that reviewed new
destination and remembers the values only after its preferences persist. The
contextual `/keys` panel lists the shortcut without adding it to the compact
footer.

The slash commands do not read or change those remembered values. Supplying
`PATH` gives that invocation an explicit one-shot destination.

Without `PATH`, both commands write a collision-free Markdown artifact to
agentgrep's private export directory. Its root follows `XDG_DATA_HOME`; when
set, artifacts go under `$XDG_DATA_HOME/agentgrep/exports`, and otherwise the
standard XDG data location is used. The directory uses mode `0700`, and each
artifact uses mode `0600`. With an explicit path, the destination must be new:
the TUI refuses to overwrite an existing file and rejects symlinks or an alias
of a selected source store. Use {ref}`agentgrep export <cli-export>` when an
explicit replacement is needed.

TUI exports include bodies and use Markdown. A success notification shows only
the artifact's basename, format, selection, and record count; failures omit
local paths. Work stays off the Textual message pump. Identity, rendering, and
disk I/O all run in the export worker. A second request reports that an export
is already in progress, and an observed-thread export cancels if its result
view changes while the HUD is taking the snapshot.

Export does not replace the loaded results or change the detail selection.
Only the new artifact is written; source stores remain read-only. See
{ref}`ADR 0017 <adr-portable-record-export>` for the payload, fidelity, and
file-safety contract.

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
