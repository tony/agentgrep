"""Server instructions shown to MCP clients on handshake."""

from __future__ import annotations


def _build_instructions() -> str:
    """Return server instructions for MCP clients."""
    return (
        "agentgrep is a read-only MCP server for local AI agent history search. "
        "Use `search` to retrieve full prompt/history matches and `find` to inspect "
        "discovered stores and session files. Search results are newest-first and "
        "duplicate prompts within the same session are collapsed. "
        "This server never mutates agent stores, never opens SQLite in write mode, "
        "and never executes arbitrary shell commands."
    )
