"""Sphinx extension registering agentgrep's custom Pygments lexers.

Wires :class:`agentgrep.query.pygments_lexer.AgentgrepQueryLexer` to the
``agentgrep-query`` code-block alias so MyST fences like ```` ```agentgrep-query ````
highlight query examples in the documentation.
"""

from __future__ import annotations

import typing as t

from agentgrep.query.pygments_lexer import AgentgrepQueryLexer

if t.TYPE_CHECKING:
    from sphinx.application import Sphinx


def setup(app: Sphinx) -> dict[str, t.Any]:
    """Register the agentgrep query lexer."""
    app.add_lexer("agentgrep-query", AgentgrepQueryLexer)
    return {
        "version": "0.1.0",
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
