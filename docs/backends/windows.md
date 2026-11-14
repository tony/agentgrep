(backends-windows)=

# Windows and WSL

When agentgrep runs inside a WSL distro, it also searches the agents
installed on the Windows side. Claude desktop, Codex desktop, Cursor, VS Code,
and ChatGPT desktop keep their history under the Windows profile, which the
distro sees under `/mnt/c/Users/<name>/`. agentgrep finds each profile, adds
its stores to the same search, and prints the paths without the Windows
account name.

Detection is automatic: the probe runs only when `/proc/version` names a
WSL kernel, and only for directories that exist.

## What is searched

| Agent | Windows location | Searched as | Default prompt search |
| --- | --- | --- | --- |
| Claude Code (run by Claude desktop) | `%USERPROFILE%/.claude/` | {doc}`claude` transcripts | `history.jsonl` if present |
| Claude desktop Cowork | `%APPDATA%/Claude/local-agent-mode-sessions/` | {doc}`claude` transcripts | no, `--exhaustive` |
| Claude desktop Code tab | `%APPDATA%/Claude/claude-code-sessions/` | session titles | no, opt-in inventory |
| Codex desktop | `%USERPROFILE%/.codex/` | {doc}`codex` sessions and SQLite state | `history.jsonl` if present |
| Cursor agent | `%USERPROFILE%/.cursor/projects/` | {doc}`cursor-cli` transcripts | `prompt_history.json` if present |
| Cursor IDE | `%APPDATA%/Cursor/User/` | {doc}`cursor-ide` state databases | as on Linux |
| VS Code Copilot Chat | `%APPDATA%/Code/User/` | {doc}`vscode` chat sessions | as on Linux |
| ChatGPT desktop | `%LOCALAPPDATA%/Packages/OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0/` | {doc}`chatgpt` conversation titles | no, conversation scope |

The catalogue records each store's Windows path in its `win32` platform
variant, which `get_store_descriptor` and the backend pages show.

Claude desktop's claude.ai chat client keeps only a rolling cache of a few
recently opened conversations in Chromium IndexedDB, catalogued as
{storage:storeref}`claude.desktop_chat_cache` and not searched; the full chat
history stays on Anthropic's servers.

## Other agents on Windows

Gemini CLI, Grok, Pi, OpenCode, and Antigravity CLI were not installed on the
Windows host checked. Their Windows locations come from each tool's source,
which resolves its directory from the user's home on Windows as on Linux:

| Agent | Windows location | Override |
| --- | --- | --- |
| Gemini CLI | `%USERPROFILE%/.gemini/` | `GEMINI_CLI_HOME` (the home that holds `.gemini`) |
| Grok | `%USERPROFILE%/.grok/` | `GROK_HOME` |
| Pi | `%USERPROFILE%/.pi/agent/` | `PI_CODING_AGENT_DIR` |
| OpenCode | `%USERPROFILE%/.local/share/opencode/`, `%USERPROFILE%/.local/state/opencode/` | `XDG_DATA_HOME`, `XDG_STATE_HOME` |
| Antigravity CLI | `%USERPROFILE%/.gemini/antigravity-cli/` (inferred: closed source) | none documented |

Gemini CLI, Antigravity CLI, and Antigravity IDE all write under
`%USERPROFILE%/.gemini`, so that directory alone does not show which is
installed. The backend pages list each store's `win32` path.

## Paths in output

A Windows path is shown with the profile prefix replaced by the Windows
variable that names it, the way a Linux path under your home starts with
`~`:

| Real path | Shown as |
| --- | --- |
| `/mnt/c/Users/<name>/AppData/Roaming/Claude/...` | `%APPDATA%/Claude/...` |
| `/mnt/c/Users/<name>/AppData/Local/Packages/...` | `%LOCALAPPDATA%/Packages/...` |
| `/mnt/c/Users/<name>/.codex/sessions/...` | `%USERPROFILE%/.codex/sessions/...` |
| `/mnt/c/Users/<name>/.claude/projects/C--Users-<name>-work/...` | `%USERPROFILE%/.claude/projects/C--Users-%USERNAME%-work/...` |

Agents also name project folders after the working directory, which can
carry an account name. Those encoded names are rewritten the same way:

| Encoded folder name | Shown as |
| --- | --- |
| `C--Users-<name>-work` (Windows profile) | `C--Users-%USERNAME%-work` |
| `-mnt-c-Users-<name>-work` (Linux side) | `-mnt-c-Users-%USERNAME%-work` |
| `-home-<user>-work` (Linux home) | `-~-work` |
| `%2Fhome%2F<user>%2Fwork` (Grok) | `~%2Fwork` |

The rewrite applies to text, JSON, NDJSON, the TUI, and MCP results. To reach
the real file:

- Opaque result refs open it.
- `find -a` prints real absolute paths in text output; JSON keeps the display
  form.

Two cases are left as is:

- A single-component home such as `/root` encodes to a common word.
- A repository whose own name contains the account name keeps its name.

A printed path works as a `path:` query, so the Windows-side results can be
selected or left out. `%APPDATA%` and `%LOCALAPPDATA%` sit inside the
profile, so `path:%USERPROFILE%` covers them too. Only the Windows side:

```console
$ agentgrep find 'path:%USERPROFILE%'
```

Everything except the Windows side:

```console
$ agentgrep search 'NOT path:%USERPROFILE% deploy'
```

Over MCP, the same predicates go in {tool}`search`'s `terms`, and a returned
`ref` opens the real file through {tool}`inspect_result`; see
{ref}`library-examples-mcp-windows`.

## Choosing the Windows users directory

The users directory defaults to `<automount root>/c/Users`, where the
automount root is `root` in the `[automount]` section of `/etc/wsl.conf`
(`/mnt/` when unset). Set `AGENTGREP_WSL_USERS_ROOT` when drive `C:` is
mounted elsewhere, or to search a different drive:

```console
$ AGENTGREP_WSL_USERS_ROOT=/mnt/d/Users agentgrep find --agent codex
```

Point it at a directory that does not exist to turn the Windows probe off.

## Copies of WSL sessions

Claude desktop runs a session that targets a WSL distro inside that
distro, so its transcript is written to the distro's `~/.claude`, and the
app keeps a copy under `%USERPROFILE%/.claude/projects/ssh-<id>/` on
Windows. agentgrep skips a Windows copy when a transcript with the same
file name is already in the local `~/.claude/projects`, so each session is
read once. Copies of sessions from other hosts are kept.

## Speed

The Windows drive is a 9p network mount, where every `stat` is a round trip.
The default prompt search reads only each profile's prompt-history files and
skips the desktop app directories. `find` and `--exhaustive` walk every
Windows-side transcript tree and overlap the `stat` calls on the mount. Off
WSL, none of this runs.

The figures below come from one WSL host where `find` lists 382 Windows-side
sources (most from a large Claude desktop install). "Before" is the main
branch, which runs 0.1.0a54's code with updated dependencies, measured in the
same session and harness run as "now". They answer three questions in turn: how many
round trips a discovery pass makes on the mount, whether whole commands got
slower, and how long an exhaustive search over the Windows side takes.

### Operations on the mount

One discovery pass, counted by wrapping `os.stat`, `os.scandir`, and `fd`
calls on paths under the mount, so the counts do not depend on machine load:

| Pass | Windows sources | `stat` calls | `stat` per source | Directory scans | `fd` runs |
| --- | --- | --- | --- | --- | --- |
| Prompt search, before | 1 | 39 | 39 | 2 | 0 |
| Prompt search, now | 1 | 36 | 36 | 5 | 0 |
| `find`, before | 103 | 327 | 3.2 | 51 | 0 |
| `find`, now | 382 | 653 | 1.7 | 66 | 10 |

Prompt search makes three fewer `stat` calls and three more directory scans.
All five scans list the Windows users directory, once for each backend that
probes it (Claude, Codex, Cursor CLI, VS Code, and Cursor IDE, where 0.1.0a54
probed only the last two); each is one round trip, and the `search --limit 1`
rows below show no slowdown from them.

`find` makes about half as many `stat` calls per source.

### Wall-clock time

The median of the whole command, with the range in brackets, from one
harness run of 5 samples per row after 2 warm-ups. The mount-hidden rows ran with an empty directory
bind-mounted over `/mnt/c`, standing in for a Linux machine. The host is
shared, so differences under about 10% are noise:

| Command | Before | Now |
| --- | --- | --- |
| `agentgrep find` | 4.06 s (3.78–4.47) | 4.02 s (2.74–4.10) |
| `agentgrep search --limit 1` | 1.77 s (1.63–2.36) | 1.29 s (1.20–1.33) |
| `agentgrep find`, mount hidden | 1.50 s (1.40–1.58) | 0.94 s (0.93–1.01) |
| `agentgrep search --limit 1`, mount hidden | 1.55 s (1.35–1.97) | 1.34 s (1.27–1.37) |

`find` reads 3.7 times as many Windows sources in the same time: its medians
differ by 0.04 s and its ranges overlap. No other row is slower. The two
larger gaps (`search` with the mount, `find` without it) come from that single
harness run on a busy machine, so they are not claimed as speedups. The {ref}`benchmark harness <benchmark>` reruns the
comparison against the main branch:

```console
$ uv run scripts/benchmark.py run \
    --head-vs-trunk \
    --commands find,search-limit-1
```

### Exhaustive search

An exhaustive search over the Windows-side Claude stores on the host above
reads about 865 sources (276 MB, after skipping the copies of WSL-distro
transcripts). Each figure is a single run on a shared machine:

- The first run, while the Windows side read the files from disk, took about
  100 s (at an earlier commit of this change, before ChatGPT title search).
- A warm run of this version took about 19 s.

### Cancelling or skipping the walk

A cancelled search stops the walk instead of finishing it in the background:

- The profile listing and any running directory listing end, and no new
  `stat` starts on the mount.
- Only the calls already in flight finish: at most 16, the size of the `stat`
  pool every search in the process shares. A single directory listing step
  cannot be interrupted, only the walk between steps.
- The same holds when an MCP client cancels {tool}`find`,
  {tool}`list_sources`, {tool}`filter_sources`, {tool}`summarize_discovery`,
  {tool}`recent_sessions`, {tool}`inspect_result`, or
  {tool}`inspect_record_sample`, or an `agentgrep://sources` read.

To leave the Windows side out entirely, point `AGENTGREP_WSL_USERS_ROOT` at a
missing directory. A `NOT path:%USERPROFILE%` filter drops the Windows-side
results but still walks the mount.

## Python API

{mod}`agentgrep.wsl` exposes the same lookups: {func}`~agentgrep.wsl.host_user_profiles`
lists the Windows profiles, and its path type is a {class}`pathlib.PosixPath`
whose `str()` is the real path while `repr()` and `.display` hide the account
name. {ref}`library-examples-wsl` has a runnable example.
