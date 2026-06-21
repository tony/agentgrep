"""Pygments lexer for the agentgrep query language.

Used by the docs (Sphinx/MyST) so ```` ```agentgrep-query ```` code blocks are
syntax-highlighted by whatever Pygments style the docs theme selects — the
query blocks then match the rest of the documentation's code blocks and adapt
to light/dark mode.

The lexer reuses :func:`agentgrep.highlight_query_spans` — the same grammar the
CLI ``--help`` and the Textual TUI highlight with — and maps each shared role to
a standard Pygments token type, so the three surfaces never drift. It is not
imported by the agentgrep runtime (CLI/TUI/MCP); it is a docs/test-time helper,
so :mod:`pygments` need not be a runtime dependency.
"""

from __future__ import annotations

import typing as t

from pygments.lexer import Lexer
from pygments.token import (
    Keyword,
    Literal,
    Name,
    Operator,
    Punctuation,
    String,
    Text,
)

from agentgrep._text import highlight_query_spans

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from pygments.token import _TokenType

__all__ = ["AgentgrepQueryLexer"]

# Shared highlight role -> standard Pygments token type. Standard tokens keep
# the docs themeable (the active Pygments style colors them).
_ROLE_TOKENS: dict[str, _TokenType] = {
    "whitespace": Text.Whitespace,
    "field": Name.Attribute,
    "keyword": Keyword,
    "operator": Operator,
    "wildcard": Operator,
    "negation": Operator,
    "punct": Punctuation,
    "date": Literal.Date,
    "phrase": String.Double,
    "value": Text,
}


class AgentgrepQueryLexer(Lexer):
    """Highlight agentgrep query syntax (``agent:codex model:gpt*``)."""

    # Pygments' ``Lexer`` declares these as plain (non-ClassVar) attributes, so
    # they are matched here rather than annotated ``ClassVar`` (which would
    # violate LSP per ty). ``filenames`` / ``mimetypes`` keep the base ``[]``.
    name = "agentgrep query"
    aliases = ["agentgrep-query"]  # noqa: RUF012 -- matches Pygments Lexer base
    url = "https://agentgrep.org/library/query-language.html"

    def get_tokens_unprocessed(
        self,
        text: str,
    ) -> cabc.Iterator[tuple[int, _TokenType, str]]:
        """Yield ``(index, token_type, value)`` for each query span.

        Delegates tokenization to :func:`agentgrep.highlight_query_spans` so the
        token boundaries are identical to the CLI and TUI highlighters.
        """
        for start, role, value in highlight_query_spans(text):
            yield start, _ROLE_TOKENS.get(role, Text), value
