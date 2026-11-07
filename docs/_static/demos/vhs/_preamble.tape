# Shared VHS settings for every agentgrep demo tape.
#
# Env vars mirror docs/_static/demos/demo-env.sh: the recordings run against
# the synthetic home from docs/_static/demos/seed_demo_home.py, never the real
# one. AGENTGREP_WSL_USERS_ROOT is load-bearing -- without it the WSL
# /mnt/c/Users probe reaches the Windows host's real VS Code chat history,
# which is outside $HOME.

Set Shell "bash"
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms
Set WaitPattern /\$\s*$/
Set WaitTimeout 30s
Set Padding 20

Env HOME "/tmp/agentgrep-demo"
Env CODEX_HOME ""
Env CODEX_SQLITE_HOME ""
Env CLAUDE_CONFIG_DIR ""
Env GEMINI_CLI_HOME ""
Env GROK_HOME ""
Env PI_CODING_AGENT_DIR ""
Env PI_CODING_AGENT_SESSION_DIR ""
Env OPENCODE_DB ""
Env OPENCODE_CONFIG_DIR ""
Env VSCODE_APPDATA ""
Env XDG_CONFIG_HOME "/tmp/agentgrep-demo/.config"
Env XDG_DATA_HOME "/tmp/agentgrep-demo/.local/share"
Env XDG_STATE_HOME "/tmp/agentgrep-demo/.local/state"
Env AGENTGREP_WSL_USERS_ROOT "/tmp/agentgrep-demo/no-windows-mount"
Env COLORTERM "truecolor"
Env TERM "xterm-256color"
Env PATH "/tmp/agentgrep-demo/.local/bin:/usr/local/bin:/usr/bin:/bin"
Env PS1 "$ "
Env AGENTGREP_DEMO "1"
