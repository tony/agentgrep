#!/usr/bin/env bash
# Run a command against the synthetic demo home, with every store-discovery
# escape hatch neutralized.
#
# Setting HOME alone is NOT enough. On WSL, agentgrep auto-probes the Windows
# host's VS Code / Cursor data under /mnt/c/Users, which is independent of
# $HOME -- so a recording made without AGENTGREP_WSL_USERS_ROOT would leak real
# Copilot Chat transcripts. The per-agent *_HOME / *_CONFIG_DIR overrides would
# likewise escape the sandbox if they happen to be set in the ambient shell.
#
# Usage: docs/_static/demos/demo-env.sh agentgrep search deploy
set -euo pipefail

DEMO_HOME="/tmp/agentgrep-demo"

exec env \
  -u NO_COLOR \
  -u FORCE_COLOR \
  -u CODEX_HOME \
  -u CODEX_SQLITE_HOME \
  -u CLAUDE_CONFIG_DIR \
  -u GEMINI_CLI_HOME \
  -u GROK_HOME \
  -u PI_CODING_AGENT_DIR \
  -u PI_CODING_AGENT_SESSION_DIR \
  -u OPENCODE_DB \
  -u OPENCODE_CONFIG_DIR \
  -u VSCODE_APPDATA \
  HOME="$DEMO_HOME" \
  XDG_CONFIG_HOME="$DEMO_HOME/.config" \
  XDG_DATA_HOME="$DEMO_HOME/.local/share" \
  XDG_STATE_HOME="$DEMO_HOME/.local/state" \
  AGENTGREP_WSL_USERS_ROOT="$DEMO_HOME/no-windows-mount" \
  COLORTERM=truecolor \
  TERM=xterm-256color \
  PATH="$DEMO_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  PS1='$ ' \
  "$@"
