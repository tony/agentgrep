"""Live query-syntax highlighting for the Textual explorer's inputs.

Textual's :class:`textual.widgets.Input` applies a :class:`rich.highlighter.Highlighter`
to its value on every keystroke (via ``Input._value``). :class:`QueryHighlighter`
colors the typed query — field names, ``:``, values, ``*`` / ``?`` wildcards,
``AND`` / ``OR`` / ``NOT`` / ``TO``, the ``-`` / ``+`` negation sigil, comparison
operators, and ``"phrases"`` — reusing :func:`agentgrep.highlight_query_spans`,
the same grammar the CLI ``--help`` highlighter uses, so the two never drift.

Concrete Rich styles are applied by offset because Rich highlighters cannot
resolve Textual theme variables. The dark palette preserves the CLI Design-A
hues; a separate light palette keeps every syntax role readable.
"""

from __future__ import annotations

import collections.abc as cabc
import typing as t

from rich.highlighter import Highlighter

from agentgrep._text import highlight_query_spans

if t.TYPE_CHECKING:
    from rich.text import Text

# Semantic role -> concrete Rich style. The dark map mirrors the CLI Design-A
# (see ``AnsiHelpTheme.default``): teal field, dim-grey punctuation, near-fg
# value, amber keyword/operator, gold wildcard, rose negation. ``date`` shares
# the value hue (Design A). ``whitespace`` and ``phrase`` are handled inline.
_DARK_ROLE_STYLES: dict[str, str] = {
    "field": "color(79)",
    "keyword": "bold color(215)",
    "operator": "color(215)",
    "wildcard": "bold color(222)",
    "negation": "bold color(204)",
    "punct": "color(245)",
    "value": "color(252)",
    "date": "color(252)",
}
_LIGHT_ROLE_STYLES: dict[str, str] = {
    "field": "#007f7f",
    "keyword": "bold #502000",
    "operator": "#502000",
    "wildcard": "bold #000080",
    "negation": "bold #9b2242",
    "punct": "#202020",
    "value": "#202020",
    "date": "#008000",
}


class QueryHighlighter(Highlighter):
    """Highlight agentgrep query syntax live in a Textual ``Input``."""

    def __init__(
        self,
        *,
        dark: bool = True,
        theme_variables: cabc.Mapping[str, str] | None = None,
    ) -> None:
        """Initialize the highlighter for a dark or light canvas.

        Parameters
        ----------
        dark : bool
            Whether to select the dark-canvas syntax palette.
        theme_variables : collections.abc.Mapping[str, str] | None
            Concrete semantic query tokens for an owned profile.
        """
        self.set_theme(dark=dark, theme_variables=theme_variables)

    def set_theme(
        self,
        *,
        dark: bool = True,
        theme_variables: cabc.Mapping[str, str] | None = None,
    ) -> None:
        """Select the concrete syntax-role palette for the active theme.

        Parameters
        ----------
        dark : bool
            Whether the active theme uses a dark canvas.
        theme_variables : collections.abc.Mapping[str, str] | None
            Concrete semantic query tokens, or ``None`` for polarity fallback.
        """
        fallback = _DARK_ROLE_STYLES if dark else _LIGHT_ROLE_STYLES
        if theme_variables is None:
            self._role_styles = fallback
            return
        self._role_styles = {
            role: self._profile_style(role, theme_variables, fallback[role]) for role in fallback
        }

    @staticmethod
    def _profile_style(
        role: str,
        variables: cabc.Mapping[str, str],
        fallback: str,
    ) -> str:
        """Return one Rich style backed by a semantic query token."""
        color = variables.get(f"ag-query-{role}")
        if not color:
            return fallback
        return f"bold {color}" if role in {"keyword", "wildcard", "negation"} else color

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
                text.stylize(self._role_styles["punct"], start, start + 1)
                if end - start > 2:
                    text.stylize(self._role_styles["value"], start + 1, end - 1)
                text.stylize(self._role_styles["punct"], end - 1, end)
                continue
            style = self._role_styles.get(role)
            if style is not None:
                text.stylize(style, start, end)
