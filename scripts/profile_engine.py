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
import json
import pathlib
import sys
import typing as t

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import agentgrep  # noqa: E402  (standalone script bootstraps src/ above)
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


@dataclasses.dataclass(frozen=True, slots=True)
class ProfileRunSpec:
    """One profiler component invocation."""

    component: str
    command: ProfileCommand
    scope: agentgrep.SearchScope | None = None
    type_filter: FindProfileType | None = None
    match_surface: agentgrep.SearchMatchSurface = "haystack"


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
            ),
        )
    return (PROFILE_COMPONENTS[t.cast("ProfileComponent", component)],)


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
        profiled_find = profile_find_query(
            home,
            agents,
            pattern=" ".join(args.terms) if args.terms else None,
            limit=limit,
            type_filter=type_filter,
        )
        payload = profiled_find.to_payload()
        payload["type_filter"] = type_filter
    else:
        scope = (
            spec.scope if spec.scope is not None else t.cast("agentgrep.SearchScope", args.scope)
        )
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
        profiled_search = profile_search_query(home, query)
        payload = profiled_search.to_payload()
        payload["scope"] = scope
    payload["profile_command"] = spec.command
    payload["profile_component"] = spec.component
    payload["agent_count"] = len(agents)
    payload["term_count"] = len(args.terms)
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
    if len(specs) == 1:
        return _run_spec(args, specs[0], home=home, agents=agents, limit=limit)
    runs = [_run_spec(args, spec, home=home, agents=agents, limit=limit) for spec in specs]
    return {
        "kind": "profile_batch",
        "profile_command": "all",
        "profile_component": "all",
        "agent_count": len(agents),
        "term_count": len(args.terms),
        "limit": limit,
        "runs": runs,
    }


def main(argv: list[str] | None = None) -> int:
    """Run the profiler command."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        payload = _run(args)
    except ValueError as exc:
        parser.error(str(exc))
    json.dump(payload, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
