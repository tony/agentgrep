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

from agentgrep._query_gate import (
    UnregisteredFieldToken,
    has_query_syntax,
    unregistered_field_predicates_in,
)
from agentgrep._text import (
    CLI_DESCRIPTION,
    FIND_DESCRIPTION,
    GREP_DESCRIPTION,
    SEARCH_DESCRIPTION,
    UI_DESCRIPTION,
)
from agentgrep.cli.help_theme import create_themed_formatter
from agentgrep.origin import normalize_origin_path_text, origin_filter_nodes
from agentgrep.project_context import ProjectContext, detect_project_context
from agentgrep.records import (
    AGENT_CHOICES,
    DEFAULT_TARGETED_CONVERSATION_LIMIT,
    AgentName,
    ColorMode,
    GrepStyle,
    OutputMode,
    ProgressMode,
    RecordOrigin,
    SearchEffort,
    SearchScope,
    SearchScopeProvenance,
)

if t.TYPE_CHECKING:
    from agentgrep.query import CompiledQuery, FieldEqNode, QueryNode

CaseMode = t.Literal["smart", "ignore", "respect"]
PatternMode = t.Literal["regex", "fixed", "word"]
FindPatternMode = t.Literal["regex", "glob", "fixed", "exact"]
FindTypeFilter = t.Literal["prompts", "history", "sessions", "all"]
_OMITTED_SEARCH_EFFORT = t.cast("SearchEffort", None)


def _normalize_args_effort(
    effort: object,
    scope: SearchScope,
) -> SearchEffort:
    """Normalize an omitted public constructor effort and reject invalid values.

    Parameters
    ----------
    effort : object
        Constructor value. ``None`` means a pre-effort caller omitted the new
        trailing field.
    scope : SearchScope
        Scope used to derive the compatibility default.

    Returns
    -------
    SearchEffort
        Validated prompt, targeted, or exhaustive effort.

    Raises
    ------
    ValueError
        If the runtime value is invalid or its scope is incompatible.
    """
    if effort is None:
        return "prompt" if scope == "prompts" else "exhaustive"
    if effort not in t.get_args(SearchEffort):
        msg = "effort must be 'prompt', 'targeted', or 'exhaustive'"
        raise ValueError(msg)
    if effort == "prompt" and scope != "prompts":
        msg = "prompt effort requires prompt scope"
        raise ValueError(msg)
    if effort == "targeted" and scope == "prompts":
        msg = "targeted effort requires conversation or all scope"
        raise ValueError(msg)
    return t.cast("SearchEffort", effort)


def _normalize_args_conversation_limit(
    value: int | None,
    *,
    effort: SearchEffort,
) -> int | None:
    """Normalize the targeted conversation-attempt bound."""
    if effort != "targeted":
        if value is not None:
            msg = "conversation_limit requires targeted effort"
            raise ValueError(msg)
        return None
    if value is None:
        return DEFAULT_TARGETED_CONVERSATION_LIMIT
    if value < 1:
        msg = "conversation_limit must be greater than 0"
        raise ValueError(msg)
    return value


__all__ = [
    "CaseMode",
    "FindArgs",
    "FindPatternMode",
    "FindTypeFilter",
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
class FindArgs:
    """Typed arguments for ``agentgrep find``.

    fd-shaped: ``pattern_mode`` defaults to regex like fd does. ``-F``
    selects literal-substring (which was the previous default before the
    fd alignment landed); ``-g`` selects glob; ``--exact`` selects exact
    adapter_id matching. ``type_filter`` constrains by record kind;
    ``extensions`` restricts to paths with matching suffixes.

    Attributes
    ----------
    pattern : str | None
        Text matched against agent, store, adapter_id, and path. ``None`` when the
        positional was omitted, which lists every discovered source.
    agents : tuple[AgentName, ...]
        Agents to search, from repeatable ``--agent``. Every agent in
        :data:`~agentgrep.records.AGENT_CHOICES` when the flag is unset or names ``all``.
    limit : int | None
        Result ceiling from ``--limit``. ``None`` returns every match.
    output_mode : OutputMode
        Rendering target chosen by ``--json`` / ``--ndjson`` / ``--ui``, else ``"text"``.
    color_mode : ColorMode
        ``--color`` selection: ``"auto"``, ``"always"``, or ``"never"``.
    pattern_mode : FindPatternMode
        How ``pattern`` is read: ``"regex"`` by default, ``"glob"`` under ``-g``,
        ``"fixed"`` (literal substring) under ``-F``, ``"exact"`` (equality against
        adapter_id) under ``--exact``.
    type_filter : FindTypeFilter
        Record kind from ``-t/--type``. ``"all"`` admits prompts, history, and sessions.
    extensions : tuple[str, ...]
        Suffixes from repeatable ``-e/--extension``. ``()`` accepts any extension.
    case_mode : CaseMode
        Case resolution: ``"smart"`` by default, ``"ignore"`` under ``-i``, ``"respect"``
        under ``-s``.
    list_details : bool
        Whether ``-l/--list-details`` selected the long format — agent, kind, store,
        adapter_id, path.
    print0 : bool
        Whether ``-0/--print0`` separates output records with NUL instead of newline.
    absolute_path : bool
        Whether ``-a/--absolute-path`` prints real absolute paths instead of
        privacy-collapsed display paths.
    full_path : bool
        Whether ``--full-path`` matches a ``-g`` glob against the absolute path rather
        than the file basename (fd's ``-p``).
    progress_mode : ProgressMode
        ``--progress`` selection for the stderr source-discovery spinner.
    compiled : CompiledQuery | None
        Compiled query-language predicate. ``None`` when the pattern carried no query
        syntax, or the query collapsed to bare text the legacy path already handles.
    raw_query : str
        Pattern text exactly as typed, before compilation, used to seed the explorer's
        search box. ``""`` when no pattern was given.
    diagnostics : tuple[UnregisteredFieldToken, ...]
        Non-fatal warnings for a field-predicate-shaped pattern whose field isn't
        registered (e.g. a typo'd field name), found on the legacy literal path.
        Empty when the pattern compiled cleanly or carried no such shape.
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
    diagnostics: tuple[UnregisteredFieldToken, ...] = ()


@dataclasses.dataclass(slots=True)
class UIArgs:
    """Typed arguments for ``agentgrep ui``.

    Attributes
    ----------
    initial_query : str
        Search text the explorer opens with. ``""`` starts on an empty search box.
    color_mode : ColorMode
        ``--color`` selection: ``"auto"``, ``"always"``, or ``"never"``.
    """

    initial_query: str
    color_mode: ColorMode


@dataclasses.dataclass(slots=True)
class GrepArgs:
    """Typed arguments for ``agentgrep grep``.

    Mirrors the rg/ag flag surface. ``case_mode`` and ``pattern_mode``
    are tri-state selectors rather than independent booleans so the
    resolution order (``-s`` > ``-i`` > ``-S`` / ``-F`` > ``-w`` > ``-E``)
    is enforced at parse time.

    Attributes
    ----------
    patterns : tuple[str, ...]
        Text patterns, combined as AND. Holds the residual text after query compilation,
        so a ``field:value`` positional does not reach line matching.
    agents : tuple[AgentName, ...]
        Agents to search, from repeatable ``--agent``. Every agent in
        :data:`~agentgrep.records.AGENT_CHOICES` when the flag is unset or names ``all``.
    scope : SearchScope
        Record kinds admitted after query reconciliation.
    effort : SearchEffort
        Read policy: ``"prompt"`` opens only prompt-history stores; ``"exhaustive"``
        also admits transcript stores.
    case_mode : CaseMode
        Case resolution: ``"smart"`` by default, ``"ignore"`` under ``-i``, ``"respect"``
        under ``-s``.
    pattern_mode : PatternMode
        How each pattern is read: ``"regex"`` by default, ``"fixed"`` (literal) under
        ``-F``, ``"word"`` (whole-word) under ``-w``.
    invert_match : bool
        Whether ``-v/--invert-match`` selects records that do not match.
    count_only : bool
        Whether ``-c/--count`` prints only the match count per (agent, store).
    files_with_matches : bool
        Whether ``-l/--files-with-matches`` lists source paths instead of match text.
    only_matching : bool
        Whether ``-o/--only-matching`` prints only the matched portion of each record.
    no_dedupe : bool
        Whether ``--no-dedupe`` disables per-session dedup for a raw rg-style view.
    line_number : bool | None
        Line-number prefix forced on by ``-n`` or off by ``-N``. ``None`` leaves the
        choice to the renderer's TTY-aware default.
    heading : bool | None
        File-grouped headings forced on by ``--heading`` or off by ``--no-heading``.
        ``None`` leaves the choice to the renderer's TTY-aware default.
    limit : int | None
        Match ceiling from ``--limit`` / ``-m`` / ``--max-count``. ``None`` returns every
        match.
    vimgrep : bool
        Whether ``--vimgrep`` emits one match per line as ``path:line:col:text``.
    column : bool
        Whether ``--column`` adds column numbers, which also forces line numbers on.
    output_mode : OutputMode
        Rendering target chosen by ``--json`` / ``--ndjson`` / ``--ui``, else ``"text"``.
    color_mode : ColorMode
        ``--color`` selection: ``"auto"``, ``"always"``, or ``"never"``.
    progress_mode : ProgressMode
        ``--progress`` selection for the stderr search spinner.
    style : GrepStyle
        ``--style`` selection: ``"default"`` renders rg-faithful output, ``"pretty"``
        renders snippet-first with amber highlights.
    compiled : CompiledQuery | None
        Compiled query-language predicate. ``None`` when the positionals carried no query
        syntax, or the query collapsed to bare text the legacy path already handles.
    raw_query : str
        Positionals joined by spaces exactly as typed, before compilation, used to seed
        the explorer's search box under ``--ui``.
    base_scope : SearchScope
        Scope the explorer returns to for an interactive query with no ``scope:``
        predicate, taken from ``--scope`` before query widening.
    base_effort : SearchEffort
        Read policy the explorer returns to after replacing an inline scope predicate.
        Preserves explicit ``--exhaustive`` or broad ``--scope`` authorization.
    scope_provenance : SearchScopeProvenance
        Whether the effective scope was inferred or explicitly selected.
    base_scope_provenance : SearchScopeProvenance
        Provenance restored with ``base_scope`` after an inline scope
        predicate is replaced.
    conversation_limit : int | None
        Distinct conversation-attempt cap for targeted effort.
    diagnostics : tuple[UnregisteredFieldToken, ...]
        Non-fatal warnings for field-predicate-shaped patterns whose field isn't
        registered (e.g. a typo'd field name), found on the legacy literal path.
        Empty when every pattern compiled cleanly or carried no such shape.
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
    base_scope: SearchScope = "prompts"
    effort: SearchEffort = _OMITTED_SEARCH_EFFORT
    base_effort: SearchEffort = _OMITTED_SEARCH_EFFORT
    scope_provenance: SearchScopeProvenance = "inferred"
    base_scope_provenance: SearchScopeProvenance = "inferred"
    conversation_limit: int | None = None
    diagnostics: tuple[UnregisteredFieldToken, ...] = ()

    def __post_init__(self) -> None:
        """Normalize and validate public constructor effort values."""
        self.effort = _normalize_args_effort(self.effort, self.scope)
        self.base_effort = _normalize_args_effort(self.base_effort, self.base_scope)
        self.conversation_limit = _normalize_args_conversation_limit(
            self.conversation_limit,
            effort=self.effort,
        )


@dataclasses.dataclass(slots=True)
class SearchArgs:
    """Typed arguments for ``agentgrep search``.

    Differentiates from ``grep`` by applying rapidfuzz relevance scoring
    and session grouping to produce a best-first result set.

    Attributes
    ----------
    terms : tuple[str, ...]
        Search terms, combined as AND. Holds the residual text after query compilation.
        Empty when an origin flag alone drives the search.
    agents : tuple[AgentName, ...]
        Agents to search, from repeatable ``--agent``. Every agent in
        :data:`~agentgrep.records.AGENT_CHOICES` when the flag is unset or names ``all``.
    scope : SearchScope
        Record kinds admitted after query reconciliation.
    effort : SearchEffort
        Read policy: ``"prompt"`` opens only prompt-history stores; ``"exhaustive"``
        also admits transcript stores.
    case_sensitive : bool
        Whether ``--case-sensitive`` forces case-sensitive matching.
    limit : int | None
        Result ceiling applied after ranking, from ``--limit``. ``None`` returns every
        match.
    output_mode : OutputMode
        Rendering target chosen by ``--json`` / ``--ndjson`` / ``--ui``, else ``"text"``.
    color_mode : ColorMode
        ``--color`` selection: ``"auto"``, ``"always"``, or ``"never"``.
    progress_mode : ProgressMode
        ``--progress`` selection for the stderr search spinner.
    threshold : int
        Minimum fuzzy score from ``--threshold``, between 0 and 100. ``0`` keeps every
        match the filters admit.
    no_group : bool
        Whether ``--no-group`` emits flat results instead of session-grouped ones.
    no_rank : bool
        Whether ``--no-rank`` returns globally newest-first records instead of relevance scoring.
    compiled : CompiledQuery | None
        Compiled query-language predicate. ``None`` when the terms carried no query
        syntax, or the query collapsed to bare text the legacy path already handles.
    raw_query : str
        Query text used to seed the explorer's search box, with the origin flags rendered
        back as ``cwd:`` / ``repo:`` / ``branch:`` predicates ahead of the typed terms.
    origin_boost : RecordOrigin | None
        Current project whose records rank higher under ``--here``, without filtering
        anything out. ``None`` applies no boost.
    origin_filter : RecordOrigin | None
        Project filter from ``--cwd`` / ``--repo`` / ``--branch`` / ``--only-here``, held
        apart from ``compiled`` so text-only searches keep the fast path. ``None`` filters
        by no origin.
    base_scope : SearchScope
        Scope the explorer returns to for an interactive query with no ``scope:``
        predicate, taken from ``--scope`` before query widening.
    base_effort : SearchEffort
        Read policy the explorer returns to after replacing an inline scope predicate.
        Preserves explicit ``--exhaustive`` or broad ``--scope`` authorization.
    scope_provenance : SearchScopeProvenance
        Whether the effective scope was inferred or explicitly selected.
    base_scope_provenance : SearchScopeProvenance
        Provenance restored with ``base_scope`` after an inline scope
        predicate is replaced.
    conversation_limit : int | None
        Distinct conversation-attempt cap for targeted effort.
    diagnostics : tuple[UnregisteredFieldToken, ...]
        Non-fatal warnings for field-predicate-shaped terms whose field isn't
        registered (e.g. a typo'd field name), found on the legacy literal path.
        Empty when every term compiled cleanly or carried no such shape.
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
    origin_boost: RecordOrigin | None = None
    origin_filter: RecordOrigin | None = None
    base_scope: SearchScope = "prompts"
    effort: SearchEffort = _OMITTED_SEARCH_EFFORT
    base_effort: SearchEffort = _OMITTED_SEARCH_EFFORT
    scope_provenance: SearchScopeProvenance = "inferred"
    base_scope_provenance: SearchScopeProvenance = "inferred"
    conversation_limit: int | None = None
    diagnostics: tuple[UnregisteredFieldToken, ...] = ()

    def __post_init__(self) -> None:
        """Normalize and validate public constructor effort values."""
        self.effort = _normalize_args_effort(self.effort, self.scope)
        self.base_effort = _normalize_args_effort(self.base_effort, self.base_scope)
        self.conversation_limit = _normalize_args_conversation_limit(
            self.conversation_limit,
            effort=self.effort,
        )


@dataclasses.dataclass(slots=True)
class ParserBundle:
    """CLI parsers used for root and subcommand help.

    The subcommand parsers are carried alongside the root parser so argument validation
    can route an error through the parser whose usage line the user needs to see.

    Attributes
    ----------
    parser : argparse.ArgumentParser
        Root ``agentgrep`` parser, which owns ``--color`` and the subparsers.
    find_parser : argparse.ArgumentParser
        Subparser for ``agentgrep find``.
    grep_parser : argparse.ArgumentParser
        Subparser for ``agentgrep grep``.
    search_parser : argparse.ArgumentParser
        Subparser for ``agentgrep search``.
    """

    parser: argparse.ArgumentParser
    find_parser: argparse.ArgumentParser
    grep_parser: argparse.ArgumentParser
    search_parser: argparse.ArgumentParser


class _VersionAction(argparse.Action):
    """Print the release version plus build provenance, then exit.

    Argparse's own ``version`` action wants the string at parser-construction
    time, which would make every ``agentgrep`` invocation — ``--help``
    included — pay for reading the version and probing git. This action resolves
    both inside ``__call__``, so only ``--version`` pays.
    """

    def __init__(
        self,
        option_strings: cabc.Sequence[str],
        dest: str = argparse.SUPPRESS,
        default: str = argparse.SUPPRESS,
        help: str | None = None,  # noqa: A002  (argparse's own parameter name)
    ) -> None:
        super().__init__(
            option_strings=list(option_strings),
            dest=dest,
            default=default,
            nargs=0,
            help=help,
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        """Write ``prog version`` to stdout and exit successfully."""
        del namespace, values, option_string
        from agentgrep import _version

        line = _version.format_version_line(_version.build_provenance())
        sys.stdout.write(f"{parser.prog} {line}\n")
        parser.exit()


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
                return value
        if argument.startswith("--color="):
            value = argument.partition("=")[2]
            if value in {"auto", "always", "never"}:
                return value
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
    _ = parser.add_argument(
        "--version",
        action=_VersionAction,
        help="show the released version (plus the git ref in a checkout) and exit",
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
        help="Result scope: prompts, conversations, or all (default: prompts)",
    )
    grep_effort = grep_parser.add_mutually_exclusive_group()
    _ = grep_effort.add_argument(
        "--deep",
        action="store_true",
        help="Search prompts plus selected conversations (approximate)",
    )
    _ = grep_effort.add_argument(
        "--exhaustive",
        action="store_true",
        help="Search every readable conversation backend",
    )
    _ = grep_parser.add_argument(
        "--conversation-limit",
        type=int,
        metavar="N",
        help="Attempt at most N distinct conversations with --deep (default: 25)",
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
        help="Result scope: prompts, conversations, or all (default: prompts)",
    )
    search_effort = search_parser.add_mutually_exclusive_group()
    _ = search_effort.add_argument(
        "--deep",
        action="store_true",
        help="Search prompts plus selected conversations (approximate)",
    )
    _ = search_effort.add_argument(
        "--exhaustive",
        action="store_true",
        help="Search every readable conversation backend",
    )
    _ = search_parser.add_argument(
        "--conversation-limit",
        type=int,
        metavar="N",
        help="Attempt at most N distinct conversations with --deep (default: 25)",
    )
    _ = search_parser.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Force case-sensitive matching",
    )
    _ = search_parser.add_argument(
        "--cwd",
        metavar="PATH",
        help="Only return records whose recorded cwd matches PATH",
    )
    _ = search_parser.add_argument(
        "--repo",
        metavar="PATH",
        help="Only return records whose recorded repository root matches PATH",
    )
    _ = search_parser.add_argument(
        "--branch",
        metavar="NAME",
        help="Only return records whose recorded git branch matches NAME",
    )
    here_group = search_parser.add_mutually_exclusive_group()
    _ = here_group.add_argument(
        "--here",
        action="store_true",
        help="Boost records from the current project without filtering",
    )
    _ = here_group.add_argument(
        "--only-here",
        action="store_true",
        help="Only return records from the current project",
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
        help="Globally newest-first, no relevance scoring",
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

    return ParserBundle(
        parser=parser,
        find_parser=find_parser,
        grep_parser=grep_parser,
        search_parser=search_parser,
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


def _explicit_depth_flag(namespace: argparse.Namespace) -> str | None:
    """Return the CLI flag name that already selected an effort, if any."""
    if t.cast("bool", namespace.deep):
        return "--deep"
    if t.cast("bool", namespace.exhaustive):
        return "--exhaustive"
    return None


def _search_explicit_flags(namespace: argparse.Namespace) -> dict[str, str]:
    """Map query-field name → CLI flag name for `search` flag/field collisions."""
    flags: dict[str, str] = {}
    if t.cast("list[str]", namespace.agent):
        flags["agent"] = "--agent"
    if t.cast("str | None", namespace.scope) is not None:
        flags["scope"] = "--scope"
    depth_flag = _explicit_depth_flag(namespace)
    if depth_flag is not None:
        flags["depth"] = depth_flag
    if t.cast("str", namespace.cwd or "").strip():
        flags["cwd"] = "--cwd"
    if t.cast("str", namespace.repo or "").strip():
        flags["repo"] = "--repo"
    if t.cast("str", namespace.branch or "").strip():
        flags["branch"] = "--branch"
    return flags


def _search_has_origin_filter(namespace: argparse.Namespace) -> bool:
    """Return whether ``search`` has a flag that can run without text terms."""
    return bool(
        t.cast("str", namespace.cwd or "").strip()
        or t.cast("str", namespace.repo or "").strip()
        or t.cast("str", namespace.branch or "").strip()
        or t.cast("bool", namespace.only_here),
    )


def _build_search_origin_nodes(
    namespace: argparse.Namespace,
) -> tuple[tuple[FieldEqNode, ...], RecordOrigin | None, RecordOrigin | None]:
    """Build display predicates, same-project boost, and hard origin filter."""
    cwd = normalize_origin_path_text(t.cast("str | None", namespace.cwd))
    repo = normalize_origin_path_text(t.cast("str | None", namespace.repo))
    raw_branch = t.cast("str | None", namespace.branch)
    branch = raw_branch if raw_branch and raw_branch.strip() else None
    origin_boost: RecordOrigin | None = None
    if t.cast("bool", namespace.here) or t.cast("bool", namespace.only_here):
        context = detect_project_context()
        if t.cast("bool", namespace.here):
            origin_boost = _search_here_origin_boost(context)
        if t.cast("bool", namespace.only_here):
            cwd = cwd or str(context.worktree or context.repo or context.cwd)
    origin_filter = RecordOrigin(cwd=cwd, repo=repo, branch=branch)
    if origin_filter.is_empty():
        origin_filter = None
    return origin_filter_nodes(cwd=cwd, repo=repo, branch=branch), origin_boost, origin_filter


def _search_here_origin_boost(context: ProjectContext) -> RecordOrigin:
    project_root = context.worktree or context.repo
    if project_root is not None:
        return RecordOrigin(repo=str(project_root))
    return RecordOrigin(cwd=str(context.cwd))


def _query_value_display(value: str) -> str:
    """Quote a generated predicate value for the UI search-box seed."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _grep_explicit_flags(namespace: argparse.Namespace) -> dict[str, str]:
    """Map query-field name → CLI flag name for `grep` flag/field collisions."""
    flags: dict[str, str] = {}
    if t.cast("list[str]", namespace.agent):
        flags["agent"] = "--agent"
    if t.cast("str | None", namespace.scope) is not None:
        flags["scope"] = "--scope"
    depth_flag = _explicit_depth_flag(namespace)
    if depth_flag is not None:
        flags["depth"] = depth_flag
    return flags


def _find_explicit_flags(namespace: argparse.Namespace) -> dict[str, str]:
    """Map query-field name → CLI flag name for `find` flag/field collisions."""
    flags: dict[str, str] = {}
    if t.cast("list[str]", namespace.agent):
        flags["agent"] = "--agent"
    if t.cast("str | None", namespace.find_type) is not None:
        flags["type"] = "--type"
    return flags


def _base_search_scope(namespace: argparse.Namespace) -> SearchScope:
    """Return the interactive scope before query predicates widen discovery."""
    explicit = t.cast("SearchScope | None", namespace.scope)
    if explicit is None and t.cast("bool", namespace.deep):
        return "all"
    return "prompts" if explicit is None else explicit


def _base_scope_provenance(
    namespace: argparse.Namespace,
) -> SearchScopeProvenance:
    """Return provenance for the stable scope before inline query widening."""
    return "explicit" if t.cast("SearchScope | None", namespace.scope) is not None else "inferred"


def _base_search_effort(namespace: argparse.Namespace) -> SearchEffort:
    """Return the stable launch read policy before inline scope predicates."""
    if t.cast("bool", namespace.deep):
        return "targeted"
    if t.cast("bool", namespace.exhaustive) or _base_search_scope(namespace) != "prompts":
        return "exhaustive"
    return "prompt"


def _resolve_scope_and_effort(
    namespace: argparse.Namespace,
    user_ast: QueryNode | None,
    *,
    color_mode: ColorMode,
    subparser: argparse.ArgumentParser,
) -> tuple[SearchScope, SearchEffort, SearchScopeProvenance]:
    """Resolve the effective scope/effort from CLI flags plus an inline directive.

    Delegates to :func:`agentgrep.query.resolve_request_modifiers` — the same
    resolver :func:`agentgrep.query.build_query_from_input` uses for the TUI
    search box — so an inline ``scope:``/``depth:``/``effort:`` predicate
    widens the CLI's flag-derived baseline (:func:`_base_search_scope`,
    :func:`_base_search_effort`) exactly the way it widens a search-box edit,
    instead of the CLI keeping its own copy of the ladder.

    Skips importing :mod:`agentgrep.query` entirely when ``user_ast`` is
    ``None`` (the common bare-term path — no query syntax was present), so a
    plain ``agentgrep search foo`` never pays for the query package's import
    cost.
    """
    base_scope = _base_search_scope(namespace)
    base_effort = _base_search_effort(namespace)
    explicit_scope_flag = t.cast("SearchScope | None", namespace.scope) is not None
    if user_ast is None:
        return base_scope, base_effort, ("explicit" if explicit_scope_flag else "inferred")
    from agentgrep.query import (
        QueryCompileError,
        default_registry,
        fields_in_ast,
        resolve_request_modifiers,
    )

    registry = default_registry()
    try:
        scope, effort = resolve_request_modifiers(
            user_ast,
            registry,
            base_scope=base_scope,
            base_effort=base_effort,
            base_scope_explicit=explicit_scope_flag,
        )
    except QueryCompileError as exc:
        with configured_color_environment(color_mode):
            subparser.error(f"invalid query: {exc}")
    provenance: SearchScopeProvenance = (
        "explicit" if explicit_scope_flag or "scope" in fields_in_ast(user_ast) else "inferred"
    )
    return scope, effort, provenance


def _targeted_conversation_limit(
    namespace: argparse.Namespace,
    *,
    scope: SearchScope,
    effort: SearchEffort,
    color_mode: ColorMode,
    subparser: argparse.ArgumentParser,
) -> int | None:
    """Validate scope/effort compatibility and return the targeted work bound.

    Enforces the same two-way contract :func:`_normalize_args_effort` applies
    to a directly-constructed :class:`SearchArgs`/:class:`GrepArgs` — but
    here, before construction, so a combination reachable only through an
    inline ``depth:``/``effort:`` directive (not just ``--deep``/
    ``--exhaustive``/``--scope``) gets this function's clean
    ``subparser.error`` message instead of that constructor's defensive
    ``ValueError`` backstop.
    """
    value = t.cast("int | None", namespace.conversation_limit)
    if value is not None and value < 1:
        with configured_color_environment(color_mode):
            subparser.error("--conversation-limit must be greater than 0")
    if value is not None and effort != "targeted":
        with configured_color_environment(color_mode):
            subparser.error("--conversation-limit requires targeted effort")
    if effort == "targeted" and scope == "prompts":
        with configured_color_environment(color_mode):
            subparser.error("targeted effort requires conversation or all scope")
    if effort == "prompt" and scope != "prompts":
        with configured_color_environment(color_mode):
            subparser.error("prompt effort requires prompt scope")
    if effort != "targeted":
        return None
    return DEFAULT_TARGETED_CONVERSATION_LIMIT if value is None else value


def _query_syntax_present(positionals: cabc.Sequence[str]) -> bool:
    """Return whether positionals carry query-language syntax.

    Cheap, dependency-free heuristic (:func:`agentgrep._query_gate.has_query_syntax`,
    shared with :func:`agentgrep.query.compile._has_query_syntax` so the two
    can't drift the way they did before agentgrep#153) so plain bare-term
    queries (``ruff uv tmux``) keep the legacy fast path and never import
    the query module. Engages the parser when a positional carries a
    *registered* field predicate, a standalone uppercase boolean keyword, or
    a leading quote (an intended phrase). An unregistered field-shaped
    predicate does not engage the parser here — see
    :func:`agentgrep._query_gate.unregistered_field_predicates` for how that
    case is surfaced instead, without turning a plausible literal search
    into a hard parse error.

    Parameters
    ----------
    positionals : collections.abc.Sequence[str]
        Raw positional arguments for the subcommand.

    Returns
    -------
    bool
        ``True`` when the parser should be engaged.
    """
    return any(has_query_syntax(token) for token in positionals)


def _maybe_compile_query(
    positionals: cabc.Sequence[str],
    *,
    bundle: ParserBundle,
    color_mode: ColorMode,
    subparser: argparse.ArgumentParser,
    explicit_flags: dict[str, str] | None = None,
    find_mode: bool = False,
    case_sensitive: bool = False,
    extra_nodes: tuple[FieldEqNode, ...] = (),
) -> tuple[
    CompiledQuery | None,
    tuple[str, ...],
    QueryNode | None,
    tuple[UnregisteredFieldToken, ...],
]:
    """Detect Lucene-style query syntax in positionals and compile if present.

    Returns ``(compiled, residual_terms, user_ast, diagnostics)`` —
    ``compiled`` is ``None`` when no positional contains ``:`` (legacy fast
    path); ``residual_terms`` is the tuple to feed back as the legacy
    ``terms`` / ``patterns`` / ``pattern`` field so the engine's existing
    text-matching path still has the user's text query. ``user_ast`` is the
    user's own parsed query (``None`` when the positionals carried no query
    syntax) for a caller to resolve scope/effort from via
    :func:`_resolve_scope_and_effort` — this function no longer resolves them
    itself, so ``find`` (which has neither concept) pays nothing extra for
    that reconciliation.
    ``diagnostics`` carries non-fatal warnings for field-predicate-shaped
    positionals whose field isn't registered (e.g. ``kind:prompt`` before
    ``kind`` was a known field) — populated only when the positionals
    themselves carried no query syntax, since an unknown field on the
    parsed path already hard-errors via ``subparser.error()`` below.

    ``explicit_flags`` maps field name → flag name. When a field also
    has an explicitly-set flag (e.g. ``--agent`` set AND ``agent:``
    in the query, or ``--deep`` set AND ``depth:`` in the query), the
    parser errors. Pass ``None`` to skip the collision check (the
    bare-positional fast path).

    ``find_mode`` rejects queries ``find`` cannot faithfully evaluate
    (record-level and request-level field predicates, boolean text
    composition), since ``find`` only honors the source predicate and a
    flat path pattern.

    ``extra_nodes`` carries synthetic predicates (generated origin
    filters) that are ANDed with the user terms at the AST level, so
    the terms keep their bare-path semantics. Field collision checks
    cover only the user's own query.

    Parse / compile errors route through ``subparser.error()`` so the
    user sees an argparse-shaped message instead of a Python
    traceback.
    """
    query_syntax = _query_syntax_present(positionals)
    diagnostics = () if query_syntax else unregistered_field_predicates_in(positionals)
    if not query_syntax and not extra_nodes:
        return None, tuple(positionals), None, diagnostics
    from agentgrep.query import (
        QueryCompileError,
        QueryParseError,
        compile_query,
        compose_query_ast,
        default_registry,
        fields_in_ast,
        find_unsupported_reason,
    )

    registry = default_registry()
    try:
        ast, user_ast = compose_query_ast(positionals, extra_nodes, registry)
    except QueryParseError as exc:
        with configured_color_environment(color_mode):
            subparser.error(f"invalid query: {exc}")
    used_fields = fields_in_ast(user_ast) if user_ast is not None else set()
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
        compiled = compile_query(ast, registry, case_sensitive=case_sensitive)
    except QueryCompileError as exc:
        with configured_color_environment(color_mode):
            subparser.error(f"invalid query: {exc}")
    _ = bundle  # kept available for future per-bundle checks
    if compiled.is_pure_text:
        # A parsed query that collapses to bare terms (a phrase, or a
        # parenthesized AND of terms) needs no source/record predicate.
        # Return the extracted, unquoted terms so the engine's legacy
        # fast path — and its source-scan cache — stay in play.
        return None, compiled.text_terms, user_ast, diagnostics
    return compiled, compiled.text_terms, user_ast, diagnostics


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
) -> FindArgs | UIArgs | GrepArgs | SearchArgs | None:
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
        if (
            not t.cast("list[str]", namespace.terms)
            and not t.cast("bool", namespace.ui)
            and not _search_has_origin_filter(namespace)
        ):
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
    find_compiled, find_residual, _find_user_ast, find_diagnostics = _maybe_compile_query(
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
        diagnostics=find_diagnostics,
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
    # Mirror build_grep_query's rg-style case resolution so the compiled
    # record predicate agrees with grep's own line matching.
    grep_case_sensitive = case_mode == "respect" or (
        case_mode == "smart"
        and any(any(ch.isupper() for ch in pattern) for pattern in patterns_list_raw)
    )
    grep_compiled, residual_patterns, grep_user_ast, grep_diagnostics = _maybe_compile_query(
        patterns_list_raw,
        bundle=bundle,
        color_mode=color_mode,
        subparser=bundle.grep_parser,
        explicit_flags=_grep_explicit_flags(namespace),
        case_sensitive=grep_case_sensitive,
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
    files_with_matches = t.cast("bool", namespace.files_with_matches)
    only_matching = t.cast("bool", namespace.only_matching)
    if output_mode in {"json", "ndjson"}:
        terminal_reducers: list[str] = []
        if count_only:
            terminal_reducers.append("--count")
        if files_with_matches:
            terminal_reducers.append("--files-with-matches")
        if invert_match:
            terminal_reducers.append("--invert-match")
        if terminal_reducers:
            reducers = ", ".join(terminal_reducers)
            with configured_color_environment(color_mode):
                bundle.grep_parser.error(
                    f"--{output_mode} cannot be combined with terminal reducers: {reducers}",
                )
    if invert_match:
        with configured_color_environment(color_mode):
            bundle.grep_parser.error(
                "--invert-match is not implemented yet "
                "(see https://github.com/tony/agentgrep/issues/8)",
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

    scope, effort, scope_provenance = _resolve_scope_and_effort(
        namespace,
        grep_user_ast,
        color_mode=color_mode,
        subparser=bundle.grep_parser,
    )
    conversation_limit = _targeted_conversation_limit(
        namespace,
        scope=scope,
        effort=effort,
        color_mode=color_mode,
        subparser=bundle.grep_parser,
    )
    return GrepArgs(
        patterns=tuple(patterns_list),
        agents=agents,
        scope=scope,
        case_mode=case_mode,
        pattern_mode=pattern_mode,
        invert_match=invert_match,
        count_only=count_only,
        files_with_matches=files_with_matches,
        only_matching=only_matching,
        compiled=grep_compiled,
        raw_query=" ".join(patterns_list_raw),
        base_scope=_base_search_scope(namespace),
        base_effort=_base_search_effort(namespace),
        no_dedupe=t.cast("bool", namespace.no_dedupe),
        line_number=line_number,
        heading=heading,
        limit=limit,
        vimgrep=t.cast("bool", namespace.vimgrep),
        column=t.cast("bool", namespace.column),
        output_mode=output_mode,
        color_mode=color_mode,
        progress_mode=t.cast("ProgressMode", namespace.progress),
        effort=effort,
        scope_provenance=scope_provenance,
        base_scope_provenance=_base_scope_provenance(namespace),
        conversation_limit=conversation_limit,
        style=t.cast("GrepStyle", namespace.style),
        diagnostics=grep_diagnostics,
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
    # The --here boost only exists inside ranked CLI output; reject the
    # modes that would silently drop it.
    if t.cast("bool", namespace.here) and no_rank:
        with configured_color_environment(color_mode):
            bundle.search_parser.error(
                "--here has no effect with --no-rank (ranking is disabled)",
            )
    if t.cast("bool", namespace.here) and output_mode == "ui":
        with configured_color_environment(color_mode):
            bundle.search_parser.error(
                "--here has no effect with --ui (use --only-here to filter)",
            )

    origin_nodes, origin_boost, origin_filter = _build_search_origin_nodes(namespace)
    search_compiled, residual_terms, search_user_ast, search_diagnostics = _maybe_compile_query(
        terms_list,
        bundle=bundle,
        color_mode=color_mode,
        subparser=bundle.search_parser,
        explicit_flags=_search_explicit_flags(namespace),
        case_sensitive=t.cast("bool", namespace.case_sensitive),
    )
    final_terms: tuple[str, ...] = residual_terms
    case_sensitive = t.cast("bool", namespace.case_sensitive)
    raw_query = " ".join(
        (
            *(f"{node.field}:{_query_value_display(node.value)}" for node in origin_nodes),
            *terms_list,
        ),
    )

    scope, effort, scope_provenance = _resolve_scope_and_effort(
        namespace,
        search_user_ast,
        color_mode=color_mode,
        subparser=bundle.search_parser,
    )
    conversation_limit = _targeted_conversation_limit(
        namespace,
        scope=scope,
        effort=effort,
        color_mode=color_mode,
        subparser=bundle.search_parser,
    )
    return SearchArgs(
        terms=final_terms,
        agents=agents,
        scope=scope,
        case_sensitive=case_sensitive,
        limit=limit,
        output_mode=output_mode,
        color_mode=color_mode,
        progress_mode=t.cast("ProgressMode", namespace.progress),
        effort=effort,
        scope_provenance=scope_provenance,
        base_scope_provenance=_base_scope_provenance(namespace),
        conversation_limit=conversation_limit,
        threshold=threshold,
        no_group=t.cast("bool", namespace.no_group),
        no_rank=t.cast("bool", namespace.no_rank),
        compiled=search_compiled,
        raw_query=raw_query,
        origin_boost=origin_boost,
        origin_filter=origin_filter,
        base_scope=_base_search_scope(namespace),
        base_effort=_base_search_effort(namespace),
        diagnostics=search_diagnostics,
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
