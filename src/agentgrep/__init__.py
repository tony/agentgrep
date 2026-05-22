#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# dependencies = ["pydantic>=2.11.3", "textual>=3.2.0"]
# ///
"""Search local AI agent prompts and history without mutating agent stores.

The tool discovers known read-only stores under ``~/.codex``, ``~/.claude``,
``~/.cursor``, and Cursor's official IDE storage locations, then normalizes
results through named adapters.

Examples
--------
List prompts containing both ``serenity`` and ``bliss``:

>>> query = SearchQuery(
...     terms=("serenity", "bliss"),
...     search_type="prompts",
...     any_term=False,
...     regex=False,
...     case_sensitive=False,
...     agents=("codex",),
...     limit=None,
... )
>>> matches_text("A serenity prompt with bliss inside.", query)
True
>>> matches_text("Only serenity appears here.", query)
False
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import dataclasses
import datetime
import importlib
import itertools
import json
import logging
import os
import pathlib
import re
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import typing as t

import pydantic
from rich.console import Group as _RichGroup
from rich.markdown import Markdown as _RichMarkdown
from rich.syntax import Syntax as _RichSyntax
from rich.text import Text as _RichText

from agentgrep.stores import (
    DiscoverySpec,
    PathKind,
    SourceKind,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

if t.TYPE_CHECKING:
    import collections.abc as cabc

    PrivatePathBase = pathlib.Path
else:
    PrivatePathBase = type(pathlib.Path())

AgentName = t.Literal["codex", "claude", "cursor", "gemini"]
OutputMode = t.Literal["text", "json", "ndjson", "ui"]
ProgressMode = t.Literal["auto", "always", "never"]
SearchType = t.Literal["prompts", "history", "all"]
ColorMode = t.Literal["auto", "always", "never"]
type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]
type SummaryRow = tuple[object, object, object, object, object, object, object, object]
type KeyValueRow = tuple[object, object]

AGENT_CHOICES: tuple[AgentName, ...] = ("codex", "claude", "cursor", "gemini")
JSON_FILE_SUFFIXES: frozenset[str] = frozenset({".json", ".jsonl"})
SCHEMA_VERSION: str = "agentgrep.v1"
USER_ROLES: frozenset[str] = frozenset({"human", "user"})
CURSOR_STATE_TOKENS: tuple[str, ...] = ("chat", "composer", "prompt", "history")
OFFICIAL_CURSOR_STATE_PATHS: tuple[pathlib.Path, ...] = (
    pathlib.Path("~/.config/Cursor/User/globalStorage/state.vscdb").expanduser(),
    pathlib.Path(
        "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb",
    ).expanduser(),
    pathlib.Path("~/AppData/Roaming/Cursor/User/globalStorage/state.vscdb").expanduser(),
)
EnvelopeFactory = t.Callable[[str, dict[str, object], list[dict[str, object]]], dict[str, object]]

OPTIONS_EXPECTING_VALUE: frozenset[str] = frozenset(
    {
        "--agent",
        "--type",
        "--limit",
        "--color",
        "--progress",
    },
)
OPTIONS_FLAG_ONLY: frozenset[str] = frozenset(
    {
        "-h",
        "--help",
        "--any",
        "--regex",
        "--case-sensitive",
        "--json",
        "--ndjson",
        "--ui",
    },
)


def build_description(
    intro: str,
    example_blocks: cabc.Sequence[tuple[str | None, cabc.Sequence[str]]],
) -> str:
    """Assemble help text with example sections."""
    sections: list[str] = []
    intro_text = textwrap.dedent(intro).strip()
    if intro_text:
        sections.append(intro_text)

    for heading, commands in example_blocks:
        if not commands:
            continue
        title = "examples:" if heading is None else f"{heading} examples:"
        lines = [title]
        lines.extend(f"  {command}" for command in commands)
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


CLI_DESCRIPTION = build_description(
    """
    Read-only search across Codex, Claude, and Cursor local stores.

    Bare ``agentgrep`` launches the interactive Textual explorer
    (``agentgrep ui``). ``agentgrep <terms>`` is shorthand for
    ``agentgrep search <terms>``.
    """,
    (
        (
            "quick",
            (
                "agentgrep",
                "agentgrep bliss",
                "agentgrep serene bliss --agent codex",
            ),
        ),
        (
            "search",
            (
                "agentgrep search bliss",
                "agentgrep search serene bliss --agent codex",
                "agentgrep search prompt history --type history --ndjson",
                "agentgrep search design --ui",
            ),
        ),
        (
            "find",
            (
                "agentgrep find codex",
                "agentgrep find sessions --agent codex",
                "agentgrep find cursor --json",
            ),
        ),
    ),
)
SEARCH_DESCRIPTION = build_description(
    """
    Search normalized prompts or history across supported agent stores.
    """,
    (
        (
            None,
            (
                "agentgrep search bliss",
                "agentgrep search serene bliss --agent codex",
                "agentgrep search prompt history --type history --ndjson",
                "agentgrep search serenity --json",
                "agentgrep search design --ui",
            ),
        ),
    ),
)
FIND_DESCRIPTION = build_description(
    """
    Find known prompt, history, and store paths without parsing message text.
    """,
    (
        (
            None,
            (
                "agentgrep find codex",
                "agentgrep find sessions --agent codex",
                "agentgrep find cursor --json",
            ),
        ),
    ),
)
UI_DESCRIPTION = build_description(
    """
    Launch the interactive Textual explorer. Bare ``agentgrep`` is
    equivalent to ``agentgrep ui``.
    """,
    (
        (
            None,
            (
                "agentgrep",
                "agentgrep ui",
                "agentgrep ui bliss",
            ),
        ),
    ),
)
FUZZY_DESCRIPTION = build_description(
    """
    Fuzzy match in fzf ``--filter`` mode: stdin lines are scored
    against QUERY and emitted in descending-score order.

    No QUERY and no piped stdin prints usage and exits 2 (strict, no
    interactive fallback — use ``agentgrep ui`` or ``--ui`` for
    interactive browsing).
    """,
    (
        (
            None,
            (
                "agentgrep grep -F . | agentgrep fuzzy 'config bliss'",
                "agentgrep fuzzy --exact -i 'design notes' < transcript.txt",
                "agentgrep fuzzy --algo=v1 --print-query foo",
            ),
        ),
    ),
)
GREP_DESCRIPTION = build_description(
    """
    Content search across normalized records with rg/ag-shaped flags.

    Defaults: smart-case, regex, session-deduped output. Pass
    ``--no-dedupe`` for the raw rg view, ``-F`` for literal pattern
    matching, ``-i`` / ``-s`` to override case, ``--json`` for an
    rg-style event stream.
    """,
    (
        (
            None,
            (
                "agentgrep grep bliss",
                "agentgrep grep -i 'serene bliss'",
                "agentgrep grep -F --type history TODO",
                "agentgrep grep --json design",
                "agentgrep grep --vimgrep --no-dedupe foo",
            ),
        ),
    ),
)


class PrivatePath(PrivatePathBase):
    """Path subclass that hides the user's home directory in textual output."""

    def __new__(cls, *args: t.Any, **kwargs: t.Any) -> PrivatePath:
        """Create a privacy-aware path."""
        return super().__new__(cls, *args, **kwargs)

    @classmethod
    def _collapse_home(cls, value: str) -> str:
        """Collapse the user's home directory to ``~`` when ``value`` is inside it."""
        if value.startswith("~"):
            return value

        home = str(pathlib.Path.home())
        if value == home:
            return "~"

        separators = {os.sep}
        if os.altsep:
            separators.add(os.altsep)

        for separator in separators:
            home_with_separator = home + separator
            if value.startswith(home_with_separator):
                return "~" + value[len(home) :]

        return value

    def __str__(self) -> str:
        """Return string output with the home directory collapsed."""
        return self._collapse_home(pathlib.Path.__str__(self))

    def __repr__(self) -> str:
        """Return repr output with the home directory collapsed."""
        return f"{self.__class__.__name__}({str(self)!r})"


def format_display_path(path: pathlib.Path | str, *, directory: bool = False) -> str:
    """Return a privacy-safe display path."""
    display = str(PrivatePath(path))
    if directory and not display.endswith("/"):
        return f"{display.rstrip('/')}/"
    return display


def format_compact_path(path: pathlib.Path | str, *, max_width: int) -> str:
    """Trim a long display path with middle-elision, fish-style adapted for our shapes.

    Our paths are date-segmented (`~/.codex/sessions/2024/02/14/uuid.jsonl`) so
    fish-shell's first-letter abbreviation (`~/.c/s/2/0/1/uuid.jsonl`) loses
    information. Instead we preserve the leading hidden-dir context, the
    filename, and the immediate parent dir; the middle is elided with `…/`.

    Parameters
    ----------
    path : pathlib.Path | str
        Source path; passed through :func:`format_display_path` first so the
        privacy-rewriting and ``~`` prefix logic stay consistent with the CLI.
    max_width : int
        Maximum number of display columns.

    Returns
    -------
    str
        A path string of at most ``max_width`` columns (best-effort; if even
        the filename exceeds the budget the filename is hard-truncated with
        ``…``).
    """
    display = format_display_path(path)
    if max_width <= 0 or len(display) <= max_width:
        return display
    # Split preserving leading ``~`` / ``/`` so we can rebuild correctly.
    if display.startswith("~/"):
        prefix = "~/"
        body = display[2:]
    elif display.startswith("/"):
        prefix = "/"
        body = display[1:]
    else:
        prefix = ""
        body = display
    segments = body.split("/")
    if len(segments) <= 2:
        return _hard_truncate(display, max_width)
    root = segments[0]
    filename = segments[-1]
    parent = segments[-2]
    # Tier 1: keep root + …/ + parent + / + filename
    candidate = f"{prefix}{root}/…/{parent}/{filename}"
    if len(candidate) <= max_width:
        return candidate
    # Tier 2: drop root, keep …/ + parent + / + filename
    candidate = f"…/{parent}/{filename}"
    if len(candidate) <= max_width:
        return candidate
    # Tier 3: keep just the filename, possibly truncated.
    return _hard_truncate(filename, max_width)


def _hard_truncate(text: str, max_width: int) -> str:
    """Truncate ``text`` to fit ``max_width``, appending ``…`` if shortened."""
    if max_width <= 0:
        return ""
    if len(text) <= max_width:
        return text
    if max_width == 1:
        return "…"
    return text[: max_width - 1] + "…"


def truncate_lines(text: str, max_lines: int) -> str:
    """Return the first ``max_lines`` lines of ``text``, with an overflow marker.

    Used by the TUI detail pane so a record body of any size renders in
    microseconds — only the lines that fit on screen are passed to the
    ``Static`` widget. The overflow marker (``… (+N more lines)``) tells the
    user that more content exists.
    """
    if max_lines <= 0 or not text:
        return ""
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text
    visible = lines[:max_lines]
    remaining = len(lines) - max_lines
    return "\n".join(visible) + f"\n… (+{remaining} more lines)"


DETAIL_BODY_MAX_LINES = 1000
"""Hard cap on lines rendered in the detail-pane body.

The detail pane wraps the body ``Static`` in a ``VerticalScroll`` so the user
can scroll within the pane. The cap exists purely as a defence against
multi-megabyte session logs that would otherwise stall ``Static.update``.
"""


def find_first_match_line(
    text: str,
    terms: cabc.Sequence[str],
    *,
    case_sensitive: bool = False,
    regex: bool = False,
) -> int | None:
    """Return the 0-based line index of the first line containing any term.

    Parameters
    ----------
    text : str
        The body to scan.
    terms : Sequence[str]
        Query terms (substring or regex) to search for. Empty → no match.
    case_sensitive : bool, default False
        When False, matching is case-folded.
    regex : bool, default False
        When False, each term is escaped before regex compilation. When True,
        each term is compiled as-is.

    Returns
    -------
    int | None
        The line index of the first match, or ``None`` if no line matches.
        Malformed regex patterns are silently skipped.
    """
    if not text or not terms:
        return None
    flags = 0 if case_sensitive else re.IGNORECASE
    patterns: list[str] = []
    for term in terms:
        if not term:
            continue
        compiled_source = term if regex else re.escape(term)
        try:
            re.compile(compiled_source, flags)
        except re.error:
            continue
        patterns.append(f"(?:{compiled_source})")
    if not patterns:
        return None
    combined = re.compile("|".join(patterns), flags)
    for idx, line in enumerate(text.split("\n")):
        if combined.search(line):
            return idx
    return None


def highlight_matches(
    text: str,
    terms: cabc.Sequence[str],
    *,
    case_sensitive: bool = False,
    regex: bool = False,
    style: str = "bold yellow",
) -> _RichText:
    """Build a Rich ``Text`` with every occurrence of any term styled.

    Stacks one ``highlight_regex`` pass per term so the per-pass complexity
    is linear; total cost is O(N * T) for text length N and T terms.
    Malformed regex patterns are silently skipped (mirrors
    :func:`find_first_match_line`).
    """
    rich = _RichText(text, no_wrap=False)
    if not text or not terms:
        return rich
    flags = 0 if case_sensitive else re.IGNORECASE
    for term in terms:
        if not term:
            continue
        pattern_source = term if regex else re.escape(term)
        try:
            compiled = re.compile(pattern_source, flags)
        except re.error:
            continue
        rich.highlight_regex(compiled, style=style)
    return rich


ContentFormat = t.Literal["json", "markdown", "text"]
"""Detected body format for detail-pane rendering — see :func:`detect_content_format`."""


def detect_content_format(text: str) -> ContentFormat:
    r"""Sniff the format of a record body for syntax-aware rendering.

    The decision drives whether the detail pane renders the body via
    :class:`rich.syntax.Syntax` (JSON), :class:`rich.markdown.Markdown`, or
    the existing match-highlighted :class:`rich.text.Text`. ``record.path``
    is **not** consulted because most adapters store the source file
    (``.jsonl`` / ``.sqlite``) while ``record.text`` is an extracted
    chat-message payload — the only reliable signal is the body itself.

    The markdown heuristic is intentionally false-negative-biased: a plain
    chat message that incidentally starts with ``- `` should not lose its
    match highlighting to a misfire. Only fenced code blocks (triple
    backtick) or ATX headings at the start of a line trip markdown mode.

    Parameters
    ----------
    text : str
        The body to classify.

    Returns
    -------
    {"json", "markdown", "text"}
        ``"json"`` when the body parses as JSON; ``"markdown"`` on a strong
        markdown signal; ``"text"`` otherwise (also the empty-body case).

    Examples
    --------
    >>> detect_content_format('{"a": 1}')
    'json'
    >>> detect_content_format("# Heading\\n\\nbody")
    'markdown'
    >>> detect_content_format("plain message body")
    'text'
    >>> detect_content_format("- not really markdown")
    'text'
    """
    if not text:
        return "text"
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(text)
        except ValueError:
            pass
        else:
            return "json"
    if re.search(r"^```", text, re.MULTILINE):
        return "markdown"
    if re.search(r"^#{1,6} \S", text, re.MULTILINE):
        return "markdown"
    return "text"


class SearchRecordPayload(t.TypedDict):
    """JSON payload for search records."""

    schema_version: str
    kind: t.Literal["prompt", "history"]
    agent: AgentName
    store: str
    adapter_id: str
    path: str
    text: str
    title: str | None
    role: str | None
    timestamp: str | None
    model: str | None
    session_id: str | None
    conversation_id: str | None
    metadata: dict[str, object]


class FindRecordPayload(t.TypedDict):
    """JSON payload for find records."""

    schema_version: str
    kind: t.Literal["find"]
    agent: AgentName
    store: str
    adapter_id: str
    path: str
    path_kind: PathKind
    metadata: dict[str, object]


class SourceHandlePayload(t.TypedDict):
    """JSON payload for discovered sources."""

    schema_version: str
    agent: AgentName
    store: str
    adapter_id: str
    path: str
    path_kind: PathKind
    source_kind: SourceKind
    search_root: str | None
    mtime_ns: int


class EnvelopePayload(t.TypedDict):
    """JSON payload for top-level envelopes."""

    schema_version: str
    command: str
    query: dict[str, object]
    results: list[dict[str, object]]


class PydanticTypeAdapter(t.Protocol):
    """Minimal TypeAdapter surface used by ``agentgrep``."""

    def validate_python(self, value: object, /) -> object:
        """Validate a Python object."""
        ...

    def dump_python(self, value: object, /, *, mode: str = "python") -> object:
        """Dump a Python object."""
        ...


class PydanticTypeAdapterFactory(t.Protocol):
    """Factory for creating TypeAdapters."""

    def __call__(self, value_type: object, /) -> PydanticTypeAdapter:
        """Create a TypeAdapter."""
        ...


class PydanticModule(t.Protocol):
    """Minimal Pydantic module surface used at runtime."""

    TypeAdapter: PydanticTypeAdapterFactory


class HelpTheme(t.Protocol):
    """Minimal argparse help theme surface."""

    heading: str
    reset: str
    label: str
    long_option: str
    short_option: str
    prog: str
    action: str


class AnsiHelpTheme(t.NamedTuple):
    """ANSI theme values for syntax-colored help examples."""

    heading: str
    reset: str
    label: str
    long_option: str
    short_option: str
    prog: str
    action: str

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
        )


@dataclasses.dataclass(frozen=True, slots=True)
class AnsiColors:
    """Semantic ANSI colors for terminal status output."""

    enabled: bool

    SUCCESS: t.ClassVar[str] = "\x1b[32m"
    WARNING: t.ClassVar[str] = "\x1b[33m"
    ERROR: t.ClassVar[str] = "\x1b[31m"
    INFO: t.ClassVar[str] = "\x1b[36m"
    HEADING: t.ClassVar[str] = "\x1b[1;36m"
    HIGHLIGHT: t.ClassVar[str] = "\x1b[35m"
    MUTED: t.ClassVar[str] = "\x1b[34m"
    WHITE: t.ClassVar[str] = "\x1b[37m"
    RESET: t.ClassVar[str] = "\x1b[0m"

    @classmethod
    def for_stream(cls, color_mode: ColorMode, stream: t.TextIO) -> AnsiColors:
        """Build semantic colors for ``stream`` and ``color_mode``."""
        return cls(enabled=should_enable_color(color_mode, stream))

    def colorize(self, text: str, color: str) -> str:
        """Apply ``color`` to ``text`` when colors are enabled."""
        if not self.enabled:
            return text
        return f"{color}{text}{self.RESET}"

    def success(self, text: str) -> str:
        """Format text as success."""
        return self.colorize(text, self.SUCCESS)

    def warning(self, text: str) -> str:
        """Format text as warning."""
        return self.colorize(text, self.WARNING)

    def error(self, text: str) -> str:
        """Format text as error."""
        return self.colorize(text, self.ERROR)

    def info(self, text: str) -> str:
        """Format text as informational."""
        return self.colorize(text, self.INFO)

    def heading(self, text: str) -> str:
        """Format text as a status heading."""
        return self.colorize(text, self.HEADING)

    def highlight(self, text: str) -> str:
        """Format text as highlighted."""
        return self.colorize(text, self.HIGHLIGHT)

    def muted(self, text: str) -> str:
        """Format text as muted."""
        return self.colorize(text, self.MUTED)

    def white(self, text: str) -> str:
        """Format text as plain white."""
        return self.colorize(text, self.WHITE)


class SearchColors(t.Protocol):
    """Structural surface implemented by :class:`AnsiColors` (used by the CLI chrome)."""

    def success(self, text: str) -> str:
        """Style ``text`` as success."""
        ...

    def warning(self, text: str) -> str:
        """Style ``text`` as warning."""
        ...

    def error(self, text: str) -> str:
        """Style ``text`` as error."""
        ...

    def info(self, text: str) -> str:
        """Style ``text`` as informational."""
        ...

    def heading(self, text: str) -> str:
        """Style ``text`` as a status heading."""
        ...

    def highlight(self, text: str) -> str:
        """Style ``text`` as highlighted."""
        ...

    def muted(self, text: str) -> str:
        """Style ``text`` as muted."""
        ...

    def white(self, text: str) -> str:
        """Style ``text`` as plain white."""
        ...


def should_enable_color(color_mode: ColorMode, stream: t.TextIO) -> bool:
    """Return whether output written to ``stream`` should use colors."""
    if os.environ.get("NO_COLOR"):
        return False
    if color_mode == "never":
        return False
    if color_mode == "always":
        return True
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def should_enable_help_color(color_mode: ColorMode) -> bool:
    """Return whether help output should use colors."""
    return should_enable_color(color_mode, sys.stdout)


def create_themed_formatter(color_mode: ColorMode) -> type[AgentGrepHelpFormatter]:
    """Create a formatter class with a bound theme."""
    theme = AnsiHelpTheme.default() if should_enable_help_color(color_mode) else None

    class ThemedAgentGrepHelpFormatter(AgentGrepHelpFormatter):
        """AgentGrepHelpFormatter with a configured theme."""

        _theme: object | None

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
            self._theme = theme

    return ThemedAgentGrepHelpFormatter


class AgentGrepHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Extend help output with syntax-colored example sections."""

    _theme: object | None = None

    @t.override
    def _fill_text(self, text: str, width: int, indent: str) -> str:
        """Colorize ``examples:`` blocks when a theme is available."""
        theme = t.cast("HelpTheme | None", getattr(self, "_theme", None))
        if not text or theme is None:
            return super()._fill_text(text, width, indent)

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
                formatted_content = f"{theme.heading}{content}{theme.reset}"
                in_examples_block = True
                expect_value = False
            elif in_examples_block:
                colored = self._colorize_example_line(
                    content,
                    theme=theme,
                    expect_value=expect_value,
                )
                expect_value = colored.expect_value
                formatted_content = colored.text
            else:
                formatted_content = stripped_line

            newline = "\n" if has_newline else ""
            formatted_lines.append(f"{indent}{leading}{formatted_content}{newline}")

        return "".join(formatted_lines)

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
        """Colorize program, subcommand, options, and option values."""
        parts: list[str] = []
        expecting_value = expect_value
        first_token = True
        colored_subcommand = False

        for match in re.finditer(r"\s+|\S+", content):
            token = match.group()
            if token.isspace():
                parts.append(token)
                continue

            if expecting_value:
                color = theme.label
                expecting_value = False
            elif token.startswith("--"):
                color = theme.long_option
                expecting_value = (
                    token not in OPTIONS_FLAG_ONLY and token in OPTIONS_EXPECTING_VALUE
                )
            elif token.startswith("-"):
                color = theme.short_option
                expecting_value = (
                    token not in OPTIONS_FLAG_ONLY and token in OPTIONS_EXPECTING_VALUE
                )
            elif first_token:
                color = theme.prog
            elif not colored_subcommand:
                color = theme.action
                colored_subcommand = True
            else:
                color = None

            first_token = False
            if color is None:
                parts.append(token)
            else:
                parts.append(f"{color}{token}{theme.reset}")

        return self._ColorizedLine("".join(parts), expecting_value)


class TextualContainersModule(t.Protocol):
    """Minimal Textual containers module surface."""

    Horizontal: cabc.Callable[..., t.ContextManager[object]]
    Vertical: cabc.Callable[..., t.ContextManager[object]]
    VerticalScroll: cabc.Callable[..., t.ContextManager[object]]


class TextualAppModule(t.Protocol):
    """Minimal Textual app module surface."""

    App: type[object]


class TextualMessageModule(t.Protocol):
    """Minimal Textual message module surface."""

    Message: type[object]


class RichTextModule(t.Protocol):
    """Minimal Rich text module surface."""

    Text: cabc.Callable[..., t.Any]


class StreamingAppLike(t.Protocol):
    """App methods needed by the streaming TUI: workers, timers, cross-thread calls."""

    def post_message(self, message: object) -> bool:
        """Post a message to the app's queue (thread-safe)."""
        ...

    def call_from_thread(
        self,
        callback: cabc.Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> object:
        """Invoke ``callback(*args, **kwargs)`` on the event loop from a worker thread.

        Bypasses the message queue, so high-frequency data updates don't
        starve keystroke and timer events.
        """
        ...

    def query_one(self, selector: object, expect_type: object | None = None) -> object:
        """Look up one widget."""
        ...

    def run_worker(
        self,
        work: cabc.Callable[..., object],
        *,
        name: str = ...,
        group: str = ...,
        thread: bool = ...,
        exclusive: bool = ...,
    ) -> object:
        """Spawn a background worker."""
        ...

    def set_interval(
        self,
        interval: float,
        callback: cabc.Callable[[], object],
    ) -> object:
        """Register a recurring callback."""
        ...


class StaticLike(t.Protocol):
    """Minimal Static widget surface used by the TUI."""

    def update(self, content: str) -> None:
        """Update widget contents."""
        ...


class QueryAppLike(t.Protocol):
    """Minimal Textual app query surface used by the TUI."""

    def query_one(self, selector: object, expect_type: object | None = None) -> object:
        """Look up one widget."""
        ...


class RunnableAppLike(t.Protocol):
    """Minimal runnable app surface."""

    def run(self) -> None:
        """Run the application."""
        ...


class TextualWidgetsModule(t.Protocol):
    """Minimal Textual widgets module surface."""

    Footer: cabc.Callable[[], object]
    Header: cabc.Callable[[], object]
    Input: type[object]
    OptionList: type[object]
    Static: type[object]


class TextualOptionListInternalsModule(t.Protocol):
    """Minimal Textual option_list module surface for the ``Option`` class."""

    Option: t.Any


class TextualBindingModule(t.Protocol):
    """Minimal Textual binding module surface for the ``Binding`` class."""

    Binding: t.Any


@dataclasses.dataclass(slots=True)
class BackendSelection:
    """Selected optional subprocess backends."""

    find_tool: str | None
    grep_tool: str | None
    json_tool: str | None


@dataclasses.dataclass(slots=True)
class SearchQuery:
    """Compiled search configuration."""

    terms: tuple[str, ...]
    search_type: SearchType
    any_term: bool
    regex: bool
    case_sensitive: bool
    agents: tuple[AgentName, ...]
    limit: int | None
    dedupe: bool = True


@dataclasses.dataclass(slots=True)
class SourceHandle:
    """A discovered, parseable source file or SQLite database."""

    agent: AgentName
    store: str
    adapter_id: str
    path: pathlib.Path
    path_kind: PathKind
    source_kind: SourceKind
    search_root: pathlib.Path | None
    mtime_ns: int


@dataclasses.dataclass(slots=True)
class SearchRecord:
    """Normalized prompt/history record."""

    kind: t.Literal["prompt", "history"]
    agent: AgentName
    store: str
    adapter_id: str
    path: pathlib.Path
    text: str
    title: str | None = None
    role: str | None = None
    timestamp: str | None = None
    model: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    metadata: dict[str, object] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(slots=True)
class FindRecord:
    """Normalized discovery record for ``agentgrep find``."""

    kind: t.Literal["find"]
    agent: AgentName
    store: str
    adapter_id: str
    path: pathlib.Path
    path_kind: PathKind
    metadata: dict[str, object] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(slots=True)
class MessageCandidate:
    """Intermediate parsed message representation."""

    role: str | None
    text: str
    title: str | None = None
    timestamp: str | None = None
    model: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None


class SearchControl:
    """Thread-safe cooperative controls for an active search."""

    def __init__(self) -> None:
        self._answer_now = threading.Event()

    def request_answer_now(self) -> None:
        """Request that search return the results collected so far."""
        self._answer_now.set()

    def answer_now_requested(self) -> bool:
        """Return whether search should stop and answer with partial results."""
        return self._answer_now.is_set()


class AnswerNowInputListener:
    """Listen for a blank Enter keypress and request a partial answer."""

    def __init__(
        self,
        control: SearchControl,
        *,
        stream: t.TextIO | None = None,
        poll_interval: float = 0.1,
    ) -> None:
        self._control = control
        self._stream = stream if stream is not None else sys.stdin
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start listening for a blank line on stdin."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="agentgrep-answer-now-input",
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop listening when possible."""
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=0.2)

    def _run(self) -> None:
        selectable = self._stream_is_selectable()
        while not self._stop_event.is_set() and not self._control.answer_now_requested():
            line = self._read_line(selectable)
            if line is None:
                continue
            if line == "":
                return
            if line.strip() == "":
                self._control.request_answer_now()
                return
            if not selectable:
                return

    def _read_line(self, selectable: bool) -> str | None:
        if selectable:
            try:
                readable, _, _ = select.select([self._stream], [], [], self._poll_interval)
            except OSError, TypeError, ValueError:
                return None
            if not readable:
                return None
        try:
            return self._stream.readline()
        except OSError, ValueError:
            return ""

    def _stream_is_selectable(self) -> bool:
        try:
            _ = self._stream.fileno()
            readable, _, _ = select.select([self._stream], [], [], 0)
        except AttributeError, OSError, TypeError, ValueError:
            return False
        return isinstance(readable, list)


class SearchProgress(t.Protocol):
    """Progress reporter used by search internals."""

    def start(self, query: SearchQuery) -> None:
        """Mark search start."""
        ...

    def sources_discovered(self, count: int) -> None:
        """Report discovered source count."""
        ...

    def prefilter_started(self, root: pathlib.Path) -> None:
        """Report root prefilter start."""
        ...

    def sources_planned(self, planned: int, total: int) -> None:
        """Report selected source count."""
        ...

    def source_started(self, index: int, total: int, source: SourceHandle) -> None:
        """Report source scan start."""
        ...

    def source_finished(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        records: int,
        matches: int,
    ) -> None:
        """Report source scan completion."""
        ...

    def result_added(self, count: int) -> None:
        """Report deduped result count."""
        ...

    def record_added(self, record: SearchRecord) -> None:
        """Report a newly deduped record (streaming consumers only)."""
        ...

    def finish(self, result_count: int) -> None:
        """Report search completion."""
        ...

    def answer_now(self, result_count: int) -> None:
        """Report early search completion with partial results."""
        ...

    def interrupt(self) -> None:
        """Report interrupted search."""
        ...

    def close(self) -> None:
        """Release any progress resources."""
        ...


class NoopSearchProgress:
    """Silent search progress reporter."""

    def start(self, query: SearchQuery) -> None:
        """Ignore search start."""

    def sources_discovered(self, count: int) -> None:
        """Ignore discovered source count."""

    def prefilter_started(self, root: pathlib.Path) -> None:
        """Ignore root prefilter start."""

    def sources_planned(self, planned: int, total: int) -> None:
        """Ignore selected source count."""

    def source_started(self, index: int, total: int, source: SourceHandle) -> None:
        """Ignore source scan start."""

    def source_finished(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        records: int,
        matches: int,
    ) -> None:
        """Ignore source scan completion."""

    def result_added(self, count: int) -> None:
        """Ignore deduped result count."""

    def record_added(self, record: SearchRecord) -> None:
        """Ignore newly deduped record."""

    def finish(self, result_count: int) -> None:
        """Ignore search completion."""

    def answer_now(self, result_count: int) -> None:
        """Ignore early search completion."""

    def interrupt(self) -> None:
        """Ignore interrupted search."""

    def close(self) -> None:
        """Nothing to release."""


class ConsoleSearchProgress:
    """Human progress reporter for potentially long searches."""

    _SPINNER_FRAMES: t.ClassVar[str] = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(
        self,
        *,
        enabled: bool,
        stream: t.TextIO | None = None,
        tty: bool | None = None,
        color_mode: ColorMode = "auto",
        refresh_interval: float = 0.1,
        heartbeat_interval: float = 10.0,
        answer_now_hint: bool = False,
    ) -> None:
        self._enabled = enabled
        self._stream = stream if stream is not None else sys.stderr
        self._tty = (
            tty
            if tty is not None
            else bool(
                getattr(self._stream, "isatty", lambda: False)(),
            )
        )
        self._colors = AnsiColors.for_stream(color_mode, self._stream)
        self._refresh_interval = refresh_interval
        self._heartbeat_interval = heartbeat_interval
        self._answer_now_hint = answer_now_hint
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._last_heartbeat_at: float | None = None
        self._last_line_len = 0
        self._query_label = "search"
        self._phase = "starting"
        self._detail: str | None = None
        self._current: int | None = None
        self._total: int | None = None
        self._matches = 0
        self._finished = False

    def start(self, query: SearchQuery) -> None:
        """Begin progress reporting for ``query``."""
        if not self._enabled:
            return
        label = " ".join(query.terms) if query.terms else "all records"
        now = time.monotonic()
        with self._lock:
            self._query_label = label
            self._phase = "discovering"
            self._detail = None
            self._current = None
            self._total = None
            self._matches = 0
            self._started_at = now
            self._last_heartbeat_at = now
            self._finished = False
        if self._tty:
            self._ensure_tty_thread()
        else:
            self._emit_line(self._start_line(label))

    def sources_discovered(self, count: int) -> None:
        """Report discovered source count."""
        self.set_status("discovered", total=count, detail=f"{count} sources")

    def prefilter_started(self, root: pathlib.Path) -> None:
        """Report root prefilter start."""
        self.set_status("prefiltering", detail=format_display_path(root, directory=True))

    def sources_planned(self, planned: int, total: int) -> None:
        """Report selected source count."""
        self.set_status("planning", current=planned, total=total, detail="candidate sources")

    def source_started(self, index: int, total: int, source: SourceHandle) -> None:
        """Report source scan start."""
        self.set_status("scanning", current=index, total=total, detail=source.path.name)

    def source_finished(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        records: int,
        matches: int,
    ) -> None:
        """Report source scan completion."""
        self.set_status(
            "scanning",
            current=index,
            total=total,
            detail=f"{records} records, {format_match_count(matches)} in {source.path.name}",
        )

    def result_added(self, count: int) -> None:
        """Report deduped result count."""
        if not self._enabled:
            return
        with self._lock:
            self._matches = count
        self._emit_heartbeat_if_due()

    def record_added(self, record: SearchRecord) -> None:
        """Ignore the per-record broadcast; counter is tracked via ``result_added``."""

    def set_status(
        self,
        phase: str,
        *,
        current: int | None = None,
        total: int | None = None,
        detail: str | None = None,
    ) -> None:
        """Update the current progress status."""
        if not self._enabled:
            return
        with self._lock:
            self._phase = phase
            self._current = current
            self._total = total
            self._detail = detail
        self._emit_heartbeat_if_due()

    def finish(self, result_count: int) -> None:
        """Finish progress reporting."""
        if not self._enabled:
            return
        with self._lock:
            self._matches = result_count
            self._phase = "complete"
            self._finished = True
        if self._tty:
            self._stop_tty_thread()
            self._clear_tty_line()
            return
        elapsed = self._elapsed_seconds()
        self._emit_line(
            self._finish_line(result_count, elapsed),
        )

    def answer_now(self, result_count: int) -> None:
        """Finish progress reporting with a partial-answer status."""
        if not self._enabled:
            return
        with self._lock:
            self._matches = result_count
            self._phase = "answering now"
            self._finished = True
        line = self._answer_now_line(result_count)
        if self._tty:
            self._stop_tty_thread()
            self._write_tty_line(line)
            return
        self._emit_line(line)

    def close(self) -> None:
        """Stop any active progress renderer."""
        if not self._enabled:
            return
        if self._tty:
            self._stop_tty_thread()
            self._clear_tty_line()

    def interrupt(self) -> None:
        """Stop progress rendering while preserving the current status."""
        if not self._enabled:
            return
        if self._tty:
            self._stop_tty_thread()
            self._write_tty_summary_line()
            return
        self._emit_line(self._summary())

    def _ensure_tty_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._tty_loop,
            daemon=True,
            name="agentgrep-search-progress",
        )
        self._thread.start()

    def _stop_tty_thread(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)

    def _tty_loop(self) -> None:
        frames = itertools.cycle(self._SPINNER_FRAMES)
        while not self._stop_event.is_set():
            self._render_tty(next(frames))
            self._stop_event.wait(self._refresh_interval)

    def _render_tty(self, frame: str) -> None:
        summary = self._summary()
        line = f"{self._colors.info(frame)} {summary}"
        with self._lock:
            try:
                self._stream.write("\r\033[2K" + line)
                self._stream.flush()
                self._last_line_len = len(line)
            except OSError, ValueError:
                pass

    def _clear_tty_line(self) -> None:
        with self._lock:
            if self._last_line_len == 0:
                return
            try:
                self._stream.write("\r\033[2K")
                self._stream.flush()
            except OSError, ValueError:
                pass
            self._last_line_len = 0

    def _write_tty_summary_line(self) -> None:
        line = self._summary()
        self._write_tty_line(line)

    def _write_tty_line(self, line: str) -> None:
        with self._lock:
            try:
                self._stream.write("\r\033[2K" + line + "\n")
                self._stream.flush()
            except OSError, ValueError:
                pass
            self._last_line_len = 0

    def _emit_heartbeat_if_due(self) -> None:
        if not self._enabled or self._tty:
            return
        with self._lock:
            last = self._last_heartbeat_at
            label = self._query_label
        if last is None:
            return
        now = time.monotonic()
        if now - last < self._heartbeat_interval:
            return
        elapsed = self._elapsed_seconds()
        self._emit_line(
            self._heartbeat_line(label, elapsed),
        )
        with self._lock:
            self._last_heartbeat_at = now

    def _emit_line(self, line: str) -> None:
        try:
            self._stream.write(line + "\n")
            self._stream.flush()
        except OSError, ValueError:
            pass

    def _summary(self) -> str:
        return format_search_progress_line(
            self._snapshot(),
            colors=self._colors,
            answer_now_hint=self._answer_now_hint,
        )

    def _snapshot(self) -> ProgressSnapshot:
        elapsed = self._elapsed_seconds()
        with self._lock:
            return ProgressSnapshot(
                query_label=self._query_label,
                phase=self._phase,
                current=self._current,
                total=self._total,
                detail=self._detail,
                matches=self._matches,
                elapsed=elapsed,
            )

    def _start_line(self, label: str) -> str:
        return f"{self._colors.heading('Searching')} {self._colors.highlight(label)}"

    def _heartbeat_line(self, label: str, elapsed: float) -> str:
        prefix = f"{self._colors.muted('...')} {self._colors.heading('still searching')}"
        elapsed_text = self._colors.muted(f"{elapsed:.0f}s elapsed")
        return f"{prefix} {self._colors.highlight(label)}: {self._status_text()} ({elapsed_text})"

    def _finish_line(self, result_count: int, elapsed: float) -> str:
        return (
            f"{self._colors.success('Search complete:')} "
            f"{self._colors.warning(format_match_count(result_count))} "
            f"({self._colors.muted(f'{elapsed:.1f}s elapsed')})"
        )

    def _answer_now_line(self, result_count: int) -> str:
        return (
            f"{self._colors.success('Answering now:')} "
            f"{self._colors.warning(format_match_count(result_count))}"
        )

    def _status_text(self) -> str:
        with self._lock:
            phase = self._phase
            current = self._current
            total = self._total
            detail = self._detail
        if current is not None and total is not None:
            count = self._colors.warning(f"{current}/{total}")
            return f"{self._colors.heading(phase)} {count} {self._colors.muted('sources')}"
        if detail:
            return f"{self._colors.heading(phase)} {self._colors.muted(detail)}"
        return self._colors.heading(phase)

    def _elapsed_seconds(self) -> float:
        with self._lock:
            started = self._started_at
        if started is None:
            return 0.0
        return time.monotonic() - started


def format_match_count(count: int) -> str:
    """Return a human-readable match count."""
    suffix = "match" if count == 1 else "matches"
    return f"{count} {suffix}"


@dataclasses.dataclass(frozen=True)
class ProgressSnapshot:
    """Immutable view of search-progress state for one render pass."""

    query_label: str
    phase: str
    current: int | None
    total: int | None
    detail: str | None
    matches: int
    elapsed: float


def format_search_progress_line(
    snapshot: ProgressSnapshot,
    *,
    colors: SearchColors,
    answer_now_hint: bool = False,
) -> str:
    """Format the single-line progress summary used by both the CLI and the TUI.

    Parameters
    ----------
    snapshot : ProgressSnapshot
        Frozen view of progress counters.
    colors : SearchColors
        An :class:`AnsiColors` instance (used by the CLI chrome).
    answer_now_hint : bool, default False
        When ``True``, append the ``[Press enter, answer now]`` reminder.

    Returns
    -------
    str
        ``"Searching <q> | <phase> N/M sources | K matches | T.Ts"`` with
        each segment styled through ``colors``.
    """
    label_part = f"{colors.heading('Searching')} {colors.highlight(snapshot.query_label)}"
    if snapshot.current is not None and snapshot.total is not None:
        count = colors.warning(f"{snapshot.current}/{snapshot.total}")
        status_part = f"{colors.heading(snapshot.phase)} {count} {colors.muted('sources')}"
    elif snapshot.detail:
        status_part = f"{colors.heading(snapshot.phase)} {colors.muted(snapshot.detail)}"
    else:
        status_part = colors.heading(snapshot.phase)
    parts = [
        label_part,
        status_part,
        colors.warning(format_match_count(snapshot.matches)),
        colors.muted(f"{snapshot.elapsed:.1f}s"),
    ]
    if answer_now_hint:
        parts.append(colors.white("[Press enter, answer now]"))
    return " | ".join(parts)


def noop_search_progress() -> SearchProgress:
    """Return a silent search progress reporter."""
    return NoopSearchProgress()


@dataclasses.dataclass(frozen=True)
class StreamingRecordsBatch:
    """Batch of newly deduped records emitted by :meth:`StreamingSearchProgress.flush`."""

    records: tuple[SearchRecord, ...]
    total: int


@dataclasses.dataclass(frozen=True)
class StreamingSearchFinished:
    """Terminal event emitted by :class:`StreamingSearchProgress` when the search ends."""

    outcome: t.Literal["complete", "interrupted", "error"]
    total: int
    elapsed: float
    error: BaseException | None = None


class RecordsAppendedPayload(pydantic.BaseModel):
    """Pydantic payload for the ``RecordsAppended`` Textual message."""

    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True, frozen=True)

    records: tuple[SearchRecord, ...]
    total: int


class ProgressUpdatedPayload(pydantic.BaseModel):
    """Pydantic payload for the ``ProgressUpdated`` Textual message."""

    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True, frozen=True)

    snapshot: ProgressSnapshot


class SearchFinishedPayload(pydantic.BaseModel):
    """Pydantic payload for the ``SearchFinished`` Textual message."""

    model_config = pydantic.ConfigDict(frozen=True)

    outcome: t.Literal["complete", "interrupted", "error"]
    total: int
    elapsed: float
    error_message: str | None = None


class FilterRequestedPayload(pydantic.BaseModel):
    """Pydantic payload for a debounced filter-text-changed Textual message."""

    model_config = pydantic.ConfigDict(frozen=True)

    text: str


class SearchRequestedPayload(pydantic.BaseModel):
    """Pydantic payload for a debounced search-bar-changed Textual message."""

    model_config = pydantic.ConfigDict(frozen=True)

    text: str


class FilterCompletedPayload(pydantic.BaseModel):
    """Pydantic payload for a worker-completed filter result Textual message."""

    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True, frozen=True)

    text: str
    matching: tuple[SearchRecord, ...]


class StreamingSearchProgress:
    """Search-progress reporter that emits structured events through an ``emit`` callback.

    Records are buffered under a lock and released as a single
    :class:`StreamingRecordsBatch` per :meth:`flush` (or on terminal events).
    Progress callbacks emit :class:`ProgressSnapshot` instances directly.
    The callback is invoked from whichever thread drives the search and is
    expected to be safe to call cross-thread (e.g. Textual's ``post_message``).
    """

    _FLUSH_INTERVAL_SECONDS: t.ClassVar[float] = 0.05

    def __init__(self, emit: cabc.Callable[[object], None]) -> None:
        self._emit = emit
        self._lock = threading.Lock()
        self._buffer: list[SearchRecord] = []
        self._query_label = "search"
        self._phase = "starting"
        self._detail: str | None = None
        self._current: int | None = None
        self._total: int | None = None
        self._matches = 0
        self._started_at: float | None = None
        self._last_flush_at: float = time.monotonic()

    def start(self, query: SearchQuery) -> None:
        """Record search start and emit the initial progress snapshot."""
        label = " ".join(query.terms) if query.terms else "all records"
        now = time.monotonic()
        with self._lock:
            self._query_label = label
            self._phase = "discovering"
            self._started_at = now
        self._emit_progress()

    def sources_discovered(self, count: int) -> None:
        """Report discovered-source count."""
        with self._lock:
            self._phase = "discovered"
            self._detail = f"{count} sources"
        self._emit_progress()

    def prefilter_started(self, root: pathlib.Path) -> None:
        """Report root prefilter start."""
        with self._lock:
            self._phase = "prefiltering"
            self._detail = format_display_path(root, directory=True)
        self._emit_progress()

    def sources_planned(self, planned: int, total: int) -> None:
        """Report planned-source count."""
        with self._lock:
            self._phase = "planning"
            self._current = planned
            self._total = total
            self._detail = "candidate sources"
        self._emit_progress()

    def source_started(self, index: int, total: int, source: SourceHandle) -> None:
        """Report source-scan start."""
        with self._lock:
            self._phase = "scanning"
            self._current = index
            self._total = total
            self._detail = source.path.name
        self._emit_progress()

    def source_finished(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        records: int,
        matches: int,
    ) -> None:
        """Report source-scan completion."""
        with self._lock:
            self._phase = "scanning"
            self._current = index
            self._total = total
            self._detail = f"{records} records, {format_match_count(matches)} in {source.path.name}"
        self._emit_progress()

    def result_added(self, count: int) -> None:
        """Update the cumulative match counter."""
        with self._lock:
            self._matches = count

    def record_added(self, record: SearchRecord) -> None:
        """Buffer ``record``; auto-flush when the batching window elapses.

        The window is checked under the buffer lock, so the worker thread paces
        its own emit cadence without needing a main-thread timer to pull from
        the buffer. Explicit :meth:`flush` calls (e.g. on terminal events) still
        drain the remainder.
        """
        with self._lock:
            self._buffer.append(record)
            should_flush = time.monotonic() - self._last_flush_at >= self._FLUSH_INTERVAL_SECONDS
        if should_flush:
            self.flush()

    def finish(self, result_count: int) -> None:
        """Flush pending records and emit a successful terminal event."""
        self.flush()
        self._emit(
            StreamingSearchFinished(
                "complete",
                total=result_count,
                elapsed=self._elapsed(),
            ),
        )

    def answer_now(self, result_count: int) -> None:
        """Flush pending records and emit an interrupted terminal event."""
        self.flush()
        self._emit(
            StreamingSearchFinished(
                "interrupted",
                total=result_count,
                elapsed=self._elapsed(),
            ),
        )

    def interrupt(self) -> None:
        """Flush pending records and emit an interrupted terminal event."""
        self.flush()
        with self._lock:
            matches = self._matches
        self._emit(
            StreamingSearchFinished(
                "interrupted",
                total=matches,
                elapsed=self._elapsed(),
            ),
        )

    def close(self) -> None:
        """No-op: no resources to release."""

    def flush(self) -> None:
        """Drain the record buffer into a single :class:`StreamingRecordsBatch`."""
        with self._lock:
            if not self._buffer:
                return
            batch = tuple(self._buffer)
            self._buffer.clear()
            total = self._matches
            self._last_flush_at = time.monotonic()
        self._emit(StreamingRecordsBatch(records=batch, total=total))

    def _emit_progress(self) -> None:
        self._emit(self._snapshot())

    def _snapshot(self) -> ProgressSnapshot:
        with self._lock:
            current = self._current
            total = self._total
            detail = self._detail
            phase = self._phase
            label = self._query_label
            matches = self._matches
            started = self._started_at
        elapsed = (time.monotonic() - started) if started is not None else 0.0
        return ProgressSnapshot(
            query_label=label,
            phase=phase,
            current=current,
            total=total,
            detail=detail,
            matches=matches,
            elapsed=elapsed,
        )

    def _elapsed(self) -> float:
        with self._lock:
            started = self._started_at
        return (time.monotonic() - started) if started is not None else 0.0


def select_backends() -> BackendSelection:
    """Return the best available subprocess helpers."""
    return BackendSelection(
        find_tool=which_first(("fd", "fdfind")),
        grep_tool=which_first(("rg", "ag")),
        json_tool=which_first(("jq", "jaq")),
    )


def which_first(names: tuple[str, ...]) -> str | None:
    """Return the first executable available on ``PATH``."""
    for name in names:
        found = shutil.which(name)
        if found is not None:
            return found
    return None


def run_readonly_command(
    command: list[str],
    *,
    control: SearchControl | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell and capture text output."""
    if control is None:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.05)
        except subprocess.TimeoutExpired:
            if control.answer_now_requested():
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(
                    command,
                    process.returncode,
                    stdout,
                    stderr,
                )
            continue
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def make_search_query(args: SearchArgs) -> SearchQuery:
    """Convert parsed search arguments into a query object."""
    return SearchQuery(
        terms=args.terms,
        search_type=args.search_type,
        any_term=args.any_term,
        regex=args.regex,
        case_sensitive=args.case_sensitive,
        agents=args.agents,
        limit=args.limit,
    )


def discover_sources(
    home: pathlib.Path,
    agents: tuple[AgentName, ...],
    backends: BackendSelection,
) -> list[SourceHandle]:
    """Discover all known parseable sources for the selected agents."""
    discovered: list[SourceHandle] = []
    for agent in agents:
        if agent == "codex":
            discovered.extend(discover_codex_sources(home, backends))
        elif agent == "claude":
            discovered.extend(discover_claude_sources(home, backends))
        elif agent == "cursor":
            discovered.extend(discover_cursor_sources(home, backends))
        elif agent == "gemini":
            discovered.extend(discover_gemini_sources(home, backends))
    discovered.sort(key=lambda item: (item.agent, item.store, str(item.path)))
    return discovered


def file_mtime_ns(path: pathlib.Path) -> int:
    """Return a cached modification time for a path."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def resolve_env_root(env_var: str, default: pathlib.Path) -> pathlib.Path:
    """Resolve a base directory from an environment variable, with safety.

    When ``env_var`` is set to a non-empty path that is an existing directory,
    return that path. When it is set but points to a non-existent or
    non-directory location, emit a ``WARNING`` log and fall back to
    ``default``. When unset or empty, return ``default``.

    Parameters
    ----------
    env_var : str
        Environment variable name (e.g. ``"CODEX_HOME"``).
    default : pathlib.Path
        Fallback path when the env var is unset, empty, or unusable.

    Returns
    -------
    pathlib.Path
        Resolved base directory.
    """
    value = os.environ.get(env_var)
    if not value:
        return default
    candidate = pathlib.Path(value)
    if candidate.is_dir():
        return candidate
    status = "not_a_directory" if candidate.exists() else "not_found"
    logger.warning(
        "env-override path unavailable, fell back to default",
        extra={
            "agentgrep_env_var": env_var,
            "agentgrep_env_path": value,
            "agentgrep_env_path_status": status,
        },
    )
    return default


def handles_from_discovery(
    spec: DiscoverySpec,
    agent: AgentName,
    root: pathlib.Path,
    backends: BackendSelection,
) -> list[SourceHandle]:
    """Produce ``SourceHandle``s from a :class:`DiscoverySpec`.

    Applies the spec's ``home_subpath`` under ``root`` to derive the search
    root, then enumerates source files via ``files`` (single-file lookups),
    ``glob`` (recursive walk with optional ``path_parts_required`` filter),
    and ``platform_paths`` (absolute paths).
    """
    sources: list[SourceHandle] = []
    search_root = root.joinpath(*spec.home_subpath) if spec.home_subpath else root

    for name in spec.files:
        candidate = search_root / name
        if candidate.is_file():
            sources.append(
                SourceHandle(
                    agent=agent,
                    store=spec.store,
                    adapter_id=spec.adapter_id,
                    path=candidate,
                    path_kind=spec.path_kind,
                    source_kind=spec.source_kind,
                    search_root=None,
                    mtime_ns=file_mtime_ns(candidate),
                ),
            )

    if spec.glob is not None and search_root.exists():
        required_parts = set(spec.path_parts_required)
        for path in list_files_matching(search_root, spec.glob, backends.find_tool):
            if required_parts and not required_parts.issubset(path.parts):
                continue
            sources.append(
                SourceHandle(
                    agent=agent,
                    store=spec.store,
                    adapter_id=spec.adapter_id,
                    path=path,
                    path_kind=spec.path_kind,
                    source_kind=spec.source_kind,
                    search_root=search_root,
                    mtime_ns=file_mtime_ns(path),
                ),
            )

    for absolute_path_str in spec.platform_paths:
        candidate = pathlib.Path(absolute_path_str).expanduser()
        if candidate.is_file():
            sources.append(
                SourceHandle(
                    agent=agent,
                    store=spec.store,
                    adapter_id=spec.adapter_id,
                    path=candidate,
                    path_kind=spec.path_kind,
                    source_kind=spec.source_kind,
                    search_root=None,
                    mtime_ns=file_mtime_ns(candidate),
                ),
            )

    return sources


def isoformat_from_mtime_ns(mtime_ns: int) -> str | None:
    """Convert a nanosecond ``mtime`` to an ISO-8601 UTC timestamp.

    Used as a timestamp fallback for stores whose records carry no native
    timestamp — most notably Cursor CLI agent transcripts.
    """
    if mtime_ns <= 0:
        return None
    return (
        datetime.datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=datetime.UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def format_timestamp_tig(value: str | None) -> str:
    """Render an ISO-8601 timestamp as ``YYYY-MM-DD HH:MM ±HHMM`` (tig style).

    Localizes to the system timezone before formatting so the displayed
    time matches what the user expects to see — tig's main view does the
    same. Returns ``""`` for ``None`` / empty input and a clipped raw
    string for unparseable input so callers can pad consistently.

    Examples
    --------
    >>> format_timestamp_tig(None)
    ''
    >>> format_timestamp_tig("")
    ''
    >>> # An ISO timestamp with explicit timezone — formatted result keeps
    >>> # the offset for the system's local timezone (whose exact value
    >>> # varies by host, so we just check shape here).
    >>> sample = format_timestamp_tig("2026-05-17T11:59:12+00:00")
    >>> len(sample)
    22
    >>> sample[4], sample[7], sample[10], sample[13], sample[16]
    ('-', '-', ' ', ':', ' ')
    >>> format_timestamp_tig("not-a-real-timestamp")
    'not-a-real-timestamp'
    """
    if not value:
        return ""
    candidate = value.replace("Z", "+00:00")
    try:
        moment = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        return value[:22]
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %z")


def discover_from_catalog(
    home: pathlib.Path,
    agent: AgentName,
    base: pathlib.Path,
    backends: BackendSelection,
) -> list[SourceHandle]:
    """Walk every catalogue row for ``agent`` and emit ``SourceHandle``s.

    Each row's :class:`agentgrep.stores.DiscoverySpec` entries drive
    enumeration via :func:`handles_from_discovery`. Rows whose ``discovery``
    tuple is empty are documentary-only and contribute no sources.
    Rows whose ``search_by_default`` is exactly ``False`` are skipped so
    the catalogue contract documented in
    :mod:`agentgrep.store_catalog` is honoured at runtime;
    ``True`` and ``None`` (decision-deferred) are searched.
    """
    from agentgrep.store_catalog import CATALOG

    sources: list[SourceHandle] = []
    for descriptor in CATALOG.for_agent(agent):
        if descriptor.search_by_default is False:
            continue
        # Per-descriptor dedup: a row whose discovery tuple has more than one
        # spec (e.g. Cursor IDE state.vscdb with both modern platform_paths
        # and a legacy ~/.cursor glob) must not yield the same file twice
        # under different adapter ids on layouts where both specs match.
        seen_paths: set[pathlib.Path] = set()
        for spec in descriptor.discovery:
            for handle in handles_from_discovery(spec, agent, base, backends):
                if handle.path in seen_paths:
                    continue
                seen_paths.add(handle.path)
                sources.append(handle)
    return sources


def discover_codex_sources(
    home: pathlib.Path,
    backends: BackendSelection,
) -> list[SourceHandle]:
    """Discover Codex sessions and command history.

    Honours the ``CODEX_HOME`` environment variable (see upstream
    ``codex-rs/utils/home-dir/src/lib.rs``); falls back to ``${HOME}/.codex``
    when unset or empty. Path roots, globs, file lists, and adapter metadata
    come from the ``codex.*`` rows of
    :data:`agentgrep.store_catalog.CATALOG`.
    """
    root = resolve_env_root("CODEX_HOME", home / ".codex")
    if not root.exists():
        return []
    return discover_from_catalog(home, "codex", root, backends)


def discover_claude_sources(
    home: pathlib.Path,
    backends: BackendSelection,
) -> list[SourceHandle]:
    """Discover Claude Code project session files.

    Path roots, globs, and adapter metadata come from the ``claude.*`` rows
    of :data:`agentgrep.store_catalog.CATALOG`.
    """
    return discover_from_catalog(home, "claude", home, backends)


def discover_cursor_sources(
    home: pathlib.Path,
    backends: BackendSelection,
) -> list[SourceHandle]:
    """Discover Cursor databases from both home-local and official roots.

    Includes the AI-tracking SQLite, Cursor IDE platform-specific
    ``state.vscdb`` locations, the legacy ``~/.cursor/state.vscdb`` glob,
    and the Cursor CLI agent transcripts. Driven entirely by the
    ``cursor.*`` catalogue rows.
    """
    return discover_from_catalog(home, "cursor", home, backends)


def discover_gemini_sources(
    home: pathlib.Path,
    backends: BackendSelection,
) -> list[SourceHandle]:
    """Discover Gemini CLI sessions and prompt logs.

    Honours the ``GEMINI_CLI_HOME`` environment variable (see upstream
    ``packages/cli/index.ts``); falls back to ``${HOME}/.gemini`` when
    unset or empty. Path roots, globs, and adapter metadata come from the
    ``gemini.*`` rows of :data:`agentgrep.store_catalog.CATALOG`.
    """
    base = resolve_env_root("GEMINI_CLI_HOME", home / ".gemini")
    if not base.exists():
        return []
    return discover_from_catalog(home, "gemini", base, backends)


def list_files_matching(
    root: pathlib.Path,
    glob_pattern: str,
    fd_program: str | None,
) -> list[pathlib.Path]:
    """List files under ``root`` that match a glob."""
    if not root.exists():
        return []
    if fd_program is not None:
        command = [
            fd_program,
            "-H",
            "-I",
            "-t",
            "f",
            "--glob",
            glob_pattern,
            str(root),
        ]
        completed = run_readonly_command(command)
        if completed.returncode == 0:
            return [pathlib.Path(line) for line in completed.stdout.splitlines() if line.strip()]
    return sorted(path for path in root.rglob(glob_pattern) if path.is_file())


def search_sources(
    query: SearchQuery,
    sources: list[SourceHandle],
    backends: BackendSelection,
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
) -> list[SearchRecord]:
    """Parse and filter search results across all selected sources."""
    active_progress = noop_search_progress() if progress is None else progress
    active_control = SearchControl() if control is None else control
    planned_sources = plan_search_sources(
        query,
        sources,
        backends,
        progress=active_progress,
        control=active_control,
    )
    if active_control.answer_now_requested():
        active_progress.answer_now(0)
        return []
    active_progress.sources_planned(len(planned_sources), len(sources))
    records = collect_search_records(
        query,
        planned_sources,
        progress=active_progress,
        control=active_control,
    )
    if active_control.answer_now_requested():
        active_progress.answer_now(len(records))
    else:
        active_progress.finish(len(records))
    return records


def run_search_query(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    backends: BackendSelection | None = None,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
) -> list[SearchRecord]:
    """Discover sources and run a normalized search query."""
    active_backends = select_backends() if backends is None else backends
    active_progress = noop_search_progress() if progress is None else progress
    active_control = SearchControl() if control is None else control
    active_progress.start(query)
    interrupted = False
    try:
        sources = discover_sources(home, query.agents, active_backends)
        active_progress.sources_discovered(len(sources))
        return search_sources(
            query,
            sources,
            active_backends,
            progress=active_progress,
            control=active_control,
        )
    except KeyboardInterrupt:
        interrupted = True
        active_progress.interrupt()
        raise
    finally:
        if not interrupted:
            active_progress.close()


def plan_search_sources(
    query: SearchQuery,
    sources: list[SourceHandle],
    backends: BackendSelection,
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
) -> list[SourceHandle]:
    """Return the candidate sources to parse for a search query."""
    active_progress = noop_search_progress() if progress is None else progress
    active_control = SearchControl() if control is None else control
    if not query.terms:
        return sources

    planned_sources = list(sources)
    if backends.grep_tool is not None:
        planned_sources = prefilter_sources_by_root(
            query,
            planned_sources,
            backends.grep_tool,
            progress=active_progress,
            control=active_control,
        )
    ordered_sources = [
        source
        for source in planned_sources
        if not active_control.answer_now_requested()
        and (
            source.search_root is not None
            or direct_source_matches(source, query, backends, active_control)
        )
    ]
    ordered_sources.sort(key=source_order_key)
    return ordered_sources


def source_order_key(source: SourceHandle) -> tuple[int, str]:
    """Return a newest-first search order key for sources."""
    return (-source.mtime_ns, str(source.path))


def prefilter_sources_by_root(
    query: SearchQuery,
    sources: list[SourceHandle],
    grep_program: str,
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
) -> list[SourceHandle]:
    """Prefilter file-backed sources by searching each root once."""
    active_progress = noop_search_progress() if progress is None else progress
    active_control = SearchControl() if control is None else control
    matched_paths_by_root: dict[pathlib.Path, set[pathlib.Path] | None] = {}
    filtered_sources: list[SourceHandle] = []
    for source in sources:
        if active_control.answer_now_requested():
            break
        search_root = source.search_root
        if search_root is None:
            filtered_sources.append(source)
            continue

        if search_root not in matched_paths_by_root:
            active_progress.prefilter_started(search_root)
            matched_paths_by_root[search_root] = grep_root_paths(
                search_root,
                query,
                grep_program,
                control=active_control,
            )
            if active_control.answer_now_requested():
                break

        matched_paths = matched_paths_by_root[search_root]
        if matched_paths is None or source.path in matched_paths:
            filtered_sources.append(source)
    return filtered_sources


def grep_root_paths(
    search_root: pathlib.Path,
    query: SearchQuery,
    grep_program: str,
    *,
    control: SearchControl | None = None,
) -> set[pathlib.Path] | None:
    """Return file paths matched by a whole-root grep."""
    active_control = SearchControl() if control is None else control
    matched_sets: list[set[pathlib.Path]] = []
    for term in query.terms:
        if active_control.answer_now_requested():
            return set()
        command = build_grep_command(
            grep_program,
            term,
            search_root,
            regex=query.regex,
            case_sensitive=query.case_sensitive,
        )
        completed = run_readonly_command(command, control=active_control)
        if active_control.answer_now_requested():
            return set()
        if completed.returncode not in {0, 1}:
            return None
        matched_sets.append(
            {pathlib.Path(line) for line in completed.stdout.splitlines() if line.strip()},
        )

    if not matched_sets:
        return set()
    if query.any_term:
        merged: set[pathlib.Path] = set()
        for matched in matched_sets:
            merged.update(matched)
        return merged

    intersection = matched_sets[0].copy()
    for matched in matched_sets[1:]:
        intersection.intersection_update(matched)
    return intersection


def direct_source_matches(
    source: SourceHandle,
    query: SearchQuery,
    backends: BackendSelection,
    control: SearchControl | None = None,
) -> bool:
    """Return whether a direct source should be parsed."""
    active_control = SearchControl() if control is None else control
    if active_control.answer_now_requested():
        return False
    if source.source_kind == "sqlite":
        return True
    if backends.grep_tool is not None:
        grep_match = grep_file_matches(
            source.path,
            query,
            backends.grep_tool,
            control=active_control,
        )
        if active_control.answer_now_requested():
            return False
        if grep_match is not None:
            return grep_match
    if source.path.suffix in JSON_FILE_SUFFIXES and backends.json_tool is not None:
        extracted = flatten_json_strings_with_tool(
            source.path,
            backends.json_tool,
            control=active_control,
        )
        if active_control.answer_now_requested():
            return False
        if extracted is not None:
            return matches_text(extracted, query)
    return matches_text(read_text_file(source.path), query)


def collect_search_records(
    query: SearchQuery,
    sources: list[SourceHandle],
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
) -> list[SearchRecord]:
    """Parse candidate sources and collect matching records."""
    active_progress = noop_search_progress() if progress is None else progress
    active_control = SearchControl() if control is None else control
    deduped: dict[tuple[str, str, str, str, str], SearchRecord] = {}
    raw: list[SearchRecord] = []
    total = len(sources)

    def current_count() -> int:
        return len(deduped) if query.dedupe else len(raw)

    for index, source in enumerate(sources, start=1):
        if active_control.answer_now_requested() or (
            query.limit is not None and current_count() >= query.limit
        ):
            break
        active_progress.source_started(index, total, source)
        records_seen = 0
        matches_seen = 0
        matching_records: list[SearchRecord] = []
        for record in iter_source_records(source):
            if active_control.answer_now_requested():
                break
            records_seen += 1
            if matches_record(record, query):
                matches_seen += 1
                matching_records.append(record)
        active_progress.source_finished(index, total, source, records_seen, matches_seen)
        matching_records.sort(key=search_record_sort_key, reverse=True)
        for record in matching_records:
            if query.dedupe:
                dedupe_key = record_dedupe_key(record)
                if dedupe_key not in deduped:
                    deduped[dedupe_key] = record
                    active_progress.record_added(record)
                    active_progress.result_added(len(deduped))
            else:
                raw.append(record)
                active_progress.record_added(record)
                active_progress.result_added(len(raw))
            if active_control.answer_now_requested() or (
                query.limit is not None and current_count() >= query.limit
            ):
                break
    results = list(deduped.values()) if query.dedupe else list(raw)
    results.sort(key=search_record_sort_key, reverse=True)
    return results


def find_sources(
    pattern: str | None,
    sources: list[SourceHandle],
    limit: int | None,
) -> list[FindRecord]:
    """Build filtered ``find`` results from discovered sources."""
    query = pattern.casefold() if pattern is not None else None
    results: list[FindRecord] = []
    for source in sources:
        record = FindRecord(
            kind="find",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            path_kind=source.path_kind,
            metadata={"source_kind": source.source_kind},
        )
        if query is not None:
            haystack = " ".join(
                (
                    record.agent,
                    record.store,
                    record.adapter_id,
                    str(record.path),
                    record.path_kind,
                ),
            ).casefold()
            if query not in haystack:
                continue
        results.append(record)
        if limit is not None and len(results) >= limit:
            break
    return results


def run_find_query(
    home: pathlib.Path,
    agents: tuple[AgentName, ...],
    *,
    pattern: str | None,
    limit: int | None,
    backends: BackendSelection | None = None,
) -> list[FindRecord]:
    """Discover sources and build normalized ``find`` results."""
    active_backends = select_backends() if backends is None else backends
    sources = discover_sources(home, agents, active_backends)
    return find_sources(pattern, sources, limit)


def iter_source_records(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Dispatch to the adapter parser for one source."""
    if source.adapter_id == "codex.sessions_jsonl.v1":
        yield from parse_codex_session_file(source)
        return
    if source.adapter_id == "codex.history_json.v1":
        yield from parse_codex_history_file(source)
        return
    if source.adapter_id == "claude.projects_jsonl.v1":
        yield from parse_claude_project_file(source)
        return
    if source.adapter_id == "cursor.ai_tracking_sqlite.v1":
        yield from parse_cursor_ai_tracking_db(source)
        return
    if source.adapter_id in {"cursor.state_vscdb_modern.v1", "cursor.state_vscdb_legacy.v1"}:
        yield from parse_cursor_state_db(source)
        return
    if source.adapter_id == "cursor.cli_jsonl.v1":
        yield from parse_cursor_cli_transcript(source)
        return
    if source.adapter_id == "gemini.tmp_chats_jsonl.v1":
        yield from parse_gemini_chat_file(source)
        return
    if source.adapter_id == "gemini.tmp_chats_legacy_json.v1":
        yield from parse_gemini_chat_legacy_file(source)
        return
    if source.adapter_id == "gemini.tmp_logs_json.v1":
        yield from parse_gemini_logs_file(source)
        return


def parse_codex_session_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Codex session JSONL files."""
    session_id = source.path.stem
    session_model: str | None = None
    for event in iter_jsonl(source.path):
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", ""))
        payload = event.get("payload")
        if event_type == "session_meta" and isinstance(payload, dict):
            session_id = as_optional_str(payload.get("id")) or session_id
            session_model = (
                as_optional_str(payload.get("model"))
                or as_optional_str(payload.get("model_name"))
                or as_optional_str(payload.get("model_provider"))
                or session_model
            )
            continue
        if event_type != "response_item" or not isinstance(payload, dict):
            continue
        candidate = candidate_from_mapping(
            t.cast("dict[str, object]", payload),
            timestamp=as_optional_str(event.get("timestamp")),
            model=session_model,
            session_id=session_id,
            conversation_id=session_id,
        )
        if candidate is None:
            continue
        yield build_search_record(source, candidate)


def parse_codex_history_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Codex command history files."""
    entries: list[JSONValue]
    if source.source_kind == "json":
        payload = read_json_file(source.path)
        entries = payload if isinstance(payload, list) else []
    else:
        entries = list(iter_jsonl(source.path))

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        command = as_optional_str(entry.get("command"))
        if not command:
            continue
        yield SearchRecord(
            kind="history",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=command,
            title="Codex command history",
            role="user",
            timestamp=as_optional_str(entry.get("timestamp")),
        )


def parse_claude_project_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Claude Code project JSONL files using lightweight heuristics."""
    conversation_id = source.path.stem
    seen: set[tuple[str | None, str, str | None, str | None]] = set()
    for event in iter_jsonl(source.path):
        for candidate in iter_message_candidates(
            event,
            fallback_conversation_id=conversation_id,
        ):
            key = (
                candidate.role,
                candidate.text,
                candidate.timestamp,
                candidate.conversation_id,
            )
            if key in seen:
                continue
            seen.add(key)
            yield build_search_record(source, candidate)


def parse_cursor_cli_transcript(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a Cursor CLI agent transcript JSONL file.

    Each line is ``{"role": "user" | "assistant", "message": {"content": [...]}}``;
    ``iter_message_candidates`` handles the nested shape directly. Cursor
    transcripts carry no native per-turn timestamp, so the file's mtime is
    used as a session-level fallback.
    """
    conversation_id = source.path.stem
    fallback_timestamp = isoformat_from_mtime_ns(source.mtime_ns)
    seen: set[tuple[str | None, str, str | None, str | None]] = set()
    for event in iter_jsonl(source.path):
        for candidate in iter_message_candidates(
            event,
            fallback_conversation_id=conversation_id,
        ):
            if candidate.timestamp is None and fallback_timestamp is not None:
                candidate = dataclasses.replace(candidate, timestamp=fallback_timestamp)
            key = (
                candidate.role,
                candidate.text,
                candidate.timestamp,
                candidate.conversation_id,
            )
            if key in seen:
                continue
            seen.add(key)
            yield build_search_record(source, candidate)


def _gemini_thoughts_text(thoughts: object) -> str:
    """Flatten Gemini's ``thoughts[]`` into a single searchable string.

    Each entry carries ``subject`` (short label) and ``description``
    (multi-sentence reasoning). Concatenating them per-record keeps the
    conversation-turn boundary intact while still surfacing the assistant's
    output in the search corpus.
    """
    if not isinstance(thoughts, list):
        return ""
    parts: list[str] = []
    for entry in thoughts:
        if not isinstance(entry, dict):
            continue
        mapping = t.cast("dict[str, object]", entry)
        subject = as_optional_str(mapping.get("subject"))
        description = as_optional_str(mapping.get("description"))
        if subject:
            parts.append(subject)
        if description:
            parts.append(description)
    return "\n".join(parts)


def _gemini_tool_calls_text(tool_calls: object) -> str:
    """Flatten Gemini's ``toolCalls[]`` into a searchable string.

    ``name`` and ``description`` carry the human-readable text; ``args`` is
    JSON-shaped and contributes lower-signal noise, so it is omitted.
    """
    if not isinstance(tool_calls, list):
        return ""
    parts: list[str] = []
    for entry in tool_calls:
        if not isinstance(entry, dict):
            continue
        mapping = t.cast("dict[str, object]", entry)
        name = as_optional_str(mapping.get("name"))
        description = as_optional_str(mapping.get("description"))
        if name:
            parts.append(name)
        if description:
            parts.append(description)
    return "\n".join(parts)


def _gemini_message_record_to_candidate(
    mapping: dict[str, object],
    session_id: str | None,
) -> MessageCandidate | None:
    """Extract a ``MessageCandidate`` from one Gemini MessageRecord.

    For user records the searchable text is the ``content`` field. For
    gemini-typed records the model's prose often lives in ``thoughts[]``
    (with ``content`` empty) and tool invocations live in ``toolCalls[]``;
    both are concatenated into the candidate's text. Returns ``None`` only
    when no field carries any text.
    """
    role = as_optional_str(mapping.get("type"))
    if not role:
        return None
    text_parts: list[str] = []
    content_text = flatten_content_value(
        t.cast("JSONValue | None", mapping.get("content")),
    )
    if content_text:
        text_parts.append(content_text)
    if role == "gemini":
        thoughts_text = _gemini_thoughts_text(mapping.get("thoughts"))
        if thoughts_text:
            text_parts.append(thoughts_text)
        tool_calls_text = _gemini_tool_calls_text(mapping.get("toolCalls"))
        if tool_calls_text:
            text_parts.append(tool_calls_text)
    if not text_parts:
        return None
    return MessageCandidate(
        role=role,
        text="\n".join(text_parts),
        timestamp=as_optional_str(mapping.get("timestamp")),
        model=as_optional_str(mapping.get("model")),
        session_id=session_id or as_optional_str(mapping.get("sessionId")),
        conversation_id=session_id,
    )


def parse_gemini_chat_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a Gemini CLI chat session JSONL file.

    The file mixes record kinds: a leading ``SessionMetadataRecord``
    (``{"sessionId", "projectHash", "startTime", "lastUpdated", "kind"}``),
    ``MessageRecord`` turns (``{"id", "timestamp", "type": "user"|"gemini",
    "content"}``), and ``MetadataUpdateRecord`` updates (``{"$set": {...}}``).
    Gemini stores the role in a ``type`` key — not the ``role`` key the
    shared ``extract_role`` helper recognises — so this adapter extracts
    fields directly rather than going through ``iter_message_candidates``.
    """
    session_id: str | None = None
    for event in iter_jsonl(source.path):
        if not isinstance(event, dict):
            continue
        mapping = t.cast("dict[str, object]", event)
        if "$set" in mapping:
            continue
        if "kind" in mapping:
            # SessionMetadataRecord: upstream discriminates by ``kind``
            # (e.g. ``"main"``) rather than by the absence of ``type``,
            # so this stays correct even if a future schema adds a
            # ``type`` field to the metadata record.
            session_id = as_optional_str(mapping.get("sessionId"))
            continue
        candidate = _gemini_message_record_to_candidate(mapping, session_id)
        if candidate is None:
            continue
        yield build_search_record(source, candidate)


def parse_gemini_chat_legacy_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a pre-Feb 2026 Gemini CLI single-file ``.json`` chat session.

    The legacy format is a JSON object with session metadata at the top
    level and the full conversation under a ``messages`` array. Upstream
    still reads this shape via the ``isLegacyRecord`` discriminator at
    ``packages/core/src/services/chatRecordingService.ts``. Each entry of
    ``messages`` carries the same per-turn fields the JSONL format uses,
    so record extraction is shared with :func:`parse_gemini_chat_file`.
    """
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    container = t.cast("dict[str, object]", payload)
    session_id = as_optional_str(container.get("sessionId"))
    messages = container.get("messages")
    if not isinstance(messages, list):
        return
    for entry in messages:
        if not isinstance(entry, dict):
            continue
        mapping = t.cast("dict[str, object]", entry)
        candidate = _gemini_message_record_to_candidate(mapping, session_id)
        if candidate is None:
            continue
        yield build_search_record(source, candidate)


def parse_gemini_logs_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a Gemini CLI ``logs.json`` file (flat JSON array of LogEntry).

    Records are emitted as ``kind="history"`` — the file is an audit log of
    user prompts, the same role ``codex.history`` plays for Codex.
    """
    payload = read_json_file(source.path)
    entries = payload if isinstance(payload, list) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mapping = t.cast("dict[str, object]", entry)
        message = as_optional_str(mapping.get("message"))
        if not message:
            continue
        session_id = as_optional_str(mapping.get("sessionId"))
        yield SearchRecord(
            kind="history",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=message,
            title="Gemini prompt history",
            role=as_optional_str(mapping.get("type")) or "user",
            timestamp=as_optional_str(mapping.get("timestamp")),
            session_id=session_id,
            conversation_id=session_id,
        )


def parse_cursor_ai_tracking_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Cursor AI tracking summaries."""
    connection = open_readonly_sqlite(source.path)
    try:
        for row in iter_conversation_summaries(connection):
            (
                conversation_id,
                title,
                tldr,
                overview,
                bullets,
                model,
                mode,
                updated_at,
            ) = row
            text_parts = [
                part
                for part in (
                    as_optional_str(title),
                    as_optional_str(tldr),
                    as_optional_str(overview),
                    flatten_summary_bullets(bullets),
                )
                if part
            ]
            if not text_parts:
                continue
            yield SearchRecord(
                kind="history",
                agent=source.agent,
                store=source.store,
                adapter_id=source.adapter_id,
                path=source.path,
                text="\n\n".join(text_parts),
                title=as_optional_str(title),
                role="assistant",
                timestamp=as_optional_str(updated_at),
                model=as_optional_str(model),
                conversation_id=as_optional_str(conversation_id),
                metadata={"mode": as_optional_str(mode) or ""},
            )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_cursor_state_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Cursor ``state.vscdb`` tables with generic JSON extraction."""
    connection = open_readonly_sqlite(source.path)
    try:
        tables = sqlite_table_names(connection)
        candidate_tables = [name for name in ("ItemTable", "cursorDiskKV") if name in tables]
        seen: set[tuple[str | None, str, str | None, str | None]] = set()
        for table in candidate_tables:
            for key, raw_value in iter_key_value_rows(connection, table):
                lowered_key = key.casefold()
                if not any(token in lowered_key for token in CURSOR_STATE_TOKENS):
                    continue
                decoded = decode_sqlite_value(raw_value)
                if decoded is None:
                    continue
                parsed = parse_embedded_json(decoded)
                if parsed is None:
                    continue
                for candidate in iter_message_candidates(
                    parsed,
                    fallback_title=key,
                    fallback_conversation_id=key,
                ):
                    entry_key = (
                        candidate.role,
                        candidate.text,
                        candidate.timestamp,
                        candidate.conversation_id,
                    )
                    if entry_key in seen:
                        continue
                    seen.add(entry_key)
                    yield build_search_record(source, candidate)
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def open_readonly_sqlite(path: pathlib.Path) -> sqlite3.Connection:
    """Open a SQLite database with a read-only URI."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def sqlite_table_names(connection: sqlite3.Connection) -> set[str]:
    """Return the table names from a SQLite connection."""
    rows = t.cast(
        "cabc.Iterable[tuple[object]]",
        connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'"),
    )
    names: set[str] = set()
    for row in rows:
        name = row[0]
        if isinstance(name, str):
            names.add(name)
    return names


def iter_key_value_rows(
    connection: sqlite3.Connection,
    table: str,
) -> cabc.Iterator[tuple[str, object]]:
    """Yield likely key/value rows from a SQLite table."""
    if table not in {"ItemTable", "cursorDiskKV"}:
        return
    info = t.cast(
        "cabc.Iterable[tuple[object, ...]]",
        connection.execute(f"PRAGMA table_info({table})"),
    )
    columns = [str(row[1]) for row in info]
    if "key" not in columns or "value" not in columns:
        return
    query = "SELECT key, value FROM ItemTable"
    if table == "cursorDiskKV":
        query = "SELECT key, value FROM cursorDiskKV"
    rows = t.cast("cabc.Iterable[KeyValueRow]", connection.execute(query))
    for key, value in rows:
        if isinstance(key, str):
            yield key, value


def iter_conversation_summaries(
    connection: sqlite3.Connection,
) -> cabc.Iterator[SummaryRow]:
    """Yield typed rows from Cursor AI tracking summaries."""
    query = """
        SELECT
            conversationId,
            title,
            tldr,
            overview,
            summaryBullets,
            model,
            mode,
            updatedAt
        FROM conversation_summaries
    """
    rows = t.cast("cabc.Iterable[SummaryRow]", connection.execute(query))
    yield from rows


def build_grep_command(
    grep_program: str,
    term: str,
    target: pathlib.Path,
    *,
    regex: bool,
    case_sensitive: bool,
) -> list[str]:
    """Build a read-only grep command for one term and target.

    Always passes flags that disable ignore-file semantics — agent stores live
    inside the user's ``$HOME`` and may sit beneath a ``.gitignore`` from a
    dotfile manager (yadm, chezmoi, stow, bare-git). The grep tools would
    otherwise silently skip everything.
    """
    if grep_program.endswith("rg"):
        ignore_flags = ["--no-ignore", "--hidden"]
        fixed_flag = "-F"
    else:
        ignore_flags = ["--unrestricted", "--hidden"]
        fixed_flag = "-Q"
    command = [grep_program, *ignore_flags, "-l", term, str(target)]
    if not regex:
        command.insert(command.index("-l"), fixed_flag)
    if not case_sensitive:
        command.insert(1, "-i")
    return command


def flatten_json_strings_with_tool(
    path: pathlib.Path,
    program: str,
    *,
    control: SearchControl | None = None,
) -> str | None:
    """Return flattened JSON strings using ``jq`` or ``jaq``."""
    command = [program, "-r", ".. | strings", str(path)]
    completed = run_readonly_command(command, control=control)
    if completed.returncode != 0:
        return None
    return completed.stdout


def grep_file_matches(
    path: pathlib.Path,
    query: SearchQuery,
    program: str,
    *,
    control: SearchControl | None = None,
) -> bool | None:
    """Use ``rg`` or ``ag`` as a read-only prefilter."""
    active_control = SearchControl() if control is None else control
    matchers = [
        run_readonly_command(
            build_grep_command(
                program,
                term,
                path,
                regex=query.regex,
                case_sensitive=query.case_sensitive,
            ),
            control=active_control,
        ).returncode
        == 0
        for term in query.terms
        if not active_control.answer_now_requested()
    ]
    if active_control.answer_now_requested():
        return False
    return any(matchers) if query.any_term else all(matchers)


def read_text_file(path: pathlib.Path) -> str:
    """Read a text file with replacement for decode errors."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def read_json_file(path: pathlib.Path) -> JSONValue | None:
    """Read a JSON file."""
    try:
        parsed = t.cast("object", json.loads(path.read_text(encoding="utf-8")))
    except OSError, json.JSONDecodeError:
        return None
    if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
        return t.cast("JSONValue", parsed)
    return None


def iter_jsonl(path: pathlib.Path) -> cabc.Iterator[JSONValue]:
    """Yield decoded JSON objects from a JSONL file."""
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    parsed = t.cast("object", json.loads(stripped))
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
                    yield t.cast("JSONValue", parsed)
    except OSError:
        return


def candidate_from_mapping(
    mapping: dict[str, object],
    *,
    timestamp: str | None,
    model: str | None,
    session_id: str | None,
    conversation_id: str | None,
) -> MessageCandidate | None:
    """Extract one message candidate from a known message-like mapping."""
    role = extract_role(mapping)
    text = extract_message_text(mapping)
    if role is None or not text:
        return None
    return MessageCandidate(
        role=role,
        text=text,
        title=extract_title(mapping),
        timestamp=timestamp or extract_timestamp(mapping),
        model=model or extract_model(mapping),
        session_id=session_id or extract_session_id(mapping),
        conversation_id=conversation_id or extract_conversation_id(mapping),
    )


def iter_message_candidates(
    value: JSONValue | None,
    *,
    fallback_title: str | None = None,
    fallback_conversation_id: str | None = None,
) -> cabc.Iterator[MessageCandidate]:
    """Recursively walk a JSON value and yield message candidates."""
    if isinstance(value, dict):
        mapping = t.cast("dict[str, object]", value)
        role = extract_role(mapping)
        text = extract_message_text(mapping)
        if role is not None and text:
            yield MessageCandidate(
                role=role,
                text=text,
                title=extract_title(mapping) or fallback_title,
                timestamp=extract_timestamp(mapping),
                model=extract_model(mapping),
                session_id=extract_session_id(mapping),
                conversation_id=extract_conversation_id(mapping) or fallback_conversation_id,
            )
        for nested in mapping.values():
            yield from iter_message_candidates(
                t.cast("JSONValue | None", nested),
                fallback_title=fallback_title,
                fallback_conversation_id=fallback_conversation_id,
            )
    elif isinstance(value, list):
        for item in value:
            yield from iter_message_candidates(
                item,
                fallback_title=fallback_title,
                fallback_conversation_id=fallback_conversation_id,
            )


def extract_role(mapping: dict[str, object]) -> str | None:
    """Extract a normalized role from a mapping."""
    for key in ("role", "sender", "author", "speaker"):
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested_mapping = t.cast("dict[str, object]", value)
            nested = as_optional_str(nested_mapping.get("role")) or as_optional_str(
                nested_mapping.get("name"),
            )
            if nested is not None:
                return nested
    return None


def extract_message_text(mapping: dict[str, object]) -> str | None:
    """Extract message text from common content fields."""
    for key in ("content", "text", "message", "body", "prompt", "value", "parts"):
        if key in mapping:
            flattened = flatten_content_value(t.cast("JSONValue | None", mapping[key]))
            if flattened:
                return flattened
    return None


def flatten_content_value(value: JSONValue | None) -> str | None:
    """Flatten a message content payload into text."""
    parts = list(iter_text_fragments(value))
    if not parts:
        return None
    return "\n".join(part for part in parts if part.strip()).strip() or None


def iter_text_fragments(
    value: JSONValue | None,
) -> cabc.Iterator[str]:
    """Yield text fragments from a nested content payload."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            yield stripped
        return
    if isinstance(value, list):
        for item in value:
            yield from iter_text_fragments(item)
        return
    if isinstance(value, dict):
        mapping = t.cast("dict[str, object]", value)
        for key in ("text", "content", "message", "body", "prompt", "value", "parts"):
            if key in mapping:
                yield from iter_text_fragments(t.cast("JSONValue | None", mapping[key]))


def extract_title(mapping: dict[str, object]) -> str | None:
    """Extract a title-like field."""
    for key in ("title", "name", "topic"):
        title = as_optional_str(mapping.get(key))
        if title is not None:
            return title
    return None


def extract_timestamp(mapping: dict[str, object]) -> str | None:
    """Extract a timestamp-like field."""
    for key in ("timestamp", "updatedAt", "createdAt", "ts"):
        timestamp = as_optional_str(mapping.get(key))
        if timestamp is not None:
            return timestamp
    return None


def extract_model(mapping: dict[str, object]) -> str | None:
    """Extract a model name."""
    for key in ("model", "modelName", "model_name"):
        model = as_optional_str(mapping.get(key))
        if model is not None:
            return model
    return None


def extract_session_id(mapping: dict[str, object]) -> str | None:
    """Extract a session identifier."""
    for key in ("session_id", "sessionId", "id"):
        value = as_optional_str(mapping.get(key))
        if value is not None:
            return value
    return None


def extract_conversation_id(mapping: dict[str, object]) -> str | None:
    """Extract a conversation identifier."""
    for key in ("conversation_id", "conversationId", "threadId"):
        value = as_optional_str(mapping.get(key))
        if value is not None:
            return value
    return None


def flatten_summary_bullets(value: object) -> str | None:
    """Flatten Cursor summary bullets."""
    if value is None:
        return None
    if isinstance(value, str):
        parsed = parse_embedded_json(value)
        if isinstance(parsed, list):
            bullets = [item for item in parsed if isinstance(item, str) and item.strip()]
            return "\n".join(f"- {item}" for item in bullets) if bullets else value.strip() or None
        return value.strip() or None
    if isinstance(value, (bytes, bytearray)):
        decoded = decode_sqlite_value(value)
        return flatten_summary_bullets(decoded)
    return None


def decode_sqlite_value(value: object) -> str | None:
    """Decode a SQLite value into UTF-8 text if possible."""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return None


def parse_embedded_json(text: str) -> JSONValue | None:
    """Parse a JSON-encoded string, returning ``None`` when unavailable."""
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        parsed = t.cast("object", json.loads(stripped))
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
        return t.cast("JSONValue", parsed)
    return None


def build_search_record(source: SourceHandle, candidate: MessageCandidate) -> SearchRecord:
    """Convert a parsed candidate into a normalized search record."""
    role = candidate.role.casefold() if candidate.role is not None else None
    kind: t.Literal["prompt", "history"] = "prompt" if role in USER_ROLES else "history"
    return SearchRecord(
        kind=kind,
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=candidate.text,
        title=candidate.title,
        role=candidate.role,
        timestamp=candidate.timestamp,
        model=candidate.model,
        session_id=candidate.session_id,
        conversation_id=candidate.conversation_id,
    )


def matches_record(record: SearchRecord, query: SearchQuery) -> bool:
    """Return whether a normalized record should be included."""
    if query.search_type == "prompts" and record.kind != "prompt":
        return False
    if query.search_type == "history" and record.kind != "history":
        return False
    return matches_text(build_search_haystack(record), query)


def build_search_haystack(record: SearchRecord) -> str:
    """Build a searchable text surface for a record."""
    parts = [
        record.title or "",
        record.text,
        record.model or "",
        record.role or "",
        str(record.path),
    ]
    return "\n".join(part for part in parts if part)


_HAYSTACK_CACHE: dict[int, str] = {}


def cached_haystack(record: SearchRecord) -> str:
    """Return the casefolded haystack for ``record``, memoized by ``id``.

    The filter worker scans every loaded record on every keystroke;
    recomputing ``build_search_haystack(...).casefold()`` per record per
    pass dominates filter latency once the result set grows past a few
    thousand records. Memoizing by ``id`` is safe because the app
    retains every record in ``AgentGrepApp.all_records`` for the
    lifetime of one search, so Python cannot recycle a collected
    record's id while its entry sits in :data:`_HAYSTACK_CACHE`.

    Callers that need to invalidate (because a new search will allocate
    new records) should call :func:`clear_haystack_cache`.
    """
    key = id(record)
    cached = _HAYSTACK_CACHE.get(key)
    if cached is None:
        cached = build_search_haystack(record).casefold()
        _HAYSTACK_CACHE[key] = cached
    return cached


def clear_haystack_cache() -> None:
    """Drop every memoized haystack — call before allocating a new record set."""
    _HAYSTACK_CACHE.clear()


def compute_filter_matches(
    records: cabc.Sequence[SearchRecord],
    text: str,
) -> tuple[SearchRecord, ...]:
    """Return the subset of ``records`` whose haystack contains ``text`` (case-fold).

    Used by the TUI's filter worker. Pure function so the filter logic is
    directly unit-testable without spinning up a Textual app.

    Parameters
    ----------
    records : Sequence[SearchRecord]
        Records to test.
    text : str
        Filter text. Whitespace-trimmed and case-folded before matching.
        An empty (or whitespace-only) ``text`` returns all records.

    Returns
    -------
    tuple[SearchRecord, ...]
        Matching records in input order.
    """
    normalized = text.strip().casefold()
    if not normalized:
        return tuple(records)
    return tuple(record for record in records if normalized in cached_haystack(record))


def matches_text(text: str, query: SearchQuery) -> bool:
    """Return whether ``text`` matches the query."""
    if not query.terms:
        return True
    if query.regex:
        flags = 0 if query.case_sensitive else re.IGNORECASE
        results = [re.search(term, text, flags) is not None for term in query.terms]
    else:
        haystack = text if query.case_sensitive else text.casefold()
        needles = (
            query.terms if query.case_sensitive else tuple(term.casefold() for term in query.terms)
        )
        results = [needle in haystack for needle in needles]
    return any(results) if query.any_term else all(results)


def search_record_sort_key(record: SearchRecord) -> tuple[str, str, str]:
    """Return a stable sort key."""
    return (record.timestamp or "", record.agent, str(record.path))


def record_dedupe_key(record: SearchRecord) -> tuple[str, str, str, str, str]:
    """Return the per-session dedupe key for a search record."""
    session_identity = record.session_id or record.conversation_id or str(record.path)
    return (
        record.kind,
        record.agent,
        record.store,
        session_identity,
        record.text,
    )


def as_optional_str(value: object) -> str | None:
    """Return a stripped string when possible."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def maybe_use_pydantic() -> tuple[
    t.Callable[[SearchRecord], dict[str, object]],
    t.Callable[[FindRecord], dict[str, object]],
    EnvelopeFactory,
]:
    """Return serializers backed by Pydantic when available."""
    pydantic_module = t.cast(
        "PydanticModule",
        t.cast("object", importlib.import_module("pydantic")),
    )
    search_adapter = pydantic_module.TypeAdapter(SearchRecordPayload)
    find_adapter = pydantic_module.TypeAdapter(FindRecordPayload)
    envelope_adapter = pydantic_module.TypeAdapter(EnvelopePayload)

    def pydantic_search(record: SearchRecord) -> dict[str, object]:
        payload = search_adapter.validate_python(serialize_search_record(record))
        dumped = search_adapter.dump_python(payload, mode="json")
        return t.cast("dict[str, object]", dumped)

    def pydantic_find(record: FindRecord) -> dict[str, object]:
        payload = find_adapter.validate_python(serialize_find_record(record))
        dumped = find_adapter.dump_python(payload, mode="json")
        return t.cast("dict[str, object]", dumped)

    def pydantic_envelope(
        command: str,
        query_data: dict[str, object],
        results: list[dict[str, object]],
    ) -> dict[str, object]:
        payload = envelope_adapter.validate_python(
            build_envelope(command, query_data, results),
        )
        dumped = envelope_adapter.dump_python(payload, mode="json")
        return t.cast("dict[str, object]", dumped)

    return pydantic_search, pydantic_find, pydantic_envelope


def search_progress_enabled(args: SearchArgs) -> bool:
    """Return whether search progress should be shown for ``args``."""
    human_output = args.output_mode in {"text", "ui"}
    return args.progress_mode == "always" or (args.progress_mode == "auto" and human_output)


def should_enable_answer_now(
    args: SearchArgs,
    *,
    stdin: t.TextIO | None = None,
    stderr: t.TextIO | None = None,
) -> bool:
    """Return whether Enter should request a partial answer for this search."""
    input_stream = stdin if stdin is not None else sys.stdin
    error_stream = stderr if stderr is not None else sys.stderr
    return (
        args.output_mode == "text"
        and search_progress_enabled(args)
        and bool(getattr(input_stream, "isatty", lambda: False)())
        and bool(getattr(error_stream, "isatty", lambda: False)())
    )


def build_search_progress(args: SearchArgs, *, answer_now_hint: bool = False) -> SearchProgress:
    """Build the progress reporter for a search invocation."""
    enabled = search_progress_enabled(args)
    if not enabled:
        return noop_search_progress()
    return ConsoleSearchProgress(
        enabled=True,
        color_mode=args.color_mode,
        answer_now_hint=answer_now_hint,
    )


def run_ui(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
) -> None:
    """Launch the streaming Textual explorer for ``query``.

    Thin wrapper that imports the real implementation from
    :mod:`agentgrep.ui.app` lazily so a bare ``import agentgrep`` never
    pulls in Textual.
    """
    from agentgrep.ui.app import run_ui as _run_ui

    _run_ui(home, query, control=control)


def build_streaming_ui_app(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
) -> object:
    """Construct the streaming Textual app without entering its run loop.

    Thin wrapper that imports the real factory from :mod:`agentgrep.ui.app`
    lazily — Textual is only required at the moment the UI is actually
    built, never at import time of the top-level package.
    """
    from agentgrep.ui.app import build_streaming_ui_app as _build

    return _build(home, query, control=control)


def _exit_on_sigint() -> t.NoReturn:
    """Terminate with Ctrl-C signal semantics where the platform supports them."""
    if sys.platform == "win32":
        raise SystemExit(130)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.raise_signal(signal.SIGINT)
    raise SystemExit(130)  # pragma: no cover


def _write_interrupt_notice() -> None:
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.write("Interrupted by user.\n")
        sys.stderr.flush()


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Run the CLI."""
    try:
        parsed = parse_args(argv)
        if parsed is None:
            return 0
        if isinstance(parsed, GrepArgs):
            return run_grep_command(parsed)
        if isinstance(parsed, FuzzyArgs):
            return run_fuzzy_command(parsed)
        if isinstance(parsed, SearchArgs):
            return run_search_command(parsed)
        if isinstance(parsed, UIArgs):
            return run_ui_command(parsed)
        return run_find_command(parsed)
    except KeyboardInterrupt:
        _write_interrupt_notice()
        _exit_on_sigint()


from agentgrep.cli.parser import (  # noqa: E402  (re-exports must follow main definition)
    CaseMode,
    FindArgs,
    FindPatternMode,
    FindTypeFilter,
    FuzzyAlgo,
    FuzzyArgs,
    FuzzyTiebreak,
    GrepArgs,
    ParserBundle,
    PatternMode,
    SearchArgs,
    UIArgs,
    add_common_agent_options,
    add_output_mode_options,
    build_docs_parser,
    configured_color_environment,
    create_parser,
    normalize_color_mode,
    parse_agents,
    parse_args,
    parse_output_mode,
)
from agentgrep.cli.render import (  # noqa: E402  (re-exports must follow main definition)
    build_envelope,
    build_grep_query,
    filter_find_records,
    format_grep_record,
    fuzzy_filter_lines,
    maybe_build_pydantic,
    print_find_results,
    print_grep_results,
    print_search_results,
    run_find_command,
    run_fuzzy_command,
    run_grep_command,
    run_search_command,
    run_ui_command,
    serialize_find_record,
    serialize_grep_record,
    serialize_search_record,
    serialize_source_handle,
)

if __name__ == "__main__":
    raise SystemExit(main())
