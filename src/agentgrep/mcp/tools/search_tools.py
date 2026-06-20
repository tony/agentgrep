"""Search-domain MCP tools."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import pathlib
import time
import typing as t

import mcp.types as mt
from fastmcp import Context
from fastmcp.exceptions import ToolError
from mcp import McpError
from pydantic import Field

from agentgrep import _telemetry, events as ag_events
from agentgrep._query_gate import unregistered_field_predicates_in
from agentgrep.mcp._library import (
    READONLY_TAGS,
    TOOL_ANNOTATIONS,
    AgentSelector,
    SearchEffortName,
    SearchRecordLike,
    SearchScopeName,
    agentgrep,
    normalize_agent_selection,
)
from agentgrep.mcp.middleware import TOOL_ARGUMENT_NAMES_STATE_KEY
from agentgrep.mcp.models import (
    DiagnosticModel,
    NextActionModel,
    NormalizedSearchRequestModel,
    RecentSessionsRequest,
    RecentSessionsResponse,
    ResultStatsModel,
    RunStatusModel,
    SearchCoverageModel,
    SearchEffortModel,
    SearchPageModel,
    SearchRecordModel,
    SearchRequestModel,
    SearchToolResponse,
    SourceRecordModel,
)
from agentgrep.origin import normalize_origin_path_text
from agentgrep.query.help import query_language_summary
from agentgrep.records import RecordOrigin

if t.TYPE_CHECKING:
    from fastmcp import FastMCP

    from agentgrep._engine.runtime import SearchRuntime
    from agentgrep._query_gate import UnregisteredFieldToken
    from agentgrep.records import SearchQuery
    from agentgrep.results import RunSummary

_TARGETED_PROMPT_SCOPE_ERROR = "targeted effort requires conversation or all scope"
_EFFORT_PARAM_TERM_COLLISION_ERROR = (
    "cannot combine the effort parameter with a depth:/effort: term; pick one"
)


def _invalid_params_error(message: str) -> McpError:
    """Build a public MCP invalid-params error.

    Parameters
    ----------
    message : str
        Actionable request constraint without rejected values.

    Returns
    -------
    McpError
        Error that FastMCP preserves as ``INVALID_PARAMS``.
    """
    return McpError(
        mt.ErrorData(
            code=mt.INVALID_PARAMS,
            message=f"Invalid params: {message}",
        ),
    )


def _request_has_origin_filter(request: SearchRequestModel) -> bool:
    return bool(
        (request.cwd or "").strip()
        or (request.repo or "").strip()
        or (request.branch or "").strip(),
    )


def _normalize_request_depth(
    request: SearchRequestModel,
) -> tuple[SearchScopeName, SearchEffortName, int | None]:
    """Normalize MCP effort, inferred scope, and targeted work bound.

    Only validates ``conversation_limit`` against the structured request —
    an inline ``depth:``/``effort:`` term (resolved later, by
    :func:`_compile_request_query`) can still change the effort this
    function computed here. The targeted-effort requirement is re-checked
    against the fully-resolved query in :func:`_search_async`, after that
    later resolution has had its say.
    """
    scope = request.scope
    effort = request.effort
    if effort is None:
        effort = "prompt" if scope == "prompts" else "exhaustive"
    elif request.scope_provenance == "inferred" and effort in {
        "targeted",
        "exhaustive",
    }:
        scope = "all"
    if effort == "prompt" and scope != "prompts":
        msg = "prompt effort requires prompt scope"
        raise ToolError(msg)
    if effort == "targeted" and scope == "prompts":
        raise _invalid_params_error(_TARGETED_PROMPT_SCOPE_ERROR)
    conversation_limit = request.conversation_limit
    if conversation_limit is not None and conversation_limit < 1:
        msg = "conversation_limit must be greater than 0"
        raise ToolError(msg)
    return scope, effort, conversation_limit


def _compile_request_query(
    base_query: SearchQuery,
    request: SearchRequestModel,
) -> tuple[SearchQuery, tuple[UnregisteredFieldToken, ...]]:
    """Apply the query language and origin filters to a search request.

    User terms compile exactly as the CLI's bare path compiles them —
    field predicates, booleans, phrases, and wildcards all apply, and
    plain terms stay literal substrings. Origin filters are ANDed in as
    synthetic AST nodes via :func:`agentgrep.query.compose_query_ast`.
    A malformed query raises a :class:`ToolError` with the parse/compile
    message. Scope and effort are resolved together by
    :func:`agentgrep.query.resolve_request_modifiers` — the same resolver
    the CLI and TUI use — so an inline ``scope:``/``depth:``/``effort:``
    predicate widens ``base_query`` identically everywhere.

    An inline ``depth:``/``effort:`` term is rejected outright when the
    structured ``effort`` request parameter was also set — unlike an inline
    ``scope:`` predicate, which is always allowed to widen the structured
    ``scope`` parameter (and has been since before this field existed).
    Effort is asymmetric because :func:`_normalize_request_depth` already
    validated and normalized the structured ``effort`` before this function
    runs; silently letting an inline directive override it here would mean
    that earlier validation ran against a value the request no longer uses.
    Requiring one syntax avoids that ordering hazard instead of reordering
    the whole request pipeline. ``conversation_limit`` has no such collision
    to avoid — the client only ever states it once, in the structured
    request — so it is validated by :func:`_search_async` against the fully
    resolved query instead, after any inline directive has had its say.

    Returns the rebuilt query plus any non-fatal
    :class:`~agentgrep._query_gate.UnregisteredFieldToken` diagnostics for
    field-predicate-shaped terms whose field isn't registered (``()`` when
    the terms carried no such shape, or the parser was engaged — an
    unregistered field there already raised :class:`ToolError` above
    instead of reaching this point).
    """
    from agentgrep.query import (
        QueryCompileError,
        QueryParseError,
        compile_query,
        compose_query_ast,
        default_registry,
        fields_in_ast,
        resolve_request_modifiers,
    )

    origin_filter = RecordOrigin(
        cwd=normalize_origin_path_text(request.cwd),
        repo=normalize_origin_path_text(request.repo),
        branch=request.branch if request.branch and request.branch.strip() else None,
    )
    if origin_filter.is_empty():
        origin_filter = None
    # Whitespace-split each element: MCP terms have always been words
    # (the pre-origin path joined and re-split them), unlike CLI argv
    # elements, which stay whole to match the bare fast path.
    terms = tuple(word for term in request.terms for word in term.split())
    if not terms:
        if origin_filter is None:
            return base_query, ()
        return dataclasses.replace(base_query, terms=(), origin_filter=origin_filter), ()
    registry = default_registry()
    try:
        ast, user_ast = compose_query_ast(terms, (), registry)
        compiled = compile_query(ast, registry, case_sensitive=base_query.case_sensitive)
    except (QueryParseError, QueryCompileError) as exc:
        message = f"invalid query: {exc}"
        raise ToolError(message) from exc
    diagnostics = () if user_ast is not None else unregistered_field_predicates_in(terms)
    used_fields = fields_in_ast(user_ast) if user_ast is not None else set()
    if request.effort is not None and "depth" in used_fields:
        raise _invalid_params_error(_EFFORT_PARAM_TERM_COLLISION_ERROR)
    try:
        scope, effort = resolve_request_modifiers(
            user_ast,
            registry,
            base_scope=base_query.scope,
            base_effort=base_query.effort,
            base_scope_explicit=request.scope_provenance == "explicit",
        )
    except QueryCompileError as exc:
        message = f"invalid query: {exc}"
        raise ToolError(message) from exc
    if effort == "targeted" and scope == "prompts":
        raise _invalid_params_error(_TARGETED_PROMPT_SCOPE_ERROR)
    if effort == "prompt" and scope != "prompts":
        msg = "prompt effort requires prompt scope"
        raise ToolError(msg)
    return (
        dataclasses.replace(
            base_query,
            terms=compiled.text_terms,
            compiled=None if compiled.is_pure_text else compiled,
            scope=scope,
            # Whether scope was *stated* by the client — a structured
            # scope_provenance="explicit" or an inline scope: predicate —
            # not whether the value changed. A depth: directive can widen
            # or narrow scope on its own (resolve_request_modifiers'
            # reconciliation); that's the client trusting the directive's
            # own semantics, not selecting a scope, so it must not report
            # as "explicit" the way the CLI/TUI's equivalent resolution
            # already doesn't.
            scope_provenance=(
                "explicit"
                if request.scope_provenance == "explicit" or "scope" in used_fields
                else "inferred"
            ),
            effort=effort,
            origin_filter=origin_filter,
        ),
        diagnostics,
    )


async def _search_async(
    request: SearchRequestModel,
    *,
    runtime: SearchRuntime | None = None,
) -> SearchToolResponse:
    """Run the async search stream and build a typed response."""
    if not request.terms and not _request_has_origin_filter(request):
        msg = "terms or an origin filter are required"
        raise ToolError(msg)
    page_limit = request.limit
    scope, effort, conversation_limit = _normalize_request_depth(request)
    base_query = t.cast(
        "SearchQuery",
        agentgrep.SearchQuery(
            terms=tuple(request.terms),
            scope=scope,
            any_term=False,
            regex=False,
            case_sensitive=request.case_sensitive,
            agents=normalize_agent_selection(request.agent),
            limit=page_limit,
            effort=effort,
            scope_provenance=request.scope_provenance,
            conversation_limit=conversation_limit,
        ),
    )
    query, query_diagnostics = _compile_request_query(base_query, request)
    # Validated here, against the fully-resolved query, because an inline
    # depth:/effort: term can set an effort the structured request never
    # named.
    if query.conversation_limit is not None and query.effort != "targeted":
        msg = "conversation_limit requires targeted effort"
        raise ToolError(msg)
    records: list[SearchRecordLike] = []
    run_summary: RunSummary | None = None
    # The engine only stops scanning when this generator is finalized: its
    # cancellation request lives in the stream's finally block. A client cancel
    # finalizes it today only because the loop body below is await-free, which
    # keeps the generator frame innermost at every suspension point. aclosing()
    # stops that from being load-bearing, so an early break or an awaiting body
    # cannot strand a live scan until the loop's asyncgen hook collects it.
    async with contextlib.aclosing(
        agentgrep.aiter_search_events(
            pathlib.Path.home(),
            query,
            runtime=runtime,
        )
    ) as stream:
        async for event in stream:
            if run_summary is not None:
                msg = "search event stream emitted data after SearchFinished"
                raise RuntimeError(msg)
            if isinstance(event, ag_events.RecordEmitted):
                records.append(t.cast("SearchRecordLike", event.record))
            elif isinstance(event, ag_events.SearchFinished):
                run_summary = event.summary
    if run_summary is None:
        msg = "search event stream ended without SearchFinished"
        raise RuntimeError(msg)
    if len(records) != run_summary.match_count:
        msg = "search event record count does not match terminal summary"
        raise RuntimeError(msg)
    # The inline execution driver emits records per source, not in final
    # result order; restore the newest-first contract the list-returning
    # search path guarantees before building the response.
    records.sort(key=agentgrep.search_record_sort_key, reverse=True)
    return SearchToolResponse(
        schema_version=agentgrep.SCHEMA_VERSION,
        request=NormalizedSearchRequestModel.from_summary(run_summary),
        effort=SearchEffortModel.from_summary(run_summary),
        outcome=run_summary.outcome,
        coverage=SearchCoverageModel.from_coverage(run_summary.coverage),
        stats=ResultStatsModel(
            sources=run_summary.coverage.sources_planned,
            searched=run_summary.coverage.records_seen,
            matched=run_summary.coverage.matches_seen,
            emitted=len(records),
        ),
        page=SearchPageModel(
            limit=page_limit,
            count=len(records),
        ),
        status=RunStatusModel.from_summary(run_summary),
        diagnostics=[
            *(DiagnosticModel.from_diagnostic(item) for item in run_summary.diagnostics),
            *(DiagnosticModel.from_query_diagnostic(item) for item in query_diagnostics),
        ],
        next_actions=[NextActionModel.from_action(action) for action in run_summary.next_actions],
        results=[SearchRecordModel.from_record(record) for record in records],
    )


def _recent_sessions_sync(request: RecentSessionsRequest) -> RecentSessionsResponse:
    """Return recently modified sources sorted newest-first."""
    backends = agentgrep.select_backends()
    sources = agentgrep.discover_sources(
        pathlib.Path.home(),
        normalize_agent_selection(request.agent),
        backends,
        version_detail="none",
    )
    cutoff_ns = time.time_ns() - request.hours * 3600 * 1_000_000_000
    recent = [source for source in sources if source.mtime_ns >= cutoff_ns]
    recent.sort(key=lambda s: s.mtime_ns, reverse=True)
    if request.limit is not None:
        recent = recent[: request.limit]
    cutoff_iso = datetime.datetime.fromtimestamp(
        cutoff_ns / 1_000_000_000,
        tz=datetime.UTC,
    ).isoformat()
    return RecentSessionsResponse(
        cutoff_iso=cutoff_iso,
        sources=[SourceRecordModel.from_source(source) for source in recent],
    )


def register(mcp: FastMCP, *, runtime: SearchRuntime | None = None) -> None:
    """Register search-domain tools."""

    @mcp.tool(
        name="search",
        tags=READONLY_TAGS | {"search"},
        annotations=TOOL_ANNOTATIONS,
        description=(
            "Search fast prompt-history backends by default. Set effort to targeted "
            "for a bounded approximate conversation search, or exhaustive for all "
            "eligible readable conversations. Scope controls returned record kinds. "
            "Terms accept agentgrep's query language (field predicates, booleans, "
            "phrases, and wildcards) including an inline depth:/effort: term as an "
            "alternative to the effort parameter — combining both is an error; "
            "see agentgrep://query-language."
        ),
    )
    async def search_tool(
        mcp_context: Context,
        terms: t.Annotated[
            list[str] | None,
            Field(
                default=None,
                description=f"Search terms. {query_language_summary()}",
            ),
        ] = None,
        agent: t.Annotated[
            AgentSelector,
            Field(description="Limit search to one agent or search all agents."),
        ] = "all",
        scope: t.Annotated[
            SearchScopeName,
            Field(description="Return prompts, conversations, or both."),
        ] = "prompts",
        effort: t.Annotated[
            SearchEffortName | None,
            Field(
                default=None,
                description=(
                    "Search prompt evidence, a targeted conversation selection, "
                    "or every eligible readable conversation."
                ),
            ),
        ] = None,
        conversation_limit: t.Annotated[
            int | None,
            Field(
                default=None,
                ge=1,
                description=(
                    "Maximum distinct conversation attempts for targeted effort (default: 25)."
                ),
            ),
        ] = None,
        case_sensitive: t.Annotated[
            bool,
            Field(description="Perform case-sensitive matching."),
        ] = False,
        limit: t.Annotated[
            int | None,
            Field(
                default=20,
                ge=1,
                description="Maximum number of search results to return.",
            ),
        ] = 20,
        cwd: t.Annotated[
            str | None,
            Field(
                default=None,
                description="Only return records whose recorded cwd matches this path.",
            ),
        ] = None,
        repo: t.Annotated[
            str | None,
            Field(
                default=None,
                description="Only return records whose recorded repository root matches this path.",
            ),
        ] = None,
        branch: t.Annotated[
            str | None,
            Field(
                default=None,
                description="Only return records whose recorded git branch matches this name.",
            ),
        ] = None,
    ) -> SearchToolResponse:
        argument_names = await mcp_context.get_state(TOOL_ARGUMENT_NAMES_STATE_KEY)
        request = SearchRequestModel(
            terms=terms or [],
            agent=agent,
            scope=scope,
            effort=effort,
            conversation_limit=conversation_limit,
            case_sensitive=case_sensitive,
            limit=limit,
            cwd=cwd,
            repo=repo,
            branch=branch,
            scope_provenance=(
                "explicit"
                if isinstance(argument_names, frozenset) and "scope" in argument_names
                else "inferred"
            ),
        )
        return await _search_async(request, runtime=runtime)

    _ = search_tool

    @mcp.tool(
        name="recent_sessions",
        tags=READONLY_TAGS | {"search"},
        annotations=TOOL_ANNOTATIONS,
        description="Return sources modified in the last N hours, newest-first.",
    )
    async def recent_sessions_tool(
        agent: t.Annotated[
            AgentSelector,
            Field(description="Limit discovery to one agent or scan every agent."),
        ] = "all",
        hours: t.Annotated[
            int,
            Field(
                default=24,
                ge=1,
                le=24 * 30,
                description="Look back this many hours (max 30 days).",
            ),
        ] = 24,
        limit: t.Annotated[
            int | None,
            Field(
                default=10,
                ge=1,
                description="Maximum number of sources to return.",
            ),
        ] = 10,
    ) -> RecentSessionsResponse:
        request = RecentSessionsRequest(agent=agent, hours=hours, limit=limit)
        return await _telemetry.to_thread(_recent_sessions_sync, request)

    _ = recent_sessions_tool
