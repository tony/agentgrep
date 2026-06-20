#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "pydantic>=2.11.3",
#     "rich>=13.0",
# ]
# ///
"""Run privacy-safe engine profiling without CLI rendering overhead."""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import logging
import pathlib
import sys
import typing as t

import rich.console
import rich.table

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import agentgrep  # noqa: E402  (standalone script bootstraps src/ above)
from agentgrep import _telemetry  # noqa: E402  (standalone script bootstraps src/ above)
from agentgrep._engine.profiling import (  # noqa: E402  (standalone script bootstraps src/ above)
    FindProfileType,
    profile_find_query,
    profile_search_query,
)

ProfileCommand = t.Literal["search", "find", "grep"]
ProfileComponent = t.Literal[
    "search-prompts",
    "search-conversations",
    "grep-prompts",
    "grep-conversations",
    "find-prompts",
]
ComponentArgument = ProfileComponent | t.Literal["all", "search", "find"]
OutputFormat = t.Literal["json", "ndjson", "rich"]
SCHEMA_VERSION = 1
PROFILE_RUN_ARTIFACT_KIND = "agentgrep.profile.run"
PROFILE_BATCH_ARTIFACT_KIND = "agentgrep.profile.batch"
logger = logging.getLogger("agentgrep.profile_engine")


@dataclasses.dataclass(frozen=True, slots=True)
class SpanSummary:
    """One flattened profiler sample for rich rendering."""

    component: str
    name: str
    duration_seconds: float
    attributes: dict[str, object]


@dataclasses.dataclass(frozen=True, slots=True)
class StrategyGroupSummary:
    """One physical strategy group from profiler planning samples."""

    component: str
    agent: str
    store: str
    adapter_id: str
    source_kind: str
    strategy: str
    source_count: int


@dataclasses.dataclass(frozen=True, slots=True)
class ProfileRunSpec:
    """One profiler component invocation."""

    component: str
    command: ProfileCommand
    scope: agentgrep.SearchScope | None = None
    type_filter: FindProfileType | None = None
    match_surface: agentgrep.SearchMatchSurface = "haystack"
    find_uses_terms: bool = False


PROFILE_COMPONENTS: dict[ProfileComponent, ProfileRunSpec] = {
    "search-prompts": ProfileRunSpec(
        component="search-prompts",
        command="search",
        scope="prompts",
    ),
    "search-conversations": ProfileRunSpec(
        component="search-conversations",
        command="search",
        scope="conversations",
    ),
    "grep-prompts": ProfileRunSpec(
        component="grep-prompts",
        command="grep",
        scope="prompts",
        match_surface="text",
    ),
    "grep-conversations": ProfileRunSpec(
        component="grep-conversations",
        command="grep",
        scope="conversations",
        match_surface="text",
    ),
    "find-prompts": ProfileRunSpec(
        component="find-prompts",
        command="find",
        type_filter="prompts",
    ),
}
PROFILE_COMPONENT_ORDER: tuple[ProfileComponent, ...] = (
    "search-prompts",
    "search-conversations",
    "grep-prompts",
    "grep-conversations",
    "find-prompts",
)


def _parse_agents(raw: str) -> tuple[agentgrep.AgentName, ...]:
    """Parse a comma-separated agent list."""
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values or "all" in values:
        return agentgrep.AGENT_CHOICES
    choices = set(agentgrep.AGENT_CHOICES)
    invalid = sorted(value for value in values if value not in choices)
    if invalid:
        msg = f"unknown agent: {', '.join(invalid)}"
        raise argparse.ArgumentTypeError(msg)
    return tuple(t.cast("agentgrep.AgentName", value) for value in values)


def _build_parser() -> argparse.ArgumentParser:
    """Build the engine profiler argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "component",
        choices=(
            "search-prompts",
            "search-conversations",
            "grep-prompts",
            "grep-conversations",
            "find-prompts",
            "all",
            "search",
            "find",
        ),
        help="Profiler component to run; search/find are legacy aliases",
    )
    parser.add_argument("terms", nargs="*", help="Search terms; text is not emitted in JSON")
    parser.add_argument("--agent", default="all", type=_parse_agents, help="Agent or comma list")
    parser.add_argument(
        "--scope",
        choices=("prompts", "conversations", "all"),
        default="prompts",
        help="Search scope",
    )
    parser.add_argument(
        "--type",
        choices=("prompts", "history", "sessions", "all"),
        default="all",
        dest="type_filter",
        help="Find source type filter",
    )
    parser.add_argument("--limit", type=int, default=None, help="Result limit")
    parser.add_argument(
        "--max-count",
        type=int,
        default=None,
        help="Grep-shaped alias for --limit; text is not emitted in JSON",
    )
    parser.add_argument(
        "--any-term",
        action="store_true",
        help="Search terms with OR semantics",
    )
    parser.add_argument("--regex", action="store_true", help="Treat terms as regular expressions")
    parser.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Search with case-sensitive matching",
    )
    parser.add_argument(
        "--query-language",
        action="store_true",
        help="Compile the terms as a query-language expression (field predicates, booleans)",
    )
    output_group = parser.add_mutually_exclusive_group()
    parser.set_defaults(output_format="rich")
    output_group.add_argument(
        "--format",
        choices=("json", "ndjson", "rich"),
        dest="output_format",
        help="Output renderer",
    )
    output_group.add_argument(
        "--json",
        action="store_const",
        const="json",
        dest="output_format",
        help="Emit one JSON profile document",
    )
    output_group.add_argument(
        "--ndjson",
        action="store_const",
        const="ndjson",
        dest="output_format",
        help="Emit one profile run per JSON line",
    )
    parser.add_argument(
        "--top-spans",
        type=int,
        default=10,
        help="Number of slowest spans to show in rich output",
    )
    return parser


def _resolve_result_limit(args: argparse.Namespace) -> int | None:
    """Return the requested result cap, validating aliases."""
    limit = t.cast("int | None", args.limit)
    max_count = t.cast("int | None", args.max_count)
    if limit is not None and max_count is not None and limit != max_count:
        msg = "--limit and --max-count disagree"
        raise ValueError(msg)
    return max_count if max_count is not None else limit


def _resolve_component_specs(args: argparse.Namespace) -> tuple[ProfileRunSpec, ...]:
    """Expand a component argument into one or more profiler runs."""
    component = t.cast("ComponentArgument", args.component)
    if component == "all":
        return tuple(PROFILE_COMPONENTS[name] for name in PROFILE_COMPONENT_ORDER)
    if component == "search":
        return (
            ProfileRunSpec(
                component="search",
                command="search",
                scope=t.cast("agentgrep.SearchScope", args.scope),
            ),
        )
    if component == "find":
        return (
            ProfileRunSpec(
                component="find",
                command="find",
                type_filter=t.cast("FindProfileType", args.type_filter),
                find_uses_terms=True,
            ),
        )
    return (PROFILE_COMPONENTS[t.cast("ProfileComponent", component)],)


def _build_search_query(
    args: argparse.Namespace,
    spec: ProfileRunSpec,
    *,
    agents: tuple[agentgrep.AgentName, ...],
    limit: int | None,
) -> tuple[agentgrep.SearchQuery, agentgrep.SearchScope]:
    """Build the search/grep ``SearchQuery``, compiling query-language input.

    With ``--query-language`` the joined terms are parsed and compiled so the
    query carries its source/record predicates; otherwise the legacy bare-term
    query is returned unchanged. Returns ``(query, scope)``.
    """
    scope = spec.scope if spec.scope is not None else t.cast("agentgrep.SearchScope", args.scope)
    query = agentgrep.SearchQuery(
        terms=tuple(t.cast("list[str]", args.terms)),
        scope=scope,
        any_term=bool(args.any_term),
        regex=bool(args.regex),
        case_sensitive=bool(args.case_sensitive),
        agents=agents,
        limit=limit,
        dedupe=True,
        match_surface=spec.match_surface,
    )
    if bool(args.query_language):
        from agentgrep.query import compile_query, default_registry, parse_query

        registry = default_registry()
        compiled = compile_query(parse_query(" ".join(query.terms), registry), registry)
        query = dataclasses.replace(
            query,
            terms=compiled.text_terms,
            compiled=None if compiled.is_pure_text else compiled,
        )
    return query, scope


def _run_spec(
    args: argparse.Namespace,
    spec: ProfileRunSpec,
    *,
    home: pathlib.Path,
    agents: tuple[agentgrep.AgentName, ...],
    limit: int | None,
) -> dict[str, object]:
    """Run one profiler spec and return a sanitized JSON-ready payload."""
    if spec.command == "find":
        type_filter = t.cast("FindProfileType", spec.type_filter)
        terms = tuple(t.cast("list[str]", args.terms)) if spec.find_uses_terms else ()
        profiled_find = profile_find_query(
            home,
            agents,
            pattern=" ".join(terms) if terms else None,
            limit=limit,
            type_filter=type_filter,
        )
        payload = profiled_find.to_payload()
        payload["type_filter"] = type_filter
    else:
        query, scope = _build_search_query(args, spec, agents=agents, limit=limit)
        profiled_search = profile_search_query(home, query)
        payload = profiled_search.to_payload()
        payload["scope"] = scope
        terms = query.terms
    payload["profile_command"] = spec.command
    payload["profile_component"] = spec.component
    payload["schema_version"] = SCHEMA_VERSION
    payload["artifact_kind"] = PROFILE_RUN_ARTIFACT_KIND
    payload["agent_count"] = len(agents)
    payload["term_count"] = len(terms)
    payload["limit"] = limit
    if spec.command == "grep":
        payload["max_count"] = limit
    return payload


def _run(args: argparse.Namespace) -> dict[str, object]:
    """Run the selected profiler component and return a JSON-ready payload."""
    home = pathlib.Path.home()
    agents = t.cast("tuple[agentgrep.AgentName, ...]", args.agent)
    limit = _resolve_result_limit(args)
    specs = _resolve_component_specs(args)
    if bool(args.query_language) and any(spec.command == "find" for spec in specs):
        msg = "--query-language is unsupported for the find profiler (no compiled-query support)"
        raise ValueError(msg)
    if len(specs) == 1:
        return _run_spec(args, specs[0], home=home, agents=agents, limit=limit)
    runs = [_run_spec(args, spec, home=home, agents=agents, limit=limit) for spec in specs]
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": PROFILE_BATCH_ARTIFACT_KIND,
        "kind": "profile_batch",
        "profile_command": "all",
        "profile_component": "all",
        "agent_count": len(agents),
        "term_count": len(args.terms),
        "limit": limit,
        "runs": runs,
    }


def _profile_runs(payload: dict[str, object]) -> tuple[dict[str, object], ...]:
    """Return child profile runs, flattening a batch payload when needed."""
    if payload.get("kind") != "profile_batch":
        return (payload,)
    runs = payload.get("runs")
    if not isinstance(runs, list):
        return ()
    return tuple(run for run in runs if isinstance(run, dict))


def _profile_samples(run: dict[str, object]) -> tuple[dict[str, object], ...]:
    """Return sample dictionaries from one profile run."""
    profile = run.get("profile")
    if not isinstance(profile, dict):
        return ()
    samples = profile.get("samples")
    if not isinstance(samples, list):
        return ()
    return tuple(sample for sample in samples if isinstance(sample, dict))


def _span_summaries(
    payload: dict[str, object],
    *,
    top_spans: int,
) -> tuple[SpanSummary, ...]:
    """Return the slowest profile samples across one payload."""
    if top_spans <= 0:
        return ()
    summaries: list[SpanSummary] = []
    for run in _profile_runs(payload):
        component = run.get("profile_component")
        component_text = component if isinstance(component, str) else "unknown"
        for sample in _profile_samples(run):
            name = sample.get("name")
            duration = sample.get("duration_seconds")
            attributes = sample.get("attributes")
            summaries.append(
                SpanSummary(
                    component=component_text,
                    name=name if isinstance(name, str) else "unknown",
                    duration_seconds=float(duration) if isinstance(duration, int | float) else 0.0,
                    attributes=dict(attributes) if isinstance(attributes, dict) else {},
                ),
            )
    return tuple(
        sorted(
            summaries,
            key=lambda summary: summary.duration_seconds,
            reverse=True,
        )[:top_spans],
    )


def _attr_text(attributes: dict[str, object], key: str) -> str:
    """Return a string profile attribute, or an empty string."""
    value = attributes.get(key)
    return value if isinstance(value, str) else ""


def _attr_int(attributes: dict[str, object], key: str) -> int:
    """Return an integer profile attribute, or zero."""
    value = attributes.get(key)
    return value if isinstance(value, int) else 0


def _strategy_group_summaries(payload: dict[str, object]) -> tuple[StrategyGroupSummary, ...]:
    """Return physical strategy groups from profiler planning samples."""
    summaries: list[StrategyGroupSummary] = []
    for run in _profile_runs(payload):
        component = run.get("profile_component")
        component_text = component if isinstance(component, str) else "unknown"
        for sample in _profile_samples(run):
            if sample.get("name") != "search.plan.strategy_group":
                continue
            attributes = sample.get("attributes")
            if not isinstance(attributes, dict):
                continue
            summaries.append(
                StrategyGroupSummary(
                    component=component_text,
                    agent=_attr_text(attributes, "agentgrep_agent"),
                    store=_attr_text(attributes, "agentgrep_store"),
                    adapter_id=_attr_text(attributes, "agentgrep_adapter_id"),
                    source_kind=_attr_text(attributes, "agentgrep_source_kind"),
                    strategy=_attr_text(attributes, "agentgrep_source_strategy"),
                    source_count=_attr_int(attributes, "agentgrep_source_count"),
                ),
            )
    return tuple(
        sorted(
            summaries,
            key=lambda summary: (
                summary.component,
                summary.agent,
                summary.store,
                summary.adapter_id,
                summary.strategy,
            ),
        ),
    )


def _fmt_duration(seconds: float) -> str:
    """Render a duration in seconds."""
    return f"{seconds:.3f}s"


#: Attribute keys that match a denied substring but carry only safe
#: classifier values (path kinds and probe-status literals), never real paths.
_SAFE_ATTRIBUTE_KEYS = frozenset(
    {
        "agentgrep_path_kind",
        "agentgrep_env_path_status",
        "agentgrep_override_path_status",
    },
)

_DENIED_ATTRIBUTE_KEY_PARTS = ("argv", "command", "path", "query")


def _fmt_attributes(attributes: dict[str, object]) -> str:
    """Render scalar span attributes for a compact table cell.

    Engine payloads are sanitized at construction; the deny list here is
    defense in depth so a future attribute addition cannot leak argv,
    query text, or local paths into terminal output.
    """
    cells: list[str] = []
    for key, value in sorted(attributes.items()):
        if key not in _SAFE_ATTRIBUTE_KEYS and any(
            part in key.casefold() for part in _DENIED_ATTRIBUTE_KEY_PARTS
        ):
            continue
        cells.append(f"{key}={value}")
        if len(cells) >= 5:
            break
    return ", ".join(cells)


def _render_json(payload: dict[str, object]) -> str:
    """Render the current single JSON document shape."""
    return json.dumps(payload, sort_keys=True)


def _render_ndjson(payload: dict[str, object]) -> str:
    """Render the already-sanitized profile runs, one JSON object per line."""
    return "\n".join(json.dumps(run, sort_keys=True) for run in _profile_runs(payload))


def _render_rich(payload: dict[str, object], *, top_spans: int) -> str:
    """Render profile summaries and the slowest spans as Rich tables."""
    console = rich.console.Console(record=True, file=io.StringIO(), width=120)
    summary = rich.table.Table(title="[bold]profile summary[/bold]")
    summary.add_column("component", style="cyan")
    summary.add_column("command")
    summary.add_column("kind")
    summary.add_column("results", justify="right")
    summary.add_column("sources", justify="right")
    summary.add_column("spans", justify="right")
    for run in _profile_runs(payload):
        source_count = run.get("planned_source_count", run.get("discovered_source_count", ""))
        summary.add_row(
            str(run.get("profile_component", "")),
            str(run.get("profile_command", "")),
            str(run.get("kind", "")),
            str(run.get("result_count", "")),
            str(source_count),
            str(len(_profile_samples(run))),
        )
    console.print(summary)

    strategy_groups = _strategy_group_summaries(payload)
    if strategy_groups:
        strategies = rich.table.Table(title="[bold]physical strategies[/bold]")
        strategies.add_column("component", style="cyan")
        strategies.add_column("agent")
        strategies.add_column("store")
        strategies.add_column("adapter")
        strategies.add_column("kind")
        strategies.add_column("strategy")
        strategies.add_column("sources", justify="right")
        for group in strategy_groups:
            strategies.add_row(
                group.component,
                group.agent,
                group.store,
                group.adapter_id,
                group.source_kind,
                group.strategy,
                str(group.source_count),
            )
        console.print(strategies)

    spans = _span_summaries(payload, top_spans=top_spans)
    span_table = rich.table.Table(title="[bold]slowest spans[/bold]")
    span_table.add_column("component", style="cyan")
    span_table.add_column("span")
    span_table.add_column("duration", justify="right")
    span_table.add_column("attributes", overflow="fold")
    for span in spans:
        span_table.add_row(
            span.component,
            span.name,
            _fmt_duration(span.duration_seconds),
            _fmt_attributes(span.attributes),
        )
    console.print(span_table)
    return console.export_text()


def _render_payload(
    payload: dict[str, object],
    *,
    output_format: str,
    top_spans: int,
) -> str:
    """Render a profiler payload using the requested output format."""
    if output_format == "json":
        return _render_json(payload)
    if output_format == "ndjson":
        return _render_ndjson(payload)
    if output_format == "rich":
        return _render_rich(payload, top_spans=top_spans)
    msg = f"unknown output format: {output_format}"
    raise ValueError(msg)


def _arg_count(argv: list[str] | None) -> int:
    """Return argument count without recording raw argv."""
    return len(sys.argv[1:] if argv is None else argv)


def _set_run_span_attributes(payload: dict[str, object], *, output_format: str) -> None:
    """Attach safe profile-run metadata to the active root span."""
    runs = _profile_runs(payload)
    _telemetry.set_span_attribute("agentgrep_output_format", output_format)
    _telemetry.set_span_attribute("agentgrep_profile_component_count", len(runs))
    for source_key, target_key in (
        ("profile_component", "agentgrep_profile_component"),
        ("profile_command", "agentgrep_command"),
        ("agent_count", "agentgrep_agent_count"),
        ("limit", "agentgrep_result_limit"),
    ):
        value = payload.get(source_key)
        if value is not None:
            _telemetry.set_span_attribute(target_key, value)
    if len(runs) == 1:
        scope = runs[0].get("scope")
        if scope is not None:
            _telemetry.set_span_attribute("agentgrep_scope", scope)


def _system_exit_code(exc: SystemExit) -> int:
    """Return a numeric ``SystemExit`` code."""
    if isinstance(exc.code, int):
        return exc.code
    if exc.code is None:
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    """Run the profiler command."""
    parser = _build_parser()
    telemetry = _telemetry.setup(repo_root=REPO_ROOT)
    try:
        with _telemetry.span(
            "agentgrep.profile_engine.run",
            agentgrep_surface="profile_engine",
            agentgrep_arg_count=_arg_count(argv),
        ):
            parse_exit: SystemExit | None = None
            args: argparse.Namespace | None = None
            with _telemetry.span(
                "agentgrep.profile_engine.parse",
                agentgrep_surface="profile_engine",
            ):
                try:
                    args = parser.parse_args(argv)
                except SystemExit as exc:
                    parse_exit = exc
                    _telemetry.set_span_attribute("agentgrep_outcome", "parse_error")
                    _telemetry.set_span_attribute(
                        "agentgrep_exit_code",
                        _system_exit_code(exc),
                    )
            if parse_exit is not None:
                exit_code = _system_exit_code(parse_exit)
                outcome = "help" if exit_code == 0 else "parse_error"
                _telemetry.set_span_attribute("agentgrep_outcome", outcome)
                _telemetry.set_span_attribute("agentgrep_exit_code", exit_code)
                logger.info(
                    "profile engine completed",
                    extra={
                        "agentgrep_surface": "profile_engine",
                        "agentgrep_outcome": outcome,
                        "agentgrep_exit_code": exit_code,
                    },
                )
                if exit_code == 0:
                    return 0
                raise parse_exit
            assert args is not None
            logger.info(
                "profile engine started",
                extra={"agentgrep_surface": "profile_engine"},
            )
            try:
                with _telemetry.span(
                    "agentgrep.profile_engine.execute",
                    agentgrep_surface="profile_engine",
                ):
                    payload = _run(args)
            except ValueError as exc:
                _telemetry.set_span_attribute("agentgrep_outcome", "parse_error")
                logger.info(
                    "profile engine failed",
                    extra={
                        "agentgrep_surface": "profile_engine",
                        "agentgrep_outcome": "parse_error",
                    },
                )
                parser.error(str(exc))
            _set_run_span_attributes(
                payload,
                output_format=t.cast("OutputFormat", args.output_format),
            )
            try:
                with _telemetry.span(
                    "agentgrep.profile_engine.render",
                    agentgrep_surface="profile_engine",
                ):
                    rendered = _render_payload(
                        payload,
                        output_format=t.cast("OutputFormat", args.output_format),
                        top_spans=t.cast("int", args.top_spans),
                    )
            except ValueError as exc:
                _telemetry.set_span_attribute("agentgrep_outcome", "parse_error")
                logger.info(
                    "profile engine failed",
                    extra={
                        "agentgrep_surface": "profile_engine",
                        "agentgrep_outcome": "parse_error",
                    },
                )
                parser.error(str(exc))
            sys.stdout.write(rendered)
            sys.stdout.write("\n")
            _telemetry.set_span_attribute("agentgrep_outcome", "ok")
            _telemetry.set_span_attribute("agentgrep_exit_code", 0)
            logger.info(
                "profile engine completed",
                extra={
                    "agentgrep_surface": "profile_engine",
                    "agentgrep_outcome": "ok",
                    "agentgrep_exit_code": 0,
                },
            )
            return 0
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
