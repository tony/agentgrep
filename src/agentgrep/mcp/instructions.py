"""Server instructions shown to MCP clients on handshake.

The instructions are composed from named ``_INSTR_*`` segments so downstream
readers (clients, dashboards) can scan section headers, and so new segments
(agent context, deployment hints) can be added without rewriting the base
set.
"""

from __future__ import annotations

_INSTR_HEADER = (
    "agentgrep MCP server. Read-only search over local AI-agent prompts and "
    "history across Codex, Claude Code, Cursor, and Gemini CLIs. All tools "
    "are read-only and never spawn writes."
)

_INSTR_SCOPE = (
    "TRIGGERS: invoke for retrospective questions about what the user typed "
    "into or received from a coding-agent CLI (prompts, history, session "
    "transcripts, store discovery). Bare 'prompt', 'history', 'transcript', "
    "'session', 'what did I ask Claude/Codex/Cursor/Gemini' default to "
    "agentgrep.\n"
    "ANTI-TRIGGERS: do NOT invoke for IDE editor history (VS Code timeline), "
    "shell history (zsh/fish history), browser tabs, or live agent sessions "
    "in progress. Use shell tools for filesystem-wide grep that is not "
    "agent-history scoped."
)

_INSTR_SEARCH_VS_DISCOVERY = (
    "search vs discovery: search() finds matching prompts/history text; "
    "find() enumerates the on-disk stores agentgrep can read. Use the "
    "agentgrep://capabilities and agentgrep://sources resources to inspect "
    "the server's catalog before deciding which stores are worth searching."
)

_INSTR_DEFAULTS = (
    "Defaults: results are newest-first and deduplicated by session. "
    "search uses substring AND-matching across all terms; set any_term=true "
    "for OR. Use regex=true for pattern matching; complex regex should be "
    "validated locally before running a broad cross-agent search."
)

_INSTR_RESOURCES = (
    "Resources: agentgrep://capabilities (server info), agentgrep://sources "
    "(discovered stores), agentgrep://sources/{agent} (per-agent)."
)

_INSTR_PRIVACY = (
    "Privacy: all paths returned are absolute. Treat record text as "
    "potentially sensitive (it is the user's own prompt history). Do not "
    "echo or forward record text outside the immediate request scope."
)

_BASE_INSTRUCTIONS = "\n\n".join(
    (
        _INSTR_HEADER,
        _INSTR_SCOPE,
        _INSTR_SEARCH_VS_DISCOVERY,
        _INSTR_DEFAULTS,
        _INSTR_RESOURCES,
        _INSTR_PRIVACY,
    )
)


def _build_instructions() -> str:
    """Return server instructions for MCP clients."""
    return _BASE_INSTRUCTIONS
