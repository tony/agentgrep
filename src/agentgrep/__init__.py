#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# dependencies = ["pydantic>=2.11.3", "textual>=3.2.0"]
# ///
"""Search local AI agent prompts and conversations without mutating agent stores.

The tool discovers known read-only stores under ``~/.codex``, ``~/.claude``,
``~/.cursor``, and Cursor's official IDE storage locations, then normalizes
results through named adapters.

Examples
--------
List prompts containing both ``serenity`` and ``bliss``:

>>> query = SearchQuery(
...     terms=("serenity", "bliss"),
...     scope="prompts",
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
import functools
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
import tomllib
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
    StoreCoverage,
    StoreDescriptor,
    StoreRole,
    VersionDetectionConfidence,
    VersionDetectionStrategy,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from agentgrep._engine.planning import PhysicalSearchPlan
    from agentgrep._engine.runtime import SearchRuntime
    from agentgrep.query.compile import CompiledQuery

    PrivatePathBase = pathlib.Path
else:
    PrivatePathBase = type(pathlib.Path())

AgentName = t.Literal[
    "codex", "claude", "cursor-cli", "cursor-ide", "gemini", "grok", "pi", "opencode"
]
OutputMode = t.Literal["text", "json", "ndjson", "ui"]
ProgressMode = t.Literal["auto", "always", "never"]
SearchScope = t.Literal["prompts", "conversations", "all"]
SearchMatchSurface = t.Literal["haystack", "text"]
DiscoveryVersionDetail = t.Literal["none", "catalog", "shape"]
DiscoveryStoreRoles = frozenset[StoreRole] | None
ColorMode = t.Literal["auto", "always", "never"]
GrepStyle = t.Literal["default", "pretty"]
type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]
type RawJsonlSkipLine = t.Callable[[str], bool]
type SummaryRow = tuple[object, object, object, object, object, object, object, object]
type KeyValueRow = tuple[object, object]
type DiscoveryRoot = pathlib.Path | tuple[pathlib.Path, ...]
type FindSourceTypeFilter = t.Literal["prompts", "history", "sessions", "all"]

AGENT_CHOICES: tuple[AgentName, ...] = (
    "codex",
    "claude",
    "cursor-cli",
    "cursor-ide",
    "gemini",
    "grok",
    "pi",
    "opencode",
)
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
ITER_SOURCE_RECORD_ADAPTERS: frozenset[str] = frozenset(
    {
        "claude.history_jsonl.v1",
        "claude.app_state_json_summary.v1",
        "claude.commands_text.v1",
        "claude.file_metadata_summary.v1",
        "claude.memory_text.v1",
        "claude.plans_text.v1",
        "claude.plugin_hooks_json.v1",
        "claude.plugin_instruction_text.v1",
        "claude.plugin_manifest_json.v1",
        "claude.project_instruction_text.v1",
        "claude.projects_memory_text.v1",
        "claude.projects_jsonl.v1",
        "claude.session_memory_text.v1",
        "claude.settings_json.v1",
        "claude.skills_text.v1",
        "claude.store_sqlite.v1",
        "claude.tasks_json.v1",
        "claude.teams_json.v1",
        "claude.todos_json.v1",
        "codex.app_state_json_summary.v1",
        "codex.config_backup_toml.v1",
        "codex.config_toml.v1",
        "codex.external_imports_json.v1",
        "codex.file_metadata_summary.v1",
        "codex.goals_sqlite.v1",
        "codex.hooks_json.v1",
        "codex.history_json.v1",
        "codex.history_jsonl.v1",
        "codex.instructions_text.v1",
        "codex.logs_sqlite.v1",
        "codex.memories_sqlite.v1",
        "codex.memories_text.v1",
        "codex.plugin_hooks_json.v1",
        "codex.plugin_instruction_text.v1",
        "codex.plugin_manifest_json.v1",
        "codex.plugin_marketplace_json.v1",
        "codex.project_config_toml.v1",
        "codex.project_skill_text.v1",
        "codex.rules_text.v1",
        "codex.session_index_jsonl.v1",
        "codex.sessions_jsonl.v1",
        "codex.sessions_legacy_json.v1",
        "codex.skills_text.v1",
        "codex.state_sqlite.v1",
        "cursor_cli.ai_tracking_sqlite.v1",
        "cursor_cli.chats_protobuf.v1",
        "cursor_cli.prompt_history_json.v1",
        "cursor_cli.transcripts_jsonl.v1",
        "cursor_ide.state_vscdb_legacy.v1",
        "cursor_ide.state_vscdb_modern.v1",
        "gemini.tmp_chats_jsonl.v1",
        "gemini.tmp_chats_legacy_json.v1",
        "gemini.tmp_logs_json.v1",
        "grok.prompt_history_jsonl.v1",
        "grok.session_search_sqlite.v1",
        "grok.sessions_jsonl.v1",
        "pi.sessions_jsonl.v1",
        "opencode.db_sqlite.v1",
    },
)
EnvelopeFactory = t.Callable[[str, dict[str, object], list[dict[str, object]]], dict[str, object]]

OPTIONS_EXPECTING_VALUE: frozenset[str] = frozenset(
    {
        "--agent",
        "--scope",
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
        "--case-sensitive",
        "--json",
        "--ndjson",
        "--ui",
    },
)
ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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
    Read-only search across Codex, Claude, Cursor, Gemini, Grok, Pi,
    and OpenCode local stores. Pick a subcommand from the list below:
    ``search`` for ranked results with dedup and session grouping,
    ``grep`` for rg-shaped content search, ``find`` for store
    enumeration, ``ui`` for the interactive Textual explorer.
    """,
    (
        (
            "grep",
            (
                "agentgrep grep bliss",
                "agentgrep grep -i 'serene bliss'",
                "agentgrep grep -F --scope conversations TODO",
                "agentgrep grep --json design",
            ),
        ),
        (
            "find",
            (
                "agentgrep find codex",
                "agentgrep find -t prompts -e jsonl",
                "agentgrep find cursor-cli --json",
            ),
        ),
        (
            "ui",
            (
                "agentgrep ui",
                "agentgrep ui bliss",
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
                "agentgrep find cursor-cli --json",
            ),
        ),
    ),
)
UI_DESCRIPTION = build_description(
    """
    Launch the interactive Textual explorer for browsing prompts and
    history across all configured agents.
    """,
    (
        (
            None,
            (
                "agentgrep ui",
                "agentgrep ui bliss",
            ),
        ),
    ),
)
SEARCH_DESCRIPTION = build_description(
    """
    Smart search with relevance ranking, deduplication, and session grouping.
    Uses rapidfuzz for scoring — results sorted by match quality.
    """,
    (
        (
            None,
            (
                "agentgrep search streaming parser",
                "agentgrep search --threshold 70 migration",
                "agentgrep search --no-rank --no-group caching",
                "agentgrep search bliss --json",
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
                "agentgrep grep -F --scope conversations TODO",
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


def _visible_width(text: str) -> int:
    """Return display width after stripping ANSI CSI escape sequences."""
    return len(ANSI_CSI_RE.sub("", text))


def _hard_truncate_ansi(text: str, max_width: int) -> str:
    """Truncate ANSI-colored text to ``max_width`` visible cells."""
    if max_width <= 0:
        return ""
    if _visible_width(text) <= max_width:
        return text
    if max_width == 1:
        return "…"
    output: list[str] = []
    visible = 0
    index = 0
    saw_escape = False
    while index < len(text) and visible < max_width - 1:
        match = ANSI_CSI_RE.match(text, index)
        if match is not None:
            output.append(match.group(0))
            index = match.end()
            saw_escape = True
            continue
        output.append(text[index])
        visible += 1
        index += 1
    output.append("…")
    if saw_escape:
        output.append(AnsiColors.RESET)
    return "".join(output)


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
    coverage: StoreCoverage
    version_detection: SourceVersionDetectionPayload | None
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
    MATCH: t.ClassVar[str] = "\x1b[1;31m"
    LINE_NUMBER: t.ClassVar[str] = "\x1b[32m"
    PATH: t.ClassVar[str] = "\x1b[38;5;177m"
    MUTED: t.ClassVar[str] = "\x1b[34m"
    WHITE: t.ClassVar[str] = "\x1b[37m"
    ACCENT: t.ClassVar[str] = "\x1b[38;5;179m"
    DIM: t.ClassVar[str] = "\x1b[38;5;245m"
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

    def match(self, text: str) -> str:
        """Format text as a matched span (rg-style red+bold)."""
        return self.colorize(text, self.MATCH)

    def line_number(self, text: str) -> str:
        """Format text as a line-number prefix (rg-style green)."""
        return self.colorize(text, self.LINE_NUMBER)

    def path(self, text: str) -> str:
        """Format text as a path prefix (bright purple)."""
        return self.colorize(text, self.PATH)

    def muted(self, text: str) -> str:
        """Format text as muted."""
        return self.colorize(text, self.MUTED)

    def white(self, text: str) -> str:
        """Format text as plain white."""
        return self.colorize(text, self.WHITE)

    def accent(self, text: str) -> str:
        """Format text as a warm-amber search accent."""
        return self.colorize(text, self.ACCENT)

    def dim(self, text: str) -> str:
        """Format text as dim (reduced intensity)."""
        return self.colorize(text, self.DIM)


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
    """Compiled search configuration.

    ``compiled`` carries the parsed-query predicates from
    :mod:`agentgrep.query`. When ``None`` (the default), the engine
    takes its legacy code path — pure-text queries and flag-only
    invocations stay on the fast path with no extra evaluation
    cost. When set, ``iter_search_events`` consults
    ``compiled.source_predicate`` to prune sources before any file
    is opened, and :func:`matches_record` consults
    ``compiled.record_predicate`` after the existing text match.
    ``match_surface`` lets line-oriented callers such as ``grep``
    require a match in record text while fuzzy search and filtering
    can keep using the metadata-rich haystack.
    """

    terms: tuple[str, ...]
    scope: SearchScope
    any_term: bool
    regex: bool
    case_sensitive: bool
    agents: tuple[AgentName, ...]
    limit: int | None
    dedupe: bool = True
    compiled: CompiledQuery | None = None
    match_surface: SearchMatchSurface = "haystack"


@dataclasses.dataclass(slots=True)
class SourceVersionDetection:
    """Detected app/data version metadata for one concrete source."""

    app_version: str | None
    data_version: str | None
    strategy: VersionDetectionStrategy
    confidence: VersionDetectionConfidence
    evidence: str


class SourceVersionDetectionPayload(t.TypedDict):
    """JSON payload for source version detection metadata."""

    app_version: str | None
    data_version: str | None
    strategy: VersionDetectionStrategy
    confidence: VersionDetectionConfidence
    evidence: str


@dataclasses.dataclass(slots=True)
class DiscoveryVersionContext:
    """Cached metadata shared across one source-discovery pass."""

    codex_client_version: str | None = None


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
    coverage: StoreCoverage = StoreCoverage.DEFAULT_SEARCH
    version_detection: SourceVersionDetection | None = None


type SourceProgressCallback = cabc.Callable[[int, int, SourceHandle, int, int], None]

_SOURCE_PROGRESS_RECORD_INTERVAL = 128
"""Parsed-record cadence for in-source progress updates and GIL yields."""

_JSONL_YIELD_LINE_INTERVAL = 128
"""Decoded-line cadence for cooperative JSONL parser yields."""

_JSONL_PREFIX_BYTES = 4096
"""Bytes read up front when a raw-line skip predicate is active."""

_JSONL_SKIP_CHUNK_BYTES = 1024 * 1024
"""Chunk size for discarding skipped oversized JSONL lines."""

_JSONL_REVERSE_CHUNK_BYTES = 1024 * 1024
"""Chunk size for reading JSONL files from end to start."""

_CODEX_RAW_SKIP_MIN_BYTES = 1024 * 1024
"""Minimum Codex session size before enabling raw-line output skipping."""


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

    def source_progress(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        records: int,
        matches: int,
    ) -> None:
        """Report in-source scan progress."""
        self.set_status(
            "scanning",
            current=index,
            total=total,
            detail=format_source_progress_detail(records, matches),
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
        frame_text = self._colors.info(frame)
        summary_width = max(1, self._terminal_width() - _visible_width(frame_text) - 1)
        summary = self._summary(max_width=summary_width)
        line = f"{frame_text} {summary}"
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
        line = self._summary(max_width=self._terminal_width())
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

    def _summary(self, *, max_width: int | None = None) -> str:
        return format_search_progress_line(
            self._snapshot(),
            colors=self._colors,
            answer_now_hint=self._answer_now_hint,
            max_width=max_width,
        )

    def _terminal_width(self) -> int:
        try:
            return max(1, os.get_terminal_size(self._stream.fileno()).columns)
        except AttributeError, OSError, TypeError, ValueError:
            return max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)

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
            text = f"{self._colors.heading(phase)} {count} {self._colors.muted('sources')}"
            if detail:
                return f"{text} | {self._colors.muted(detail)}"
            return text
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


def format_source_progress_detail(records: int, matches: int) -> str:
    """Return a concise in-source progress detail."""
    match_suffix = "source match" if matches == 1 else "source matches"
    return f"{records} records, {matches} {match_suffix}"


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
    max_width: int | None = None,
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
    max_width : int or None, default None
        Maximum visible terminal cells for the returned line. When set, the
        formatter drops optional detail and hint segments before truncating.

    Returns
    -------
    str
        ``"Searching <q> | <phase> N/M sources | K matches | T.Ts"`` with
        each segment styled through ``colors``.
    """
    variants = (
        (True, answer_now_hint),
        (False, answer_now_hint),
        (False, False),
    )
    for include_detail, include_hint in variants:
        line = _format_search_progress_line(
            snapshot,
            colors=colors,
            answer_now_hint=include_hint,
            include_detail=include_detail,
        )
        if max_width is None or _visible_width(line) <= max_width:
            return line
    if max_width is None:
        return line
    return _hard_truncate_ansi(line, max_width)


def _format_search_progress_line(
    snapshot: ProgressSnapshot,
    *,
    colors: SearchColors,
    answer_now_hint: bool,
    include_detail: bool,
) -> str:
    """Build one progress-line variant."""
    label_part = f"{colors.heading('Searching')} {colors.highlight(snapshot.query_label)}"
    detail_part = colors.muted(snapshot.detail) if include_detail and snapshot.detail else None
    if snapshot.current is not None and snapshot.total is not None:
        count = colors.warning(f"{snapshot.current}/{snapshot.total}")
        status_part = f"{colors.heading(snapshot.phase)} {count} {colors.muted('sources')}"
    elif include_detail and snapshot.detail:
        status_part = f"{colors.heading(snapshot.phase)} {colors.muted(snapshot.detail)}"
        detail_part = None
    else:
        status_part = colors.heading(snapshot.phase)
    parts = [
        label_part,
        status_part,
    ]
    if detail_part:
        parts.append(detail_part)
    parts.extend(
        [
            colors.warning(format_match_count(snapshot.matches)),
            colors.muted(f"{snapshot.elapsed:.1f}s"),
        ],
    )
    if answer_now_hint:
        parts.append(colors.white("[Press enter, answer now]"))
    return " | ".join(parts)


def noop_search_progress() -> SearchProgress:
    """Return a silent search progress reporter."""
    return NoopSearchProgress()


def _report_source_progress(
    progress: SearchProgress,
    index: int,
    total: int,
    source: SourceHandle,
    records: int,
    matches: int,
) -> None:
    """Call the optional in-source progress hook when a reporter exposes it."""
    callback = getattr(progress, "source_progress", None)
    if callable(callback):
        t.cast("SourceProgressCallback", callback)(index, total, source, records, matches)


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

    def source_progress(
        self,
        index: int,
        total: int,
        source: SourceHandle,
        records: int,
        matches: int,
    ) -> None:
        """Report in-source scan progress."""
        with self._lock:
            self._phase = "scanning"
            self._current = index
            self._total = total
            self._detail = format_source_progress_detail(records, matches)
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
    started_at = time.perf_counter()
    if control is None:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        _record_readonly_command_profile(command, started_at, completed)
        return completed
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
                completed = subprocess.CompletedProcess(
                    command,
                    process.returncode,
                    stdout,
                    stderr,
                )
                _record_readonly_command_profile(command, started_at, completed)
                return completed
            continue
        completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        _record_readonly_command_profile(command, started_at, completed)
        return completed


def _record_readonly_command_profile(
    command: list[str],
    started_at: float,
    completed: subprocess.CompletedProcess[str],
) -> None:
    """Record optional engine profiling metadata for a completed subprocess."""
    if "agentgrep._engine.profiling" not in sys.modules:
        return
    from agentgrep._engine.profiling import record_subprocess_run

    record_subprocess_run(
        command,
        duration_seconds=time.perf_counter() - started_at,
        completed=completed,
    )


def _record_engine_profile_sample(
    name: str,
    duration_seconds: float,
    **attributes: JSONScalar,
) -> None:
    """Record an optional engine profile sample when profiling is active."""
    if "agentgrep._engine.profiling" not in sys.modules:
        return
    from agentgrep._engine.profiling import current_engine_profiler

    profiler = current_engine_profiler()
    if profiler is None:
        return
    profiler.record(name, duration_seconds, **attributes)


def discover_sources(
    home: pathlib.Path,
    agents: tuple[AgentName, ...],
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover all known parseable sources for the selected agents.

    ``version_detail`` controls how eagerly source handles are enriched:
    ``"none"`` leaves ``version_detection`` empty for fast search paths,
    ``"catalog"`` attaches low-cost catalog observations, and ``"shape"``
    inspects concrete source shape for inventory surfaces. ``store_roles``
    lets latency-sensitive search paths enumerate only the catalogue roles
    that can satisfy a coarse query scope.
    """
    discovered: list[SourceHandle] = []
    for agent in agents:
        if agent == "codex":
            discovered.extend(
                discover_codex_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "claude":
            discovered.extend(
                discover_claude_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "cursor-cli":
            discovered.extend(
                discover_cursor_cli_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "cursor-ide":
            discovered.extend(
                discover_cursor_ide_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "gemini":
            discovered.extend(
                discover_gemini_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "grok":
            discovered.extend(
                discover_grok_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "pi":
            discovered.extend(
                discover_pi_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
        elif agent == "opencode":
            discovered.extend(
                discover_opencode_sources(
                    home,
                    backends,
                    include_non_default=include_non_default,
                    version_detail=version_detail,
                    store_roles=store_roles,
                ),
            )
    discovered.sort(key=lambda item: (item.agent, item.store, str(item.path)))
    return discovered


def file_mtime_ns(path: pathlib.Path) -> int:
    """Return a cached modification time for a path."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _file_size(path: pathlib.Path) -> int:
    """Return file size in bytes, falling back to zero on stat failure."""
    try:
        return path.stat().st_size
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


def _resolve_optional_root(value: str | None, default: pathlib.Path, *, label: str) -> pathlib.Path:
    """Resolve an optional path override, warning and falling back on bad paths."""
    if not value:
        return default
    candidate = pathlib.Path(os.path.expandvars(value)).expanduser()
    if candidate.is_dir():
        return candidate
    status = "not_a_directory" if candidate.exists() else "not_found"
    logger.warning(
        "path override unavailable, fell back to default",
        extra={
            "agentgrep_override_label": label,
            "agentgrep_override_path": value,
            "agentgrep_override_path_status": status,
        },
    )
    return default


def _codex_sqlite_home_from_config(codex_root: pathlib.Path) -> str | None:
    """Return Codex's configured ``sqlite_home`` value when present."""
    config_path = codex_root / "config.toml"
    if not config_path.is_file():
        return None
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning(
            "codex config parse failed",
            extra={
                "agentgrep_path": str(config_path),
                "agentgrep_error": type(exc).__name__,
            },
        )
        return None
    value = payload.get("sqlite_home")
    return value if isinstance(value, str) else None


def resolve_codex_sqlite_root(codex_root: pathlib.Path) -> pathlib.Path:
    """Resolve Codex's SQLite root from env/config, falling back to ``CODEX_HOME``."""
    env_value = os.environ.get("CODEX_SQLITE_HOME")
    if env_value:
        return _resolve_optional_root(env_value, codex_root, label="CODEX_SQLITE_HOME")
    return _resolve_optional_root(
        _codex_sqlite_home_from_config(codex_root),
        codex_root,
        label="sqlite_home",
    )


def _first_jsonl_mapping(path: pathlib.Path) -> dict[str, JSONValue] | None:
    """Return the first object record from a JSONL file."""
    for value in iter_jsonl(path):
        if isinstance(value, dict):
            return value
    return None


def _first_json_array_mapping(path: pathlib.Path) -> dict[str, JSONValue] | None:
    """Return the first object from a JSON array file."""
    value = read_json_file(path)
    if not isinstance(value, list):
        return None
    for entry in value:
        if isinstance(entry, dict):
            return entry
    return None


def _json_mapping(path: pathlib.Path) -> dict[str, JSONValue] | None:
    """Return a JSON file payload when its top-level value is an object."""
    value = read_json_file(path)
    return value if isinstance(value, dict) else None


def _safe_project_root(value: object) -> pathlib.Path | None:
    """Return a usable project root from session metadata."""
    if not isinstance(value, str) or not value:
        return None
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        return None
    return path


def _project_roots_from_jsonl_sessions(
    session_root: pathlib.Path,
    backends: BackendSelection,
) -> tuple[pathlib.Path, ...]:
    """Derive known project roots from session metadata JSONL files."""
    if not session_root.exists():
        return ()
    roots: set[pathlib.Path] = set()
    for path in list_files_matching(session_root, "*.jsonl", backends.find_tool):
        if "subagents" in path.parts:
            continue
        for index, record in enumerate(iter_jsonl(path)):
            if not isinstance(record, dict):
                if index >= 31:
                    break
                continue
            mapping = t.cast("dict[str, object]", record)
            payload = mapping.get("payload")
            candidates = [mapping.get("cwd"), mapping.get("project")]
            if isinstance(payload, dict):
                payload_mapping = t.cast("dict[str, object]", payload)
                candidates.extend((payload_mapping.get("cwd"), payload_mapping.get("project")))
            found_root = False
            for candidate in candidates:
                root = _safe_project_root(candidate)
                if root is not None:
                    roots.add(root)
                    found_root = True
                    break
            if found_root or index >= 31:
                break
    return tuple(sorted(roots))


def _codex_project_roots_from_legacy_sessions(
    session_root: pathlib.Path,
    backends: BackendSelection,
) -> tuple[pathlib.Path, ...]:
    """Derive known project roots from legacy Codex JSON session files."""
    if not session_root.exists():
        return ()
    roots: set[pathlib.Path] = set()
    for path in list_files_matching(session_root, "rollout-*.json", backends.find_tool):
        payload = read_json_file(path)
        if not isinstance(payload, dict):
            continue
        session = payload.get("session")
        if not isinstance(session, dict):
            continue
        mapping = t.cast("dict[str, object]", session)
        root = _safe_project_root(mapping.get("cwd") or mapping.get("project"))
        if root is not None:
            roots.add(root)
    return tuple(sorted(roots))


def _claude_project_roots(
    root: pathlib.Path,
    backends: BackendSelection,
) -> tuple[pathlib.Path, ...]:
    """Return project roots Claude Code has already referenced in transcripts."""
    return _project_roots_from_jsonl_sessions(root / "projects", backends)


def _codex_project_roots(
    root: pathlib.Path,
    backends: BackendSelection,
) -> tuple[pathlib.Path, ...]:
    """Return project roots Codex has already referenced in transcripts."""
    session_root = root / "sessions"
    return tuple(
        sorted(
            {
                *_project_roots_from_jsonl_sessions(session_root, backends),
                *_codex_project_roots_from_legacy_sessions(session_root, backends),
            },
        ),
    )


def _codex_client_version_from_cache(codex_root: pathlib.Path | None) -> str | None:
    """Return Codex's local client-version hint without spawning the CLI."""
    if codex_root is None:
        return None
    value = read_json_file(codex_root / "models_cache.json")
    if not isinstance(value, dict):
        return None
    return as_optional_str(value.get("client_version"))


def _catalog_version_detection(
    descriptor: StoreDescriptor,
    spec: DiscoverySpec,
    *,
    app_version: str | None = None,
) -> SourceVersionDetection:
    """Build the low-confidence fallback for sources without shape evidence."""
    return SourceVersionDetection(
        app_version=app_version,
        data_version=spec.data_version,
        strategy=VersionDetectionStrategy.CATALOG_OBSERVATION,
        confidence=VersionDetectionConfidence.LOW,
        evidence=f"catalog observed_version: {descriptor.observed_version}",
    )


def _codex_source_version_detection(
    source: SourceHandle,
    descriptor: StoreDescriptor,
    spec: DiscoverySpec,
    context: DiscoveryVersionContext,
) -> SourceVersionDetection:
    """Detect Codex source versions from local metadata and concrete shape."""
    app_version = context.codex_client_version

    if source.adapter_id == "codex.history_jsonl.v1":
        record = _first_jsonl_mapping(source.path)
        if record is not None and {"session_id", "ts", "text"}.issubset(record):
            return SourceVersionDetection(
                app_version=app_version,
                data_version="codex.history_jsonl.current",
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="history.jsonl object keys include session_id, ts, text",
            )
    elif source.adapter_id == "codex.history_json.v1":
        record = _first_json_array_mapping(source.path)
        if record is not None and {"command", "timestamp"}.issubset(record):
            return SourceVersionDetection(
                app_version=app_version,
                data_version="codex.history_json.legacy",
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="history.json array object keys include command, timestamp",
            )
    elif source.adapter_id == "codex.sessions_jsonl.v1":
        record = _first_jsonl_mapping(source.path)
        if record is not None and record.get("type") == "session_meta":
            payload = record.get("payload")
            embedded_version: str | None = None
            if isinstance(payload, dict):
                embedded_version = as_optional_str(payload.get("cli_version"))
            if embedded_version:
                return SourceVersionDetection(
                    app_version=embedded_version,
                    data_version=spec.data_version,
                    strategy=VersionDetectionStrategy.EMBEDDED_METADATA,
                    confidence=VersionDetectionConfidence.HIGH,
                    evidence="session_meta.payload keys include cli_version",
                )
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="jsonl event type includes session_meta",
            )
    elif source.adapter_id == "codex.sessions_legacy_json.v1":
        record = _json_mapping(source.path)
        if record is not None and {"session", "items"}.issubset(record):
            return SourceVersionDetection(
                app_version=app_version,
                data_version="codex.sessions.legacy_json.v1",
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="legacy session JSON object keys include session, items",
            )
    elif source.adapter_id == "codex.session_index_jsonl.v1":
        record = _first_jsonl_mapping(source.path)
        if record is not None and {"id", "thread_name", "updated_at"}.issubset(record):
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="session_index.jsonl object keys include id, thread_name, updated_at",
            )
    elif source.adapter_id == "codex.external_imports_json.v1":
        record = _json_mapping(source.path)
        if record is not None and "records" in record:
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="external import ledger object key includes records",
            )
    elif source.adapter_id == "codex.memories_text.v1":
        return SourceVersionDetection(
            app_version=app_version,
            data_version=spec.data_version,
            strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
            confidence=VersionDetectionConfidence.MEDIUM,
            evidence="markdown memory file discovered under memories",
        )
    elif source.adapter_id in {
        "codex.config_toml.v1",
        "codex.config_backup_toml.v1",
        "codex.project_config_toml.v1",
    }:
        try:
            payload = tomllib.loads(source.path.read_text(encoding="utf-8"))
        except OSError, tomllib.TOMLDecodeError:
            payload = {}
        if payload:
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="TOML top-level keys observed",
            )
    elif source.adapter_id == "codex.app_state_json_summary.v1":
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="app-state JSON object keys observed",
            )
    elif source.adapter_id == "codex.plugin_manifest_json.v1":
        record = _json_mapping(source.path)
        if record is not None and {"name", "description"}.intersection(record):
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="plugin manifest JSON object keys observed",
            )
    elif source.adapter_id in {
        "codex.hooks_json.v1",
        "codex.plugin_hooks_json.v1",
        "codex.plugin_marketplace_json.v1",
    }:
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="JSON object keys observed for Codex hook or plugin metadata",
            )
    elif source.adapter_id in {
        "codex.plugin_instruction_text.v1",
        "codex.project_skill_text.v1",
        "codex.rules_text.v1",
        "codex.skills_text.v1",
    }:
        return SourceVersionDetection(
            app_version=app_version,
            data_version=spec.data_version,
            strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
            confidence=VersionDetectionConfidence.MEDIUM,
            evidence="instruction text file discovered for Codex",
        )
    elif source.adapter_id == "codex.file_metadata_summary.v1":
        return SourceVersionDetection(
            app_version=app_version,
            data_version=spec.data_version,
            strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
            confidence=VersionDetectionConfidence.LOW,
            evidence="metadata-only raw state file observed",
        )
    elif source.source_kind == "sqlite" and spec.data_version is not None:
        match = re.fullmatch(r".+_([0-9]+)\.sqlite", source.path.name)
        if match is not None:
            return SourceVersionDetection(
                app_version=app_version,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence=f"filename suffix _{match.group(1)}.sqlite",
            )

    return _catalog_version_detection(descriptor, spec, app_version=app_version)


def _claude_source_version_detection(
    source: SourceHandle,
    descriptor: StoreDescriptor,
    spec: DiscoverySpec,
) -> SourceVersionDetection:
    """Detect Claude Code source versions from embedded metadata and shape."""
    if source.adapter_id == "claude.history_jsonl.v1":
        record = _first_jsonl_mapping(source.path)
        if record is not None and {"display", "timestamp", "project"}.issubset(record):
            return SourceVersionDetection(
                app_version=None,
                data_version="claude.history_jsonl.log_entry.v1",
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="history.jsonl object keys include display, timestamp, project",
            )
    elif source.adapter_id == "claude.projects_jsonl.v1":
        record = _first_jsonl_mapping(source.path)
        if record is not None:
            app_version = as_optional_str(record.get("version")) or as_optional_str(
                record.get("claude_code_version"),
            )
            if app_version:
                return SourceVersionDetection(
                    app_version=app_version,
                    data_version=spec.data_version,
                    strategy=VersionDetectionStrategy.EMBEDDED_METADATA,
                    confidence=VersionDetectionConfidence.HIGH,
                    evidence="project transcript keys include version",
                )
            if {"type", "sessionId", "message"}.issubset(record):
                return SourceVersionDetection(
                    app_version=None,
                    data_version=spec.data_version,
                    strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                    confidence=VersionDetectionConfidence.MEDIUM,
                    evidence="project transcript keys include type, sessionId, message",
                )
    elif source.adapter_id == "claude.tasks_json.v1":
        record = _json_mapping(source.path)
        if record is not None and {"id", "subject", "description", "status"}.issubset(record):
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="task JSON object keys include id, subject, description, status",
            )
    elif source.adapter_id == "claude.settings_json.v1":
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="settings JSON object keys observed",
            )
    elif source.adapter_id == "claude.todos_json.v1":
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="todo JSON object keys observed",
            )
    elif source.adapter_id == "claude.teams_json.v1":
        record = _json_mapping(source.path)
        if record is not None and {"name", "members"}.issubset(record):
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.HIGH,
                evidence="team config JSON object keys include name, members",
            )
    elif source.adapter_id == "claude.app_state_json_summary.v1":
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="app-state JSON object keys observed",
            )
    elif source.adapter_id in {
        "claude.plugin_hooks_json.v1",
        "claude.plugin_manifest_json.v1",
    }:
        record = _json_mapping(source.path)
        if record is not None:
            return SourceVersionDetection(
                app_version=None,
                data_version=spec.data_version,
                strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
                confidence=VersionDetectionConfidence.MEDIUM,
                evidence="plugin JSON object keys observed",
            )
    elif source.adapter_id in {
        "claude.commands_text.v1",
        "claude.memory_text.v1",
        "claude.plugin_instruction_text.v1",
        "claude.project_instruction_text.v1",
        "claude.projects_memory_text.v1",
        "claude.session_memory_text.v1",
        "claude.skills_text.v1",
    }:
        return SourceVersionDetection(
            app_version=None,
            data_version=spec.data_version,
            strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
            confidence=VersionDetectionConfidence.MEDIUM,
            evidence="instruction or memory text file discovered for Claude",
        )
    elif source.adapter_id == "claude.file_metadata_summary.v1":
        return SourceVersionDetection(
            app_version=None,
            data_version=spec.data_version,
            strategy=VersionDetectionStrategy.SHAPE_INFERENCE,
            confidence=VersionDetectionConfidence.LOW,
            evidence="metadata-only raw state file observed",
        )

    return _catalog_version_detection(descriptor, spec)


def detect_source_version(
    source: SourceHandle,
    descriptor: StoreDescriptor,
    spec: DiscoverySpec,
    context: DiscoveryVersionContext,
) -> SourceVersionDetection:
    """Detect concrete source version metadata for discovery payloads."""
    if source.agent == "codex":
        return _codex_source_version_detection(source, descriptor, spec, context)
    if source.agent == "claude":
        return _claude_source_version_detection(source, descriptor, spec)
    return _catalog_version_detection(descriptor, spec)


def build_discovery_version_context(
    agent: AgentName,
    primary_roots: dict[str, pathlib.Path],
    version_detail: DiscoveryVersionDetail,
) -> DiscoveryVersionContext:
    """Build cached version metadata for a single discovery pass."""
    codex_client_version: str | None = None
    if agent == "codex" and version_detail != "none":
        codex_client_version = _codex_client_version_from_cache(primary_roots.get("default"))
    return DiscoveryVersionContext(codex_client_version=codex_client_version)


def handles_from_discovery(
    spec: DiscoverySpec,
    agent: AgentName,
    root: pathlib.Path,
    backends: BackendSelection,
    coverage: StoreCoverage,
) -> list[SourceHandle]:
    """Produce ``SourceHandle``s from a :class:`DiscoverySpec`.

    Applies the spec's ``home_subpath`` under ``root`` to derive the search
    root, then enumerates source files via ``files`` (single-file lookups),
    ``glob`` (recursive walk with optional path-part filters), and
    ``platform_paths`` (absolute paths).
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
                    coverage=coverage,
                ),
            )

    if spec.glob is not None and search_root.exists():
        required_parts = set(spec.path_parts_required)
        excluded_parts = set(spec.path_parts_excluded)
        for path in list_files_matching(search_root, spec.glob, backends.find_tool):
            if required_parts and not required_parts.issubset(path.parts):
                continue
            if excluded_parts and excluded_parts.intersection(path.parts):
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
                    coverage=coverage,
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
                    coverage=coverage,
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
    base: pathlib.Path | dict[str, DiscoveryRoot],
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Walk every catalogue row for ``agent`` and emit ``SourceHandle``s.

    Each row's :class:`agentgrep.stores.DiscoverySpec` entries drive
    enumeration via :func:`handles_from_discovery`. Named roots may point to
    one directory or a bounded tuple of known project directories. Rows whose
    ``discovery`` tuple is empty are documentary-only and contribute no sources.
    ``DEFAULT_SEARCH`` rows are emitted by default. Inventory callers can
    set ``include_non_default`` to include ``INSPECTABLE`` and
    ``CATALOG_ONLY`` rows that carry discovery specs. ``PRIVATE`` rows are
    never enumerated from disk. ``version_detail`` lets latency-sensitive
    callers skip source-version enrichment until a metadata-rich surface asks
    for it. ``store_roles`` restricts enumeration before any filesystem walk,
    which lets search avoid stores its scope cannot consume.
    """
    from agentgrep.store_catalog import CATALOG

    roots: dict[str, DiscoveryRoot] = {"default": base} if isinstance(base, pathlib.Path) else base
    primary_roots: dict[str, pathlib.Path] = {}
    for key, value in roots.items():
        if isinstance(value, pathlib.Path):
            primary_roots[key] = value
        elif value:
            primary_roots[key] = value[0]
    version_context = build_discovery_version_context(agent, primary_roots, version_detail)
    sources: list[SourceHandle] = []
    for descriptor in CATALOG.for_agent(agent):
        coverage = descriptor.coverage_level
        if coverage is StoreCoverage.PRIVATE:
            continue
        if store_roles is not None and descriptor.role not in store_roles:
            continue
        if coverage is not StoreCoverage.DEFAULT_SEARCH and not include_non_default:
            continue
        # Per-descriptor dedup: a row whose discovery tuple has more than one
        # spec (e.g. Cursor IDE state.vscdb with both modern platform_paths
        # and a legacy ~/.cursor glob) must not yield the same file twice
        # under different adapter ids on layouts where both specs match.
        seen_paths: set[pathlib.Path] = set()
        for spec in descriptor.discovery:
            root_value = roots.get(spec.root_key)
            if root_value is None:
                continue
            root_paths = root_value if isinstance(root_value, tuple) else (root_value,)
            for root in root_paths:
                for handle in handles_from_discovery(spec, agent, root, backends, coverage):
                    if handle.path in seen_paths:
                        continue
                    seen_paths.add(handle.path)
                    if version_detail == "catalog":
                        handle.version_detection = _catalog_version_detection(
                            descriptor,
                            spec,
                            app_version=version_context.codex_client_version
                            if agent == "codex"
                            else None,
                        )
                    elif version_detail == "shape":
                        handle.version_detection = detect_source_version(
                            handle,
                            descriptor,
                            spec,
                            version_context,
                        )
                    sources.append(handle)
    return sources


def discover_codex_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
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
    sqlite_root = resolve_codex_sqlite_root(root)
    roots: dict[str, DiscoveryRoot] = {"default": root, "codex_sqlite": sqlite_root}
    if include_non_default:
        roots["codex_project"] = _codex_project_roots(root, backends)
    return discover_from_catalog(
        home,
        "codex",
        roots,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_claude_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Claude Code project session files.

    Honours ``CLAUDE_CONFIG_DIR`` and otherwise falls back to
    ``${HOME}/.claude``. Path roots, globs, and adapter metadata come from
    the ``claude.*`` rows of :data:`agentgrep.store_catalog.CATALOG`.
    """
    root = resolve_env_root("CLAUDE_CONFIG_DIR", home / ".claude")
    if not root.exists():
        return []
    roots: dict[str, DiscoveryRoot] = {"default": root}
    if include_non_default:
        roots["claude_project"] = _claude_project_roots(root, backends)
    return discover_from_catalog(
        home,
        "claude",
        roots,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_cursor_cli_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Cursor CLI (``cursor-agent``) sources.

    Covers the terminal agent's transcripts under ``~/.cursor/projects``,
    the AI-tracking SQLite, and the lowercase ``~/.config/cursor`` home
    (prompt history and chat ``store.db`` blobs). Driven entirely by the
    ``cursor-cli.*`` catalogue rows.
    """
    return discover_from_catalog(
        home,
        "cursor-cli",
        home,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def _cursor_ide_workspace_root(home: pathlib.Path) -> pathlib.Path:
    """Resolve the Cursor IDE ``workspaceStorage`` directory for this platform."""
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage"
    if sys.platform == "win32":
        return home / "AppData" / "Roaming" / "Cursor" / "User" / "workspaceStorage"
    return home / ".config" / "Cursor" / "User" / "workspaceStorage"


def discover_cursor_ide_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Cursor IDE (desktop app) sources.

    Covers the VS Code-style ``state.vscdb`` databases: the
    platform-specific ``globalStorage`` location, the legacy
    ``~/.cursor/state.vscdb`` glob, and the per-workspace
    ``workspaceStorage/<hash>/state.vscdb`` databases resolved through the
    ``ide_workspace`` root. Driven entirely by the ``cursor-ide.*``
    catalogue rows.
    """
    roots: dict[str, DiscoveryRoot] = {
        "default": home,
        "ide_workspace": _cursor_ide_workspace_root(home),
    }
    return discover_from_catalog(
        home,
        "cursor-ide",
        roots,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_gemini_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
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
    return discover_from_catalog(
        home,
        "gemini",
        base,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_grok_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover Grok CLI sessions and prompt history.

    Honours the ``GROK_HOME`` environment variable; falls back to
    ``${HOME}/.grok`` when unset or empty. Path roots, globs, file
    lists, and adapter metadata come from the ``grok.*`` rows of
    :data:`agentgrep.store_catalog.CATALOG`.
    """
    base = resolve_env_root("GROK_HOME", home / ".grok")
    if not base.exists():
        return []
    return discover_from_catalog(
        home,
        "grok",
        base,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_pi_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover pi (earendil-works/pi) session transcripts.

    Honours ``PI_CODING_AGENT_DIR`` (pi's agent data directory, used
    verbatim) and falls back to ``${HOME}/.pi/agent``. The optional
    ``PI_CODING_AGENT_SESSION_DIR`` overrides the sessions directory
    directly: when set, pi writes session files flat into it with no
    per-working-directory subdirectory, so it is resolved as a separate
    discovery root. Path roots, globs, and adapter metadata come from
    the ``pi.*`` rows of :data:`agentgrep.store_catalog.CATALOG`.
    """
    agent_dir = resolve_env_root("PI_CODING_AGENT_DIR", home / ".pi" / "agent")
    session_dir = _resolve_optional_root(
        os.environ.get("PI_CODING_AGENT_SESSION_DIR"),
        agent_dir / "sessions",
        label="PI_CODING_AGENT_SESSION_DIR",
    )
    if not agent_dir.exists() and not session_dir.exists():
        return []
    roots: dict[str, DiscoveryRoot] = {
        "default": agent_dir,
        "pi_session": session_dir,
    }
    return discover_from_catalog(
        home,
        "pi",
        roots,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def discover_opencode_sources(
    home: pathlib.Path,
    backends: BackendSelection,
    *,
    include_non_default: bool = False,
    version_detail: DiscoveryVersionDetail = "shape",
    store_roles: DiscoveryStoreRoles = None,
) -> list[SourceHandle]:
    """Discover OpenCode (anomalyco/opencode) SQLite databases.

    OpenCode stores conversations in ``opencode.db`` under its XDG data
    directory (``${XDG_DATA_HOME}/opencode``, falling back to
    ``${HOME}/.local/share/opencode``). The store is discovered by
    filename (not a glob) so the binary SQLite file bypasses the
    text prefilter, the same way the Grok SQLite store is.

    ``OPENCODE_DB`` overrides the database location: when it points at an
    absolute file, OpenCode uses that file (any filename) instead of the
    default, so agentgrep discovers that exact file directly — which also
    makes non-stable channel databases (``opencode-<channel>.db``)
    reachable by pointing ``OPENCODE_DB`` at them. The default lookup and
    adapter metadata come from the ``opencode.*`` rows of
    :data:`agentgrep.store_catalog.CATALOG`.
    """
    db_override = os.environ.get("OPENCODE_DB")
    if db_override and db_override != ":memory:":
        candidate = pathlib.Path(os.path.expandvars(db_override)).expanduser()
        if candidate.is_absolute():
            if not candidate.is_file():
                return []
            from agentgrep.store_catalog import CATALOG

            descriptor = CATALOG.by_id("opencode.db")
            if store_roles is not None and descriptor.role not in store_roles:
                return []
            handle = SourceHandle(
                agent="opencode",
                store="opencode.db",
                adapter_id="opencode.db_sqlite.v1",
                path=candidate,
                path_kind="sqlite_db",
                source_kind="sqlite",
                search_root=None,
                mtime_ns=file_mtime_ns(candidate),
            )
            if version_detail == "catalog":
                handle.version_detection = _catalog_version_detection(
                    descriptor,
                    descriptor.discovery[0],
                )
            elif version_detail == "shape":
                handle.version_detection = detect_source_version(
                    handle,
                    descriptor,
                    descriptor.discovery[0],
                    DiscoveryVersionContext(),
                )
            return [handle]
    base = resolve_env_root("XDG_DATA_HOME", home / ".local" / "share") / "opencode"
    if not base.exists():
        return []
    return discover_from_catalog(
        home,
        "opencode",
        base,
        backends,
        include_non_default=include_non_default,
        version_detail=version_detail,
        store_roles=store_roles,
    )


def list_files_matching(
    root: pathlib.Path,
    glob_pattern: str,
    fd_program: str | None,
) -> list[pathlib.Path]:
    """List files under ``root`` that match a glob."""
    if not root.exists():
        return []
    if "/" in glob_pattern or "\\" in glob_pattern:
        return sorted(path for path in root.glob(glob_pattern) if path.is_file())
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
    runtime: SearchRuntime | None = None,
) -> list[SearchRecord]:
    """Parse and filter search results across all selected sources."""
    active_progress = noop_search_progress() if progress is None else progress
    active_control = SearchControl() if control is None else control
    # Apply the compiled-query source predicate before planning so the
    # ripgrep prefilter (which is the heavy step in
    # ``plan_search_sources``) runs on the smaller set. Without this
    # the per-file prefilter runs against every discovered source even
    # when ``agent:codex`` could rule most out from metadata alone.
    if query.compiled is not None and query.compiled.source_predicate is not None:
        sources = [s for s in sources if query.compiled.source_predicate(s)]
    from agentgrep._engine.planning import build_physical_search_plan

    plan = build_physical_search_plan(
        query,
        sources,
        backends,
        progress=active_progress,
        control=active_control,
    )
    if active_control.answer_now_requested():
        active_progress.answer_now(0)
        return []
    active_progress.sources_planned(len(plan.tasks), len(sources))
    records = collect_search_records_from_plan(
        query,
        plan,
        progress=active_progress,
        control=active_control,
        runtime=runtime,
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
    runtime: SearchRuntime | None = None,
) -> list[SearchRecord]:
    """Discover sources and run a normalized search query."""
    active_backends = select_backends() if backends is None else backends
    active_progress = noop_search_progress() if progress is None else progress
    active_control = SearchControl() if control is None else control
    active_progress.start(query)
    interrupted = False
    try:
        sources = discover_sources_for_search(
            home,
            query,
            active_backends,
            version_detail="none",
        )
        active_progress.sources_discovered(len(sources))
        return search_sources(
            query,
            sources,
            active_backends,
            progress=active_progress,
            control=active_control,
            runtime=runtime,
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
    from agentgrep._engine.planning import build_physical_search_plan

    plan = build_physical_search_plan(
        query,
        sources,
        backends,
        progress=progress,
        control=control,
    )
    return [task.source for task in plan.tasks]


def source_order_key(source: SourceHandle) -> tuple[int, str]:
    """Return a newest-first search order key for sources."""
    return (-source.mtime_ns, str(source.path))


def _source_profile_attributes(source: SourceHandle) -> dict[str, JSONScalar]:
    """Return privacy-safe profiler attributes for a source handle."""
    return {
        "agentgrep_agent": source.agent,
        "agentgrep_store": source.store,
        "agentgrep_adapter_id": source.adapter_id,
        "agentgrep_path_kind": source.path_kind,
        "agentgrep_source_kind": source.source_kind,
    }


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
        if source.source_kind == "sqlite":
            filtered_sources.append(source)
            continue
        search_root = source.search_root
        if search_root is None:
            filtered_sources.append(source)
            continue

        if search_root not in matched_paths_by_root:
            active_progress.prefilter_started(search_root)
            started_at = time.perf_counter()
            matched_paths_by_root[search_root] = grep_root_paths(
                search_root,
                query,
                grep_program,
                control=active_control,
            )
            matched_paths = matched_paths_by_root[search_root]
            _record_engine_profile_sample(
                "search.plan.prefilter_root",
                time.perf_counter() - started_at,
                # SQLite candidates bypass root prefiltering above, so they
                # do not count toward the sources this grep pass covers.
                agentgrep_source_count=sum(
                    1
                    for candidate in sources
                    if candidate.search_root == search_root and candidate.source_kind != "sqlite"
                ),
                agentgrep_matched_source_count=len(matched_paths)
                if matched_paths is not None
                else None,
                agentgrep_unknown=matched_paths is None,
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
    started_at = time.perf_counter()
    matched = False
    aborted = False
    if active_control.answer_now_requested():
        return False
    try:
        if source.adapter_id == "claude.history_jsonl.v1":
            matched = True
            return matched
        if source.source_kind == "sqlite":
            matched = True
            return matched
        if backends.grep_tool is not None:
            grep_match = grep_file_matches(
                source.path,
                query,
                backends.grep_tool,
                control=active_control,
            )
            if active_control.answer_now_requested():
                aborted = True
                return False
            if grep_match is not None:
                matched = grep_match
                return matched
        if source.path.suffix in JSON_FILE_SUFFIXES and backends.json_tool is not None:
            extracted = flatten_json_strings_with_tool(
                source.path,
                backends.json_tool,
                control=active_control,
            )
            if active_control.answer_now_requested():
                aborted = True
                return False
            if extracted is not None:
                matched = matches_text(extracted, query)
                return matched
        matched = matches_text(read_text_file(source.path), query)
        return matched
    finally:
        # An answer-now abort is not a non-match; record nothing, matching
        # the pre-try early return above.
        if not aborted:
            _record_engine_profile_sample(
                "search.plan.direct_source",
                time.perf_counter() - started_at,
                **_source_profile_attributes(source),
                agentgrep_matched=matched,
            )


def collect_search_records(
    query: SearchQuery,
    sources: list[SourceHandle],
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
    runtime: SearchRuntime | None = None,
) -> list[SearchRecord]:
    """Parse candidate sources and collect matching records."""
    from agentgrep._engine.planning import (
        PhysicalSearchPlan,
        SourceTask,
        build_logical_search_plan,
    )

    plan = PhysicalSearchPlan(
        logical=build_logical_search_plan(query),
        tasks=tuple(
            SourceTask(
                source=source,
                strategy="direct_full_scan",
                record_order="unknown",
                limit_behavior="drain_source",
                can_stream_records=True,
                restore_order_key=source_order_key(source),
            )
            for source in sources
        ),
        decisions=(),
    )
    return collect_search_records_from_plan(
        query,
        plan,
        progress=progress,
        control=control,
        runtime=runtime,
    )


def collect_search_records_from_plan(
    query: SearchQuery,
    plan: PhysicalSearchPlan,
    *,
    progress: SearchProgress | None = None,
    control: SearchControl | None = None,
    runtime: SearchRuntime | None = None,
) -> list[SearchRecord]:
    """Execute a physical search plan and collect matching records."""
    from agentgrep._engine.execution import ExecutionRecordEmitted, select_execution_driver

    results = [
        event.record
        for event in select_execution_driver(query, plan).iter_search_plan(
            query,
            plan,
            progress=progress,
            control=control,
            runtime=runtime,
        )
        if isinstance(event, ExecutionRecordEmitted)
    ]
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
    sources = discover_sources(home, agents, active_backends, version_detail="none")
    return find_sources(pattern, sources, limit)


def iter_source_records(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Dispatch to the adapter parser for one source."""
    if source.adapter_id == "codex.sessions_jsonl.v1":
        yield from parse_codex_session_file(
            source,
            raw_skip_line=raw_skip_line,
            reverse=reverse,
        )
        return
    if source.adapter_id == "codex.sessions_legacy_json.v1":
        yield from parse_codex_legacy_session_file(source)
        return
    if source.adapter_id in {"codex.history_json.v1", "codex.history_jsonl.v1"}:
        yield from parse_codex_history_file(
            source,
            raw_skip_line=raw_skip_line,
            reverse=reverse,
        )
        return
    if source.adapter_id == "codex.session_index_jsonl.v1":
        yield from parse_codex_session_index_file(source)
        return
    if source.adapter_id == "claude.history_jsonl.v1":
        yield from parse_claude_history_file(source)
        return
    if source.adapter_id == "claude.projects_jsonl.v1":
        yield from parse_claude_project_file(
            source,
            raw_skip_line=raw_skip_line,
            reverse=reverse,
        )
        return
    if source.adapter_id == "claude.store_sqlite.v1":
        yield from parse_claude_store_db(source)
        return
    if source.adapter_id == "claude.tasks_json.v1":
        yield from parse_claude_task_file(source)
        return
    if source.adapter_id == "claude.todos_json.v1":
        yield from parse_claude_todo_file(source)
        return
    if source.adapter_id == "claude.teams_json.v1":
        yield from parse_claude_team_file(source)
        return
    if source.adapter_id == "claude.settings_json.v1":
        yield from parse_claude_settings_file(source)
        return
    if source.adapter_id == "claude.app_state_json_summary.v1":
        yield from parse_json_summary_file(source, label="Claude app state")
        return
    if source.adapter_id == "claude.file_metadata_summary.v1":
        yield from parse_file_metadata_summary_file(source, label="Claude raw state")
        return
    if source.adapter_id == "claude.plugin_manifest_json.v1":
        yield from parse_json_summary_file(source, label="Claude plugin manifest")
        return
    if source.adapter_id == "claude.plugin_hooks_json.v1":
        yield from parse_hooks_summary_file(source, label="Claude plugin hooks")
        return
    if source.adapter_id in {
        "claude.commands_text.v1",
        "claude.memory_text.v1",
        "claude.projects_memory_text.v1",
        "claude.plugin_instruction_text.v1",
        "claude.project_instruction_text.v1",
        "claude.session_memory_text.v1",
        "claude.skills_text.v1",
        "claude.plans_text.v1",
        "codex.instructions_text.v1",
        "codex.memories_text.v1",
        "codex.plugin_instruction_text.v1",
        "codex.project_skill_text.v1",
        "codex.rules_text.v1",
        "codex.skills_text.v1",
    }:
        yield from parse_text_store_file(source)
        return
    if source.adapter_id in {
        "codex.config_toml.v1",
        "codex.config_backup_toml.v1",
        "codex.project_config_toml.v1",
    }:
        yield from parse_toml_summary_file(source)
        return
    if source.adapter_id == "codex.app_state_json_summary.v1":
        yield from parse_json_summary_file(source, label="Codex app state")
        return
    if source.adapter_id == "codex.file_metadata_summary.v1":
        yield from parse_file_metadata_summary_file(source, label="Codex raw state")
        return
    if source.adapter_id == "codex.hooks_json.v1":
        yield from parse_hooks_summary_file(source, label="Codex hooks")
        return
    if source.adapter_id == "codex.plugin_hooks_json.v1":
        yield from parse_hooks_summary_file(source, label="Codex plugin hooks")
        return
    if source.adapter_id == "codex.plugin_manifest_json.v1":
        yield from parse_json_summary_file(source, label="Codex plugin manifest")
        return
    if source.adapter_id == "codex.plugin_marketplace_json.v1":
        yield from parse_json_summary_file(source, label="Codex plugin marketplace")
        return
    if source.adapter_id == "codex.state_sqlite.v1":
        yield from parse_codex_state_db(source)
        return
    if source.adapter_id == "codex.logs_sqlite.v1":
        yield from parse_codex_logs_db(source)
        return
    if source.adapter_id == "codex.memories_sqlite.v1":
        yield from parse_codex_memories_db(source)
        return
    if source.adapter_id == "codex.goals_sqlite.v1":
        yield from parse_codex_goals_db(source)
        return
    if source.adapter_id == "codex.external_imports_json.v1":
        yield from parse_codex_external_imports_file(source)
        return
    if source.adapter_id == "cursor_cli.ai_tracking_sqlite.v1":
        yield from parse_cursor_ai_tracking_db(source)
        return
    if source.adapter_id in {
        "cursor_ide.state_vscdb_modern.v1",
        "cursor_ide.state_vscdb_legacy.v1",
    }:
        yield from parse_cursor_state_db(source)
        return
    if source.adapter_id == "cursor_cli.transcripts_jsonl.v1":
        yield from parse_cursor_cli_transcript(source)
        return
    if source.adapter_id == "cursor_cli.prompt_history_json.v1":
        yield from parse_cursor_prompt_history(source)
        return
    if source.adapter_id == "cursor_cli.chats_protobuf.v1":
        yield from parse_cursor_cli_chats_db(source)
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
    if source.adapter_id == "grok.prompt_history_jsonl.v1":
        yield from parse_grok_prompt_history(
            source,
            raw_skip_line=raw_skip_line,
            reverse=reverse,
        )
        return
    if source.adapter_id == "grok.sessions_jsonl.v1":
        yield from parse_grok_chat_history(
            source,
            raw_skip_line=raw_skip_line,
            reverse=reverse,
        )
        return
    if source.adapter_id == "grok.session_search_sqlite.v1":
        yield from parse_grok_session_search_db(source)
        return
    if source.adapter_id == "pi.sessions_jsonl.v1":
        yield from parse_pi_session_file(
            source,
            raw_skip_line=raw_skip_line,
            reverse=reverse,
        )
        return
    if source.adapter_id == "opencode.db_sqlite.v1":
        yield from parse_opencode_db(source)
        return


def parse_codex_session_file(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse Codex session JSONL files."""
    session_id = source.path.stem
    session_model: str | None = None
    codex_skip_line = (
        _is_codex_function_call_output_line
        if _file_size(source.path) >= _CODEX_RAW_SKIP_MIN_BYTES
        else None
    )
    if codex_skip_line is not None:
        # Keep the cheap prefix-mode tool-output skip even when a raw text
        # prefilter is active: the prefix predicate discards oversized
        # function_call_output lines in chunks while the text prefilter
        # still sees every surviving line in full before JSON decode.
        events = _iter_jsonl(
            source.path,
            skip_line=codex_skip_line,
            skip_line_mode="prefix",
            full_line_skip=raw_skip_line,
            reverse=reverse,
        )
    elif raw_skip_line is not None:
        events = _iter_jsonl(
            source.path,
            skip_line=raw_skip_line,
            skip_line_mode="line",
            reverse=reverse,
        )
    else:
        events = _iter_jsonl(source.path, reverse=reverse)
    for event in events:
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


def parse_codex_legacy_session_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse legacy root-level Codex ``rollout-*.json`` session files."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    session_raw = payload.get("session")
    session = t.cast("dict[str, object]", session_raw) if isinstance(session_raw, dict) else {}
    session_id = as_optional_str(session.get("id")) or source.path.stem
    timestamp = as_optional_str(session.get("timestamp")) or as_optional_str(
        session.get("created_at"),
    )
    model = (
        as_optional_str(session.get("model"))
        or as_optional_str(session.get("model_name"))
        or as_optional_str(session.get("modelProvider"))
    )
    items = payload.get("items")
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = candidate_from_mapping(
            t.cast("dict[str, object]", item),
            timestamp=as_optional_str(item.get("timestamp")) or timestamp,
            model=model,
            session_id=session_id,
            conversation_id=session_id,
        )
        if candidate is None:
            continue
        yield build_search_record(source, candidate)


def parse_codex_history_file(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse Codex prompt/command history files."""
    entries: cabc.Iterable[JSONValue]
    if source.source_kind == "json":
        payload = read_json_file(source.path)
        entries = payload if isinstance(payload, list) else []
    else:
        entries = (
            _iter_jsonl(
                source.path,
                skip_line=raw_skip_line,
                skip_line_mode="line",
                reverse=reverse,
            )
            if raw_skip_line is not None
            else _iter_jsonl(source.path, reverse=reverse)
        )

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = as_optional_str(entry.get("text")) or as_optional_str(entry.get("command"))
        if not text:
            continue
        session_id = as_optional_str(entry.get("session_id"))
        timestamp = as_optional_str(entry.get("timestamp"))
        ts = entry.get("ts")
        if timestamp is None and isinstance(ts, int):
            timestamp = (
                datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
        yield SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=text,
            title="Codex prompt history",
            role="user",
            timestamp=timestamp,
            session_id=session_id,
            conversation_id=session_id,
        )


def parse_codex_session_index_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Codex ``session_index.jsonl`` records as opt-in thread summaries."""
    for entry in iter_jsonl(source.path):
        if not isinstance(entry, dict):
            continue
        mapping = t.cast("dict[str, object]", entry)
        thread_name = as_optional_str(mapping.get("thread_name"))
        if not thread_name:
            continue
        session_id = as_optional_str(mapping.get("id"))
        yield SearchRecord(
            kind="history",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=thread_name,
            title=thread_name,
            role="assistant",
            timestamp=as_optional_str(mapping.get("updated_at")),
            session_id=session_id,
            conversation_id=session_id,
        )


def parse_claude_project_file(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse Claude Code project JSONL files using lightweight heuristics."""
    conversation_id = source.path.stem
    seen: set[tuple[str | None, str, str | None, str | None]] = set()
    events = (
        _iter_jsonl(
            source.path,
            skip_line=raw_skip_line,
            skip_line_mode="line",
            reverse=reverse,
        )
        if raw_skip_line is not None
        else _iter_jsonl(source.path, reverse=reverse)
    )
    for event in events:
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


def _json_string_list(value: object) -> list[str]:
    """Return a list of non-empty strings from a JSON list-like field."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def parse_claude_task_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Claude Code task JSON files as opt-in task samples."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    mapping = t.cast("dict[str, object]", payload)
    subject = as_optional_str(mapping.get("subject"))
    description = as_optional_str(mapping.get("description"))
    text = "\n\n".join(part for part in (subject, description) if part)
    if not text:
        return
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        title=subject,
        role="task",
        timestamp=as_optional_str(mapping.get("updatedAt"))
        or as_optional_str(mapping.get("updated_at"))
        or isoformat_from_mtime_ns(source.mtime_ns),
        session_id=as_optional_str(mapping.get("id")),
        metadata={
            "status": as_optional_str(mapping.get("status")) or "",
            "task_id": as_optional_str(mapping.get("id")) or "",
            "blocks": _json_string_list(mapping.get("blocks")),
            "blocked_by": _json_string_list(mapping.get("blockedBy")),
        },
    )


def _json_value_shape(value: object) -> str:
    """Return a value-free shape label for safe config/app-state summaries."""
    if isinstance(value, dict):
        return f"object[{len(value)}]"
    if isinstance(value, list):
        return f"array[{len(value)}]"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if value is None:
        return "null"
    return type(value).__name__


def _safe_mapping_summary(label: str, payload: dict[str, object]) -> str:
    """Summarize mapping keys and value shapes without including raw values."""
    key_shapes = [
        f"{key} ({_json_value_shape(payload[key])})" for key in sorted(payload) if key.strip()
    ]
    return f"{label} keys: {', '.join(key_shapes)}"


def parse_json_summary_file(
    source: SourceHandle,
    *,
    label: str,
) -> cabc.Iterator[SearchRecord]:
    """Parse a JSON object as a key/type summary without raw values."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    mapping = t.cast("dict[str, object]", payload)
    if not mapping:
        return
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=_safe_mapping_summary(label, mapping),
        title=source.path.name,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"key_count": len(mapping)},
    )


def _safe_nested_keys(payload: dict[str, object], key: str) -> list[str]:
    """Return sorted keys from a nested object without exposing values."""
    nested = payload.get(key)
    if not isinstance(nested, dict):
        return []
    return sorted(nested_key for nested_key in nested if isinstance(nested_key, str))


def parse_hooks_summary_file(
    source: SourceHandle,
    *,
    label: str,
) -> cabc.Iterator[SearchRecord]:
    """Parse hook JSON as event/key summaries without raw commands."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    mapping = t.cast("dict[str, object]", payload)
    if not mapping:
        return
    hook_events = _safe_nested_keys(mapping, "hooks")
    text = _safe_mapping_summary(label, mapping)
    if hook_events:
        text = f"{text}; hook events: {', '.join(hook_events)}"
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        title=source.path.name,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"key_count": len(mapping), "hook_event_count": len(hook_events)},
    )


def _line_count(path: pathlib.Path) -> int:
    """Count text lines without exposing their contents."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def parse_file_metadata_summary_file(
    source: SourceHandle,
    *,
    label: str,
) -> cabc.Iterator[SearchRecord]:
    """Parse raw/cache text files as metadata-only summaries."""
    byte_size = _file_size(source.path)
    line_count = _line_count(source.path)
    suffix = source.path.suffix or "<none>"
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=(
            f"{label} file metadata: name={source.path.name}, "
            f"suffix={suffix}, bytes={byte_size}, lines={line_count}"
        ),
        title=source.path.name,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"byte_size": byte_size, "line_count": line_count},
    )


def parse_toml_summary_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a TOML file as a key/type summary without raw values."""
    try:
        payload = tomllib.loads(source.path.read_text(encoding="utf-8"))
    except OSError, tomllib.TOMLDecodeError:
        return
    if not payload:
        return
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=_safe_mapping_summary("Codex config", t.cast("dict[str, object]", payload)),
        title=source.path.name,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"key_count": len(payload)},
    )


def _iter_todo_mappings(payload: object) -> cabc.Iterator[dict[str, object]]:
    """Yield task-like mappings from common Claude todo container shapes."""
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield t.cast("dict[str, object]", item)
        return
    if not isinstance(payload, dict):
        return
    mapping = t.cast("dict[str, object]", payload)
    if any(key in mapping for key in ("content", "text", "subject", "description", "title")):
        yield mapping
    for key in ("todos", "items", "tasks"):
        nested = mapping.get(key)
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    yield t.cast("dict[str, object]", item)


def parse_claude_todo_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Claude todo JSON files as opt-in todo samples."""
    payload = read_json_file(source.path)
    for mapping in _iter_todo_mappings(payload):
        first_line = (
            as_optional_str(mapping.get("content"))
            or as_optional_str(mapping.get("text"))
            or as_optional_str(mapping.get("subject"))
            or as_optional_str(mapping.get("title"))
        )
        description = as_optional_str(mapping.get("description"))
        text = "\n\n".join(part for part in (first_line, description) if part)
        if not text:
            continue
        todo_id = as_optional_str(mapping.get("id"))
        yield SearchRecord(
            kind="history",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=text,
            title=first_line,
            role="todo",
            timestamp=as_optional_str(mapping.get("updatedAt"))
            or as_optional_str(mapping.get("updated_at"))
            or isoformat_from_mtime_ns(source.mtime_ns),
            session_id=todo_id,
            metadata={
                "status": as_optional_str(mapping.get("status")) or "",
                "todo_id": todo_id or "",
            },
        )


def parse_claude_team_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Claude team config JSON as opt-in team instruction samples."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    mapping = t.cast("dict[str, object]", payload)
    parts: list[str] = []
    team_name = as_optional_str(mapping.get("name"))
    description = as_optional_str(mapping.get("description"))
    if team_name:
        parts.append(f"Team: {team_name}")
    if description:
        parts.append(description)
    members = mapping.get("members")
    member_count = len(members) if isinstance(members, list) else 0
    if isinstance(members, list):
        for member in members:
            if not isinstance(member, dict):
                continue
            member_mapping = t.cast("dict[str, object]", member)
            prompt = as_optional_str(member_mapping.get("prompt"))
            if not prompt:
                continue
            name = as_optional_str(member_mapping.get("name")) or "member"
            parts.append(f"{name}: {prompt}")
    text = "\n\n".join(parts)
    if not text:
        return
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        title=team_name or source.path.parent.name,
        role="team",
        timestamp=_unix_millis_to_isoformat(mapping.get("createdAt"))
        or isoformat_from_mtime_ns(source.mtime_ns),
        session_id=as_optional_str(mapping.get("leadSessionId")),
        metadata={"member_count": member_count},
    )


def parse_claude_settings_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Claude settings JSON as a key summary without raw values."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    keys = sorted(key for key in payload if key.strip())
    if not keys:
        return
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=f"Claude settings keys: {', '.join(keys)}",
        title=source.path.name,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"key_count": len(keys)},
    )


CLAUDE_PASTE_REF_RE = re.compile(
    r"\[(?:Pasted text|Image|\.\.\.Truncated text) #(?P<id>\d+)(?: \+\d+ lines)?\.*\]",
)
CLAUDE_PASTE_HASH_RE = re.compile(r"^[0-9a-fA-F]{16}$")


def parse_claude_history_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Claude Code's global ``history.jsonl`` prompt audit log."""
    paste_cache_dir = source.path.parent / "paste-cache"
    for event in iter_jsonl(source.path):
        if not isinstance(event, dict):
            continue
        mapping = t.cast("dict[str, object]", event)
        display = as_optional_str(mapping.get("display"))
        if not display:
            continue
        session_id = as_optional_str(mapping.get("sessionId"))
        yield SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=expand_claude_history_pastes(
                display,
                mapping.get("pastedContents"),
                paste_cache_dir,
            ),
            title="Claude prompt history",
            role="user",
            timestamp=_unix_millis_to_isoformat(mapping.get("timestamp")),
            session_id=session_id,
            conversation_id=session_id,
            metadata={"project": as_optional_str(mapping.get("project")) or ""},
        )


def expand_claude_history_pastes(
    display: str,
    pasted_contents: object,
    paste_cache_dir: pathlib.Path,
) -> str:
    """Replace Claude history paste placeholders with stored text when available.

    Examples
    --------
    >>> expand_claude_history_pastes(
    ...     "Review [Pasted text #1]",
    ...     {"1": {"type": "text", "content": "inline text"}},
    ...     pathlib.Path("/missing"),
    ... )
    'Review inline text'
    >>> expand_claude_history_pastes(
    ...     "Review [Image #1]",
    ...     {"1": {"type": "image", "content": "ignored"}},
    ...     pathlib.Path("/missing"),
    ... )
    'Review [Image #1]'
    """
    if not isinstance(pasted_contents, dict):
        return display
    refs = t.cast("dict[object, object]", pasted_contents)

    def replace(match: re.Match[str]) -> str:
        ref_id = match.group("id")
        stored = refs.get(ref_id)
        replacement = claude_history_paste_text(stored, paste_cache_dir)
        return replacement if replacement is not None else match.group(0)

    return CLAUDE_PASTE_REF_RE.sub(replace, display)


def claude_history_paste_text(
    stored: object,
    paste_cache_dir: pathlib.Path,
) -> str | None:
    """Return stored Claude pasted text, resolving content hashes if needed."""
    if not isinstance(stored, dict):
        return None
    mapping = t.cast("dict[str, object]", stored)
    if mapping.get("type") != "text":
        return None
    content = mapping.get("content")
    if isinstance(content, str) and content:
        return content
    content_hash = as_optional_str(mapping.get("contentHash"))
    if content_hash is None or CLAUDE_PASTE_HASH_RE.fullmatch(content_hash) is None:
        return None
    cached = read_text_file(paste_cache_dir / f"{content_hash}.txt")
    return cached or None


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

    Records are emitted as ``kind="prompt"`` — the file is an audit log of
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
            kind="prompt",
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


def parse_grok_prompt_history(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse a Grok CLI ``prompt_history.jsonl`` file.

    Each line is ``{"timestamp": "…", "session_id": "…", "prompt": "…",
    "is_bash": bool}`` — one record per user prompt, append-only across
    all sessions within one project directory.
    """
    events = (
        _iter_jsonl(
            source.path,
            skip_line=raw_skip_line,
            skip_line_mode="line",
            reverse=reverse,
        )
        if raw_skip_line is not None
        else _iter_jsonl(source.path, reverse=reverse)
    )
    for event in events:
        if not isinstance(event, dict):
            continue
        mapping = t.cast("dict[str, object]", event)
        prompt = as_optional_str(mapping.get("prompt"))
        if not prompt:
            continue
        session_id = as_optional_str(mapping.get("session_id"))
        yield SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=prompt,
            title="Grok prompt history",
            role="user",
            timestamp=as_optional_str(mapping.get("timestamp")),
            session_id=session_id,
            conversation_id=session_id,
            metadata={"is_bash": mapping.get("is_bash", False)},
        )


def parse_grok_chat_history(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse a Grok CLI ``chat_history.jsonl`` session transcript.

    Lines carry a ``type`` field (system / user / assistant / tool_use /
    tool_result) and ``content`` (text or content-blocks array). All
    record types are emitted to maximise searchable content.
    """
    conversation_id = source.path.parent.name
    events = (
        _iter_jsonl(
            source.path,
            skip_line=raw_skip_line,
            skip_line_mode="line",
            reverse=reverse,
        )
        if raw_skip_line is not None
        else _iter_jsonl(source.path, reverse=reverse)
    )
    for event in events:
        if not isinstance(event, dict):
            continue
        mapping = t.cast("dict[str, object]", event)
        record_type = as_optional_str(mapping.get("type"))
        if not record_type:
            continue
        content_text = flatten_content_value(
            t.cast("JSONValue | None", mapping.get("content")),
        )
        if not content_text:
            continue
        yield SearchRecord(
            kind="prompt" if record_type == "user" else "history",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=content_text,
            role=record_type,
            timestamp=as_optional_str(mapping.get("timestamp")),
            session_id=conversation_id,
            conversation_id=conversation_id,
        )


def _unix_to_isoformat(value: object) -> str | None:
    """Convert a unix-seconds integer to an ISO-8601 UTC timestamp.

    Examples
    --------
    >>> _unix_to_isoformat(1700000000)
    '2023-11-14T22:13:20Z'
    >>> _unix_to_isoformat(0) is None
    True
    >>> _unix_to_isoformat(float("nan")) is None
    True
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return (
            datetime.datetime.fromtimestamp(value, tz=datetime.UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except ValueError, OSError, OverflowError:
        return None


def _unix_millis_to_isoformat(value: object) -> str | None:
    """Convert a unix-milliseconds timestamp to ISO-8601 UTC.

    Examples
    --------
    >>> _unix_millis_to_isoformat(1700000000000)
    '2023-11-14T22:13:20Z'
    >>> _unix_millis_to_isoformat(0) is None
    True
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return (
            datetime.datetime.fromtimestamp(value / 1000, tz=datetime.UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except ValueError, OSError, OverflowError:
        return None


def _pi_message_candidate(
    entry: dict[str, object],
    entry_timestamp: str | None,
    session_id: str | None,
    conversation_id: str | None,
) -> MessageCandidate | None:
    """Build a candidate from a pi ``message`` session entry.

    The entry wraps an LLM message under ``message`` (``role`` plus
    ``content`` that is a string or content-blocks array). The
    entry-level ISO timestamp is preferred; the inner unix-milliseconds
    ``timestamp`` is the fallback for v1 entries that lack one.
    """
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    message_map = t.cast("dict[str, object]", message)
    role = as_optional_str(message_map.get("role"))
    text = flatten_content_value(t.cast("JSONValue | None", message_map.get("content")))
    if role is None or not text:
        return None
    timestamp = entry_timestamp or _unix_millis_to_isoformat(message_map.get("timestamp"))
    return MessageCandidate(
        role=role,
        text=text,
        timestamp=timestamp,
        model=as_optional_str(message_map.get("model")),
        session_id=session_id,
        conversation_id=conversation_id,
    )


def _pi_entry_text(entry_type: str, entry: dict[str, object]) -> str | None:
    """Return searchable text from a non-message pi session entry.

    ``compaction``/``branch_summary`` carry a ``summary``; ``session_info``
    carries a user-set ``name``. Other entry types (model/thinking-level
    changes, custom, label) are metadata-only and yield no text.
    """
    if entry_type in {"compaction", "branch_summary"}:
        return as_optional_str(entry.get("summary"))
    if entry_type == "session_info":
        return as_optional_str(entry.get("name"))
    return None


def parse_pi_session_file(
    source: SourceHandle,
    *,
    raw_skip_line: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[SearchRecord]:
    """Parse a pi (earendil-works/pi) session JSONL transcript.

    Line 1 is a ``type:"session"`` header (capturing ``id``/``cwd``);
    ``version`` may be absent in v1 files. Each later line is a
    ``SessionEntry`` tagged union. ``message`` entries become candidates
    whose role drives the prompt/history split (user turns are prompts);
    ``compaction``/``branch_summary`` summaries and ``session_info`` names
    are emitted as history text. Metadata-only entries are skipped.
    """
    session_id: str | None = source.path.stem
    conversation_id: str | None = None
    events = (
        _iter_jsonl(
            source.path,
            skip_line=raw_skip_line,
            skip_line_mode="line",
            reverse=reverse,
        )
        if raw_skip_line is not None
        else _iter_jsonl(source.path, reverse=reverse)
    )
    for event in events:
        if not isinstance(event, dict):
            continue
        mapping = t.cast("dict[str, object]", event)
        entry_type = as_optional_str(mapping.get("type"))
        if not entry_type:
            continue
        if entry_type == "session":
            session_id = as_optional_str(mapping.get("id")) or session_id
            conversation_id = as_optional_str(mapping.get("cwd"))
            continue
        entry_timestamp = as_optional_str(mapping.get("timestamp"))
        if entry_type == "message":
            candidate = _pi_message_candidate(
                mapping,
                entry_timestamp,
                session_id,
                conversation_id,
            )
            if candidate is not None:
                yield build_search_record(source, candidate)
            continue
        text = _pi_entry_text(entry_type, mapping)
        if not text:
            continue
        yield SearchRecord(
            kind="history",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=text,
            role=entry_type,
            timestamp=entry_timestamp,
            session_id=session_id,
            conversation_id=conversation_id,
        )


def parse_text_store_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse opt-in plain-text inventory stores as one sample record."""
    text = read_text_file(source.path).strip()
    if not text:
        return
    yield SearchRecord(
        kind="history",
        agent=source.agent,
        store=source.store,
        adapter_id=source.adapter_id,
        path=source.path,
        text=text,
        title=source.store,
        timestamp=isoformat_from_mtime_ns(source.mtime_ns),
        metadata={"coverage": source.coverage.value},
    )


def parse_claude_store_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse opt-in Claude Code ``__store.db`` message samples."""
    connection = open_readonly_sqlite(source.path)
    try:
        tables = sqlite_table_names(connection)
        has_base = "base_messages" in tables
        if "user_messages" in tables:
            query = (
                """
                SELECT u.uuid, u.message, u.timestamp, b.session_id
                FROM user_messages u
                LEFT JOIN base_messages b ON b.uuid = u.uuid
                """
                if has_base
                else "SELECT uuid, message, timestamp, NULL FROM user_messages"
            )
            rows = t.cast(
                "cabc.Iterable[tuple[object, object, object, object]]",
                connection.execute(query),
            )
            for uuid, message, timestamp, session in rows:
                text = decode_sqlite_value(message) or as_optional_str(message)
                if not text:
                    continue
                session_id = as_optional_str(session)
                yield SearchRecord(
                    kind="prompt",
                    agent=source.agent,
                    store=source.store,
                    adapter_id=source.adapter_id,
                    path=source.path,
                    text=text,
                    title="Claude SQLite user message",
                    role="user",
                    timestamp=as_optional_str(timestamp),
                    session_id=session_id,
                    conversation_id=session_id or as_optional_str(uuid),
                )
        if "assistant_messages" in tables:
            query = (
                """
                SELECT a.uuid, a.message, a.timestamp, a.model, b.session_id
                FROM assistant_messages a
                LEFT JOIN base_messages b ON b.uuid = a.uuid
                """
                if has_base
                else "SELECT uuid, message, timestamp, model, NULL FROM assistant_messages"
            )
            rows = t.cast(
                "cabc.Iterable[tuple[object, object, object, object, object]]",
                connection.execute(query),
            )
            for uuid, message, timestamp, model, session in rows:
                text = decode_sqlite_value(message) or as_optional_str(message)
                if not text:
                    continue
                session_id = as_optional_str(session)
                yield SearchRecord(
                    kind="history",
                    agent=source.agent,
                    store=source.store,
                    adapter_id=source.adapter_id,
                    path=source.path,
                    text=text,
                    title="Claude SQLite assistant message",
                    role="assistant",
                    timestamp=as_optional_str(timestamp),
                    model=as_optional_str(model),
                    session_id=session_id,
                    conversation_id=session_id or as_optional_str(uuid),
                )
        if "conversation_summaries" in tables:
            query = (
                """
                SELECT c.leaf_uuid, c.summary, c.updated_at, b.session_id
                FROM conversation_summaries c
                LEFT JOIN base_messages b ON b.uuid = c.leaf_uuid
                """
                if has_base
                else "SELECT leaf_uuid, summary, updated_at, NULL FROM conversation_summaries"
            )
            rows = t.cast(
                "cabc.Iterable[tuple[object, object, object, object]]",
                connection.execute(query),
            )
            for leaf_uuid, summary, updated_at, session in rows:
                text = decode_sqlite_value(summary) or as_optional_str(summary)
                if not text:
                    continue
                session_id = as_optional_str(session)
                yield SearchRecord(
                    kind="history",
                    agent=source.agent,
                    store=source.store,
                    adapter_id=source.adapter_id,
                    path=source.path,
                    text=text,
                    title="Claude conversation summary",
                    role="assistant",
                    timestamp=as_optional_str(updated_at),
                    session_id=session_id,
                    conversation_id=session_id or as_optional_str(leaf_uuid),
                )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_codex_state_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse opt-in Codex ``state_5.sqlite`` prompt-bearing fields."""
    connection = open_readonly_sqlite(source.path)
    try:
        tables = sqlite_table_names(connection)
        if "threads" in tables:
            columns = sqlite_column_names(connection, "threads")
            if {"id", "first_user_message"}.issubset(columns):
                preview_expr = "preview" if "preview" in columns else "NULL"
                title_expr = "title" if "title" in columns else "NULL"
                updated_expr = "updated_at_ms" if "updated_at_ms" in columns else "NULL"
                rows = t.cast(
                    "cabc.Iterable[tuple[object, object, object, object, object]]",
                    connection.execute(
                        "SELECT id, first_user_message, "
                        f"{preview_expr}, {title_expr}, {updated_expr} FROM threads",
                    ),
                )
                for thread_id, first_message, preview, title, updated_at in rows:
                    conversation_id = as_optional_str(thread_id)
                    thread_title = as_optional_str(title)
                    timestamp = _unix_millis_to_isoformat(updated_at)
                    text = decode_sqlite_value(first_message) or as_optional_str(first_message)
                    if text:
                        yield SearchRecord(
                            kind="prompt",
                            agent=source.agent,
                            store=source.store,
                            adapter_id=source.adapter_id,
                            path=source.path,
                            text=text,
                            title=thread_title or "Codex thread first prompt",
                            role="user",
                            timestamp=timestamp,
                            session_id=conversation_id,
                            conversation_id=conversation_id,
                        )
                    preview_text = decode_sqlite_value(preview) or as_optional_str(preview)
                    if preview_text and preview_text != text:
                        yield SearchRecord(
                            kind="history",
                            agent=source.agent,
                            store=source.store,
                            adapter_id=source.adapter_id,
                            path=source.path,
                            text=preview_text,
                            title=thread_title or "Codex thread preview",
                            role="assistant",
                            timestamp=timestamp,
                            session_id=conversation_id,
                            conversation_id=conversation_id,
                            metadata={"field": "preview"},
                        )
        if "agent_jobs" in tables:
            columns = sqlite_column_names(connection, "agent_jobs")
            if {"id", "instruction"}.issubset(columns):
                thread_expr = "thread_id" if "thread_id" in columns else "NULL"
                updated_expr = "updated_at_ms" if "updated_at_ms" in columns else "NULL"
                rows = t.cast(
                    "cabc.Iterable[tuple[object, object, object, object]]",
                    connection.execute(
                        f"SELECT id, {thread_expr}, instruction, {updated_expr} FROM agent_jobs",
                    ),
                )
                for job_id, thread_id, instruction, updated_at in rows:
                    text = decode_sqlite_value(instruction) or as_optional_str(instruction)
                    if not text:
                        continue
                    conversation_id = as_optional_str(thread_id)
                    yield SearchRecord(
                        kind="prompt",
                        agent=source.agent,
                        store=source.store,
                        adapter_id=source.adapter_id,
                        path=source.path,
                        text=text,
                        title="Codex agent job instruction",
                        role="user",
                        timestamp=_unix_millis_to_isoformat(updated_at),
                        session_id=conversation_id,
                        conversation_id=conversation_id,
                        metadata={"job_id": as_optional_str(job_id) or ""},
                    )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_codex_logs_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse opt-in Codex ``logs_2.sqlite`` feedback log bodies."""
    connection = open_readonly_sqlite(source.path)
    try:
        tables = sqlite_table_names(connection)
        if "logs" not in tables:
            return
        columns = sqlite_column_names(connection, "logs")
        if "feedback_log_body" not in columns:
            return
        id_expr = "id" if "id" in columns else "NULL"
        ts_expr = "ts" if "ts" in columns else "NULL"
        level_expr = "level" if "level" in columns else "NULL"
        target_expr = "target" if "target" in columns else "NULL"
        thread_expr = "thread_id" if "thread_id" in columns else "NULL"
        rows = t.cast(
            "cabc.Iterable[tuple[object, object, object, object, object, object]]",
            connection.execute(
                f"SELECT {id_expr}, {ts_expr}, {level_expr}, {target_expr}, "
                f"feedback_log_body, {thread_expr} FROM logs",
            ),
        )
        for row_id, timestamp, level, target, body, thread_id in rows:
            text = decode_sqlite_value(body) or as_optional_str(body)
            if not text:
                continue
            conversation_id = as_optional_str(thread_id)
            metadata: dict[str, object] = {}
            level_text = as_optional_str(level)
            target_text = as_optional_str(target)
            if level_text:
                metadata["level"] = level_text
            if target_text:
                metadata["target"] = target_text
            log_id = as_optional_str(row_id)
            if log_id and not metadata:
                metadata["log_id"] = log_id
            yield SearchRecord(
                kind="history",
                agent=source.agent,
                store=source.store,
                adapter_id=source.adapter_id,
                path=source.path,
                text=text,
                title="Codex feedback log",
                role="system",
                timestamp=as_optional_str(timestamp),
                session_id=conversation_id,
                conversation_id=conversation_id,
                metadata=metadata,
            )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_codex_memories_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse opt-in Codex ``memories_1.sqlite`` memory summaries."""
    connection = open_readonly_sqlite(source.path)
    try:
        tables = sqlite_table_names(connection)
        if "stage1_outputs" not in tables:
            return
        columns = sqlite_column_names(connection, "stage1_outputs")
        if not {"thread_id", "raw_memory"}.issubset(columns):
            return
        summary_expr = "rollout_summary" if "rollout_summary" in columns else "NULL"
        slug_expr = "rollout_slug" if "rollout_slug" in columns else "NULL"
        rows = t.cast(
            "cabc.Iterable[tuple[object, object, object, object]]",
            connection.execute(
                f"SELECT thread_id, raw_memory, {summary_expr}, {slug_expr} FROM stage1_outputs",
            ),
        )
        for thread_id, raw_memory, rollout_summary, rollout_slug in rows:
            conversation_id = as_optional_str(thread_id)
            for field_name, value in (
                ("raw_memory", raw_memory),
                ("rollout_summary", rollout_summary),
            ):
                text = decode_sqlite_value(value) or as_optional_str(value)
                if not text:
                    continue
                yield SearchRecord(
                    kind="history",
                    agent=source.agent,
                    store=source.store,
                    adapter_id=source.adapter_id,
                    path=source.path,
                    text=text,
                    title=as_optional_str(rollout_slug) or "Codex memory",
                    role="assistant",
                    session_id=conversation_id,
                    conversation_id=conversation_id,
                    metadata={"field": field_name},
                )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_codex_external_imports_file(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse Codex external-agent session import ledgers as opt-in summaries."""
    payload = read_json_file(source.path)
    if not isinstance(payload, dict):
        return
    records = payload.get("records")
    if not isinstance(records, list):
        return
    for entry in records:
        if not isinstance(entry, dict):
            continue
        mapping = t.cast("dict[str, object]", entry)
        thread_id = (
            as_optional_str(mapping.get("imported_thread_id"))
            or as_optional_str(mapping.get("thread_id"))
            or as_optional_str(mapping.get("id"))
        )
        if not thread_id:
            continue
        source_path = as_optional_str(mapping.get("source_path"))
        metadata: dict[str, object] = {}
        content_hash = as_optional_str(mapping.get("content_hash"))
        if content_hash:
            metadata["content_hash"] = content_hash
        if source_path:
            metadata["source_name"] = pathlib.PurePath(source_path).name
        yield SearchRecord(
            kind="history",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=f"Imported external agent session {thread_id}",
            title="Codex external import",
            timestamp=as_optional_str(mapping.get("imported_at")),
            session_id=thread_id,
            conversation_id=thread_id,
            metadata=metadata,
        )


def parse_codex_goals_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse opt-in Codex ``goals_1.sqlite`` goal objectives."""
    connection = open_readonly_sqlite(source.path)
    try:
        tables = sqlite_table_names(connection)
        if "thread_goals" not in tables:
            return
        columns = sqlite_column_names(connection, "thread_goals")
        if not {"thread_id", "goal_id", "objective"}.issubset(columns):
            return
        status_expr = "status" if "status" in columns else "NULL"
        updated_expr = "updated_at_ms" if "updated_at_ms" in columns else "NULL"
        rows = t.cast(
            "cabc.Iterable[tuple[object, object, object, object, object]]",
            connection.execute(
                f"SELECT thread_id, goal_id, objective, {status_expr}, {updated_expr} "
                "FROM thread_goals",
            ),
        )
        for thread_id, goal_id, objective, status, updated_at in rows:
            text = decode_sqlite_value(objective) or as_optional_str(objective)
            if not text:
                continue
            conversation_id = as_optional_str(thread_id)
            yield SearchRecord(
                kind="prompt",
                agent=source.agent,
                store=source.store,
                adapter_id=source.adapter_id,
                path=source.path,
                text=text,
                title="Codex goal objective",
                role="user",
                timestamp=_unix_millis_to_isoformat(updated_at),
                session_id=conversation_id,
                conversation_id=conversation_id,
                metadata={
                    "goal_id": as_optional_str(goal_id) or "",
                    "status": as_optional_str(status) or "",
                },
            )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def parse_grok_session_search_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse the Grok CLI ``session_search.sqlite`` FTS5 index.

    Table ``session_docs`` has columns: ``session_id``, ``cwd``,
    ``updated_at`` (unix seconds), ``title`` (generated), ``content``
    (full-text indexed session body), ``content_hash``.
    """
    connection = open_readonly_sqlite(source.path)
    try:
        cursor = connection.execute(
            "SELECT session_id, title, content, updated_at FROM session_docs",
        )
        for row in cursor:
            session_id_raw, title_raw, content_raw, updated_at_raw = row
            text = content_raw if isinstance(content_raw, str) and content_raw.strip() else None
            if not text:
                continue
            session_id = as_optional_str(session_id_raw)
            yield SearchRecord(
                kind="history",
                agent=source.agent,
                store=source.store,
                adapter_id=source.adapter_id,
                path=source.path,
                text=text,
                title=title_raw if isinstance(title_raw, str) else None,
                role="assistant",
                timestamp=_unix_to_isoformat(updated_at_raw),
                session_id=session_id,
                conversation_id=session_id,
            )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


def _opencode_json_object(raw: object) -> dict[str, object] | None:
    """Parse a JSON object from an OpenCode SQLite ``data`` text column."""
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except ValueError, TypeError:
        return None
    return t.cast("dict[str, object]", value) if isinstance(value, dict) else None


def _opencode_part_text(part_type: str, part_data: dict[str, object]) -> str | None:
    """Return the searchable text for an OpenCode message part.

    ``text``/``reasoning`` parts carry the prompt, reply, or model thinking
    under ``text``; ``subtask`` parts carry a ``prompt``/``description``.
    Other part types (tool, file, snapshot, patch, step markers, …) are
    metadata or opt-in and contribute no default-search text.
    """
    if part_type in {"text", "reasoning"}:
        return as_optional_str(part_data.get("text"))
    if part_type == "subtask":
        return as_optional_str(part_data.get("prompt")) or as_optional_str(
            part_data.get("description"),
        )
    return None


def parse_opencode_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse an OpenCode ``opencode.db`` SQLite store.

    Joins ``part`` -> ``message`` -> ``session``: each text-bearing part
    becomes one record whose ``kind`` is derived from the joined message
    ``role`` (user -> prompt, else history), with the session title,
    working directory, and the message model/timestamp attached. Degrades
    gracefully when the expected tables or columns are absent.
    """
    connection = open_readonly_sqlite(source.path)
    try:
        if not {"session", "message", "part"}.issubset(sqlite_table_names(connection)):
            return
        cursor = connection.execute(
            "SELECT p.data, m.data, s.title, s.directory, s.id "
            "FROM part p "
            "JOIN message m ON p.message_id = m.id "
            "JOIN session s ON p.session_id = s.id "
            "ORDER BY s.id, m.id, p.id",
        )
        for part_raw, message_raw, title_raw, directory_raw, session_id_raw in cursor:
            part_data = _opencode_json_object(part_raw)
            if part_data is None:
                continue
            part_type = as_optional_str(part_data.get("type"))
            if not part_type:
                continue
            text = _opencode_part_text(part_type, part_data)
            if not text:
                continue
            message_data = _opencode_json_object(message_raw) or {}
            role = as_optional_str(message_data.get("role")) or "assistant"
            kind: t.Literal["prompt", "history"] = (
                "prompt" if role.casefold() in USER_ROLES else "history"
            )
            time_obj = message_data.get("time")
            created = (
                t.cast("dict[str, object]", time_obj).get("created")
                if isinstance(time_obj, dict)
                else None
            )
            session_id = as_optional_str(session_id_raw)
            directory = as_optional_str(directory_raw)
            yield SearchRecord(
                kind=kind,
                agent=source.agent,
                store=source.store,
                adapter_id=source.adapter_id,
                path=source.path,
                text=text,
                title=as_optional_str(title_raw),
                role=role,
                timestamp=_unix_millis_to_isoformat(created),
                model=as_optional_str(message_data.get("modelID")),
                session_id=session_id,
                conversation_id=session_id,
                metadata={"directory": directory} if directory else {},
            )
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()


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


def parse_cursor_prompt_history(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Parse a Cursor CLI ``prompt_history.json`` file.

    The file is a flat JSON array of strings — one entry per prompt the
    user typed into ``cursor-agent``, oldest first. It is the CLI's
    up-arrow recall buffer, giving Cursor the same prompt-history store
    the ``claude``/``codex``/``grok`` backends already expose. The file
    carries no per-entry timestamps, so the file mtime is used as a
    shared fallback.
    """
    payload = read_json_file(source.path)
    if not isinstance(payload, list):
        return
    timestamp = isoformat_from_mtime_ns(source.mtime_ns)
    seen: set[str] = set()
    for entry in payload:
        prompt = as_optional_str(entry)
        if prompt is None:
            continue
        prompt = prompt.strip()
        if not prompt or prompt in seen:
            continue
        seen.add(prompt)
        yield SearchRecord(
            kind="prompt",
            agent=source.agent,
            store=source.store,
            adapter_id=source.adapter_id,
            path=source.path,
            text=prompt,
            title="Cursor CLI prompt history",
            role="user",
            timestamp=timestamp,
        )


_CURSOR_CHATS_MIN_TEXT = 16
"""Shortest decoded protobuf run treated as Cursor CLI chat text.

Long enough to drop field junk (model ids, UUIDs, time zones) while
keeping real prompts and assistant turns. Content-addressed child
hashes are stored as raw bytes, so they fail the UTF-8 gate before this
length check ever applies.
"""


def _read_varint(data: bytes, start: int) -> tuple[int | None, int]:
    """Decode a base-128 varint.

    Returns ``(value, next_index)``; ``value`` is ``None`` when the bytes
    run out mid-varint or the value would exceed 64 bits.
    """
    result = 0
    shift = 0
    index = start
    length = len(data)
    while index < length:
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, index
        shift += 7
        if shift > 63:
            return None, index
    return None, index


def _looks_like_protobuf_message(chunk: bytes) -> bool:
    """Guess whether a length-delimited chunk is a nested message.

    A nested message begins with a tag byte: a low value whose lowest
    three bits are a valid wire type. Real UTF-8 text begins with a
    printable byte (``>= 0x20``) or a multi-byte lead, so the two rarely
    collide.
    """
    if not chunk:
        return False
    first = chunk[0]
    return first < 0x20 and (first & 0x07) in (0, 1, 2, 5)


def _decode_protobuf_text(chunk: bytes, min_length: int) -> str | None:
    """Return ``chunk`` as text when it is a plausible UTF-8 string."""
    if len(chunk) < min_length:
        return None
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return None
    printable = sum(1 for char in text if char.isprintable() or char in "\n\t")
    if printable / len(text) < 0.85:
        return None
    return text


def iter_protobuf_text_fields(
    data: bytes,
    *,
    min_length: int = 2,
    _depth: int = 0,
) -> cabc.Iterator[str]:
    r"""Yield readable UTF-8 runs from an unknown protobuf message.

    Walks the protobuf wire format without a schema: each
    length-delimited (wire type 2) field is decoded as UTF-8 and yielded
    when it looks like text, otherwise recursed into as a nested message.
    A best-effort extractor for opaque protobuf blobs — such as the
    Cursor CLI chat ``store.db`` — whose schema is unofficial and may
    drift. It never raises on malformed input; unparseable bytes simply
    end the walk.

    Parameters
    ----------
    data : bytes
        Raw protobuf message bytes.
    min_length : int
        Shortest decoded string to yield.

    Yields
    ------
    str
        Each plausible text run, in wire order.

    Examples
    --------
    >>> list(iter_protobuf_text_fields(b"\x0a\x05hello"))
    ['hello']
    >>> list(iter_protobuf_text_fields(b"\x0a\x07\x0a\x05world"))
    ['world']
    >>> list(iter_protobuf_text_fields(b"\x08\x96\x01"))
    []
    """
    if _depth > 12:
        return
    index = 0
    length = len(data)
    while index < length:
        tag, index = _read_varint(data, index)
        if tag is None:
            return
        wire_type = tag & 0x07
        if wire_type == 0:
            _, index = _read_varint(data, index)
        elif wire_type == 2:
            size, index = _read_varint(data, index)
            if size is None or index + size > length:
                return
            chunk = data[index : index + size]
            index += size
            if _looks_like_protobuf_message(chunk):
                yield from iter_protobuf_text_fields(
                    chunk, min_length=min_length, _depth=_depth + 1
                )
                continue
            text = _decode_protobuf_text(chunk, min_length)
            if text is not None:
                yield text
            else:
                yield from iter_protobuf_text_fields(
                    chunk, min_length=min_length, _depth=_depth + 1
                )
        elif wire_type == 5:
            index += 4
        elif wire_type == 1:
            index += 8
        else:
            return


def parse_cursor_cli_chats_db(
    source: SourceHandle,
) -> cabc.Iterator[SearchRecord]:
    """Best-effort parse of a Cursor CLI ``chats/*/store.db`` blob store.

    The CLI persists each session as content-addressed protobuf blobs in
    a ``blobs(id, data)`` table, rooted by ``meta``'s ``latestRootBlobId``.
    Cursor publishes no schema, so agentgrep walks the protobuf wire
    format generically (:func:`iter_protobuf_text_fields`) and surfaces
    the readable UTF-8 runs it finds. The adapter is versioned by
    observation date (``cursor_cli.chats_protobuf.v1``) because the layout
    is unofficial and may shift. The session UUID comes from the parent
    directory name.
    """
    session_uuid = source.path.parent.name
    timestamp = isoformat_from_mtime_ns(source.mtime_ns)
    connection = open_readonly_sqlite(source.path)
    try:
        if "blobs" not in sqlite_table_names(connection):
            return
        rows = t.cast(
            "cabc.Iterable[tuple[object]]",
            connection.execute("SELECT data FROM blobs"),
        )
        seen: set[str] = set()
        for (blob,) in rows:
            if not isinstance(blob, (bytes, bytearray)):
                continue
            for text in iter_protobuf_text_fields(bytes(blob), min_length=_CURSOR_CHATS_MIN_TEXT):
                normalized = text.strip()
                if len(normalized) < _CURSOR_CHATS_MIN_TEXT or normalized in seen:
                    continue
                seen.add(normalized)
                yield SearchRecord(
                    kind="history",
                    agent=source.agent,
                    store=source.store,
                    adapter_id=source.adapter_id,
                    path=source.path,
                    text=normalized,
                    title="Cursor CLI chat",
                    role=None,
                    timestamp=timestamp,
                    session_id=session_uuid,
                    conversation_id=session_uuid,
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
            for key, raw_value in iter_key_value_rows(
                connection,
                table,
                key_tokens=CURSOR_STATE_TOKENS,
            ):
                decoded = decode_sqlite_value(raw_value)
                if decoded is None:
                    continue
                parsed = parse_embedded_json(decoded)
                if parsed is None:
                    continue
                candidates = itertools.chain(
                    iter_message_candidates(
                        parsed,
                        fallback_title=key,
                        fallback_conversation_id=key,
                    ),
                    iter_cursor_prompt_candidates(parsed, fallback_conversation_id=key),
                )
                for candidate in candidates:
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


def sqlite_column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    """Return the column names for a known SQLite table."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        return set()
    rows = t.cast(
        "cabc.Iterable[tuple[object, ...]]",
        connection.execute(f"PRAGMA table_info({table})"),
    )
    columns: set[str] = set()
    for row in rows:
        if len(row) > 1 and isinstance(row[1], str):
            columns.add(row[1])
    return columns


def iter_key_value_rows(
    connection: sqlite3.Connection,
    table: str,
    *,
    key_tokens: cabc.Sequence[str] | None = None,
) -> cabc.Iterator[tuple[str, object]]:
    """Yield likely key/value rows, reading values only for matched keys.

    Stage 1 selects keys only — optionally filtered in SQL by
    ``key_tokens`` substrings — so large non-matching ``value`` BLOBs are
    never materialized; on the real Cursor schema the key scan rides a
    covering index. Stage 2 point-fetches ``value`` per distinct matched
    key, yielding every row for keys that repeat in index-less databases.
    """
    if table not in {"ItemTable", "cursorDiskKV"}:
        return
    info = t.cast(
        "cabc.Iterable[tuple[object, ...]]",
        connection.execute(f"PRAGMA table_info({table})"),
    )
    columns = [str(row[1]) for row in info]
    if "key" not in columns or "value" not in columns:
        return
    key_query = f"SELECT key FROM {table}"  # table validated against the allowlist above
    parameters: tuple[str, ...] = ()
    if key_tokens is not None:
        tokens = tuple(token for token in key_tokens if token)
        if tokens:
            predicates = " OR ".join("key LIKE ? COLLATE NOCASE" for _ in tokens)
            key_query = f"{key_query} WHERE {predicates}"
            parameters = tuple(f"%{token}%" for token in tokens)
    seen_keys: set[str] = set()
    matched_keys: list[str] = []
    key_rows = t.cast(
        "cabc.Iterable[tuple[object]]",
        connection.execute(key_query, parameters),
    )
    for (key,) in key_rows:
        if isinstance(key, str) and key not in seen_keys:
            seen_keys.add(key)
            matched_keys.append(key)
    value_query = f"SELECT value FROM {table} WHERE key = ?"  # table validated above
    for key in matched_keys:
        value_rows = t.cast(
            "cabc.Iterable[tuple[object]]",
            connection.execute(value_query, (key,)),
        )
        for (value,) in value_rows:
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
    yield from _iter_jsonl(path)


def _iter_jsonl(
    path: pathlib.Path,
    *,
    skip_line: RawJsonlSkipLine | None = None,
    skip_line_mode: t.Literal["prefix", "line"] = "prefix",
    full_line_skip: RawJsonlSkipLine | None = None,
    reverse: bool = False,
) -> cabc.Iterator[JSONValue]:
    """Yield decoded JSON objects from a JSONL file with an optional raw-line filter.

    ``skip_line`` runs in ``skip_line_mode``: ``"prefix"`` checks only the
    first :data:`_JSONL_PREFIX_BYTES` of each line so oversized lines can be
    discarded in chunks without full allocation, while ``"line"`` checks the
    whole line. ``full_line_skip`` always sees the complete decoded line
    before JSON decode, so predicates that may match past the prefix window
    stay correct alongside a cheap prefix skip. Reverse iteration ignores
    ``skip_line_mode``: both predicates are combined and run against full
    decoded lines, because reverse reads already materialize each line from
    tail chunks.
    """
    if reverse:
        yield from _iter_jsonl_reverse(
            path,
            skip_line=_combine_raw_skip_lines(skip_line, full_line_skip),
        )
        return
    if skip_line is not None:
        if skip_line_mode == "line":
            combined = _combine_raw_skip_lines(skip_line, full_line_skip)
            assert combined is not None
            yield from _iter_jsonl_with_raw_line_skip(path, combined)
        else:
            yield from _iter_jsonl_with_raw_prefix_skip(
                path,
                skip_line,
                full_line_skip=full_line_skip,
            )
        return
    if full_line_skip is not None:
        yield from _iter_jsonl_with_raw_line_skip(path, full_line_skip)
        return
    try:
        with path.open(encoding="utf-8") as handle:
            decoded_lines = 0
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                decoded_lines += 1
                if decoded_lines % _JSONL_YIELD_LINE_INTERVAL == 0:
                    time.sleep(0)
                try:
                    parsed = t.cast("object", json.loads(stripped))
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
                    yield t.cast("JSONValue", parsed)
    except OSError:
        return


def _iter_jsonl_reverse(
    path: pathlib.Path,
    *,
    skip_line: RawJsonlSkipLine | None = None,
) -> cabc.Iterator[JSONValue]:
    """Yield decoded JSONL values from the end of ``path`` toward the start."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            pending = b""
            decoded_lines = 0
            while position > 0:
                read_size = min(_JSONL_REVERSE_CHUNK_BYTES, position)
                position -= read_size
                handle.seek(position)
                pending = handle.read(read_size) + pending
                lines = pending.split(b"\n")
                pending = lines[0]
                for raw_line in reversed(lines[1:]):
                    decoded = _decode_jsonl_raw_line(raw_line, skip_line=skip_line)
                    if decoded is _SKIPPED_JSONL_LINE:
                        continue
                    decoded_lines += 1
                    if decoded_lines % _JSONL_YIELD_LINE_INTERVAL == 0:
                        time.sleep(0)
                    yield t.cast("JSONValue", decoded)
            if pending.strip():
                decoded = _decode_jsonl_raw_line(pending, skip_line=skip_line)
                if decoded is not _SKIPPED_JSONL_LINE:
                    decoded_lines += 1
                    if decoded_lines % _JSONL_YIELD_LINE_INTERVAL == 0:
                        time.sleep(0)
                    yield t.cast("JSONValue", decoded)
    except OSError:
        return


_SKIPPED_JSONL_LINE = object()


def _decode_jsonl_raw_line(
    raw_line: bytes,
    *,
    skip_line: RawJsonlSkipLine | None = None,
) -> JSONValue | object:
    """Decode one raw JSONL line, or return a sentinel for skipped/invalid lines."""
    if not raw_line.strip():
        return _SKIPPED_JSONL_LINE
    line = raw_line.decode("utf-8", errors="replace")
    if skip_line is not None and skip_line(line):
        return _SKIPPED_JSONL_LINE
    stripped = line.strip()
    if not stripped:
        return _SKIPPED_JSONL_LINE
    try:
        parsed = t.cast("object", json.loads(stripped))
    except json.JSONDecodeError:
        return _SKIPPED_JSONL_LINE
    if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
        return t.cast("JSONValue", parsed)
    return _SKIPPED_JSONL_LINE


def _iter_jsonl_with_raw_prefix_skip(
    path: pathlib.Path,
    skip_line: RawJsonlSkipLine,
    *,
    full_line_skip: RawJsonlSkipLine | None = None,
) -> cabc.Iterator[JSONValue]:
    """Yield decoded JSON objects while skipping matched raw prefixes.

    ``skip_line`` sees only the line prefix and gates the chunked discard
    path; ``full_line_skip`` sees the fully accumulated line before JSON
    decode.
    """
    try:
        with path.open("rb") as handle:
            decoded_lines = 0
            while True:
                prefix = handle.readline(_JSONL_PREFIX_BYTES)
                if not prefix:
                    break
                if not prefix.strip():
                    continue
                decoded_lines += 1
                if decoded_lines % _JSONL_YIELD_LINE_INTERVAL == 0:
                    time.sleep(0)
                prefix_text = prefix.decode("utf-8", errors="replace")
                if skip_line(prefix_text):
                    _discard_rest_of_line(handle, prefix)
                    continue
                raw_line = bytearray(prefix)
                while raw_line and not raw_line.endswith(b"\n"):
                    chunk = handle.readline(_JSONL_SKIP_CHUNK_BYTES)
                    if not chunk:
                        break
                    raw_line.extend(chunk)
                    time.sleep(0)
                full_text = raw_line.decode("utf-8", errors="replace")
                if full_line_skip is not None and full_line_skip(full_text):
                    continue
                stripped = full_text.strip()
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


def _iter_jsonl_with_raw_line_skip(
    path: pathlib.Path,
    skip_line: RawJsonlSkipLine,
) -> cabc.Iterator[JSONValue]:
    """Yield decoded JSON objects while skipping matched full raw lines."""
    try:
        with path.open("rb") as handle:
            decoded_lines = 0
            for raw_line in handle:
                if not raw_line.strip():
                    continue
                decoded_lines += 1
                if decoded_lines % _JSONL_YIELD_LINE_INTERVAL == 0:
                    time.sleep(0)
                line = raw_line.decode("utf-8", errors="replace")
                if skip_line(line):
                    continue
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


def _combine_raw_skip_lines(
    first: RawJsonlSkipLine | None,
    second: RawJsonlSkipLine | None,
) -> RawJsonlSkipLine | None:
    """Return a raw-line predicate that skips when either predicate skips."""
    if first is None:
        return second
    if second is None:
        return first

    def skip_line(raw_line: str) -> bool:
        return first(raw_line) or second(raw_line)

    return skip_line


def _discard_rest_of_line(handle: t.BinaryIO, prefix: bytes) -> None:
    """Discard the unread remainder of the current physical line."""
    chunk = prefix
    while chunk and not chunk.endswith(b"\n"):
        chunk = handle.readline(_JSONL_SKIP_CHUNK_BYTES)
        time.sleep(0)


def _is_codex_function_call_output_line(line: str) -> bool:
    """Return whether a Codex JSONL line is a tool output record."""
    prefix = line[:512].replace(" ", "")
    return (
        '"type":"response_item"' in prefix and '"payload":{"type":"function_call_output"' in prefix
    )


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


def iter_cursor_prompt_candidates(
    value: JSONValue | None,
    *,
    fallback_conversation_id: str | None = None,
) -> cabc.Iterator[MessageCandidate]:
    """Yield user-prompt candidates from Cursor ``aiService.prompts`` data.

    Cursor stores typed prompts as ``{"prompts": [{"text": ...,
    "commandType": int}]}`` (or a bare list of such entries). These carry
    no ``role`` field, so :func:`iter_message_candidates` skips them even
    though every entry is a user prompt. This recovers them for both the
    global and per-workspace ``state.vscdb`` stores.
    """
    entries: list[object] = []
    if isinstance(value, dict):
        prompts = t.cast("dict[str, object]", value).get("prompts")
        if isinstance(prompts, list):
            entries = list(t.cast("list[object]", prompts))
    elif isinstance(value, list):
        entries = [
            item
            for item in t.cast("list[object]", value)
            if isinstance(item, dict) and "commandType" in t.cast("dict[str, object]", item)
        ]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = as_optional_str(t.cast("dict[str, object]", entry).get("text"))
        if not text:
            continue
        yield MessageCandidate(
            role="user",
            text=text,
            title=None,
            timestamp=None,
            model=None,
            session_id=None,
            conversation_id=fallback_conversation_id,
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


PROMPT_HISTORY_STORE_ROLES: frozenset[StoreRole] = frozenset({StoreRole.PROMPT_HISTORY})

CONVERSATION_STORE_ROLES: frozenset[StoreRole] = frozenset(
    {StoreRole.PRIMARY_CHAT, StoreRole.SUPPLEMENTARY_CHAT},
)


def find_store_roles_for_type_filter(
    type_filter: FindSourceTypeFilter,
) -> DiscoveryStoreRoles:
    """Return catalogue roles that can satisfy a ``find --type`` filter."""
    if type_filter in {"prompts", "history"}:
        return PROMPT_HISTORY_STORE_ROLES
    if type_filter == "sessions":
        return CONVERSATION_STORE_ROLES
    return None


@functools.cache
def store_descriptor_for_record(store: str, adapter_id: str) -> StoreDescriptor | None:
    """Return the catalog descriptor for a normalized record's source store."""
    from agentgrep.store_catalog import CATALOG

    for descriptor in CATALOG.stores:
        for spec in descriptor.discovery:
            if spec.store == store and spec.adapter_id == adapter_id:
                return descriptor
    return None


def store_role_for_record(store: str, adapter_id: str) -> StoreRole | None:
    """Return the catalog role for a normalized record's source store."""
    descriptor = store_descriptor_for_record(store, adapter_id)
    if descriptor is None:
        return None
    return descriptor.role


def record_matches_scope(record: SearchRecord, scope: SearchScope) -> bool:
    """Return whether ``record`` belongs to the requested search scope."""
    if scope == "all":
        return True
    if scope == "prompts":
        return record.kind == "prompt"
    role = store_role_for_record(record.store, record.adapter_id)
    return role in CONVERSATION_STORE_ROLES


def prompt_history_agents_for_sources(sources: cabc.Iterable[SourceHandle]) -> frozenset[str]:
    """Return agents with a dedicated prompt-history source in ``sources``."""
    return frozenset(
        source.agent
        for source in sources
        if store_role_for_record(source.store, source.adapter_id) == StoreRole.PROMPT_HISTORY
    )


def discover_sources_for_search(
    home: pathlib.Path,
    query: SearchQuery,
    backends: BackendSelection,
    *,
    version_detail: DiscoveryVersionDetail = "none",
) -> list[SourceHandle]:
    """Discover only the source roles needed for a search query scope."""
    from agentgrep._engine.planning import build_logical_search_plan

    logical_plan = build_logical_search_plan(query)
    if query.scope == "all":
        return discover_sources(
            home,
            query.agents,
            backends,
            version_detail=version_detail,
        )
    if query.scope == "conversations":
        return discover_sources(
            home,
            query.agents,
            backends,
            version_detail=version_detail,
            store_roles=logical_plan.initial_store_roles,
        )

    prompt_sources = discover_sources(
        home,
        query.agents,
        backends,
        version_detail=version_detail,
        store_roles=logical_plan.initial_store_roles,
    )
    agents_with_prompt_history = frozenset(
        source.agent
        for source in prompt_sources
        if store_role_for_record(source.store, source.adapter_id) == StoreRole.PROMPT_HISTORY
    )
    fallback_agents = tuple(
        agent for agent in query.agents if agent not in agents_with_prompt_history
    )
    if not fallback_agents:
        return prompt_sources

    sources = [
        *prompt_sources,
        *discover_sources(
            home,
            fallback_agents,
            backends,
            version_detail=version_detail,
            store_roles=CONVERSATION_STORE_ROLES,
        ),
    ]
    deduped: list[SourceHandle] = []
    seen: set[tuple[AgentName, str, str, pathlib.Path]] = set()
    for source in sources:
        key = (source.agent, source.store, source.adapter_id, source.path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(source)
    return deduped


def source_matches_scope(
    source: SourceHandle,
    scope: SearchScope,
    *,
    prompt_history_agents: frozenset[str] = frozenset(),
) -> bool:
    """Return whether ``source`` can yield records for the requested scope."""
    if scope == "all":
        return True
    role = store_role_for_record(source.store, source.adapter_id)
    if scope == "conversations":
        return role in CONVERSATION_STORE_ROLES
    if role == StoreRole.PROMPT_HISTORY:
        return True
    if role in CONVERSATION_STORE_ROLES:
        return source.agent not in prompt_history_agents
    return True


def matches_record(record: SearchRecord, query: SearchQuery) -> bool:
    """Return whether a normalized record should be included.

    When ``query.compiled`` carries a record-level predicate, the
    record must satisfy it in addition to the existing text + scope
    checks. Pure-text queries skip the predicate evaluation since
    the compiler leaves ``compiled = None`` for them.
    """
    from agentgrep._engine.matching import matches_record as compiled_matches_record

    return compiled_matches_record(record, query)


def build_record_match_surface(record: SearchRecord, surface: SearchMatchSurface) -> str:
    """Build the text surface used for unfielded query terms."""
    if surface == "text":
        return record.text
    return build_search_haystack(record)


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


def run_ui(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
    initial_search_text: str | None = None,
) -> None:
    """Launch the streaming Textual explorer for ``query``.

    Thin wrapper that imports the real implementation from
    :mod:`agentgrep.ui.app` lazily so a bare ``import agentgrep`` never
    pulls in Textual.

    ``initial_search_text`` populates the TUI search box on open so a
    launch like ``agentgrep search --ui agent:codex bliss`` shows the
    full query string (not just the text terms). ``None`` falls back
    to the space-joined ``query.terms`` for compatibility with the
    pre-query-language callers.
    """
    from agentgrep.ui.app import run_ui as _run_ui

    _run_ui(
        home,
        query,
        control=control,
        initial_search_text=initial_search_text,
    )


def build_streaming_ui_app(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
    initial_search_text: str | None = None,
) -> object:
    """Construct the streaming Textual app without entering its run loop.

    Thin wrapper that imports the real factory from :mod:`agentgrep.ui.app`
    lazily — Textual is only required at the moment the UI is actually
    built, never at import time of the top-level package.
    """
    from agentgrep.ui.app import build_streaming_ui_app as _build

    return _build(
        home,
        query,
        control=control,
        initial_search_text=initial_search_text,
    )


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
        if isinstance(parsed, SearchArgs):
            return run_search_command(parsed)
        if isinstance(parsed, UIArgs):
            return run_ui_command(parsed)
        return run_find_command(parsed)
    except KeyboardInterrupt:
        _write_interrupt_notice()
        _exit_on_sigint()


from agentgrep._engine import (  # noqa: E402  (re-exports must follow main definition)
    SearchRuntime,
    SourceScanCache,
    SourceScanCacheStats,
    aiter_search_events,
    iter_find_events,
    iter_search_events,
)
from agentgrep.cli.parser import (  # noqa: E402  (re-exports must follow main definition)
    CaseMode,
    FindArgs,
    FindPatternMode,
    FindTypeFilter,
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
    maybe_build_pydantic,
    print_find_results,
    print_grep_results,
    run_find_command,
    run_grep_command,
    run_search_command,
    run_ui_command,
    serialize_find_record,
    serialize_grep_record,
    serialize_search_record,
    serialize_source_handle,
    stream_find_results,
    stream_grep_results,
)

if __name__ == "__main__":
    raise SystemExit(main())
