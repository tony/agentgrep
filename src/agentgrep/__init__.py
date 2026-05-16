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
import contextlib
import dataclasses
import importlib
import itertools
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import typing as t

if t.TYPE_CHECKING:
    import collections.abc as cabc

AgentName = t.Literal["codex", "claude", "cursor"]
OutputMode = t.Literal["text", "json", "ndjson", "ui"]
ProgressMode = t.Literal["auto", "always", "never"]
PathKind = t.Literal["history_file", "session_file", "sqlite_db"]
SearchType = t.Literal["prompts", "history", "all"]
SourceKind = t.Literal["json", "jsonl", "sqlite"]
ColorMode = t.Literal["auto", "always", "never"]
type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]
type SummaryRow = tuple[object, object, object, object, object, object, object, object]
type KeyValueRow = tuple[object, object]

AGENT_CHOICES: tuple[AgentName, ...] = ("codex", "claude", "cursor")
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

    ``search`` is the default subcommand. ``agentgrep bliss`` is
    equivalent to ``agentgrep search bliss``.
    """,
    (
        (
            "quick",
            (
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


def should_enable_help_color(color_mode: ColorMode) -> bool:
    """Return whether help output should use colors."""
    if os.environ.get("NO_COLOR"):
        return False
    if color_mode == "never":
        return False
    if color_mode == "always":
        return True
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


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


class TextualAppModule(t.Protocol):
    """Minimal Textual app module surface."""

    App: type[object]


class DataTableLike(t.Protocol):
    """Minimal DataTable surface used by the TUI."""

    cursor_type: str

    def add_columns(self, *labels: str) -> None:
        """Add columns."""
        ...

    def clear(self) -> None:
        """Clear rows."""
        ...

    def add_row(self, *values: str, key: str | None = None) -> None:
        """Add one row."""
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

    DataTable: cabc.Callable[..., object]
    Footer: cabc.Callable[[], object]
    Header: cabc.Callable[[], object]
    Input: cabc.Callable[..., object]
    Static: cabc.Callable[..., object]


@dataclasses.dataclass(slots=True)
class BackendSelection:
    """Selected optional subprocess backends."""

    find_tool: str | None
    grep_tool: str | None
    json_tool: str | None


@dataclasses.dataclass(slots=True)
class SearchArgs:
    """Typed arguments for ``agentgrep search``."""

    terms: tuple[str, ...]
    agents: tuple[AgentName, ...]
    search_type: SearchType
    any_term: bool
    regex: bool
    case_sensitive: bool
    limit: int | None
    output_mode: OutputMode
    color_mode: ColorMode
    progress_mode: ProgressMode


@dataclasses.dataclass(slots=True)
class FindArgs:
    """Typed arguments for ``agentgrep find``."""

    pattern: str | None
    agents: tuple[AgentName, ...]
    limit: int | None
    output_mode: OutputMode
    color_mode: ColorMode


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

    def finish(self, result_count: int) -> None:
        """Report search completion."""
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

    def finish(self, result_count: int) -> None:
        """Ignore search completion."""

    def close(self) -> None:
        """Nothing to release."""


class ConsoleSearchProgress:
    """Human progress reporter for potentially long searches."""

    _SPINNER_FRAMES: t.ClassVar[str] = "|/-\\"

    def __init__(
        self,
        *,
        enabled: bool,
        stream: t.TextIO | None = None,
        tty: bool | None = None,
        refresh_interval: float = 0.1,
        heartbeat_interval: float = 10.0,
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
        self._refresh_interval = refresh_interval
        self._heartbeat_interval = heartbeat_interval
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
            self._emit_line(f"Searching {label}")

    def sources_discovered(self, count: int) -> None:
        """Report discovered source count."""
        self.set_status("discovered", total=count, detail=f"{count} sources")

    def prefilter_started(self, root: pathlib.Path) -> None:
        """Report root prefilter start."""
        self.set_status("prefiltering", detail=str(root))

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
            f"Search complete: {format_match_count(result_count)} ({elapsed:.1f}s elapsed)",
        )

    def close(self) -> None:
        """Stop any active progress renderer."""
        if not self._enabled:
            return
        if self._tty:
            self._stop_tty_thread()
            self._clear_tty_line()

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
        line = f"{frame} {summary}"
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
            f"... still searching {label}: {self._status_text()} ({elapsed:.0f}s elapsed)",
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
        elapsed = self._elapsed_seconds()
        return " | ".join(
            (
                f"Searching {self._query_label}",
                self._status_text(),
                format_match_count(self._matches),
                f"{elapsed:.1f}s",
            ),
        )

    def _status_text(self) -> str:
        with self._lock:
            phase = self._phase
            current = self._current
            total = self._total
            detail = self._detail
        if current is not None and total is not None:
            return f"{phase} {current}/{total} sources"
        if detail:
            return f"{phase} {detail}"
        return phase

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


def noop_search_progress() -> SearchProgress:
    """Return a silent search progress reporter."""
    return NoopSearchProgress()


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


def run_readonly_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell and capture text output."""
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )


@dataclasses.dataclass(slots=True)
class ParserBundle:
    """CLI parsers used for root and subcommand help."""

    parser: argparse.ArgumentParser
    search_parser: argparse.ArgumentParser
    find_parser: argparse.ArgumentParser


def normalize_color_mode(argv: cabc.Sequence[str] | None) -> ColorMode:
    """Return the requested CLI color mode."""
    if argv is None:
        argv = sys.argv[1:]
    for index, argument in enumerate(argv):
        if argument == "--color" and index + 1 < len(argv):
            value = argv[index + 1]
            if value in {"auto", "always", "never"}:
                return t.cast("ColorMode", value)
        if argument.startswith("--color="):
            value = argument.partition("=")[2]
            if value in {"auto", "always", "never"}:
                return t.cast("ColorMode", value)
    return "auto"


SUBCOMMANDS: frozenset[str] = frozenset({"search", "find"})


def inject_default_subcommand(
    argv: cabc.Sequence[str] | None,
) -> cabc.Sequence[str] | None:
    """Prepend ``search`` to ``argv`` when no subcommand is supplied.

    Walks ``argv`` skipping the global ``--color`` option and any help flag.
    If the first remaining token is not a known subcommand, inserts
    ``search`` at that position so ``agentgrep bliss`` parses identically
    to ``agentgrep search bliss``. Returns the input unchanged when no
    injection is needed.

    Examples
    --------
    >>> inject_default_subcommand(["bliss"])
    ['search', 'bliss']
    >>> inject_default_subcommand(["search", "bliss"])
    ['search', 'bliss']
    >>> inject_default_subcommand(["find", "codex"])
    ['find', 'codex']
    >>> inject_default_subcommand(["--color", "never", "bliss"])
    ['--color', 'never', 'search', 'bliss']
    >>> inject_default_subcommand(["--help"])
    ['--help']
    >>> inject_default_subcommand([])
    []
    """
    effective = list(sys.argv[1:]) if argv is None else list(argv)
    index = 0
    while index < len(effective):
        token = effective[index]
        if token in {"-h", "--help"}:
            return argv
        if token == "--color" and index + 1 < len(effective):
            index += 2
            continue
        if token.startswith("--color="):
            index += 1
            continue
        if token in SUBCOMMANDS:
            return argv
        effective.insert(index, "search")
        return effective
    return argv


@contextlib.contextmanager
def configured_color_environment(color_mode: ColorMode) -> cabc.Iterator[None]:
    """Temporarily configure env vars for argparse help color handling."""
    force_color = os.environ.get("FORCE_COLOR")
    try:
        if color_mode == "always" and not os.environ.get("NO_COLOR"):
            os.environ["FORCE_COLOR"] = "1"
        yield
    finally:
        if force_color is None:
            _ = os.environ.pop("FORCE_COLOR", None)
        else:
            os.environ["FORCE_COLOR"] = force_color


def create_parser(
    color_mode: ColorMode,
) -> ParserBundle:
    """Create the root parser and subparsers."""
    formatter_class = create_themed_formatter(color_mode)
    parser = argparse.ArgumentParser(
        prog="agentgrep",
        description=CLI_DESCRIPTION,
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    _ = parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="when to use colors: auto (default), always, or never",
    )
    subparsers = parser.add_subparsers(dest="command")

    search_parser = subparsers.add_parser(
        "search",
        help="Search normalized prompts or history",
        description=SEARCH_DESCRIPTION,
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    add_common_agent_options(search_parser)
    _ = search_parser.add_argument("terms", nargs="*", help="Keywords or regex patterns")
    _ = search_parser.add_argument(
        "--type",
        choices=["prompts", "history", "all"],
        default="prompts",
        dest="search_type",
        help="Record type to search (default: prompts)",
    )
    _ = search_parser.add_argument(
        "--any",
        action="store_true",
        help="Match any term instead of requiring all terms",
    )
    _ = search_parser.add_argument(
        "--regex",
        action="store_true",
        help="Treat terms as regular expressions",
    )
    _ = search_parser.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Perform case-sensitive matching",
    )
    _ = search_parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Limit the number of results",
    )
    _ = search_parser.add_argument(
        "--progress",
        choices=["auto", "always", "never"],
        default="auto",
        help="Show search progress on stderr",
    )
    add_output_mode_options(search_parser, allow_ui=True)

    find_parser = subparsers.add_parser(
        "find",
        help="Find known prompt/history stores and session files",
        description=FIND_DESCRIPTION,
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    add_common_agent_options(find_parser)
    _ = find_parser.add_argument(
        "pattern",
        nargs="?",
        help="Optional substring to match against discovered paths",
    )
    _ = find_parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Limit the number of results",
    )
    add_output_mode_options(find_parser, allow_ui=False)
    return ParserBundle(parser=parser, search_parser=search_parser, find_parser=find_parser)


def parse_args(
    argv: cabc.Sequence[str] | None = None,
) -> SearchArgs | FindArgs | None:
    """Parse CLI arguments into typed dataclasses."""
    color_mode = normalize_color_mode(argv)
    argv = inject_default_subcommand(argv)
    with configured_color_environment(color_mode):
        bundle = create_parser(color_mode)
        namespace = bundle.parser.parse_args(argv)
    if t.cast("str | None", getattr(namespace, "command", None)) is None:
        with configured_color_environment(color_mode):
            bundle.parser.print_help()
        return None
    agents = parse_agents(t.cast("list[str]", namespace.agent))
    output_mode = parse_output_mode(namespace)
    limit = t.cast("int | None", namespace.limit)
    if limit is not None and limit < 1:
        with configured_color_environment(color_mode):
            bundle.parser.error("--limit must be greater than 0")

    command = t.cast("str", namespace.command)
    if command == "search":
        terms = tuple(t.cast("list[str]", namespace.terms))
        if not terms:
            with configured_color_environment(color_mode):
                bundle.search_parser.print_help()
            return None
        return SearchArgs(
            terms=terms,
            agents=agents,
            search_type=t.cast("SearchType", namespace.search_type),
            any_term=t.cast("bool", namespace.any),
            regex=t.cast("bool", namespace.regex),
            case_sensitive=t.cast("bool", namespace.case_sensitive),
            limit=limit,
            output_mode=output_mode,
            color_mode=color_mode,
            progress_mode=t.cast("ProgressMode", namespace.progress),
        )
    pattern = t.cast("str | None", namespace.pattern)
    if not pattern:
        with configured_color_environment(color_mode):
            bundle.find_parser.print_help()
        return None
    return FindArgs(
        pattern=pattern,
        agents=agents,
        limit=limit,
        output_mode=output_mode,
        color_mode=color_mode,
    )


def add_common_agent_options(parser: argparse.ArgumentParser) -> None:
    """Attach shared agent selection flags."""
    _ = parser.add_argument(
        "--agent",
        action="append",
        choices=[*AGENT_CHOICES, "all"],
        default=[],
        help="Limit results to a specific agent; repeatable",
    )


def add_output_mode_options(
    parser: argparse.ArgumentParser,
    *,
    allow_ui: bool,
) -> None:
    """Attach mutually exclusive output mode flags."""
    group = parser.add_mutually_exclusive_group()
    _ = group.add_argument("--json", action="store_true", help="Emit one JSON document")
    _ = group.add_argument("--ndjson", action="store_true", help="Emit one JSON object per line")
    if allow_ui:
        _ = group.add_argument("--ui", action="store_true", help="Launch a read-only UI")


def parse_agents(values: list[str]) -> tuple[AgentName, ...]:
    """Normalize ``--agent`` selections."""
    if not values or "all" in values:
        return AGENT_CHOICES
    ordered = tuple(t.cast("AgentName", value) for value in values if value != "all")
    return ordered or AGENT_CHOICES


def parse_output_mode(namespace: argparse.Namespace) -> OutputMode:
    """Return the selected output mode."""
    if getattr(namespace, "json", False):
        return "json"
    if getattr(namespace, "ndjson", False):
        return "ndjson"
    if getattr(namespace, "ui", False):
        return "ui"
    return "text"


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
    discovered.sort(key=lambda item: (item.agent, item.store, str(item.path)))
    return discovered


def file_mtime_ns(path: pathlib.Path) -> int:
    """Return a cached modification time for a path."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def discover_codex_sources(
    home: pathlib.Path,
    backends: BackendSelection,
) -> list[SourceHandle]:
    """Discover Codex sessions and command history."""
    root = home / ".codex"
    sources: list[SourceHandle] = []
    if not root.exists():
        return sources

    for name in ("history.json", "history.jsonl"):
        path = root / name
        if path.is_file():
            sources.append(
                SourceHandle(
                    agent="codex",
                    store="codex.history",
                    adapter_id="codex.history_json.v1",
                    path=path,
                    path_kind="history_file",
                    source_kind="jsonl" if path.suffix == ".jsonl" else "json",
                    search_root=None,
                    mtime_ns=file_mtime_ns(path),
                ),
            )

    sessions_root = root / "sessions"
    sources.extend(
        SourceHandle(
            agent="codex",
            store="codex.sessions",
            adapter_id="codex.sessions_jsonl.v1",
            path=path,
            path_kind="session_file",
            source_kind="jsonl",
            search_root=sessions_root,
            mtime_ns=file_mtime_ns(path),
        )
        for path in list_files_matching(sessions_root, "*.jsonl", backends.find_tool)
    )
    return sources


def discover_claude_sources(
    home: pathlib.Path,
    backends: BackendSelection,
) -> list[SourceHandle]:
    """Discover Claude Code project session files."""
    root = home / ".claude" / "projects"
    if not root.exists():
        return []
    return [
        SourceHandle(
            agent="claude",
            store="claude.projects",
            adapter_id="claude.projects_jsonl.v1",
            path=path,
            path_kind="session_file",
            source_kind="jsonl",
            search_root=root,
            mtime_ns=file_mtime_ns(path),
        )
        for path in list_files_matching(root, "*.jsonl", backends.find_tool)
    ]


def discover_cursor_sources(
    home: pathlib.Path,
    backends: BackendSelection,
) -> list[SourceHandle]:
    """Discover Cursor databases from both home-local and official roots."""
    sources: list[SourceHandle] = []
    tracking_db = home / ".cursor" / "ai-tracking" / "ai-code-tracking.db"
    if tracking_db.is_file():
        sources.append(
            SourceHandle(
                agent="cursor",
                store="cursor.ai_tracking",
                adapter_id="cursor.ai_tracking_sqlite.v1",
                path=tracking_db,
                path_kind="sqlite_db",
                source_kind="sqlite",
                search_root=None,
                mtime_ns=file_mtime_ns(tracking_db),
            ),
        )

    seen_paths: set[pathlib.Path] = set()
    for path in OFFICIAL_CURSOR_STATE_PATHS:
        if path.is_file():
            seen_paths.add(path)
            sources.append(
                SourceHandle(
                    agent="cursor",
                    store="cursor.state",
                    adapter_id="cursor.state_vscdb_modern.v1",
                    path=path,
                    path_kind="sqlite_db",
                    source_kind="sqlite",
                    search_root=None,
                    mtime_ns=file_mtime_ns(path),
                ),
            )
    cursor_root = home / ".cursor"
    for path in list_files_matching(cursor_root, "state.vscdb", backends.find_tool):
        if path in seen_paths:
            continue
        sources.append(
            SourceHandle(
                agent="cursor",
                store="cursor.state",
                adapter_id="cursor.state_vscdb_legacy.v1",
                path=path,
                path_kind="sqlite_db",
                source_kind="sqlite",
                search_root=None,
                mtime_ns=file_mtime_ns(path),
            ),
        )
    return sources


def list_files_matching(
    root: pathlib.Path,
    glob_pattern: str,
    fd_program: str | None,
) -> list[pathlib.Path]:
    """List files under ``root`` that match a glob."""
    if not root.exists():
        return []
    if fd_program is not None:
        command = [fd_program, "-a", "-t", "f", "--glob", glob_pattern, str(root)]
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
) -> list[SearchRecord]:
    """Parse and filter search results across all selected sources."""
    active_progress = noop_search_progress() if progress is None else progress
    planned_sources = plan_search_sources(query, sources, backends, progress=active_progress)
    active_progress.sources_planned(len(planned_sources), len(sources))
    records = collect_search_records(query, planned_sources, progress=active_progress)
    active_progress.finish(len(records))
    return records


def run_search_query(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    backends: BackendSelection | None = None,
    progress: SearchProgress | None = None,
) -> list[SearchRecord]:
    """Discover sources and run a normalized search query."""
    active_backends = select_backends() if backends is None else backends
    active_progress = noop_search_progress() if progress is None else progress
    active_progress.start(query)
    try:
        sources = discover_sources(home, query.agents, active_backends)
        active_progress.sources_discovered(len(sources))
        return search_sources(
            query,
            sources,
            active_backends,
            progress=active_progress,
        )
    finally:
        active_progress.close()


def plan_search_sources(
    query: SearchQuery,
    sources: list[SourceHandle],
    backends: BackendSelection,
    *,
    progress: SearchProgress | None = None,
) -> list[SourceHandle]:
    """Return the candidate sources to parse for a search query."""
    active_progress = noop_search_progress() if progress is None else progress
    if not query.terms:
        return sources

    planned_sources = list(sources)
    if backends.grep_tool is not None:
        planned_sources = prefilter_sources_by_root(
            query,
            planned_sources,
            backends.grep_tool,
            progress=active_progress,
        )
    ordered_sources = [
        source
        for source in planned_sources
        if source.search_root is not None or direct_source_matches(source, query, backends)
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
) -> list[SourceHandle]:
    """Prefilter file-backed sources by searching each root once."""
    active_progress = noop_search_progress() if progress is None else progress
    matched_paths_by_root: dict[pathlib.Path, set[pathlib.Path] | None] = {}
    filtered_sources: list[SourceHandle] = []
    for source in sources:
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
            )

        matched_paths = matched_paths_by_root[search_root]
        if matched_paths is None or source.path in matched_paths:
            filtered_sources.append(source)
    return filtered_sources


def grep_root_paths(
    search_root: pathlib.Path,
    query: SearchQuery,
    grep_program: str,
) -> set[pathlib.Path] | None:
    """Return file paths matched by a whole-root grep."""
    matched_sets: list[set[pathlib.Path]] = []
    for term in query.terms:
        command = build_grep_command(
            grep_program,
            term,
            search_root,
            regex=query.regex,
            case_sensitive=query.case_sensitive,
        )
        completed = run_readonly_command(command)
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
) -> bool:
    """Return whether a direct source should be parsed."""
    if source.source_kind == "sqlite":
        return True
    if backends.grep_tool is not None:
        grep_match = grep_file_matches(source.path, query, backends.grep_tool)
        if grep_match is not None:
            return grep_match
    if source.path.suffix in JSON_FILE_SUFFIXES and backends.json_tool is not None:
        extracted = flatten_json_strings_with_tool(source.path, backends.json_tool)
        if extracted is not None:
            return matches_text(extracted, query)
    return matches_text(read_text_file(source.path), query)


def collect_search_records(
    query: SearchQuery,
    sources: list[SourceHandle],
    *,
    progress: SearchProgress | None = None,
) -> list[SearchRecord]:
    """Parse candidate sources and collect matching records."""
    active_progress = noop_search_progress() if progress is None else progress
    deduped: dict[tuple[str, str, str, str, str], SearchRecord] = {}
    total = len(sources)
    for index, source in enumerate(sources, start=1):
        if query.limit is not None and len(deduped) >= query.limit:
            break
        active_progress.source_started(index, total, source)
        records_seen = 0
        matches_seen = 0
        matching_records: list[SearchRecord] = []
        for record in iter_source_records(source):
            records_seen += 1
            if matches_record(record, query):
                matches_seen += 1
                matching_records.append(record)
        active_progress.source_finished(index, total, source, records_seen, matches_seen)
        matching_records.sort(key=search_record_sort_key, reverse=True)
        for record in matching_records:
            dedupe_key = record_dedupe_key(record)
            if dedupe_key not in deduped:
                deduped[dedupe_key] = record
                active_progress.result_added(len(deduped))
            if query.limit is not None and len(deduped) >= query.limit:
                break
    results = list(deduped.values())
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
    """Build a read-only grep command for one term and target."""
    command = [grep_program, "-l", term, str(target)]
    if not regex:
        fixed_flag = "-F" if grep_program.endswith("rg") else "-Q"
        command.insert(2, fixed_flag)
    if not case_sensitive:
        command.insert(1, "-i")
    return command


def flatten_json_strings_with_tool(path: pathlib.Path, program: str) -> str | None:
    """Return flattened JSON strings using ``jq`` or ``jaq``."""
    command = [program, "-r", ".. | strings", str(path)]
    completed = run_readonly_command(command)
    if completed.returncode != 0:
        return None
    return completed.stdout


def grep_file_matches(
    path: pathlib.Path,
    query: SearchQuery,
    program: str,
) -> bool | None:
    """Use ``rg`` or ``ag`` as a read-only prefilter."""
    matchers = [
        run_readonly_command(
            build_grep_command(
                program,
                term,
                path,
                regex=query.regex,
                case_sensitive=query.case_sensitive,
            ),
        ).returncode
        == 0
        for term in query.terms
    ]
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


def maybe_build_pydantic() -> tuple[
    t.Callable[[SearchRecord], dict[str, object]],
    t.Callable[[FindRecord], dict[str, object]],
    EnvelopeFactory,
]:
    """Return Pydantic serializers or plain fallbacks."""
    try:
        return maybe_use_pydantic()
    except ImportError:
        return (
            lambda record: dict(serialize_search_record(record)),
            lambda record: dict(serialize_find_record(record)),
            lambda command, query_data, results: dict(build_envelope(command, query_data, results)),
        )


def serialize_search_record(record: SearchRecord) -> SearchRecordPayload:
    """Serialize a search record to a JSON-compatible mapping."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": record.kind,
        "agent": record.agent,
        "store": record.store,
        "adapter_id": record.adapter_id,
        "path": str(record.path),
        "text": record.text,
        "title": record.title,
        "role": record.role,
        "timestamp": record.timestamp,
        "model": record.model,
        "session_id": record.session_id,
        "conversation_id": record.conversation_id,
        "metadata": record.metadata,
    }


def serialize_find_record(record: FindRecord) -> FindRecordPayload:
    """Serialize a find record to a JSON-compatible mapping."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": record.kind,
        "agent": record.agent,
        "store": record.store,
        "adapter_id": record.adapter_id,
        "path": str(record.path),
        "path_kind": record.path_kind,
        "metadata": record.metadata,
    }


def serialize_source_handle(source: SourceHandle) -> SourceHandlePayload:
    """Serialize a source handle to a JSON-compatible mapping."""
    return {
        "schema_version": SCHEMA_VERSION,
        "agent": source.agent,
        "store": source.store,
        "adapter_id": source.adapter_id,
        "path": str(source.path),
        "path_kind": source.path_kind,
        "source_kind": source.source_kind,
        "search_root": None if source.search_root is None else str(source.search_root),
        "mtime_ns": source.mtime_ns,
    }


def build_envelope(
    command: str,
    query_data: dict[str, object],
    results: list[dict[str, object]],
) -> EnvelopePayload:
    """Build a JSON envelope."""
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "query": query_data,
        "results": results,
    }


def print_search_results(records: list[SearchRecord], args: SearchArgs) -> None:
    """Emit search results in the requested format."""
    serialize_search, _, serialize_envelope = maybe_build_pydantic()
    query_data: dict[str, object] = {
        "terms": list(args.terms),
        "agents": list(args.agents),
        "type": args.search_type,
        "any": args.any_term,
        "regex": args.regex,
        "case_sensitive": args.case_sensitive,
        "limit": args.limit,
    }
    if args.output_mode == "json":
        payload = serialize_envelope(
            "search",
            query_data,
            [serialize_search(record) for record in records],
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.output_mode == "ndjson":
        for record in records:
            print(json.dumps(serialize_search(record), ensure_ascii=False))
        return
    for index, record in enumerate(records, start=1):
        heading = f"[{index}] {record.agent} {record.kind} {record.store}"
        details = [record.timestamp, record.model, str(record.path)]
        print(heading)
        print(" | ".join(detail for detail in details if detail))
        if record.title:
            print(record.title)
        print()
        print(record.text)
        print()


def build_search_progress(args: SearchArgs) -> SearchProgress:
    """Build the progress reporter for a search invocation."""
    human_output = args.output_mode in {"text", "ui"}
    enabled = args.progress_mode == "always" or (args.progress_mode == "auto" and human_output)
    if not enabled:
        return noop_search_progress()
    return ConsoleSearchProgress(enabled=True)


def print_find_results(records: list[FindRecord], args: FindArgs) -> None:
    """Emit find results in the requested format."""
    _, serialize_find, serialize_envelope = maybe_build_pydantic()
    query_data: dict[str, object] = {
        "pattern": args.pattern,
        "agents": list(args.agents),
        "limit": args.limit,
    }
    if args.output_mode == "json":
        payload = serialize_envelope(
            "find",
            query_data,
            [serialize_find(record) for record in records],
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.output_mode == "ndjson":
        for record in records:
            print(json.dumps(serialize_find(record), ensure_ascii=False))
        return
    for record in records:
        print(f"{record.agent} {record.path_kind} {record.store}")
        print(str(record.path))
        print()


def run_ui(records: list[SearchRecord]) -> None:
    """Launch a small read-only Textual explorer."""
    try:
        textual_app = t.cast(
            "TextualAppModule",
            t.cast("object", importlib.import_module("textual.app")),
        )
        textual_containers = t.cast(
            "TextualContainersModule",
            t.cast("object", importlib.import_module("textual.containers")),
        )
        textual_widgets = t.cast(
            "TextualWidgetsModule",
            t.cast("object", importlib.import_module("textual.widgets")),
        )
    except ImportError as error:
        msg = "Textual is required for --ui. Run with `uv run py/agentgrep.py ... --ui`."
        raise RuntimeError(msg) from error

    app_type = textual_app.App
    horizontal = textual_containers.Horizontal
    vertical = textual_containers.Vertical
    data_table_type = textual_widgets.DataTable
    footer = textual_widgets.Footer
    header = textual_widgets.Header
    input_widget = textual_widgets.Input
    static_type = textual_widgets.Static

    class AgentGrepApp(app_type):  # type: ignore[valid-type, misc]
        """Read-only explorer for normalized search records."""

        CSS: t.ClassVar[str] = """
        Screen {
            layout: vertical;
        }
        #body {
            height: 1fr;
        }
        #detail {
            border: round $accent;
            padding: 1 2;
            overflow-y: auto;
        }
        DataTable {
            height: 1fr;
        }
        """
        BINDINGS: t.ClassVar[list[tuple[str, str, str]]] = [("q", "quit", "Quit")]
        all_records: list[SearchRecord]
        filtered_records: list[SearchRecord]

        def __init__(self, initial_records: list[SearchRecord]) -> None:
            super().__init__()
            self.all_records = initial_records
            self.filtered_records = initial_records

        def compose(self) -> cabc.Iterator[object]:
            yield header()
            yield input_widget(placeholder="Filter by keyword", id="filter")
            with horizontal(id="body"):
                yield data_table_type(id="results")
                with vertical():
                    yield static_type("Select a result to inspect full text.", id="detail")
            yield footer()

        def on_mount(self) -> None:
            app = t.cast("QueryAppLike", t.cast("object", self))
            table = t.cast("DataTableLike", app.query_one(data_table_type))
            table.cursor_type = "row"
            table.add_columns("Agent", "Kind", "Timestamp", "Title", "Path")
            self.refresh_table()

        def on_input_changed(self, event: object) -> None:
            value = str(getattr(event, "value", "")).strip().casefold()
            self.filtered_records = (
                self.all_records
                if not value
                else [
                    record
                    for record in self.all_records
                    if value in build_search_haystack(record).casefold()
                ]
            )
            self.refresh_table()

        def refresh_table(self) -> None:
            app = t.cast("QueryAppLike", t.cast("object", self))
            table = t.cast("DataTableLike", app.query_one(data_table_type))
            table.clear()
            for record in self.filtered_records:
                table.add_row(
                    record.agent,
                    record.kind,
                    record.timestamp or "",
                    record.title or "",
                    str(record.path),
                    key=str(id(record)),
                )
            if self.filtered_records:
                self.show_detail(self.filtered_records[0])
            else:
                detail = t.cast("StaticLike", app.query_one("#detail", static_type))
                detail.update("No results.")

        def on_data_table_row_highlighted(self, event: object) -> None:
            row_index = int(getattr(event, "cursor_row", -1))
            if 0 <= row_index < len(self.filtered_records):
                self.show_detail(self.filtered_records[row_index])

        def show_detail(self, record: SearchRecord) -> None:
            details = [
                f"Agent: {record.agent}",
                f"Kind: {record.kind}",
                f"Store: {record.store}",
                f"Adapter: {record.adapter_id}",
                f"Timestamp: {record.timestamp or 'unknown'}",
                f"Model: {record.model or 'unknown'}",
                f"Path: {record.path}",
                "",
                record.text,
            ]
            app = t.cast("QueryAppLike", t.cast("object", self))
            detail = t.cast("StaticLike", app.query_one("#detail", static_type))
            detail.update("\n".join(details))

    app = t.cast("RunnableAppLike", t.cast("object", AgentGrepApp(records)))
    app.run()


def run_search_command(args: SearchArgs) -> int:
    """Execute ``agentgrep search``."""
    if not args.terms and args.output_mode != "ui":
        msg = "search requires at least one term unless --ui is used"
        raise SystemExit(msg)
    query = make_search_query(args)
    progress = build_search_progress(args)
    records = run_search_query(pathlib.Path.home(), query, progress=progress)
    if args.output_mode == "ui":
        run_ui(records)
        return 0
    print_search_results(records, args)
    if records:
        return 0
    if args.output_mode == "text":
        print("No matches found.", file=sys.stderr)
    return 1


def run_find_command(args: FindArgs) -> int:
    """Execute ``agentgrep find``."""
    records = run_find_query(
        pathlib.Path.home(),
        args.agents,
        pattern=args.pattern,
        limit=args.limit,
    )
    print_find_results(records, args)
    if records:
        return 0
    if args.output_mode == "text":
        print("No matching sources found.", file=sys.stderr)
    return 1


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Run the CLI."""
    parsed = parse_args(argv)
    if parsed is None:
        return 0
    if isinstance(parsed, SearchArgs):
        return run_search_command(parsed)
    return run_find_command(parsed)


if __name__ == "__main__":
    raise SystemExit(main())
