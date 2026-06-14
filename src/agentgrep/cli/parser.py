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

from agentgrep._text import (
    CLI_DESCRIPTION,
    FIND_DESCRIPTION,
    GREP_DESCRIPTION,
    SEARCH_DESCRIPTION,
    UI_DESCRIPTION,
)
from agentgrep.cli.help_theme import create_themed_formatter
from agentgrep.records import (
    AGENT_CHOICES,
    AgentName,
    ColorMode,
    GrepStyle,
    OutputMode,
    ProgressMode,
    SearchScope,
)

if t.TYPE_CHECKING:
    from agentgrep.query import CompiledQuery

CaseMode = t.Literal["smart", "ignore", "respect"]
PatternMode = t.Literal["regex", "fixed", "word"]
FindPatternMode = t.Literal["regex", "glob", "fixed", "exact"]
FindTypeFilter = t.Literal["prompts", "history", "sessions", "all"]

__all__ = [
    "CaseMode",
    "FindArgs",
    "FindPatternMode",
    "FindTypeFilter",
    "GrepArgs",
    "InsightsArgs",
    "InsightsCacheArgs",
    "InsightsDoctorArgs",
    "InsightsFormat",
    "InsightsLevelsArgs",
    "InsightsModelsArgs",
    "InsightsReportArgs",
    "InsightsSetupArgs",
    "InsightsSkillsArgs",
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
    limit: int | None
    vimgrep: bool
    column: bool
    output_mode: OutputMode
    color_mode: ColorMode
    progress_mode: ProgressMode
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
    threshold: int = 0
    no_group: bool = False
    no_rank: bool = False
    compiled: CompiledQuery | None = None
    raw_query: str = ""


InsightsFormat = t.Literal["text", "markdown", "html", "json", "ndjson"]
InsightsLevelName = t.Literal[
    "builtin", "html", "ml", "embeddings", "index", "llm", "best-installed"
]
InsightsModelsAction = t.Literal["available", "list", "install"]
InsightsCacheAction = t.Literal["dir", "size", "prune"]


@dataclasses.dataclass(slots=True)
class InsightsReportArgs:
    """Typed arguments for ``agentgrep insights report``."""

    requested_level: InsightsLevelName
    output_format: InsightsFormat
    scope: SearchScope
    agents: tuple[AgentName, ...]
    limit: int | None
    model: str | None
    llm_backend: str
    index_backend: t.Literal["tantivy", "lancedb"]
    allow_download: bool
    yes: bool
    include_text: bool
    color_mode: ColorMode
    progress_mode: ProgressMode = "auto"
    since: str | None = None
    until: str | None = None
    conversation_summaries: bool = False
    graph_vector_backend: str = "sqlite-vec"


@dataclasses.dataclass(slots=True)
class InsightsLevelsArgs:
    """Typed arguments for ``agentgrep insights levels``."""

    output_format: InsightsFormat
    color_mode: ColorMode


@dataclasses.dataclass(slots=True)
class InsightsDoctorArgs:
    """Typed arguments for ``agentgrep insights doctor``."""

    output_format: InsightsFormat
    color_mode: ColorMode


@dataclasses.dataclass(slots=True)
class InsightsSetupArgs:
    """Typed arguments for ``agentgrep insights setup``."""

    level: str
    color_mode: ColorMode


@dataclasses.dataclass(slots=True)
class InsightsModelsArgs:
    """Typed arguments for ``agentgrep insights models …``."""

    action: InsightsModelsAction
    kind: t.Literal["embeddings", "llm"]
    llm_backend: str | None
    model: str | None
    yes: bool
    dry_run: bool
    output_format: InsightsFormat
    color_mode: ColorMode


@dataclasses.dataclass(slots=True)
class InsightsCacheArgs:
    """Typed arguments for ``agentgrep insights cache …``."""

    action: InsightsCacheAction
    dry_run: bool
    output_format: InsightsFormat
    color_mode: ColorMode


@dataclasses.dataclass(slots=True)
class InsightsSkillsArgs:
    """Typed arguments for ``agentgrep insights skills``."""

    output_format: InsightsFormat
    scope: SearchScope
    agents: tuple[AgentName, ...]
    limit: int | None
    model: str | None
    llm_backend: str
    use_llm: bool
    write_dir: str | None
    allow_download: bool
    yes: bool
    color_mode: ColorMode
    progress_mode: ProgressMode = "auto"
    since: str | None = None
    until: str | None = None


InsightsArgs = (
    InsightsReportArgs
    | InsightsLevelsArgs
    | InsightsDoctorArgs
    | InsightsSetupArgs
    | InsightsModelsArgs
    | InsightsCacheArgs
    | InsightsSkillsArgs
)


@dataclasses.dataclass(slots=True)
class ParserBundle:
    """CLI parsers used for root and subcommand help."""

    parser: argparse.ArgumentParser
    find_parser: argparse.ArgumentParser
    grep_parser: argparse.ArgumentParser
    search_parser: argparse.ArgumentParser


class _GrepLimitAction(argparse.Action):
    """Store grep cap aliases in one canonical ``limit`` namespace field."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        """Record a grep result cap; error when two cap aliases disagree."""
        spelling = option_string or "--limit"
        spelling_dest = f"_{self.dest}_option_string"
        value = t.cast("int", values)
        current = t.cast("int | None", getattr(namespace, self.dest, None))
        if current is not None and current != value:
            previous = t.cast("str", getattr(namespace, spelling_dest, "--limit"))
            parser.error(f"{previous} and {spelling} disagree")
        setattr(namespace, self.dest, value)
        setattr(namespace, spelling_dest, spelling)


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
        "--limit",
        "-m",
        "--max-count",
        action=_GrepLimitAction,
        dest="limit",
        type=int,
        metavar="N",
        help="Stop after N matches (-m/--max-count aliases)",
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
        help="Print real absolute paths instead of privacy-collapsed display paths",
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
    add_output_mode_options(search_parser, allow_ui=True)

    _add_insights_parser(subparsers, formatter_class, color_mode)

    return ParserBundle(
        parser=parser,
        find_parser=find_parser,
        grep_parser=grep_parser,
        search_parser=search_parser,
    )


def _add_insights_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    formatter_class: type[argparse.HelpFormatter],
    color_mode: ColorMode,
) -> None:
    """Attach the ``insights`` command tree (report/levels/doctor/setup/models/cache)."""
    insights_parser = subparsers.add_parser(
        "insights",
        help="Local activity reports with optional model-backed enrichment",
        description=(
            "Build a deterministic report over local agent records, with an "
            "opt-in ladder of HTML, ML, embeddings, index, and local-LLM enrichers."
        ),
        formatter_class=formatter_class,
        color=color_mode != "never",
    )
    insights_sub = insights_parser.add_subparsers(dest="insights_command")
    use_color = color_mode != "never"
    levels = ["builtin", "html", "ml", "embeddings", "index", "graph", "llm", "best-installed"]
    formats = ["text", "markdown", "html", "json", "ndjson"]

    report_parser = insights_sub.add_parser(
        "report",
        help="Generate an insights report",
        formatter_class=formatter_class,
        color=use_color,
    )
    add_common_agent_options(report_parser)
    _ = report_parser.add_argument(
        "--level", choices=levels, default="builtin", help="Enrichment level (default: builtin)"
    )
    _ = report_parser.add_argument(
        "--format",
        dest="insights_format",
        choices=formats,
        default="text",
        help="Output format (default: text)",
    )
    _ = report_parser.add_argument(
        "--scope",
        choices=["prompts", "conversations", "all"],
        default="prompts",
        help="Record scope (default: prompts)",
    )
    _ = report_parser.add_argument(
        "--limit", type=int, default=500, metavar="N", help="Max records to analyze"
    )
    _ = report_parser.add_argument("--model", default=None, help="Model id for embeddings/LLM")
    _ = report_parser.add_argument(
        "--backend", dest="llm_backend", default="ollama", help="Local LLM backend"
    )
    _ = report_parser.add_argument(
        "--index-backend",
        choices=["tantivy", "lancedb"],
        default="tantivy",
        help="Persistent index backend (default: tantivy)",
    )
    _ = report_parser.add_argument(
        "--auto-download-models",
        dest="allow_download",
        action="store_true",
        help="Permit downloading a missing model during the report",
    )
    _ = report_parser.add_argument(
        "--yes", action="store_true", help="Assume yes for non-interactive downloads"
    )
    _ = report_parser.add_argument(
        "--include-text",
        action="store_true",
        help="Allow raw snippets in summaries (default: aggregate only)",
    )
    _ = report_parser.add_argument(
        "--conversation-summaries",
        dest="conversation_summaries",
        action="store_true",
        help="Graph level: vector each conversation by a local-LLM summary (opt-in, slow)",
    )
    _ = report_parser.add_argument(
        "--graph-vector-backend",
        dest="graph_vector_backend",
        choices=["sqlite-vec", "lancedb"],
        default="sqlite-vec",
        help="Graph level: vector kNN backend (lancedb = IVF-PQ ANN; default sqlite-vec)",
    )
    _ = report_parser.add_argument(
        "--since",
        default=None,
        metavar="WHEN",
        help="Only analyze records on/after WHEN (e.g. 30d, 2026-05-14)",
    )
    _ = report_parser.add_argument(
        "--until", default=None, metavar="WHEN", help="Only analyze records on/before WHEN"
    )
    _ = report_parser.add_argument(
        "--progress", choices=["auto", "always", "never"], default="auto", help="Progress on stderr"
    )
    _ = report_parser.add_argument(
        "--no-progress",
        dest="progress",
        action="store_const",
        const="never",
        help=argparse.SUPPRESS,
    )

    skills_parser = insights_sub.add_parser(
        "skills",
        help="Draft SKILL.md files from recurring requests",
        formatter_class=formatter_class,
        color=use_color,
    )
    add_common_agent_options(skills_parser)
    _ = skills_parser.add_argument(
        "--format",
        dest="insights_format",
        choices=["text", "markdown", "json"],
        default="text",
        help="Output format (default: text)",
    )
    _ = skills_parser.add_argument(
        "--limit", type=int, default=500, metavar="N", help="Max records to analyze"
    )
    _ = skills_parser.add_argument(
        "--llm",
        dest="use_llm",
        action="store_true",
        help="Name and describe each skill with a local LLM (falls back to deterministic)",
    )
    _ = skills_parser.add_argument(
        "--write",
        dest="write_dir",
        default=None,
        metavar="DIR",
        help="Write each SKILL.md under DIR/<name>/ (default: print to stdout)",
    )
    _ = skills_parser.add_argument("--model", default=None, help="Model id for embeddings/LLM")
    _ = skills_parser.add_argument(
        "--backend", dest="llm_backend", default="ollama", help="Local LLM backend for --llm"
    )
    _ = skills_parser.add_argument(
        "--auto-download-models",
        dest="allow_download",
        action="store_true",
        help="Permit downloading a missing model during naming",
    )
    _ = skills_parser.add_argument(
        "--yes", action="store_true", help="Assume yes for non-interactive downloads"
    )
    _ = skills_parser.add_argument(
        "--since",
        default=None,
        metavar="WHEN",
        help="Only analyze records on/after WHEN (e.g. 30d, 2026-05-14)",
    )
    _ = skills_parser.add_argument(
        "--until", default=None, metavar="WHEN", help="Only analyze records on/before WHEN"
    )
    _ = skills_parser.add_argument(
        "--progress", choices=["auto", "always", "never"], default="auto", help="Progress on stderr"
    )
    _ = skills_parser.add_argument(
        "--no-progress",
        dest="progress",
        action="store_const",
        const="never",
        help=argparse.SUPPRESS,
    )

    levels_parser = insights_sub.add_parser(
        "levels",
        help="List enrichment levels and availability",
        formatter_class=formatter_class,
        color=use_color,
    )
    _ = levels_parser.add_argument(
        "--format", dest="insights_format", choices=formats, default="text"
    )

    doctor_parser = insights_sub.add_parser(
        "doctor",
        help="Diagnose dependencies and cache state",
        formatter_class=formatter_class,
        color=use_color,
    )
    _ = doctor_parser.add_argument(
        "--format", dest="insights_format", choices=formats, default="text"
    )

    setup_parser = insights_sub.add_parser(
        "setup",
        help="Print the install command for a level",
        formatter_class=formatter_class,
        color=use_color,
    )
    _ = setup_parser.add_argument("level", choices=levels[:-1], help="Level to set up")

    models_parser = insights_sub.add_parser(
        "models",
        help="List or install curated models",
        formatter_class=formatter_class,
        color=use_color,
    )
    models_sub = models_parser.add_subparsers(dest="models_action")
    for action in ("available", "list"):
        action_parser = models_sub.add_parser(
            action,
            help=f"{action.capitalize()} models",
            formatter_class=formatter_class,
            color=use_color,
        )
        _ = action_parser.add_argument(
            "--level", dest="model_kind", choices=["embeddings", "llm"], default="embeddings"
        )
        _ = action_parser.add_argument("--backend", dest="llm_backend", default=None)
        _ = action_parser.add_argument(
            "--format", dest="insights_format", choices=formats, default="text"
        )
    install_parser = models_sub.add_parser(
        "install", help="Download a curated model", formatter_class=formatter_class, color=use_color
    )
    _ = install_parser.add_argument("model", help="Model id to install")
    _ = install_parser.add_argument(
        "--level", dest="model_kind", choices=["embeddings", "llm"], default="embeddings"
    )
    _ = install_parser.add_argument("--backend", dest="llm_backend", default=None)
    _ = install_parser.add_argument("--yes", action="store_true")
    _ = install_parser.add_argument("--dry-run", action="store_true")
    _ = install_parser.add_argument(
        "--format", dest="insights_format", choices=formats, default="text"
    )

    cache_parser = insights_sub.add_parser(
        "cache",
        help="Inspect or prune the insights cache",
        formatter_class=formatter_class,
        color=use_color,
    )
    cache_sub = cache_parser.add_subparsers(dest="cache_action")
    for action in ("dir", "size"):
        leaf = cache_sub.add_parser(action, formatter_class=formatter_class, color=use_color)
        _ = leaf.add_argument("--format", dest="insights_format", choices=formats, default="text")
    prune_parser = cache_sub.add_parser("prune", formatter_class=formatter_class, color=use_color)
    _ = prune_parser.add_argument("--dry-run", action="store_true")
    _ = prune_parser.add_argument(
        "--format", dest="insights_format", choices=formats, default="text"
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


# Boolean keywords that engage the query parser when typed standalone and
# uppercase. Lowercase ``or``/``and``/``not`` stay literal search terms — the
# tokenizer treats them as terms, so the gate must agree.
_BOOLEAN_KEYWORDS: frozenset[str] = frozenset({"AND", "OR", "NOT"})

# Queryable field names, mirrored from ``agentgrep.query.default_registry``.
# Hardcoded here on purpose: the cold-start gate runs on every invocation and
# must not import the query module to decide whether to engage the parser.
# ``test_cli_query_field_names_mirror_the_registry`` fails if this drifts.
_QUERY_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "agent",
        "store",
        "adapter_id",
        "adapter",
        "path",
        "mtime",
        "scope",
        "timestamp",
        "date",
        "model",
        "role",
        "text",
        "human",
    },
)

# A field predicate is a known field name, not preceded by an identifier char
# (so ``myagent:`` does not match) and followed by ``:``. Restricting to known
# fields keeps URLs like ``https://host`` and path values from spuriously
# engaging the parser.
_FIELD_PREDICATE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:" + "|".join(sorted(_QUERY_FIELD_NAMES, key=len, reverse=True)) + r"):",
)


def _query_syntax_present(positionals: cabc.Sequence[str]) -> bool:
    """Return whether positionals carry query-language syntax.

    Cheap, dependency-free heuristic so plain bare-term queries
    (``ruff uv tmux``) keep the legacy fast path and never import the
    query module. Engages the parser when a positional carries a known
    field predicate, a standalone uppercase boolean keyword, or a
    leading quote (an intended phrase).

    Parameters
    ----------
    positionals : collections.abc.Sequence[str]
        Raw positional arguments for the subcommand.

    Returns
    -------
    bool
        ``True`` when the parser should be engaged.
    """
    for token in positionals:
        if not token:
            continue
        if token[:1] in {'"', "'"}:
            return True
        if _FIELD_PREDICATE_RE.search(token):
            return True
        if any(word in _BOOLEAN_KEYWORDS for word in token.split()):
            return True
    return False


def _maybe_compile_query(
    positionals: cabc.Sequence[str],
    *,
    bundle: ParserBundle,
    color_mode: ColorMode,
    subparser: argparse.ArgumentParser,
    explicit_flags: dict[str, str] | None = None,
    find_mode: bool = False,
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

    ``find_mode`` rejects queries ``find`` cannot faithfully evaluate
    (record-level field predicates, boolean text composition), since
    ``find`` only honors the source predicate and a flat path pattern.

    Parse / compile errors route through ``subparser.error()`` so the
    user sees an argparse-shaped message instead of a Python
    traceback.
    """
    if not _query_syntax_present(positionals):
        return None, tuple(positionals), set()
    from agentgrep.query import (
        QueryCompileError,
        QueryParseError,
        compile_query,
        default_registry,
        fields_in_ast,
        find_unsupported_reason,
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
    if find_mode:
        reason = find_unsupported_reason(ast, registry)
        if reason is not None:
            with configured_color_environment(color_mode):
                subparser.error(reason)
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
    if compiled.is_pure_text:
        # A parsed query that collapses to bare terms (a phrase, or a
        # parenthesized AND of terms) needs no source/record predicate.
        # Return the extracted, unquoted terms so the engine's legacy
        # fast path — and its source-scan cache — stay in play.
        return None, compiled.text_terms, used_fields
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


def _build_insights_args(
    namespace: argparse.Namespace,
    *,
    color_mode: ColorMode,
    bundle: ParserBundle,
) -> InsightsArgs:
    """Build the typed dataclass for an ``insights`` subcommand."""
    insights_command = t.cast("str | None", getattr(namespace, "insights_command", None))
    if insights_command is None:
        with configured_color_environment(color_mode):
            bundle.parser.error(
                "insights requires a subcommand: report, skills, levels, doctor, "
                "setup, models, cache"
            )
    fmt = t.cast("InsightsFormat", getattr(namespace, "insights_format", "text"))

    if insights_command == "skills":
        return InsightsSkillsArgs(
            output_format=fmt,
            scope="conversations",
            agents=parse_agents(t.cast("list[str]", namespace.agent)),
            limit=t.cast("int | None", namespace.limit),
            model=t.cast("str | None", namespace.model),
            llm_backend=t.cast("str", namespace.llm_backend),
            use_llm=t.cast("bool", namespace.use_llm),
            write_dir=t.cast("str | None", namespace.write_dir),
            allow_download=t.cast("bool", namespace.allow_download),
            yes=t.cast("bool", namespace.yes),
            color_mode=color_mode,
            progress_mode=t.cast("ProgressMode", namespace.progress),
            since=t.cast("str | None", namespace.since),
            until=t.cast("str | None", namespace.until),
        )
    if insights_command == "report":
        return InsightsReportArgs(
            requested_level=t.cast("InsightsLevelName", namespace.level),
            output_format=fmt,
            scope=t.cast("SearchScope", namespace.scope),
            agents=parse_agents(t.cast("list[str]", namespace.agent)),
            limit=t.cast("int | None", namespace.limit),
            model=t.cast("str | None", namespace.model),
            llm_backend=t.cast("str", namespace.llm_backend),
            index_backend=t.cast("t.Literal['tantivy', 'lancedb']", namespace.index_backend),
            allow_download=t.cast("bool", namespace.allow_download),
            yes=t.cast("bool", namespace.yes),
            include_text=t.cast("bool", namespace.include_text),
            color_mode=color_mode,
            progress_mode=t.cast("ProgressMode", namespace.progress),
            since=t.cast("str | None", namespace.since),
            until=t.cast("str | None", namespace.until),
            conversation_summaries=t.cast("bool", namespace.conversation_summaries),
            graph_vector_backend=t.cast("str", namespace.graph_vector_backend),
        )
    if insights_command == "levels":
        return InsightsLevelsArgs(output_format=fmt, color_mode=color_mode)
    if insights_command == "doctor":
        return InsightsDoctorArgs(output_format=fmt, color_mode=color_mode)
    if insights_command == "setup":
        return InsightsSetupArgs(level=t.cast("str", namespace.level), color_mode=color_mode)
    if insights_command == "models":
        models_action = t.cast("str | None", getattr(namespace, "models_action", None))
        if models_action is None:
            with configured_color_environment(color_mode):
                bundle.parser.error("insights models requires an action: available, list, install")
        return InsightsModelsArgs(
            action=t.cast("InsightsModelsAction", models_action),
            kind=t.cast(
                "t.Literal['embeddings', 'llm']", getattr(namespace, "model_kind", "embeddings")
            ),
            llm_backend=t.cast("str | None", getattr(namespace, "llm_backend", None)),
            model=t.cast("str | None", getattr(namespace, "model", None)),
            yes=t.cast("bool", getattr(namespace, "yes", False)),
            dry_run=t.cast("bool", getattr(namespace, "dry_run", False)),
            output_format=fmt,
            color_mode=color_mode,
        )
    # cache
    cache_action = t.cast("str | None", getattr(namespace, "cache_action", None))
    if cache_action is None:
        with configured_color_environment(color_mode):
            bundle.parser.error("insights cache requires an action: dir, size, prune")
    return InsightsCacheArgs(
        action=t.cast("InsightsCacheAction", cache_action),
        dry_run=t.cast("bool", getattr(namespace, "dry_run", False)),
        output_format=fmt,
        color_mode=color_mode,
    )


def parse_args(
    argv: cabc.Sequence[str] | None = None,
) -> FindArgs | UIArgs | GrepArgs | SearchArgs | InsightsArgs | None:
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

    if command == "insights":
        return _build_insights_args(namespace, color_mode=color_mode, bundle=bundle)

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
        # Bare `agentgrep search` (no terms) would otherwise rank every
        # record; show the subcommand help+examples instead, the way
        # bare `agentgrep` shows root help. `--ui` keeps launching the
        # explorer with an empty seed query.
        if not t.cast("list[str]", namespace.terms) and not t.cast("bool", namespace.ui):
            with configured_color_environment(color_mode):
                bundle.search_parser.print_help()
            return None
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
        find_mode=True,
    )
    pattern: str | None = " ".join(find_residual) if find_residual else None
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


def _build_grep_args(
    namespace: argparse.Namespace,
    *,
    agents: tuple[AgentName, ...],
    output_mode: OutputMode,
    color_mode: ColorMode,
    bundle: ParserBundle,
) -> GrepArgs:
    """Build :class:`GrepArgs` from a parsed argparse namespace."""
    limit = t.cast("int | None", namespace.limit)
    if limit is not None and limit < 1:
        with configured_color_environment(color_mode):
            bundle.grep_parser.error("--limit must be greater than 0")

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
    patterns_list: list[str] = list(residual_patterns)
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
        limit=limit,
        vimgrep=t.cast("bool", namespace.vimgrep),
        column=t.cast("bool", namespace.column),
        output_mode=output_mode,
        color_mode=color_mode,
        progress_mode=t.cast("ProgressMode", namespace.progress),
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
    final_terms: tuple[str, ...] = residual_terms
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
