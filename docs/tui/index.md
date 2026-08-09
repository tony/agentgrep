(tui)=

# TUI

`agentgrep ui` launches the read-only Textual explorer over dedicated
prompt-history stores. `/deep` searches selected conversations and
`/exhaustive` searches every readable conversation, either as a follow-up to
the active search or as the first search of the session. The
same depth can be selected at launch with `agentgrep search --deep QUERY
--ui` or `agentgrep search --exhaustive QUERY --ui`. Bare `agentgrep` lists
subcommands, so the explorer requires `ui`.

`--deep` infers all scope, so prompt and selected-conversation records can
appear together. `--exhaustive` keeps an omitted CLI scope at prompts; add
`--scope all` when you want both record kinds. Targeted routing is available
for Codex, Claude Code, Grok, and Antigravity CLI; other conversation backends
require exhaustive effort.

```{note}
Versions before 0.1.0a5 made bare `agentgrep` equivalent to
`agentgrep ui`. Use `ui` now, or add `--ui` to `search`, `grep`, or
`find`.
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

Open the explorer with bounded targeted conversation search:

```console
$ agentgrep search --deep bliss --ui
```

Open the explorer with every readable conversation:

```console
$ agentgrep search --exhaustive bliss --ui
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
- `/deep` runs the active query against the conversations selected from
  matching prompt evidence. `/deep 50` bounds that one request to 50
  conversations.
- `/exhaustive` or `/all` runs the active query against every readable
  conversation.
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
- `/status` or `/version` reports the running version, whether it is a release
  or development build, and the git ref when agentgrep is running from a
  checkout. `agentgrep --version` reports the same thing from the shell.

The engine offers `/deep` and `/exhaustive` as request-local follow-ups. When
the last search offered the matching escalation they apply its patch; otherwise
they escalate whatever query the search box holds, so a session can start at
either depth without spending a prompt search first. Reaching a slash command
means emptying the box, so the query you typed before `/` is the one they
escalate. If an explicit prompt scope requires confirmation, a denied `/deep` or
`/exhaustive` keeps the wider search unstarted and restores the active query in
the input. Transient slash follow-ups do not replace launch effort: an ordinary
later edit returns to the launch effort and preserves its custom targeted
conversation bound.

The terminal status tells you what completed: failures show `Search incomplete`;
cancellation or answer-now shows `Stopped at N`; prompt effort says conversation
bodies were not read; targeted effort reports completed/selected conversations;
and exhaustive effort reports completed/planned sources. Relevance or newest
ordering can keep records visually buffered while the global frontier is still
unknown, so an empty result list is not proof that the worker is stuck.

(tui-depth-discoverability)=

## Finding the depth ladder

Search effort is a ladder — `prompt`, then `targeted`, then `exhaustive` (see
{ref}`adr-progressive-deep-search`). The explorer's job is to make each rung
reachable and to make it obvious which rung you are standing on. Two surfaces
carry that: the idle canvas before a search, and the empty panel after one.

### Before a search

The idle canvas lists the depth choices the engine offers for the query the
search box would submit. Selecting one applies the engine's own request patch
to your typed query and starts that search, so `targeted` is reachable from a
cold session without first running a shallow search to unlock it. The panel is
authored entirely from engine actions: when the engine offers no deeper rung —
because you launched at `exhaustive`, or typed an inline `scope:` predicate that
already reads conversations — it lists nothing.

Pick a rung with the mouse, or reach the panel with `Tab` and use the arrow
keys and `Enter`. It leaves the tab order whenever it has nothing selectable,
so it never becomes a dead stop.

An explicitly selected prompt scope is not silently widened here either. In that
case the panel drops the selectable rows and states the scope change the wider
search would need, matching what a denied `/deep` reports after a run.

### After a search

An empty result is a claim about the surface that was read, never about your
history as a whole. The panel therefore pairs a distinguishable outcome with the
evidence behind it:

| Outcome | What it proves | What it does not prove |
| --- | --- | --- |
| `No prompt matches` | Prompt history holds no match | Nothing about conversation bodies — they were not opened |
| `No candidate conversations` | Prompt evidence selected no conversation to read | Nothing about unselected conversations |
| `No matches in selected conversations` | The conversations chosen from prompt evidence hold no match | Nothing about conversations routing did not choose |
| `No matches in readable conversations` | Every readable conversation was read and holds no match | Nothing about stores agentgrep cannot read |

`Search incomplete` is a fifth, non-terminal state: coverage was cut short by a
failure, cancellation, truncation, or a bound, so the run is not a negative
result at any depth.

The principle behind the table is that a miss at one rung is not a corpus-wide
negative. Only the last row is close to one, and even it is bounded by which
stores are readable. Each panel names its next rung so the difference between
"not there" and "not looked at" stays visible without reading the docs.

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

(tui-copying)=

## Copying

Select text with the mouse and press `Ctrl-C` to copy it. `Cmd-C` works
the same way, and `Ctrl-Shift-C` and `Cmd-Shift-C` do too in terminals
that forward them rather than claiming them for their own copy. With
nothing selected, `Ctrl-C` keeps its usual job — stop the running
search, then quit — so the one key covers both without a mode.

The detail pane also copies whole records without a mouse: `y` copies
the raw source, `Y` copies the rendered text, and `v` starts a
tmux-style visual selection you extend with `hjkl` and yank with `y`.
Inside a search box, `Ctrl-C` copies the selection when you have one and
clears the box when you do not.

:::{note}
agentgrep hands the text to your terminal with an OSC 52 escape, which
is fire-and-forget: nothing reports back whether the terminal accepted
it, so the toast says what was *sent*, not that it arrived.

Two setups drop it silently. Inside **tmux**, OSC 52 is discarded unless
your configuration sets `set -g set-clipboard on` — the shipped default
is `external`, which does not accept it. **macOS Terminal** ignores the
sequence outright; iTerm2, Ghostty, kitty, WezTerm and Alacritty accept
it. If a paste comes back stale, that is where to look first.
:::

(tui-export)=

## Export

The HUD offers two pi-like export flows:

- Press `e` with the results list or detail pane focused, or type
  `/export [PATH]`, to review exactly the selected record in the right detail
  pane. An optional path seeds the directory and filename fields; without one,
  the pane starts from the remembered directory and filename template.
- `/export-thread [PATH]` is the one-shot command. It exports the selected
  record's observed thread from the current result set after the in-list
  filter. A record without a canonical thread handle cannot be exported as a
  thread.

Slash-command text is transient: opening the export pane restores the current
search term and its exact selection, and returning from the pane restores the
originating focus. The pane previews the exact filename and keeps both fields
when No returns to editing. Save is the mutation boundary: No and cancel
perform no filesystem mutation. Save securely creates the exact app default
when needed, writes that reviewed new destination, then attempts to write the
TUI-private preference file. The remembered values change only when that
preference write succeeds. The contextual `/keys` panel lists the `e` shortcut
without adding it to the compact footer.

Without `PATH`, `/export-thread` writes a collision-free Markdown artifact to
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
Source stores remain read-only. A successful reviewed Save may write both the
new artifact and its TUI-private preference file; the one-shot thread command
writes only its artifact. See {ref}`ADR 0017 <adr-portable-record-export>` for
the payload, fidelity, and file-safety contract.

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
  the list, `Enter` to accept the highlighted entry, and `Esc` to
  dismiss it without changing your text (`Ctrl-C` dismisses it too,
  unless you have text selected — then it copies). Accepting an entry
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
