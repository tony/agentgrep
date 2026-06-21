"""Pure text-presentation helpers shared across agentgrep surfaces.

Privacy-safe path rendering, query-syntax highlighting (the single source of
truth consumed by the CLI help, the Textual TUI, and the docs lexer), help-text
assembly, match highlighting, truncation, and body-format sniffing. Depends only
on the standard library and Rich; it imports no engine, adapter, or frontend
module. See ADR 0010.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import textwrap
import typing as t

from rich.text import Text as _RichText

if t.TYPE_CHECKING:
    import collections.abc as cabc

    PrivatePathBase = pathlib.Path
else:
    PrivatePathBase = type(pathlib.Path())

__all__ = [
    "ANSI_CSI_RE",
    "CLI_DESCRIPTION",
    "DETAIL_BODY_MAX_LINES",
    "FIND_DESCRIPTION",
    "GREP_DESCRIPTION",
    "INLINE_CODE_RE",
    "QUERY_BOOLEAN_KEYWORDS",
    "QUERY_FIELD_TOKEN_RE",
    "QUERY_HIGHLIGHT_ROLES",
    "QUERY_TOKEN_RE",
    "SEARCH_DESCRIPTION",
    "SHELL_TOKEN_RE",
    "UI_DESCRIPTION",
    "ContentFormat",
    "PrivatePath",
    "build_description",
    "detect_content_format",
    "find_first_match_line",
    "format_compact_path",
    "format_display_path",
    "highlight_matches",
    "highlight_query_spans",
    "truncate_lines",
]


ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_RESET = "\x1b[0m"  # ANSI SGR reset

# Query-language highlighting inside help example lines. Detected by shape — a
# standalone boolean keyword or a ``field:`` predicate — so the help formatter
# never imports the query module (cold-start) and never couples to the
# registry. Help examples are author-controlled, so a shape-only field test is
# enough (a stray ``https:`` would only ever appear if someone wrote it).
QUERY_BOOLEAN_KEYWORDS: frozenset[str] = frozenset({"AND", "OR", "NOT", "TO"})
# Detection: a (possibly negated) field predicate, quotes already stripped.
QUERY_FIELD_TOKEN_RE = re.compile(r"^[+-]?[A-Za-z_][A-Za-z0-9_.-]*:")
# Shell-aware split that keeps a whole quoted argument together so a quoted
# query (``'agent:codex migration'``) is highlighted as one expression.
SHELL_TOKEN_RE = re.compile(r"""\s+|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|\S+""")
# Query-expression lexer: one pass over the unquoted query body. Order matters
# (longest / most specific alternatives first).
QUERY_TOKEN_RE = re.compile(
    r"""
      (?P<SPACE>\s+)
    | (?P<PHRASE>"(?:\\.|[^"\\])*")
    | (?P<BOOL>\b(?:AND|OR|NOT|TO)\b)
    | (?P<FIELD>[A-Za-z_][\w.-]*)(?=\s*:)
    | (?P<DATE>\b\d{4}-\d{2}-\d{2}(?:[Tt]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)?\b)
    | (?P<SIGN>(?<![\w])[-+])
    | (?P<OP>>=|<=|[!~^:><=])
    | (?P<PUNCT>[\[\]\(\)\{\}])
    | (?P<WILD>[*?])
    | (?P<WORD>[^\s\[\]\(\)\{\}:"~^><=!*?+]+)
    | (?P<MISC>.)
    """,
    re.VERBOSE,
)
# RST inline-code span (``code``) used in help intro prose.
INLINE_CODE_RE = re.compile(r"``([^`]+)``")

# Semantic roles emitted by :func:`highlight_query_spans`. The query-language
# grammar is lexed once here and shared by every highlighter — the CLI ANSI
# help (this module), the Textual TUI (``agentgrep.ui.highlighter``), and the
# Sphinx/MyST docs (``agentgrep.query.pygments_lexer``) — so the surfaces never
# drift. Each consumer maps these role strings to its own styling.
QUERY_HIGHLIGHT_ROLES: frozenset[str] = frozenset(
    {
        "whitespace",
        "field",
        "keyword",
        "negation",
        "operator",
        "punct",
        "wildcard",
        "date",
        "phrase",
        "value",
    },
)
# QUERY_TOKEN_RE group name -> semantic role. ``OP`` is special-cased (``:`` is
# punctuation, every other operator is a comparison/range operator).
_QUERY_TOKEN_GROUP_ROLES: dict[str, str] = {
    "SPACE": "whitespace",
    "PHRASE": "phrase",
    "BOOL": "keyword",
    "FIELD": "field",
    "DATE": "date",
    "SIGN": "negation",
    "PUNCT": "punct",
    "WILD": "wildcard",
    "WORD": "value",
    "MISC": "value",
}


def highlight_query_spans(query: str) -> list[tuple[int, str, str]]:
    """Lex a query string into contiguous ``(start, role, text)`` spans.

    The single source of truth for query-language syntax highlighting. The
    returned spans cover ``query`` end to end (including whitespace), in order,
    so a consumer can rebuild the string or stylize by offset. ``role`` is one
    of :data:`QUERY_HIGHLIGHT_ROLES`.

    Parameters
    ----------
    query : str
        The query expression (no surrounding shell quotes).

    Returns
    -------
    list[tuple[int, str, str]]
        ``(start_offset, role, text)`` spans in source order.

    Examples
    --------
    >>> highlight_query_spans("agent:codex")
    [(0, 'field', 'agent'), (5, 'punct', ':'), (6, 'value', 'codex')]
    >>> [role for _, role, _ in highlight_query_spans("ruff OR uv")]
    ['value', 'whitespace', 'keyword', 'whitespace', 'value']
    """
    spans: list[tuple[int, str, str]] = []
    for match in QUERY_TOKEN_RE.finditer(query):
        group = match.lastgroup or "MISC"
        text = match.group()
        if group == "OP":
            role = "punct" if text == ":" else "operator"
        else:
            role = _QUERY_TOKEN_GROUP_ROLES.get(group, "value")
        spans.append((match.start(), role, text))
    return spans


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
    Read-only search across Codex, Claude, Cursor, Gemini, Antigravity,
    Grok, Pi, and OpenCode local stores. Pick a subcommand from the list below:
    ``search`` for ranked results with dedup and session grouping,
    ``grep`` for rg-shaped content search, ``find`` for store
    enumeration, ``ui`` for the interactive Textual explorer.
    """,
    (
        (
            "search",
            (
                "agentgrep search streaming parser",
                "agentgrep search 'ruff OR uv'",
                "agentgrep search 'agent:codex migration'",
                "agentgrep search '\"exact phrase\"'",
            ),
        ),
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

    Terms accept a query language: bare terms are AND-combined substrings;
    compose with OR / NOT / ( ); quote "exact phrases"; filter by field
    (agent:, model:, role:, timestamp:, path:, scope:). field:* tests
    presence and field:glob* matches wildcards.
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
        (
            "query language",
            (
                "agentgrep search 'ruff OR uv'",
                "agentgrep search 'agent:codex migration'",
                "agentgrep search '\"exact phrase\"'",
                "agentgrep search 'timestamp:>2026-01-01 release'",
                "agentgrep search 'model:gpt* caching'",
                "agentgrep search 'deploy -agent:cursor-cli'",
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

    Patterns accept the same query language as ``search`` (field
    predicates, OR / NOT, "phrases"), but grep needs at least one text
    pattern to drive line-level matching.
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
        (
            "query language",
            (
                "agentgrep grep 'agent:codex deploy'",
                "agentgrep grep 'role:user TODO'",
                "agentgrep grep 'fixme OR todo'",
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
        output.append(_ANSI_RESET)
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
