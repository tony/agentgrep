(backends-windows)=

# Windows and WSL

When agentgrep runs inside a WSL distro, it also searches the agents
installed on the Windows side. Claude desktop, Codex desktop, Cursor, and
VS Code keep their history under the Windows profile, which the distro sees
under `/mnt/c/Users/<name>/`. agentgrep finds each profile, adds its stores
to the same search, and prints the paths without the Windows account name.

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

The catalogue records each store's Windows path in its `win32` platform
variant, which `get_store_descriptor` and the backend pages show.

Two desktop apps keep nothing agentgrep can read. Claude desktop holds
claude.ai chats only as a Chromium cache, and ChatGPT desktop is a
Chromium profile on Windows and encrypted files on macOS; see
{doc}`unsupported/chatgpt`.

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

Agents name project folders after the working directory, so a project under
the profile carries the account name in its folder name; that part is shown as
`%USERNAME%`. The same rule covers the Linux side: the home directory inside
an encoded folder name prints as `~` (`-home-<user>-work` as `-~-work`,
Grok's `%2Fhome%2F<user>%2Fwork` as `~%2Fwork`), and a Windows account
encoded into a Linux-side folder (`-mnt-c-Users-<name>-…`) as `%USERNAME%`.

The rewrite applies to text, JSON, NDJSON, the TUI, and MCP results.
Opaque result refs still open the real file, and `find -a` prints real
absolute paths when a script needs them. Two limits: a single-component home
such as `/root` encodes to a common word and is left as is, and a
repository whose own name contains the account name keeps its name.

A printed path works as a `path:` query, so the Windows-side results can be
selected or left out. Only the Windows side:

```console
$ agentgrep find 'path:%USERPROFILE%* OR path:%APPDATA%*'
```

Everything except the Windows side:

```console
$ agentgrep search 'NOT path:%USERPROFILE%* NOT path:%APPDATA%* deploy'
```

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
skips the desktop app directories, so it does the same work as before. `find`
and `--exhaustive` walk every Windows-side transcript tree and overlap the
`stat` calls on the mount. Off WSL, none of this runs.

The figures below come from one WSL host with a large Claude desktop install
and compare 0.1.0a54 ("before") with this version. The host is shared, so
treat timing differences under about 10% as noise.

Filesystem operations on the Windows mount for one discovery pass, counted by
wrapping `os.stat`, `os.scandir`, and `fd` calls on paths under the mount:

| Pass | Windows sources | `stat` calls | `stat` per source | Directory scans | `fd` runs |
| --- | --- | --- | --- | --- | --- |
| Prompt search, before | 1 | 39 | 39 | 2 | 0 |
| Prompt search, now | 1 | 36 | 36 | 5 | 0 |
| `find`, before | 103 | 327 | 3.2 | 51 | 0 |
| `find`, now | 376 | 641 | 1.7 | 65 | 10 |

Wall-clock time of the whole command, median of 5 runs after 2 warm-ups:

| Command | Before | Now |
| --- | --- | --- |
| `agentgrep find` | 2.86 s | 2.96 s |
| `agentgrep search --limit 1` | 1.34 s | 1.32 s |

`find` reads 3.6 times as many Windows sources for about 0.1 s more. The
{ref}`benchmark harness <benchmark>` reruns the timing comparison against the
main branch:

```console
$ uv run scripts/benchmark.py run \
    --head-vs-trunk \
    --commands find,search-limit-1
```

A cancelled search stops the walk, including a directory listing in
progress, instead of finishing it in the background. Point
`AGENTGREP_WSL_USERS_ROOT` at a missing directory to leave the Windows side
out.

## Python API

{mod}`agentgrep.wsl` exposes the same lookups: {func}`~agentgrep.wsl.host_user_profiles`
lists the Windows profiles, and its path type is a {class}`pathlib.PosixPath`
whose `str()` is the real path while `repr()` and `.display` hide the account
name. {ref}`library-examples-wsl` has a runnable example.
