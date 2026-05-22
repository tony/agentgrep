"""argparse subcommands and arg-parsing entry points for agentgrep.

This module owns the CLI grammar: the root parser, each subparser
(``search``, ``find``, ``ui``), the typed argument dataclasses returned
by :func:`parse_args`, and the helpers that resolve color mode and inject
default subcommands.

Symbols defined here are re-exported from :mod:`agentgrep` for backward
compatibility, so existing imports such as ``agentgrep.parse_args`` and
``agentgrep.SearchArgs`` continue to resolve.
"""

from __future__ import annotations

import argparse
import collections.abc as cabc
import contextlib
import dataclasses
import os
import sys
import typing as t

from agentgrep import (
    AGENT_CHOICES,
    CLI_DESCRIPTION,
    FIND_DESCRIPTION,
    FUZZY_DESCRIPTION,
    GREP_DESCRIPTION,
    SEARCH_DESCRIPTION,
    UI_DESCRIPTION,
    AgentName,
    ColorMode,
    OutputMode,
    ProgressMode,
    SearchType,
    create_themed_formatter,
)

CaseMode = t.Literal["smart", "ignore", "respect"]
PatternMode = t.Literal["regex", "fixed", "word"]
FindPatternMode = t.Literal["regex", "glob", "fixed", "exact"]
FindTypeFilter = t.Literal["prompts", "history", "sessions", "all"]
FuzzyAlgo = t.Literal["v1", "v2"]
FuzzyTiebreak = t.Literal["length", "begin", "end", "index", "chunk"]

__all__ = [
    "CaseMode",
    "FindArgs",
    "FindPatternMode",
    "FindTypeFilter",
    "FuzzyAlgo",
    "FuzzyArgs",
    "FuzzyTiebreak",
    "GrepArgs",
    "ParserBundle",
    "PatternMode",
    "SearchArgs",
    "UIArgs",
    "add_common_agent_options",
    "add_output_mode_options",
    "build_docs_parser",
    "configured_color_environment",
    "create_parser",
    "normalize_color_mode",
    "parse_agents",
    "parse_args",
    "parse_output_mode",
]


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
    """Typed arguments for ``agentgrep find``.

    fd-shaped: ``pattern_mode`` defaults to regex like fd does. ``-F``
    selects literal-substring (which was the previous default before the
    fd alignment landed); ``-g`` selects glob; ``--exact`` selects exact
    adapter_id matching. ``type_filter`` constrains by record kind;
    ``extensions`` restricts to paths with matching suffixes.
    """

    pattern: str | None
    agents: tuple[AgentName, ...]
    limit: int | None
    output_mode: OutputMode
    color_mode: ColorMode
    pattern_mode: FindPatternMode = "regex"
    type_filter: FindTypeFilter = "all"
    extensions: tuple[str, ...] = ()
    case_mode: CaseMode = "smart"
    list_details: bool = False
    print0: bool = False
    absolute_path: bool = False
    progress_mode: ProgressMode = "auto"


@dataclasses.dataclass(slots=True)
class UIArgs:
    """Typed arguments for ``agentgrep ui``."""

    initial_query: str
    color_mode: ColorMode


@dataclasses.dataclass(slots=True)
class FuzzyArgs:
    """Typed arguments for ``agentgrep fuzzy``.

    Mirrors fzf's ``--filter`` mode: stdin lines are scored against
    ``query`` and emitted in descending-score order. ``exact``
    short-circuits the fuzzy algorithm to a substring predicate;
    ``extended`` honors fzf's ``foo !bar`` / ``^foo`` / ``bar$``
    token syntax.
    """

    query: str
    agents: tuple[AgentName, ...]
    case_mode: CaseMode
    algo: FuzzyAlgo
    tiebreak: FuzzyTiebreak
    exact: bool
    extended: bool
    sort: bool
    delimiter: str | None
    nth: int | None
    with_nth: int | None
    print_query: bool
    read0: bool
    print0: bool
    output_mode: OutputMode
    color_mode: ColorMode


@dataclasses.dataclass(slots=True)
class GrepArgs:
    """Typed arguments for ``agentgrep grep``.

    Mirrors the rg/ag flag surface. ``case_mode`` and ``pattern_mode``
    are tri-state selectors rather than independent booleans so the
    resolution order (``-s`` > ``-i`` > ``-S`` / ``-F`` > ``-w`` > ``-E``)
    is enforced at parse time.
    """

    patterns: tuple[str, ...]
    agents: tuple[AgentName, ...]
    search_type: SearchType
    case_mode: CaseMode
    pattern_mode: PatternMode
    invert_match: bool
    count_only: bool
    files_with_matches: bool
    files_without_match: bool
    only_matching: bool
    no_dedupe: bool
    line_number: bool | None
    heading: bool | None
    max_count: int | None
    vimgrep: bool
    output_mode: OutputMode
    color_mode: ColorMode
    progress_mode: ProgressMode


@dataclasses.dataclass(slots=True)
class ParserBundle:
    """CLI parsers used for root and subcommand help."""

    parser: argparse.ArgumentParser
    search_parser: argparse.ArgumentParser
    find_parser: argparse.ArgumentParser
    grep_parser: argparse.ArgumentParser
    fuzzy_parser: argparse.ArgumentParser


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

    grep_parser = subparsers.add_parser(
        "grep",
        help="Content search with rg/ag-shaped flags and output",
        description=GREP_DESCRIPTION,
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    add_common_agent_options(grep_parser)
    _ = grep_parser.add_argument(
        "patterns",
        nargs="+",
        metavar="PATTERN",
        help="One or more patterns (regex by default; combined as AND)",
    )
    pattern_group = grep_parser.add_mutually_exclusive_group()
    _ = pattern_group.add_argument(
        "-F",
        "--fixed-strings",
        action="store_true",
        help="Treat patterns as literal strings, not regex",
    )
    _ = pattern_group.add_argument(
        "-E",
        "--extended-regexp",
        action="store_true",
        help="Treat patterns as regex (default)",
    )
    _ = pattern_group.add_argument(
        "-w",
        "--word-regexp",
        action="store_true",
        help="Match the pattern only as a whole word",
    )
    case_group = grep_parser.add_mutually_exclusive_group()
    _ = case_group.add_argument(
        "-i",
        "--ignore-case",
        action="store_true",
        help="Force case-insensitive matching",
    )
    _ = case_group.add_argument(
        "-s",
        "--case-sensitive",
        action="store_true",
        help="Force case-sensitive matching",
    )
    _ = case_group.add_argument(
        "-S",
        "--smart-case",
        action="store_true",
        help="Smart-case (default): case-sensitive when pattern has uppercase",
    )
    _ = grep_parser.add_argument(
        "-c",
        "--count",
        action="store_true",
        help="Print only the number of matches per (agent, store)",
    )
    _ = grep_parser.add_argument(
        "-l",
        "--files-with-matches",
        action="store_true",
        help="List source paths with at least one match",
    )
    _ = grep_parser.add_argument(
        "-L",
        "--files-without-match",
        action="store_true",
        help="List source paths with no matches",
    )
    _ = grep_parser.add_argument(
        "-o",
        "--only-matching",
        action="store_true",
        help="Print only the matched portion of each record",
    )
    _ = grep_parser.add_argument(
        "-v",
        "--invert-match",
        action="store_true",
        help="Print records that do NOT match",
    )
    _ = grep_parser.add_argument(
        "--no-dedupe",
        action="store_true",
        help="Disable per-session dedup (raw rg-style view; default dedupes)",
    )
    line_number_group = grep_parser.add_mutually_exclusive_group()
    _ = line_number_group.add_argument(
        "-n",
        "--line-number",
        dest="line_number_on",
        action="store_true",
        help="Force line numbers in output",
    )
    _ = line_number_group.add_argument(
        "-N",
        "--no-line-number",
        dest="line_number_off",
        action="store_true",
        help="Suppress line numbers",
    )
    heading_group = grep_parser.add_mutually_exclusive_group()
    _ = heading_group.add_argument(
        "--heading",
        dest="heading_on",
        action="store_true",
        help="Force file-grouped headings (default on TTY)",
    )
    _ = heading_group.add_argument(
        "--no-heading",
        dest="heading_off",
        action="store_true",
        help="Suppress file-grouped headings (default on pipe)",
    )
    _ = grep_parser.add_argument(
        "-m",
        "--max-count",
        type=int,
        metavar="N",
        help="Stop after N matches",
    )
    _ = grep_parser.add_argument(
        "--vimgrep",
        action="store_true",
        help="Emit one match per line as path:line:col:text",
    )
    _ = grep_parser.add_argument(
        "--type",
        choices=["prompts", "history", "all"],
        default="prompts",
        dest="search_type",
        help="Record type to search (default: prompts)",
    )
    _ = grep_parser.add_argument(
        "--progress",
        choices=["auto", "always", "never"],
        default="auto",
        help="Show search progress on stderr",
    )
    _ = grep_parser.add_argument(
        "--no-progress",
        dest="progress",
        action="store_const",
        const="never",
        help="Silence the stderr progress spinner (alias for --progress=never)",
    )
    add_output_mode_options(grep_parser, allow_ui=True)

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
    _ = search_parser.add_argument(
        "--no-progress",
        dest="progress",
        action="store_const",
        const="never",
        help="Silence the stderr progress spinner (alias for --progress=never)",
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
        help="Optional pattern matched against agent/store/adapter/path",
    )
    find_pattern_group = find_parser.add_mutually_exclusive_group()
    _ = find_pattern_group.add_argument(
        "-g",
        "--glob",
        dest="find_glob",
        action="store_true",
        help="Treat PATTERN as a shell glob (fnmatch)",
    )
    _ = find_pattern_group.add_argument(
        "-F",
        "--fixed-strings",
        dest="find_fixed",
        action="store_true",
        help="Treat PATTERN as a literal substring (legacy default)",
    )
    _ = find_pattern_group.add_argument(
        "--exact",
        dest="find_exact",
        action="store_true",
        help="Require PATTERN to equal the adapter_id exactly",
    )
    find_case_group = find_parser.add_mutually_exclusive_group()
    _ = find_case_group.add_argument(
        "-i",
        "--ignore-case",
        dest="find_ignore_case",
        action="store_true",
        help="Force case-insensitive matching (default smart-case)",
    )
    _ = find_case_group.add_argument(
        "-s",
        "--case-sensitive",
        dest="find_case_sensitive",
        action="store_true",
        help="Force case-sensitive matching",
    )
    _ = find_parser.add_argument(
        "-t",
        "--type",
        dest="find_type",
        choices=["prompts", "history", "sessions", "all"],
        default="all",
        help="Restrict to a record kind (default: all)",
    )
    _ = find_parser.add_argument(
        "-e",
        "--extension",
        dest="find_extensions",
        action="append",
        default=[],
        metavar="EXT",
        help="Filter by extension (repeatable, e.g. -e jsonl -e db)",
    )
    _ = find_parser.add_argument(
        "-l",
        "--list-details",
        action="store_true",
        help="Long format: agent, kind, store, adapter_id, path",
    )
    _ = find_parser.add_argument(
        "-0",
        "--print0",
        action="store_true",
        help="Separate output records with NUL instead of newline",
    )
    _ = find_parser.add_argument(
        "-a",
        "--absolute-path",
        action="store_true",
        help="Print absolute paths (already the default; flag is symbolic)",
    )
    _ = find_parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Limit the number of results",
    )
    _ = find_parser.add_argument(
        "--progress",
        choices=["auto", "always", "never"],
        default="auto",
        help="Show source-discovery progress on stderr",
    )
    _ = find_parser.add_argument(
        "--no-progress",
        dest="progress",
        action="store_const",
        const="never",
        help="Silence the stderr progress spinner (alias for --progress=never)",
    )
    add_output_mode_options(find_parser, allow_ui=True)

    ui_parser = subparsers.add_parser(
        "ui",
        help="Launch the interactive Textual explorer",
        description=UI_DESCRIPTION,
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    _ = ui_parser.add_argument(
        "initial_query",
        nargs="?",
        default="",
        help="Optional initial search text to populate the search bar",
    )
    fuzzy_parser = subparsers.add_parser(
        "fuzzy",
        help="fzf --filter-shaped fuzzy match over stdin lines",
        description=FUZZY_DESCRIPTION,
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    add_common_agent_options(fuzzy_parser)
    _ = fuzzy_parser.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Fuzzy query (also accepted via -f/--filter)",
    )
    _ = fuzzy_parser.add_argument(
        "-f",
        "--filter",
        dest="fuzzy_filter",
        default=None,
        metavar="QUERY",
        help="Explicit filter query (overrides the positional)",
    )
    fuzzy_case_group = fuzzy_parser.add_mutually_exclusive_group()
    _ = fuzzy_case_group.add_argument(
        "-i",
        "--ignore-case",
        dest="fuzzy_ignore_case",
        action="store_true",
        help="Force case-insensitive matching",
    )
    _ = fuzzy_case_group.add_argument(
        "--no-ignore-case",
        dest="fuzzy_respect_case",
        action="store_true",
        help="Force case-sensitive matching (fzf's ``+i``)",
    )
    _ = fuzzy_case_group.add_argument(
        "--smart-case",
        dest="fuzzy_smart_case",
        action="store_true",
        help="Smart-case (default): case-sensitive only when query has uppercase",
    )
    _ = fuzzy_parser.add_argument(
        "-e",
        "--exact",
        dest="fuzzy_exact",
        action="store_true",
        help="Use exact substring matching instead of fuzzy",
    )
    _ = fuzzy_parser.add_argument(
        "--no-extended",
        dest="fuzzy_no_extended",
        action="store_true",
        help="Disable extended-search tokens (``foo !bar`` etc.)",
    )
    _ = fuzzy_parser.add_argument(
        "--algo",
        dest="fuzzy_algo",
        choices=["v1", "v2"],
        default="v2",
        help="Fuzzy match algorithm (default: v2)",
    )
    _ = fuzzy_parser.add_argument(
        "--tiebreak",
        dest="fuzzy_tiebreak",
        choices=["length", "begin", "end", "index", "chunk"],
        default="length",
        help="Score tiebreak rule (default: length)",
    )
    _ = fuzzy_parser.add_argument(
        "--no-sort",
        dest="fuzzy_no_sort",
        action="store_true",
        help="Preserve input order instead of sorting by score (fzf's ``+s``)",
    )
    _ = fuzzy_parser.add_argument(
        "-d",
        "--delimiter",
        dest="fuzzy_delimiter",
        default=None,
        metavar="DELIM",
        help="Field delimiter (default: whitespace)",
    )
    _ = fuzzy_parser.add_argument(
        "-n",
        "--nth",
        dest="fuzzy_nth",
        type=int,
        default=None,
        metavar="N",
        help="Limit matching to the Nth field (1-indexed)",
    )
    _ = fuzzy_parser.add_argument(
        "--with-nth",
        dest="fuzzy_with_nth",
        type=int,
        default=None,
        metavar="N",
        help="Display only the Nth field",
    )
    _ = fuzzy_parser.add_argument(
        "--print-query",
        dest="fuzzy_print_query",
        action="store_true",
        help="Prepend the query as the first line of output",
    )
    _ = fuzzy_parser.add_argument(
        "--read0",
        dest="fuzzy_read0",
        action="store_true",
        help="Treat stdin as NUL-delimited",
    )
    _ = fuzzy_parser.add_argument(
        "--print0",
        dest="fuzzy_print0",
        action="store_true",
        help="Separate output records with NUL instead of newline",
    )
    add_output_mode_options(fuzzy_parser, allow_ui=True)

    return ParserBundle(
        parser=parser,
        search_parser=search_parser,
        find_parser=find_parser,
        grep_parser=grep_parser,
        fuzzy_parser=fuzzy_parser,
    )


def build_docs_parser() -> argparse.ArgumentParser:
    """Return the root parser with color disabled, for docs autogen.

    ``sphinx-autodoc-argparse`` expects ``:func:`` to point at a
    zero-arg callable returning :class:`argparse.ArgumentParser`.
    :func:`create_parser` requires ``color_mode`` and returns a
    :class:`ParserBundle`, so this thin adapter exists for the
    documentation toolchain.
    """
    return create_parser("never").parser


def parse_args(
    argv: cabc.Sequence[str] | None = None,
) -> SearchArgs | FindArgs | UIArgs | GrepArgs | FuzzyArgs | None:
    """Parse CLI arguments into typed dataclasses."""
    color_mode = normalize_color_mode(argv)
    with configured_color_environment(color_mode):
        bundle = create_parser(color_mode)
        namespace = bundle.parser.parse_args(argv)
    if t.cast("str | None", getattr(namespace, "command", None)) is None:
        with configured_color_environment(color_mode):
            bundle.parser.print_help()
        return None

    command = t.cast("str", namespace.command)
    if command == "ui":
        return UIArgs(
            initial_query=t.cast("str", namespace.initial_query),
            color_mode=color_mode,
        )

    agents = parse_agents(t.cast("list[str]", namespace.agent))
    output_mode = parse_output_mode(namespace)

    if command == "grep":
        return _build_grep_args(
            namespace,
            agents=agents,
            output_mode=output_mode,
            color_mode=color_mode,
            bundle=bundle,
        )

    if command == "fuzzy":
        return _build_fuzzy_args(
            namespace,
            agents=agents,
            output_mode=output_mode,
            color_mode=color_mode,
            bundle=bundle,
        )

    limit = t.cast("int | None", namespace.limit)
    if limit is not None and limit < 1:
        with configured_color_environment(color_mode):
            bundle.parser.error("--limit must be greater than 0")

    if command == "search":
        terms = tuple(t.cast("list[str]", namespace.terms))
        if not terms and output_mode != "ui":
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
    if t.cast("bool", namespace.find_glob):
        pattern_mode: FindPatternMode = "glob"
    elif t.cast("bool", namespace.find_fixed):
        pattern_mode = "fixed"
    elif t.cast("bool", namespace.find_exact):
        pattern_mode = "exact"
    else:
        pattern_mode = "regex"
    if t.cast("bool", namespace.find_ignore_case):
        find_case_mode: CaseMode = "ignore"
    elif t.cast("bool", namespace.find_case_sensitive):
        find_case_mode = "respect"
    else:
        find_case_mode = "smart"
    return FindArgs(
        pattern=pattern,
        agents=agents,
        limit=limit,
        output_mode=output_mode,
        color_mode=color_mode,
        pattern_mode=pattern_mode,
        type_filter=t.cast("FindTypeFilter", namespace.find_type),
        extensions=tuple(t.cast("list[str]", namespace.find_extensions)),
        case_mode=find_case_mode,
        list_details=t.cast("bool", namespace.list_details),
        print0=t.cast("bool", namespace.print0),
        absolute_path=t.cast("bool", namespace.absolute_path),
        progress_mode=t.cast("ProgressMode", namespace.progress),
    )


def _build_grep_args(
    namespace: argparse.Namespace,
    *,
    agents: tuple[AgentName, ...],
    output_mode: OutputMode,
    color_mode: ColorMode,
    bundle: ParserBundle,
) -> GrepArgs:
    """Build :class:`GrepArgs` from a parsed argparse namespace."""
    max_count = t.cast("int | None", namespace.max_count)
    if max_count is not None and max_count < 1:
        with configured_color_environment(color_mode):
            bundle.parser.error("--max-count must be greater than 0")

    if t.cast("bool", namespace.ignore_case):
        case_mode: CaseMode = "ignore"
    elif t.cast("bool", namespace.case_sensitive):
        case_mode = "respect"
    else:
        case_mode = "smart"

    if t.cast("bool", namespace.fixed_strings):
        pattern_mode: PatternMode = "fixed"
    elif t.cast("bool", namespace.word_regexp):
        pattern_mode = "word"
    else:
        pattern_mode = "regex"

    if t.cast("bool", namespace.line_number_on):
        line_number: bool | None = True
    elif t.cast("bool", namespace.line_number_off):
        line_number = False
    else:
        line_number = None

    if t.cast("bool", namespace.heading_on):
        heading: bool | None = True
    elif t.cast("bool", namespace.heading_off):
        heading = False
    else:
        heading = None

    return GrepArgs(
        patterns=tuple(t.cast("list[str]", namespace.patterns)),
        agents=agents,
        search_type=t.cast("SearchType", namespace.search_type),
        case_mode=case_mode,
        pattern_mode=pattern_mode,
        invert_match=t.cast("bool", namespace.invert_match),
        count_only=t.cast("bool", namespace.count),
        files_with_matches=t.cast("bool", namespace.files_with_matches),
        files_without_match=t.cast("bool", namespace.files_without_match),
        only_matching=t.cast("bool", namespace.only_matching),
        no_dedupe=t.cast("bool", namespace.no_dedupe),
        line_number=line_number,
        heading=heading,
        max_count=max_count,
        vimgrep=t.cast("bool", namespace.vimgrep),
        output_mode=output_mode,
        color_mode=color_mode,
        progress_mode=t.cast("ProgressMode", namespace.progress),
    )


def _build_fuzzy_args(
    namespace: argparse.Namespace,
    *,
    agents: tuple[AgentName, ...],
    output_mode: OutputMode,
    color_mode: ColorMode,
    bundle: ParserBundle,
) -> FuzzyArgs | None:
    """Build :class:`FuzzyArgs` from a parsed argparse namespace."""
    explicit_filter = t.cast("str | None", namespace.fuzzy_filter)
    positional_query = t.cast("str | None", namespace.query)
    query = explicit_filter if explicit_filter is not None else positional_query
    if query is None and sys.stdin.isatty():
        with configured_color_environment(color_mode):
            bundle.fuzzy_parser.print_help()
        raise SystemExit(2)
    if query is None:
        query = ""

    if t.cast("bool", namespace.fuzzy_ignore_case):
        case_mode: CaseMode = "ignore"
    elif t.cast("bool", namespace.fuzzy_respect_case):
        case_mode = "respect"
    else:
        case_mode = "smart"

    return FuzzyArgs(
        query=query,
        agents=agents,
        case_mode=case_mode,
        algo=t.cast("FuzzyAlgo", namespace.fuzzy_algo),
        tiebreak=t.cast("FuzzyTiebreak", namespace.fuzzy_tiebreak),
        exact=t.cast("bool", namespace.fuzzy_exact),
        extended=not t.cast("bool", namespace.fuzzy_no_extended),
        sort=not t.cast("bool", namespace.fuzzy_no_sort),
        delimiter=t.cast("str | None", namespace.fuzzy_delimiter),
        nth=t.cast("int | None", namespace.fuzzy_nth),
        with_nth=t.cast("int | None", namespace.fuzzy_with_nth),
        print_query=t.cast("bool", namespace.fuzzy_print_query),
        read0=t.cast("bool", namespace.fuzzy_read0),
        print0=t.cast("bool", namespace.fuzzy_print0),
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
