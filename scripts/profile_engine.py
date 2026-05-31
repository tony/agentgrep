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

Mode = t.Literal["search", "find"]


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
    parser.add_argument("mode", choices=("search", "find"), help="Engine path to profile")
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


def _run(args: argparse.Namespace) -> dict[str, object]:
    """Run the selected profiler mode and return a JSON-ready payload."""
    home = pathlib.Path.home()
    agents = t.cast("tuple[agentgrep.AgentName, ...]", args.agent)
    limit = t.cast("int | None", args.limit)
    mode = t.cast("Mode", args.mode)
    if mode == "find":
        profiled_find = profile_find_query(
            home,
            agents,
            pattern=" ".join(args.terms) if args.terms else None,
            limit=limit,
            type_filter=t.cast("FindProfileType", args.type_filter),
        )
        payload = profiled_find.to_payload()
    else:
        query = agentgrep.SearchQuery(
            terms=tuple(t.cast("list[str]", args.terms)),
            scope=t.cast("agentgrep.SearchScope", args.scope),
            any_term=bool(args.any_term),
            regex=bool(args.regex),
            case_sensitive=bool(args.case_sensitive),
            agents=agents,
            limit=limit,
            dedupe=True,
        )
        profiled_search = profile_search_query(home, query)
        payload = profiled_search.to_payload()
    payload["profile_command"] = mode
    payload["agent_count"] = len(agents)
    payload["term_count"] = len(args.terms)
    payload["limit"] = limit
    return payload


def main(argv: list[str] | None = None) -> int:
    """Run the profiler command."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    payload = _run(args)
    json.dump(payload, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
