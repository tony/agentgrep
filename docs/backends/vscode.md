(backend-vscode)=

# VS Code (GitHub Copilot Chat)

Base path: `~/.config/Code/User` on Linux
(`~/Library/Application Support/Code/User` on macOS,
`%APPDATA%/Code/User` on Windows). Env overrides: `VSCODE_APPDATA`,
`AGENTGREP_WSL_USERS_ROOT`.

`observed_version`: `VS Code GitHub Copilot Chat (chatSessions v3)`
(observed 2026-06-21).

VS Code's built-in GitHub Copilot Chat persists conversations client-side
as readable JSON under the workbench `User/` directory — one transcript
per session. This makes VS Code a JSON-transcript backend, and unlike
Windsurf's encrypted conversation blobs, the transcripts are plain text
agentgrep can read directly. Editions are covered side by side: stable
`Code`, `Code - Insiders`, `VSCodium`, and `Code - OSS` all share the same
layout under their own `User/` directory.

## Stores

```{storage:agent} vscode
```

## Record schema

### vscode.chat_sessions

Each session is one JSON object. Per-workspace transcripts live under
`workspaceStorage/<hash>/chatSessions/<uuid>.json`; sessions opened
without a folder live under `globalStorage/emptyWindowChatSessions/`.
The turn list is `requests[]`:

| Field | Record |
|-------|--------|
| `message.text` | User prompt (`kind=prompt`, `role=user`) |
| `response[]` parts with no `kind` | Assistant reply (`kind=history`, `role=assistant`), `value`s joined |
| `result.metadata.toolCallRounds[].toolCalls[].name` | `tools` metadata |
| `timestamp` | Turn time (Unix milliseconds, normalized to ISO-8601) |
| `sessionId` | `session_id` / `conversation_id` |

The assistant reply is reconstructed from the bare `MarkdownString`
response parts (shape `{value, supportHtml, supportThemeIcons}`, no
`kind`); tool-invocation, inline-reference, progress, and warning parts
are skipped. User prompts participate in the default prompt scope;
assistant text requires `--scope conversations` or `--scope all`. VS Code
does not publish a formal schema, so agentgrep's parser is the reference
implementation; a forward-compatible `markdownContent` response kind and a
per-turn `modelId` are read when a newer file carries them.

### vscode.inline_history

The workbench `globalStorage/state.vscdb` SQLite database has an
`inline-chat-history` key in its `ItemTable` holding a JSON array of the
user's Ctrl+I inline-edit prompts. agentgrep reads that key alone
(token-filtered in SQL), so the `secret://…` auth keys in the same
database are never enumerated.

## Resolving the project directory

A chat transcript's sibling `workspace.json` records the opened folder as
a URI. agentgrep resolves it to a local path and attaches it as the
record's `cwd` metadata: a `file://` URI is unquoted, and a
`vscode-remote://wsl+<distro>/<path>` remote maps to the Linux path
`<path>`. So a Copilot Chat in a WSL-remote workspace reports its real
project directory (for example `/home/you/work/proj`) rather than an
opaque storage hash.

## Cross-host discovery on WSL

When VS Code's UI runs on a Windows host and edits a project inside WSL,
the chat is written client-side on Windows under
`/mnt/c/Users/<user>/AppData/Roaming/Code/User`, not inside the distro.
On WSL, agentgrep detects this and also probes the Windows users mount so
those transcripts are searchable from Linux. `AGENTGREP_WSL_USERS_ROOT`
overrides the mount root (default `/mnt/c/Users`) for non-default drive
letters, and `VSCODE_APPDATA` pins a single `Roaming` directory when you
want to target one install. See {doc}`../dev/adr/0009-cross-host-discovery`
for the discovery and remote-URI mapping design.
