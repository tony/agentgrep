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

| Agent | Windows location | Searched as |
| --- | --- | --- |
| Claude Code (run by Claude desktop) | `%USERPROFILE%/.claude/` | {doc}`claude` transcripts |
| Claude desktop Cowork | `%APPDATA%/Claude/local-agent-mode-sessions/` | {doc}`claude` transcripts |
| Claude desktop Code tab | `%APPDATA%/Claude/claude-code-sessions/` | session titles, opt-in |
| Codex desktop | `%USERPROFILE%/.codex/` | {doc}`codex` sessions and SQLite state |
| Cursor agent | `%USERPROFILE%/.cursor/projects/` | {doc}`cursor-cli` transcripts |
| Cursor IDE | `%APPDATA%/Cursor/User/` | {doc}`cursor-ide` state databases |
| VS Code Copilot Chat | `%APPDATA%/Code/User/` | {doc}`vscode` chat sessions |

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
`%USERNAME%`.

The rewrite applies to text, JSON, NDJSON, the TUI, and MCP results.
Opaque result refs still open the real file.

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

The Windows drive is a 9p network mount and reads over it are slow. The
default prompt search adds only each profile's prompt-history files
(`.claude/history.jsonl`, `.codex/history.jsonl`, and Cursor's
`.config/cursor/prompt_history.json`) and skips the desktop app directories, so on a machine where those files are absent it pays a
few directory checks. `find` and `--exhaustive` walk every Windows-side
transcript tree as well; on a large Claude desktop install that adds
seconds to `find` and can add minutes to an exhaustive search. Point
`AGENTGREP_WSL_USERS_ROOT` at a missing directory to leave the Windows side
out.

## Python API

{mod}`agentgrep.wsl` exposes the same lookups: {func}`~agentgrep.wsl.host_user_profiles`
lists the Windows profiles, and its path type is a {class}`pathlib.PosixPath`
whose `str()` is the real path while `repr()` and `.display` hide the account
name. {ref}`library-examples-wsl` has a runnable example.
