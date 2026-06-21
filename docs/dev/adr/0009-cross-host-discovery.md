(adr-cross-host-discovery)=

# ADR 0009: Cross-host discovery and remote-workspace path mapping

## Status

Accepted.

## Context

Every agentgrep backend before VS Code resolves its stores from a single
filesystem rooted at the user's home directory. Discovery functions take a
`home` path and either expand `~`-anchored `platform_paths` or append
`home_subpath` segments to it. Tests rely on this: they point `$HOME` (or the
passed `home`) at a temporary directory and trust that no discovery escapes it.

VS Code's GitHub Copilot Chat breaks that single-root assumption in two ways.

First, the chat client stores transcripts wherever the *VS Code UI process*
runs, which is not always where the *project* lives. On WSL, the common setup
is a Windows-host VS Code editing a Linux project over a
`vscode-remote://wsl+<distro>/…` remote. The UI is a Windows process, so it
writes chat under the Windows profile —
`C:\Users\<user>\AppData\Roaming\Code\User` — which a WSL distro sees at
`/mnt/c/Users/<user>/AppData/Roaming/Code/User`. The Linux home directory holds
no chat at all. A home-rooted discovery function finds nothing.

Second, once a transcript is found, its on-disk location is an opaque
`workspaceStorage/<md5>/` hash. The human-meaningful project directory is
recorded separately, in the sibling `workspace.json`, as a URI — and for the
WSL case that URI is a `vscode-remote://` remote, not a local path. Without
mapping it, results cannot report which project a chat belonged to, and
`path:` predicates cannot match the project the user thinks in.

No existing backend needed either capability: `rg 'wsl|/mnt/c|vscode-remote'`
over `src/agentgrep` was empty before this work, and `sys.platform` reports
`linux` inside WSL, so nothing distinguished it from native Linux.

## Decision

agentgrep discovers VS Code chat across the host boundary, and maps remote
workspace URIs back to local paths, while keeping single-root home discovery
the default for every other backend.

### Discovery roots are computed, not just declared

`discover_vscode_sources` resolves a tuple of candidate `User/` directories
across editions (`Code`, `Code - Insiders`, `VSCodium`, `Code - OSS`) and
operating systems, then injects them into the catalogue's declarative
`DiscoverySpec` rows through named discovery roots (`vscode_workspace`,
`vscode_global`). This keeps the catalogue's globs and adapters declarative
while letting discovery contribute roots the catalogue cannot name statically.

### The WSL bridge is auto-detected and bounded

When discovery detects WSL — `/proc/version` contains `microsoft` — it also
probes the Windows users mount (`/mnt/c/Users/*/AppData/Roaming/<edition>/User`)
for chat written host-side. The probe is gated on WSL detection and directory
existence, so native Linux and macOS pay nothing. Native Windows host runs are
out of scope; WSL is the supported cross-host case.

### The cross-host root is an explicit, overridable seam

The Windows users mount is read from `AGENTGREP_WSL_USERS_ROOT` (default
`/mnt/c/Users`) so non-default drive letters and unusual mounts are reachable,
and `VSCODE_APPDATA` pins a single `Roaming` directory when one install should
be targeted. Because this root is independent of `$HOME`, it is the one
discovery input a temporary `$HOME` does not neutralize, so the test suite
points it at a nonexistent path by default and overrides it only where the
bridge is under test.

### Remote workspace URIs map to local paths

A transcript's project directory is resolved from its sibling `workspace.json`
`folder` URI: `file://` URIs are unquoted, and
`vscode-remote://wsl+<distro>/<path>` (and other remotes, best-effort) reduce
to their path component. The result is attached as the record's `cwd` metadata
so a WSL-remote chat reports `/home/you/work/proj` rather than a storage hash.

## Scope

This ADR governs cross-host store discovery and remote-workspace path mapping.
It applies to the VS Code backend today and to any future backend whose UI and
project live on different filesystems. It does not change the execution engine
(ADR 0004), introduce native code (ADR 0003), or alter the catalogue schema —
the catalogue stays declarative; only the discovery function computes roots.

## Requirements

- Cross-host discovery activates only on detected WSL and only for existing
  directories; other platforms incur no extra filesystem probes.
- The cross-host root is overridable (`AGENTGREP_WSL_USERS_ROOT`) and a single
  install is pinnable (`VSCODE_APPDATA`); both are declared in the affected
  catalogue rows' `env_overrides`.
- Remote-URI mapping handles `file://` and `vscode-remote://…` and returns
  `None` for non-path URIs (for example `untitled:`), never raising.
- Because the cross-host root escapes `$HOME`, the test harness neutralizes it
  by default so hermetic `find` tests never read the developer's real chat
  history; a dedicated test exercises the bridge against a synthetic mount.

## Consequences

### Positive

- VS Code Copilot Chat written by a Windows host is searchable from inside WSL,
  with results that name the real Linux project directory.
- The catalogue stays declarative; only one discovery function holds the
  cross-host knowledge, so other backends are unaffected.
- The override seam doubles as test isolation and as a real knob for
  non-default mounts.

### Tradeoffs and risks

- Globbing `workspaceStorage` over a mounted filesystem is slower than a local
  scan; the WSL-detection and existence gates keep the cost off non-WSL hosts,
  and `VSCODE_APPDATA` narrows a multi-user mount to one install.
- A monkeypatched `$HOME` no longer fully sandboxes discovery for this backend.
  The `AGENTGREP_WSL_USERS_ROOT` seam restores isolation, but it is a second
  thing a test must control; the autouse default makes that the norm rather
  than per-test boilerplate.

## Relationship to other ADRs

ADR 0001 owns version detection and the privacy rule that auth material is
documented but never enumerated; the VS Code inline-history adapter reads only
the `inline-chat-history` key and leaves the `secret://…` keys in the same
database untouched. ADR 0003 owns the native boundary, which this ADR does not
touch — discovery stays pure Python. ADR 0004 owns planning and execution,
which are unchanged: cross-host roots are resolved during discovery and flow
through the existing source/handle pipeline.

## Final position

agentgrep keeps home-rooted single-filesystem discovery as the default and adds
a bounded, auto-detected, overridable bridge for the one case that needs it —
WSL editing where the chat lives on the Windows host — together with the
remote-URI mapping that makes those results name a real project directory.
