"""Server instructions shown to MCP clients on handshake.

The instructions are composed from named ``_INSTR_*`` segments so downstream
readers (clients, dashboards) can scan section headers, and so new segments
(agent context, deployment hints) can be added without rewriting the base
set.
"""

from __future__ import annotations

_INSTR_HEADER = (
    "agentgrep MCP server. Read-only search over local AI-agent prompts and "
    "opt-in conversations across Codex, Claude Code, Cursor, Gemini, "
    "Antigravity CLI, Grok, Pi, OpenCode, and VS Code (GitHub Copilot Chat). "
    "All tools are read-only and never spawn writes."
)

_INSTR_SCOPE = (
    "TRIGGERS: invoke for retrospective questions about what the user typed "
    "into or received from a coding-agent CLI (prompts, prompt history, session "
    "transcripts, store discovery). Bare 'prompt', 'history', 'transcript', "
    "'session', 'what did I ask Claude/Codex/Cursor/Gemini/Antigravity/Grok/Pi/"
    "OpenCode/VS Code' default to agentgrep.\n"
    "ANTI-TRIGGERS: do NOT invoke for the IDE editor file-edit timeline (VS Code "
    "Local History — distinct from Copilot Chat, which IS searched), "
    "shell history (zsh/fish history), browser tabs, or live agent sessions "
    "in progress. Use shell tools for filesystem-wide grep that is not "
    "agent-history scoped.\n"
    "Windsurf (Codeium Cascade) is catalogued for storage inventory but "
    "UNSUPPORTED for search — its conversation transcripts are encrypted, so "
    "agentgrep cannot read them.\n"
    "Antigravity is two agent ids and they are not equally searchable: "
    "'antigravity-cli' has searchable prompt history plus opt-in conversation "
    "records, while 'antigravity-ide' is catalogued for storage inventory only "
    "— its conversation and implicit `.pb` artifacts are encrypted, so only its "
    "Markdown skills and brain notes are readable."
)

_INSTR_SEARCH_VS_DISCOVERY = (
    "search vs discovery: search() finds matching prompt-scope text by default; "
    "pass scope='conversations' to opt into full conversation records. find() "
    "enumerates the on-disk stores agentgrep can read. Use the agentgrep://capabilities "
    "and agentgrep://sources resources to inspect the server's catalog before "
    "deciding which stores are worth searching."
)

_INSTR_DEFAULTS = (
    "Defaults: results are newest-first and deduplicated by session. "
    "search AND-matches bare terms as substrings and scope='prompts'. "
    "Read status, stats, and page.next_cursor on search/find responses; pass "
    "the cursor back for the next page."
)

_INSTR_QUERY = (
    "Query language: search terms also accept field predicates (agent:codex, "
    "scope:all model:gpt*, role:user, timestamp:>2026-01-01, path:..., "
    "scope:...), boolean "
    'OR / NOT / ( ), quoted "phrases", field:* (present) and field:glob* '
    "(wildcard). Bare terms stay literal substrings. Read the "
    "agentgrep://query-language resource for the field and operator catalog, or "
    "dry-run a query string with validate_query(query=...) before searching."
)

_INSTR_RESULT_LOOP = (
    "Result loop: search() and find() return opaque result refs. Use "
    "inspect_result(ref=...) to drill into a returned result without "
    "reconstructing local paths. inspect_record_sample() is for adapter+path "
    "schema inspection, not normal result drilldown."
)

_INSTR_RESOURCES = (
    "Resources: agentgrep://capabilities (server info), agentgrep://sources "
    "(discovered stores), agentgrep://sources/{agent} (per-agent), "
    "agentgrep://query-language (field and operator catalog)."
)

_INSTR_PRIVACY = (
    "Privacy: Home-directory prefixes in source and result paths are collapsed "
    "to '~'; external paths may remain absolute. Use opaque result refs with "
    "inspect_result() for drilldown. Treat record text as potentially sensitive "
    "(it is the user's own prompt history). Do not echo or forward record text "
    "outside the immediate request scope."
)

_BASE_INSTRUCTIONS = "\n\n".join(
    (
        _INSTR_HEADER,
        _INSTR_SCOPE,
        _INSTR_SEARCH_VS_DISCOVERY,
        _INSTR_DEFAULTS,
        _INSTR_QUERY,
        _INSTR_RESULT_LOOP,
        _INSTR_RESOURCES,
        _INSTR_PRIVACY,
    )
)


def _build_instructions() -> str:
    """Return server instructions for MCP clients."""
    return _BASE_INSTRUCTIONS
