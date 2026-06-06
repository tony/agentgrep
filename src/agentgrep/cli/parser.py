"""argparse subcommands and arg-parsing entry points for agentgrep.

This module owns the CLI grammar: the root parser, each subparser
(``grep``, ``find``, ``ui``), the typed argument dataclasses
returned by :func:`parse_args`, and the helpers that resolve color mode
and inject default subcommands.

Symbols defined here are re-exported from :mod:`agentgrep` for backward
compatibility, so existing imports such as ``agentgrep.parse_args``
continue to resolve.
"""

from __future__ import annotations

import argparse
import collections.abc as cabc
import contextlib
import dataclasses
import os
import re
import sys
import typing as t

from agentgrep import (
    AGENT_CHOICES,
    CLI_DESCRIPTION,
    DB_DESCRIPTION,
    FIND_DESCRIPTION,
    GREP_DESCRIPTION,
    INSIGHTS_DESCRIPTION,
    SEARCH_DESCRIPTION,
    SUGGESTIONS_DESCRIPTION,
    UI_DESCRIPTION,
    AgentName,
    CacheMode,
    ColorMode,
    GrepStyle,
    OutputMode,
    ProgressMode,
    SearchScope,
    create_themed_formatter,
)

if t.TYPE_CHECKING:
    from agentgrep.query import CompiledQuery

CaseMode = t.Literal["smart", "ignore", "respect"]
PatternMode = t.Literal["regex", "fixed", "word"]
FindPatternMode = t.Literal["regex", "glob", "fixed", "exact"]
FindTypeFilter = t.Literal["prompts", "history", "sessions", "all"]
DbAction = t.Literal["sync", "status", "explain"]
DbFeatureMode = t.Literal["defer", "inline"]
InsightsAction = t.Literal["analyze", "list", "explain"]
InsightsKind = t.Literal["similarity", "omissions", "all"]
SuggestionsAction = t.Literal["list", "show", "render"]

DEFAULT_INSIGHTS_LIST_LIMIT = 50

__all__ = [
    "CaseMode",
    "DbArgs",
    "FindArgs",
    "FindPatternMode",
    "FindTypeFilter",
    "GrepArgs",
    "InsightsArgs",
    "ParserBundle",
    "PatternMode",
    "SearchArgs",
    "SuggestionsArgs",
    "UIArgs",
    "add_cache_options",
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
    full_path: bool = False
    progress_mode: ProgressMode = "auto"
    compiled: CompiledQuery | None = None
    raw_query: str = ""


@dataclasses.dataclass(slots=True)
class UIArgs:
    """Typed arguments for ``agentgrep ui``."""

    initial_query: str
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
    scope: SearchScope
    case_mode: CaseMode
    pattern_mode: PatternMode
    invert_match: bool
    count_only: bool
    files_with_matches: bool
    only_matching: bool
    no_dedupe: bool
    line_number: bool | None
    heading: bool | None
    max_count: int | None
    vimgrep: bool
    column: bool
    output_mode: OutputMode
    color_mode: ColorMode
    progress_mode: ProgressMode
    cache_mode: CacheMode = "auto"
    style: GrepStyle = "default"
    compiled: CompiledQuery | None = None
    raw_query: str = ""


@dataclasses.dataclass(slots=True)
class SearchArgs:
    """Typed arguments for ``agentgrep search``.

    Differentiates from ``grep`` by applying rapidfuzz relevance scoring,
    near-duplicate collapsing (WRatio > 90), and session grouping to
    produce a best-first result set.
    """

    terms: tuple[str, ...]
    agents: tuple[AgentName, ...]
    scope: SearchScope
    case_sensitive: bool
    limit: int | None
    output_mode: OutputMode
    color_mode: ColorMode
    progress_mode: ProgressMode
    cache_mode: CacheMode = "auto"
    threshold: int = 0
    no_group: bool = False
    no_rank: bool = False
    compiled: CompiledQuery | None = None
    raw_query: str = ""


@dataclasses.dataclass(slots=True)
class DbArgs:
    """Typed arguments for ``agentgrep db`` subcommands."""

    action: DbAction
    db_path: str | None
    agents: tuple[AgentName, ...]
    scope: SearchScope
    output_mode: OutputMode
    color_mode: ColorMode
    progress_mode: ProgressMode
    limit_sources: int | None = None
    features_mode: DbFeatureMode = "defer"
    force: bool = False


@dataclasses.dataclass(slots=True)
class InsightsArgs:
    """Typed arguments for ``agentgrep insights`` subcommands."""

    action: InsightsAction
    db_path: str | None
    kind: InsightsKind
    target: str | None
    output_mode: OutputMode
    color_mode: ColorMode = "auto"
    progress_mode: ProgressMode = "never"
    limit: int = DEFAULT_INSIGHTS_LIST_LIMIT


@dataclasses.dataclass(slots=True)
class SuggestionsArgs:
    """Typed arguments for ``agentgrep suggestions`` subcommands."""

    action: SuggestionsAction
    db_path: str | None
    suggestion_id: str | None
    target: str | None
    output_mode: OutputMode
    color_mode: ColorMode = "auto"


@dataclasses.dataclass(slots=True)
class ParserBundle:
    """CLI parsers used for root and subcommand help."""

    parser: argparse.ArgumentParser
    find_parser: argparse.ArgumentParser
    grep_parser: argparse.ArgumentParser
    search_parser: argparse.ArgumentParser
    db_parser: argparse.ArgumentParser
    insights_parser: argparse.ArgumentParser
    suggestions_parser: argparse.ArgumentParser


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
        "--column",
        action="store_true",
        help="Show column numbers in output (implies -n)",
    )
    _ = grep_parser.add_argument(
        "--scope",
        choices=["prompts", "conversations", "all"],
        dest="scope",
        help="Search scope: prompts, conversations, or all (default: prompts)",
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
    _ = grep_parser.add_argument(
        "--style",
        choices=["default", "pretty"],
        default="default",
        help="Output style: default (rg-faithful) or pretty (snippet-first, amber highlights)",
    )
    add_cache_options(grep_parser)
    add_output_mode_options(grep_parser, allow_ui=True)

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
        "--full-path",
        dest="full_path",
        action="store_true",
        help="With -g, match the glob against the absolute path "
        "instead of the file basename (fd's -p)",
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
    search_parser = subparsers.add_parser(
        "search",
        help="Smart search with relevance ranking and deduplication",
        description=SEARCH_DESCRIPTION,
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    add_common_agent_options(search_parser)
    _ = search_parser.add_argument(
        "terms",
        nargs="*",
        metavar="TERM",
        help="Search terms (combined as AND by default)",
    )
    _ = search_parser.add_argument(
        "--scope",
        choices=["prompts", "conversations", "all"],
        dest="scope",
        help="Search scope: prompts, conversations, or all (default: prompts)",
    )
    _ = search_parser.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Force case-sensitive matching",
    )
    _ = search_parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Limit the number of results after ranking",
    )
    _ = search_parser.add_argument(
        "--threshold",
        type=int,
        default=0,
        metavar="N",
        help="Minimum fuzzy score 0-100 (default: 0 = show all matches)",
    )
    _ = search_parser.add_argument(
        "--no-group",
        action="store_true",
        help="Flat results, no session grouping",
    )
    _ = search_parser.add_argument(
        "--no-rank",
        action="store_true",
        help="Discovery order, no relevance scoring",
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
    add_cache_options(search_parser)
    add_output_mode_options(search_parser, allow_ui=True)

    db_parser = subparsers.add_parser(
        "db",
        help="Sync and inspect the persistent DB index",
        description=DB_DESCRIPTION,
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    db_subparsers = db_parser.add_subparsers(
        dest="db_action",
    )
    db_sync_parser = db_subparsers.add_parser(
        "sync",
        help="Sync discovered sources into the DB index",
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    add_common_agent_options(db_sync_parser)
    _ = db_sync_parser.add_argument(
        "--scope",
        choices=["prompts", "conversations", "all"],
        default="all",
        help="Sources to sync into the DB index",
    )
    _ = db_sync_parser.add_argument("--db", dest="db_path", help="agentgrep db path")
    _ = db_sync_parser.add_argument(
        "--limit-sources",
        type=int,
        metavar="N",
        help="Limit the number of sources synced",
    )
    _ = db_sync_parser.add_argument(
        "--features",
        choices=["defer", "inline"],
        default="defer",
        help="Feature generation mode: defer expensive features or build inline",
    )
    _ = db_sync_parser.add_argument(
        "--force",
        action="store_true",
        help="Resync unchanged sources instead of using source_state freshness checks",
    )
    _ = db_sync_parser.add_argument(
        "--progress",
        choices=["auto", "always", "never"],
        default="auto",
        help="Show DB sync progress on stderr",
    )
    _ = db_sync_parser.add_argument(
        "--no-progress",
        dest="progress",
        action="store_const",
        const="never",
        help="Silence the stderr progress spinner (alias for --progress=never)",
    )
    add_output_mode_options(db_sync_parser, allow_ui=False)

    db_status_parser = db_subparsers.add_parser(
        "status",
        help="Show DB index status",
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    _ = db_status_parser.add_argument("--db", dest="db_path", help="agentgrep db path")
    add_output_mode_options(db_status_parser, allow_ui=False)

    db_explain_parser = db_subparsers.add_parser(
        "explain",
        help="Show DB planner/status details",
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    _ = db_explain_parser.add_argument("--db", dest="db_path", help="agentgrep db path")
    add_output_mode_options(db_explain_parser, allow_ui=False)

    insights_parser = subparsers.add_parser(
        "insights",
        help="Run and inspect deterministic agentic-data insights",
        description=INSIGHTS_DESCRIPTION,
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    insights_subparsers = insights_parser.add_subparsers(dest="insights_action")
    insights_analyze_parser = insights_subparsers.add_parser(
        "analyze",
        help="Analyze deterministic insights over the agentgrep db",
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    _ = insights_analyze_parser.add_argument("--db", dest="db_path", help="agentgrep db path")
    _ = insights_analyze_parser.add_argument(
        "--kind",
        choices=["similarity", "omissions", "all"],
        default="all",
        help="Insight family to analyze",
    )
    _ = insights_analyze_parser.add_argument("--target", help="Target file for omission insights")
    _ = insights_analyze_parser.add_argument(
        "--progress",
        choices=["auto", "always", "never"],
        default="auto",
        help="Show insight analysis progress on stderr",
    )
    _ = insights_analyze_parser.add_argument(
        "--no-progress",
        dest="progress",
        action="store_const",
        const="never",
        help="Silence the stderr progress spinner (alias for --progress=never)",
    )
    add_output_mode_options(insights_analyze_parser, allow_ui=False)

    insights_list_parser = insights_subparsers.add_parser(
        "list",
        help="List persisted insights",
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    _ = insights_list_parser.add_argument("--db", dest="db_path", help="agentgrep db path")
    _ = insights_list_parser.add_argument(
        "--kind",
        choices=["similarity", "omissions", "all"],
        default="all",
        help="Insight family to list",
    )
    _ = insights_list_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_INSIGHTS_LIST_LIMIT,
        help=f"Maximum rows to return per insight family (default: {DEFAULT_INSIGHTS_LIST_LIMIT})",
    )
    add_output_mode_options(insights_list_parser, allow_ui=False)

    insights_explain_parser = insights_subparsers.add_parser(
        "explain",
        help="Explain persisted insight counters",
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    _ = insights_explain_parser.add_argument("--db", dest="db_path", help="agentgrep db path")
    add_output_mode_options(insights_explain_parser, allow_ui=False)

    suggestions_parser = subparsers.add_parser(
        "suggestions",
        help="Render review-only instruction suggestions",
        description=SUGGESTIONS_DESCRIPTION,
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    suggestions_subparsers = suggestions_parser.add_subparsers(
        dest="suggestions_action",
    )
    suggestions_list_parser = suggestions_subparsers.add_parser(
        "list",
        help="List persisted suggestions",
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    _ = suggestions_list_parser.add_argument("--db", dest="db_path", help="agentgrep db path")
    _ = suggestions_list_parser.add_argument("--target", help="Create suggestions for a target")
    add_output_mode_options(suggestions_list_parser, allow_ui=False)
    for action in ("show", "render"):
        suggestion_parser = suggestions_subparsers.add_parser(
            action,
            help=f"{action.title()} one persisted suggestion",
            formatter_class=formatter_class,
            color=color_mode != "never",
        )
        _ = suggestion_parser.add_argument("suggestion_id")
        _ = suggestion_parser.add_argument("--db", dest="db_path", help="agentgrep db path")
        add_output_mode_options(suggestion_parser, allow_ui=False)

    return ParserBundle(
        parser=parser,
        find_parser=find_parser,
        grep_parser=grep_parser,
        search_parser=search_parser,
        db_parser=db_parser,
        insights_parser=insights_parser,
        suggestions_parser=suggestions_parser,
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


def _search_explicit_flags(namespace: argparse.Namespace) -> dict[str, str]:
    """Map query-field name → CLI flag name for `search` flag/field collisions."""
    flags: dict[str, str] = {}
    if t.cast("list[str]", namespace.agent):
        flags["agent"] = "--agent"
    if t.cast("str | None", namespace.scope) is not None:
        flags["scope"] = "--scope"
    return flags


def _grep_explicit_flags(namespace: argparse.Namespace) -> dict[str, str]:
    """Map query-field name → CLI flag name for `grep` flag/field collisions."""
    flags: dict[str, str] = {}
    if t.cast("list[str]", namespace.agent):
        flags["agent"] = "--agent"
    if t.cast("str | None", namespace.scope) is not None:
        flags["scope"] = "--scope"
    return flags


def _find_explicit_flags(namespace: argparse.Namespace) -> dict[str, str]:
    """Map query-field name → CLI flag name for `find` flag/field collisions."""
    flags: dict[str, str] = {}
    if t.cast("list[str]", namespace.agent):
        flags["agent"] = "--agent"
    if t.cast("str | None", namespace.find_type) is not None:
        flags["type"] = "--type"
    return flags


def _effective_search_scope(
    namespace: argparse.Namespace,
    *,
    query_fields: set[str],
) -> SearchScope:
    """Return the coarse search scope after query-language reconciliation."""
    explicit = t.cast("SearchScope | None", namespace.scope)
    if explicit is not None:
        return explicit
    if "scope" in query_fields:
        return "all"
    return "prompts"


def _maybe_compile_query(
    positionals: cabc.Sequence[str],
    *,
    bundle: ParserBundle,
    color_mode: ColorMode,
    subparser: argparse.ArgumentParser,
    explicit_flags: dict[str, str] | None = None,
) -> tuple[CompiledQuery | None, tuple[str, ...], set[str]]:
    """Detect Lucene-style query syntax in positionals and compile if present.

    Returns ``(compiled, residual_terms, fields)`` — ``compiled`` is ``None``
    when no positional contains ``:`` (legacy fast path); ``residual_terms``
    is the tuple to feed back as the legacy ``terms`` / ``patterns`` /
    ``pattern`` field so the engine's existing text-matching path
    still has the user's text query. ``fields`` is populated only for
    query-language input so callers can reconcile equivalent CLI flags.

    ``explicit_flags`` maps field name → flag name. When a field also
    has an explicitly-set flag (e.g. ``--agent`` set AND ``agent:``
    in the query), the parser errors. Pass ``None`` to skip the
    collision check (the bare-positional fast path).

    Parse / compile errors route through ``subparser.error()`` so the
    user sees an argparse-shaped message instead of a Python
    traceback.
    """
    if not any(":" in token for token in positionals):
        return None, tuple(positionals), set()
    from agentgrep.query import (
        QueryCompileError,
        QueryParseError,
        compile_query,
        default_registry,
        fields_in_ast,
        parse_query,
    )

    query_text = " ".join(positionals)
    registry = default_registry()
    try:
        ast = parse_query(query_text, registry)
    except QueryParseError as exc:
        with configured_color_environment(color_mode):
            subparser.error(f"invalid query: {exc}")
    used_fields = fields_in_ast(ast)
    if explicit_flags:
        for field_name, flag_name in explicit_flags.items():
            if field_name in used_fields:
                with configured_color_environment(color_mode):
                    subparser.error(
                        f"cannot combine {flag_name} flag with "
                        f"{field_name}: field predicate; pick one syntax",
                    )
    try:
        compiled = compile_query(ast, registry)
    except QueryCompileError as exc:
        with configured_color_environment(color_mode):
            subparser.error(f"invalid query: {exc}")
    _ = bundle  # kept available for future per-bundle checks
    return compiled, compiled.text_terms, used_fields


def _check_for_mangled_field_predicate(
    argv: cabc.Sequence[str],
    *,
    bundle: ParserBundle,
    color_mode: ColorMode,
) -> None:
    """Reject `-field:value` argv tokens before argparse mangles them.

    argparse collapses ``-agent:claude`` into combined short options
    (``-a`` from ``--absolute-path``, ``-g`` from ``--glob``,
    ``-e nt:claude`` from ``--extension``) because each leading
    character matches a defined short flag. The user's intended
    field-predicate negation is silently lost. This pre-scan catches
    the pattern before argparse runs and emits a clear error that
    points at the workarounds.

    Scans for any argv element matching ``-IDENT:`` where ``IDENT`` is
    a known field name in :func:`~agentgrep.query.default_registry`.
    Skips tokens that appear after a ``--`` separator (those are
    intentional positionals, not options).
    """
    registry = None
    after_double_dash = False
    for arg in argv:
        if after_double_dash:
            continue
        if arg == "--":
            after_double_dash = True
            continue
        if not arg.startswith("-") or arg.startswith("--"):
            continue
        if ":" not in arg:
            continue
        field_part, _, _ = arg[1:].partition(":")
        if not field_part:
            continue
        if registry is None:
            from agentgrep.query import default_registry

            registry = default_registry()
        if registry.get(field_part) is None:
            continue
        message = (
            f"argument {arg!r} looks like a field predicate but argparse "
            f"parses the leading '-' as combined short options. Use one of:\n"
            f"  --                  positional separator: agentgrep ... -- {arg}\n"
            f"  keyword negation:   agentgrep ... 'NOT {arg[1:]}'"
        )
        with configured_color_environment(color_mode):
            bundle.parser.error(message)


def parse_args(
    argv: cabc.Sequence[str] | None = None,
) -> DbArgs | FindArgs | GrepArgs | InsightsArgs | SearchArgs | SuggestionsArgs | UIArgs | None:
    """Parse CLI arguments into typed dataclasses."""
    color_mode = normalize_color_mode(argv)
    effective_argv = list(argv) if argv is not None else list(sys.argv[1:])
    with configured_color_environment(color_mode):
        bundle = create_parser(color_mode)
        _check_for_mangled_field_predicate(
            effective_argv,
            bundle=bundle,
            color_mode=color_mode,
        )
        namespace = bundle.parser.parse_args(effective_argv)
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

    if command == "db":
        if getattr(namespace, "db_action", None) is None:
            with configured_color_environment(color_mode):
                bundle.db_parser.print_help()
            return None
        return _build_db_args(namespace, color_mode=color_mode, bundle=bundle)

    if command == "insights":
        if getattr(namespace, "insights_action", None) is None:
            with configured_color_environment(color_mode):
                bundle.insights_parser.print_help()
            return None
        return _build_insights_args(namespace, color_mode=color_mode, bundle=bundle)

    if command == "suggestions":
        if getattr(namespace, "suggestions_action", None) is None:
            with configured_color_environment(color_mode):
                bundle.suggestions_parser.print_help()
            return None
        return _build_suggestions_args(namespace, color_mode=color_mode)

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

    if command == "search":
        return _build_search_args(
            namespace,
            agents=agents,
            output_mode=output_mode,
            color_mode=color_mode,
            bundle=bundle,
        )

    limit = t.cast("int | None", namespace.limit)
    if limit is not None and limit < 1:
        with configured_color_environment(color_mode):
            bundle.find_parser.error("--limit must be greater than 0")

    raw_pattern = t.cast("str | None", namespace.pattern)
    find_positionals = [raw_pattern] if raw_pattern is not None else []
    find_compiled, find_residual, _find_query_fields = _maybe_compile_query(
        find_positionals,
        bundle=bundle,
        color_mode=color_mode,
        subparser=bundle.find_parser,
        explicit_flags=_find_explicit_flags(namespace),
    )
    pattern: str | None = (
        (" ".join(find_residual) if find_residual else None)
        if find_compiled is not None
        else raw_pattern
    )
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
    if pattern is not None and pattern_mode == "regex":
        try:
            re.compile(pattern)
        except re.error as exc:
            with configured_color_environment(color_mode):
                bundle.find_parser.error(f"invalid regex: {exc}")
    return FindArgs(
        pattern=pattern,
        agents=agents,
        limit=limit,
        output_mode=output_mode,
        color_mode=color_mode,
        pattern_mode=pattern_mode,
        type_filter=t.cast("FindTypeFilter", namespace.find_type or "all"),
        extensions=tuple(t.cast("list[str]", namespace.find_extensions)),
        case_mode=find_case_mode,
        list_details=t.cast("bool", namespace.list_details),
        print0=t.cast("bool", namespace.print0),
        absolute_path=t.cast("bool", namespace.absolute_path),
        full_path=t.cast("bool", namespace.full_path),
        progress_mode=t.cast("ProgressMode", namespace.progress),
        compiled=find_compiled,
        raw_query=raw_pattern or "",
    )


def _build_db_args(
    namespace: argparse.Namespace,
    *,
    color_mode: ColorMode,
    bundle: ParserBundle,
) -> DbArgs:
    """Build :class:`DbArgs` from a parsed argparse namespace."""
    limit_sources = t.cast("int | None", getattr(namespace, "limit_sources", None))
    if limit_sources is not None and limit_sources < 1:
        with configured_color_environment(color_mode):
            bundle.db_parser.error("--limit-sources must be greater than 0")
    return DbArgs(
        action=t.cast("DbAction", namespace.db_action),
        db_path=t.cast("str | None", getattr(namespace, "db_path", None)),
        agents=parse_agents(t.cast("list[str]", getattr(namespace, "agent", []))),
        scope=t.cast("SearchScope", getattr(namespace, "scope", "all")),
        output_mode=parse_output_mode(namespace),
        color_mode=color_mode,
        progress_mode=t.cast("ProgressMode", getattr(namespace, "progress", "never")),
        limit_sources=limit_sources,
        features_mode=t.cast("DbFeatureMode", getattr(namespace, "features", "defer")),
        force=t.cast("bool", getattr(namespace, "force", False)),
    )


def _build_insights_args(
    namespace: argparse.Namespace,
    *,
    color_mode: ColorMode,
    bundle: ParserBundle,
) -> InsightsArgs:
    """Build :class:`InsightsArgs` from a parsed argparse namespace."""
    action = t.cast("InsightsAction", namespace.insights_action)
    kind = t.cast("InsightsKind", getattr(namespace, "kind", "all"))
    target = t.cast("str | None", getattr(namespace, "target", None))
    if action == "analyze" and kind == "omissions" and target is None:
        with configured_color_environment(color_mode):
            bundle.insights_parser.error("--target is required for omission insight analysis")
    limit = t.cast("int", getattr(namespace, "limit", DEFAULT_INSIGHTS_LIST_LIMIT))
    if action == "list" and limit < 1:
        with configured_color_environment(color_mode):
            bundle.insights_parser.error("--limit must be greater than 0")
    return InsightsArgs(
        action=action,
        db_path=t.cast("str | None", getattr(namespace, "db_path", None)),
        kind=kind,
        target=target,
        output_mode=parse_output_mode(namespace),
        color_mode=color_mode,
        progress_mode=t.cast("ProgressMode", getattr(namespace, "progress", "never")),
        limit=limit,
    )


def _build_suggestions_args(
    namespace: argparse.Namespace,
    *,
    color_mode: ColorMode,
) -> SuggestionsArgs:
    """Build :class:`SuggestionsArgs` from a parsed argparse namespace."""
    return SuggestionsArgs(
        action=t.cast("SuggestionsAction", namespace.suggestions_action),
        db_path=t.cast("str | None", getattr(namespace, "db_path", None)),
        suggestion_id=t.cast("str | None", getattr(namespace, "suggestion_id", None)),
        target=t.cast("str | None", getattr(namespace, "target", None)),
        output_mode=parse_output_mode(namespace),
        color_mode=color_mode,
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
            bundle.grep_parser.error("--max-count must be greater than 0")

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

    patterns_list_raw = t.cast("list[str]", namespace.patterns)
    grep_compiled, residual_patterns, grep_query_fields = _maybe_compile_query(
        patterns_list_raw,
        bundle=bundle,
        color_mode=color_mode,
        subparser=bundle.grep_parser,
        explicit_flags=_grep_explicit_flags(namespace),
    )
    patterns_list: list[str] = (
        list(residual_patterns) if grep_compiled is not None else patterns_list_raw
    )
    if any(not pattern for pattern in patterns_list):
        with configured_color_environment(color_mode):
            bundle.grep_parser.error("pattern cannot be empty")
    if grep_compiled is not None and not patterns_list:
        # Field-predicate-only grep would have no text to match line
        # output against.
        with configured_color_environment(color_mode):
            bundle.grep_parser.error(
                "grep query needs at least one text pattern; "
                "field predicates alone cannot drive line-level matching",
            )

    invert_match = t.cast("bool", namespace.invert_match)
    count_only = t.cast("bool", namespace.count)
    if invert_match and not count_only:
        with configured_color_environment(color_mode):
            bundle.grep_parser.error(
                "--invert-match for text output is not yet implemented "
                "(see https://github.com/tony/agentgrep/issues/8); use -c",
            )
    if pattern_mode != "fixed":
        case_sensitive = case_mode == "respect" or (
            case_mode == "smart" and any(any(ch.isupper() for ch in p) for p in patterns_list)
        )
        flags = 0 if case_sensitive else re.IGNORECASE
        for pattern in patterns_list:
            source = rf"\b{pattern}\b" if pattern_mode == "word" else pattern
            try:
                _ = re.compile(source, flags)
            except re.error as exc:
                with configured_color_environment(color_mode):
                    bundle.grep_parser.error(f"invalid regex {pattern!r}: {exc}")

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
        patterns=tuple(patterns_list),
        agents=agents,
        scope=_effective_search_scope(
            namespace,
            query_fields=grep_query_fields,
        ),
        case_mode=case_mode,
        pattern_mode=pattern_mode,
        invert_match=invert_match,
        count_only=count_only,
        files_with_matches=t.cast("bool", namespace.files_with_matches),
        only_matching=t.cast("bool", namespace.only_matching),
        compiled=grep_compiled,
        raw_query=" ".join(patterns_list_raw),
        no_dedupe=t.cast("bool", namespace.no_dedupe),
        line_number=line_number,
        heading=heading,
        max_count=max_count,
        vimgrep=t.cast("bool", namespace.vimgrep),
        column=t.cast("bool", namespace.column),
        output_mode=output_mode,
        color_mode=color_mode,
        progress_mode=t.cast("ProgressMode", namespace.progress),
        cache_mode=_resolved_cache_mode(namespace, bundle=bundle),
        style=t.cast("GrepStyle", namespace.style),
    )


def _build_search_args(
    namespace: argparse.Namespace,
    *,
    agents: tuple[AgentName, ...],
    output_mode: OutputMode,
    color_mode: ColorMode,
    bundle: ParserBundle,
) -> SearchArgs:
    """Build :class:`SearchArgs` from a parsed argparse namespace."""
    terms_list = t.cast("list[str]", namespace.terms)
    limit = t.cast("int | None", namespace.limit)
    if limit is not None and limit < 1:
        with configured_color_environment(color_mode):
            bundle.search_parser.error("--limit must be greater than 0")
    threshold = t.cast("int", namespace.threshold)
    if threshold < 0 or threshold > 100:
        with configured_color_environment(color_mode):
            bundle.search_parser.error("--threshold must be between 0 and 100")
    no_rank = t.cast("bool", namespace.no_rank)
    if no_rank and threshold > 0:
        with configured_color_environment(color_mode):
            bundle.search_parser.error(
                "--threshold has no effect with --no-rank (ranking is disabled)",
            )

    search_compiled, residual_terms, search_query_fields = _maybe_compile_query(
        terms_list,
        bundle=bundle,
        color_mode=color_mode,
        subparser=bundle.search_parser,
        explicit_flags=_search_explicit_flags(namespace),
    )
    final_terms: tuple[str, ...] = (
        residual_terms if search_compiled is not None else tuple(terms_list)
    )
    case_sensitive = t.cast("bool", namespace.case_sensitive)

    return SearchArgs(
        terms=final_terms,
        agents=agents,
        scope=_effective_search_scope(
            namespace,
            query_fields=search_query_fields,
        ),
        case_sensitive=case_sensitive,
        limit=limit,
        output_mode=output_mode,
        color_mode=color_mode,
        progress_mode=t.cast("ProgressMode", namespace.progress),
        cache_mode=_resolved_cache_mode(namespace, bundle=bundle),
        threshold=threshold,
        no_group=t.cast("bool", namespace.no_group),
        no_rank=t.cast("bool", namespace.no_rank),
        compiled=search_compiled,
        raw_query=" ".join(terms_list),
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


def _coerce_cache_mode(value: str) -> CacheMode:
    """Validate one cache-mode string.

    Examples
    --------
    >>> _coerce_cache_mode("off")
    'off'
    >>> _coerce_cache_mode("never")
    Traceback (most recent call last):
        ...
    ValueError: cache mode must be one of auto, require, off
    """
    if value in ("auto", "require", "off"):
        return t.cast("CacheMode", value)
    msg = "cache mode must be one of auto, require, off"
    raise ValueError(msg)


def resolve_cache_mode(
    explicit: CacheMode | None,
    env_value: str | None,
) -> CacheMode:
    """Resolve the effective cache mode: flag > AGENTGREP_CACHE > auto.

    Examples
    --------
    >>> resolve_cache_mode("require", "off")
    'require'
    >>> resolve_cache_mode(None, "off")
    'off'
    >>> resolve_cache_mode(None, None)
    'auto'
    """
    if explicit is not None:
        return explicit
    if env_value is not None:
        return _coerce_cache_mode(env_value)
    return "auto"


def _resolved_cache_mode(
    namespace: argparse.Namespace,
    *,
    bundle: ParserBundle,
) -> CacheMode:
    """Resolve cache mode from the parsed flag and AGENTGREP_CACHE."""
    explicit = t.cast("CacheMode | None", getattr(namespace, "cache_mode", None))
    try:
        return resolve_cache_mode(explicit, os.environ.get("AGENTGREP_CACHE"))
    except ValueError:
        bundle.parser.error("AGENTGREP_CACHE must be one of auto, require, off")


def add_cache_options(parser: argparse.ArgumentParser) -> None:
    """Attach cache-mode flags shared by search-shaped commands."""
    group = parser.add_mutually_exclusive_group()
    _ = group.add_argument(
        "--cache",
        choices=["auto", "require", "off"],
        default=None,
        dest="cache_mode",
        help="Use DB cache: auto (default), require, or off",
    )
    _ = group.add_argument(
        "--no-cache",
        action="store_const",
        const="off",
        dest="cache_mode",
        help="Bypass the DB cache for a fresh live scan",
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
