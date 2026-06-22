"""argparse help-output theming for the agentgrep CLI.

The syntax-colored ``--help`` formatter, its themed-subclass factory, and the
named-tuple theme. Depends on the text helpers (query highlighting, inline-code
regex) and the HelpTheme protocol; it is CLI-only and nothing below the CLI
layer imports it.
"""

from __future__ import annotations

import argparse
import re
import sys
import typing as t

from agentgrep._text import (
    INLINE_CODE_RE,
    QUERY_FIELD_TOKEN_RE,
    SHELL_TOKEN_RE,
    highlight_query_spans,
    should_enable_color,
)

if t.TYPE_CHECKING:
    from agentgrep._types import HelpTheme
    from agentgrep.records import ColorMode

__all__ = [
    "OPTIONS_EXPECTING_VALUE",
    "OPTIONS_FLAG_ONLY",
    "AgentGrepHelpFormatter",
    "AnsiHelpTheme",
    "create_themed_formatter",
    "should_enable_help_color",
]


OPTIONS_EXPECTING_VALUE: frozenset[str] = frozenset(
    {
        "--agent",
        "--scope",
        "--type",
        "--limit",
        "--color",
        "--progress",
        "--threshold",
        "-t",
        "-e",
    },
)

OPTIONS_FLAG_ONLY: frozenset[str] = frozenset(
    {
        "-h",
        "--help",
        "--case-sensitive",
        "--json",
        "--ndjson",
        "--ui",
    },
)


class AnsiHelpTheme(t.NamedTuple):
    """ANSI theme values for syntax-colored help examples.

    The ``query_*`` entries syntax-highlight a query predicate down to its
    parts so ``model:gpt*`` reads as field / ``:`` / value / wildcard, each in
    its own color. They use a 256-color sub-palette kept off the basic-color
    chrome above, so a predicate never shares a hue with a heading, option, or
    subcommand on the same line. Shell quotes around a query render plain.
    """

    heading: str
    reset: str
    label: str
    long_option: str
    short_option: str
    prog: str
    action: str
    inline_code: str
    query_keyword: str
    query_operator: str
    query_field: str
    query_punct: str
    query_value: str
    query_wildcard: str
    query_negation: str

    @classmethod
    def default(cls) -> AnsiHelpTheme:
        """Return the default help theme."""
        return cls(
            heading="\x1b[1;36m",
            reset="\x1b[0m",
            label="\x1b[33m",
            long_option="\x1b[32m",
            short_option="\x1b[32m",
            prog="\x1b[1;35m",
            action="\x1b[36m",
            inline_code="\x1b[1;34m",
            query_keyword="\x1b[1;38;5;215m",  # bold amber — AND / OR / NOT / TO
            query_operator="\x1b[38;5;215m",  # amber — > >= < <=
            query_field="\x1b[38;5;79m",  # teal
            query_punct="\x1b[38;5;245m",  # dim grey — : ( ) [ ] { }
            query_value="\x1b[38;5;252m",  # near-foreground — values + terms
            query_wildcard="\x1b[1;38;5;222m",  # bold gold — * ?
            query_negation="\x1b[1;38;5;204m",  # bold rose — leading - / +
        )


def should_enable_help_color(color_mode: ColorMode) -> bool:
    """Return whether help output should use colors."""
    return should_enable_color(color_mode, sys.stdout)


def create_themed_formatter(color_mode: ColorMode) -> type[AgentGrepHelpFormatter]:
    """Create a formatter class with a bound theme."""
    theme = AnsiHelpTheme.default() if should_enable_help_color(color_mode) else None

    class ThemedAgentGrepHelpFormatter(AgentGrepHelpFormatter):
        """AgentGrepHelpFormatter with a configured theme."""

        _agentgrep_help_theme: object | None

        def __init__(
            self,
            prog: str,
            indent_increment: int = 2,
            max_help_position: int = 24,
            width: int | None = None,
            *,
            color: bool = True,
        ) -> None:
            super().__init__(
                prog,
                indent_increment=indent_increment,
                max_help_position=max_help_position,
                width=width,
                color=color,
            )
            self._agentgrep_help_theme = theme

    return ThemedAgentGrepHelpFormatter


class AgentGrepHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Extend help output with syntax-colored example sections.

    The theme is held on ``_agentgrep_help_theme`` rather than ``_theme``
    on purpose: Python 3.14's ``argparse.HelpFormatter`` owns ``_theme``
    for its native usage/option coloring and re-sets it from
    ``_get_formatter`` via ``_set_color`` after construction, so a binding
    on ``_theme`` would be clobbered (and its namedtuple has no ``query``
    field). Keeping our theme on a private name lets argparse color the
    usage/options sections while we color the description's examples and
    inline code with :class:`AnsiHelpTheme`.
    """

    _agentgrep_help_theme: object | None = None

    @t.override
    def _fill_text(self, text: str, width: int, indent: str) -> str:
        """Style ``examples:`` blocks and strip RST inline-code backticks.

        Backtick stripping (an RST inline-code span renders as bare
        ``search``) is unconditional so piped/no-color help never shows
        literal double-backticks; coloring is applied only when a theme is
        bound.
        """
        if not text:
            return super()._fill_text(text, width, indent)
        theme = t.cast("HelpTheme | None", getattr(self, "_agentgrep_help_theme", None))

        lines = text.splitlines(keepends=True)
        formatted_lines: list[str] = []
        in_examples_block = False
        expect_value = False

        for line in lines:
            if line.strip() == "":
                in_examples_block = False
                expect_value = False
                formatted_lines.append(f"{indent}{line}")
                continue

            has_newline = line.endswith("\n")
            stripped_line = line.rstrip("\n")
            leading_length = len(stripped_line) - len(stripped_line.lstrip(" "))
            leading = stripped_line[:leading_length]
            content = stripped_line[leading_length:]
            content_lower = content.lower()
            is_section_heading = (
                content_lower.endswith("examples:") and content_lower != "examples:"
            )

            if is_section_heading or content_lower == "examples:":
                formatted_content = (
                    f"{theme.heading}{content}{theme.reset}" if theme is not None else content
                )
                in_examples_block = True
                expect_value = False
            elif in_examples_block and theme is not None:
                colored = self._colorize_example_line(
                    content,
                    theme=theme,
                    expect_value=expect_value,
                )
                expect_value = colored.expect_value
                formatted_content = colored.text
            elif in_examples_block:
                formatted_content = content
            else:
                formatted_content = self._colorize_inline_code(content, theme=theme)

            newline = "\n" if has_newline else ""
            formatted_lines.append(f"{indent}{leading}{formatted_content}{newline}")

        return "".join(formatted_lines)

    @staticmethod
    def _colorize_inline_code(content: str, *, theme: HelpTheme | None) -> str:
        """Strip RST ``code`` backticks, coloring the span when a theme is bound."""

        def _replace(match: re.Match[str]) -> str:
            code = match.group(1)
            if theme is None:
                return code
            return f"{theme.inline_code}{code}{theme.reset}"

        return INLINE_CODE_RE.sub(_replace, content)

    class _ColorizedLine(t.NamedTuple):
        """Result of colorizing one example line."""

        text: str
        expect_value: bool

    def _colorize_example_line(
        self,
        content: str,
        *,
        theme: HelpTheme,
        expect_value: bool,
    ) -> _ColorizedLine:
        """Colorize program, subcommand, options, values, and query arguments.

        Tokenizes shell-aware (a quoted argument stays one token), so a quoted
        query like ``'agent:codex migration'`` is highlighted as one
        expression. The first token is the program, the second the subcommand;
        an option that takes a value colors the next token as a value; every
        other positional is treated as a query argument.
        """
        parts: list[str] = []
        expecting_value = expect_value
        first_token = True
        colored_subcommand = False

        for match in SHELL_TOKEN_RE.finditer(content):
            token = match.group()
            if token.isspace():
                parts.append(token)
                continue

            if expecting_value:
                rendered = f"{theme.label}{token}{theme.reset}"
                expecting_value = False
            elif self._looks_like_query_argument(token):
                # Checked before the option branches so the negation shorthand
                # `-agent:codex` reads as a query predicate, not a short flag.
                rendered = self._colorize_query_argument(token, theme=theme)
            elif token.startswith("--"):
                rendered = f"{theme.long_option}{token}{theme.reset}"
                expecting_value = (
                    token not in OPTIONS_FLAG_ONLY and token in OPTIONS_EXPECTING_VALUE
                )
            elif token.startswith("-"):
                rendered = f"{theme.short_option}{token}{theme.reset}"
                expecting_value = (
                    token not in OPTIONS_FLAG_ONLY and token in OPTIONS_EXPECTING_VALUE
                )
            elif first_token:
                rendered = f"{theme.prog}{token}{theme.reset}"
            elif not colored_subcommand:
                rendered = f"{theme.action}{token}{theme.reset}"
                colored_subcommand = True
            else:
                # Bare positional after the subcommand — a query term.
                rendered = self._colorize_query_argument(token, theme=theme)

            first_token = False
            parts.append(rendered)

        return self._ColorizedLine("".join(parts), expecting_value)

    @staticmethod
    def _looks_like_query_argument(token: str) -> bool:
        """Return whether a post-subcommand token should be lexed as a query.

        A complete quoted argument or a (possibly negated) field predicate
        qualifies; a bare flag (``-i`` / ``--json``) does not.
        """
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
            return True
        return QUERY_FIELD_TOKEN_RE.match(token) is not None

    @classmethod
    def _colorize_query_argument(cls, token: str, *, theme: HelpTheme) -> str:
        """Highlight one query argument, leaving any outer shell quotes plain."""
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
            return token[0] + cls._colorize_query_expression(token[1:-1], theme=theme) + token[-1]
        return cls._colorize_query_expression(token, theme=theme)

    @classmethod
    def _colorize_query_expression(cls, query: str, *, theme: HelpTheme) -> str:
        """Syntax-highlight a query expression via :func:`highlight_query_spans`.

        Maps each shared span role to a theme color. Design A collapses
        ``date`` into the single value hue and renders a ``phrase`` with dim
        delimiters around value-colored text.
        """
        role_color = {
            "field": theme.query_field,
            "keyword": theme.query_keyword,
            "negation": theme.query_negation,
            "wildcard": theme.query_wildcard,
            "punct": theme.query_punct,
            "operator": theme.query_operator,
            "value": theme.query_value,
            "date": theme.query_value,
        }
        out: list[str] = []
        for _start, role, text in highlight_query_spans(query):
            if role == "whitespace":
                out.append(text)
            elif role == "phrase":
                out.append(cls._colorize_query_phrase(text, theme=theme))
            else:
                out.append(f"{role_color.get(role, theme.query_value)}{text}{theme.reset}")
        return "".join(out)

    @staticmethod
    def _colorize_query_phrase(token: str, *, theme: HelpTheme) -> str:
        """Color a double-quoted phrase: dim ``"`` delimiters, value-colored text."""
        if len(token) < 2:
            return f"{theme.query_value}{token}{theme.reset}"
        return (
            f"{theme.query_punct}{token[0]}{theme.reset}"
            f"{theme.query_value}{token[1:-1]}{theme.reset}"
            f"{theme.query_punct}{token[-1]}{theme.reset}"
        )
