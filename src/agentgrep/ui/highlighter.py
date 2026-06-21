"""Live query-syntax highlighting for the Textual explorer's inputs.

Textual's :class:`textual.widgets.Input` applies a :class:`rich.highlighter.Highlighter`
to its value on every keystroke (via ``Input._value``). :class:`QueryHighlighter`
colors the typed query — field names, ``:``, values, ``*`` / ``?`` wildcards,
``AND`` / ``OR`` / ``NOT`` / ``TO``, the ``-`` / ``+`` negation sigil, comparison
operators, and ``"phrases"`` — reusing :func:`agentgrep.highlight_query_spans`,
the same grammar the CLI ``--help`` highlighter uses, so the two never drift.

Concrete Rich 256-color styles (matching the CLI Design-A palette) are applied
by offset rather than theme style-names, so no Textual theme registration is
needed.
"""

from __future__ import annotations

import typing as t

from rich.highlighter import Highlighter

from agentgrep._text import highlight_query_spans

if t.TYPE_CHECKING:
    from rich.text import Text

# Semantic role -> concrete Rich style. Mirrors the CLI Design-A palette
# (see ``AnsiHelpTheme.default``): teal field, dim-grey punctuation, near-fg
# value, amber keyword/operator, gold wildcard, rose negation. ``date`` shares
# the value hue (Design A). ``whitespace`` and ``phrase`` are handled inline.
_ROLE_STYLES: dict[str, str] = {
    "field": "color(79)",
    "keyword": "bold color(215)",
    "operator": "color(215)",
    "wildcard": "bold color(222)",
    "negation": "bold color(204)",
    "punct": "color(245)",
    "value": "color(252)",
    "date": "color(252)",
}
_PHRASE_DELIM_STYLE = "color(245)"
_PHRASE_TEXT_STYLE = "color(252)"


class QueryHighlighter(Highlighter):
    """Highlight agentgrep query syntax live in a Textual ``Input``."""

    def highlight(self, text: Text) -> None:
        """Apply query-syntax styles to ``text`` in place.

        Parameters
        ----------
        text : rich.text.Text
            The input's current value; styled by offset span.
        """
        plain = text.plain
        for start, role, token in highlight_query_spans(plain):
            if role == "whitespace":
                continue
            end = start + len(token)
            if role == "phrase":
                text.stylize(_PHRASE_DELIM_STYLE, start, start + 1)
                if end - start > 2:
                    text.stylize(_PHRASE_TEXT_STYLE, start + 1, end - 1)
                    text.stylize(_PHRASE_DELIM_STYLE, end - 1, end)
                continue
            style = _ROLE_STYLES.get(role)
            if style is not None:
                text.stylize(style, start, end)
