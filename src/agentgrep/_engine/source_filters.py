"""Shared source-admission predicates for search planning and execution."""

from __future__ import annotations

from agentgrep.origin import OriginMatcher
from agentgrep.records import SearchQuery, SourceHandle


def source_may_match_query(query: SearchQuery, source: SourceHandle) -> bool:
    """Return whether ``source`` cannot be ruled out before parsing."""
    source_predicate = query.compiled.source_predicate if query.compiled is not None else None
    if source_predicate is not None and not source_predicate(source):
        return False
    if query.origin_filter is None:
        return True
    return OriginMatcher.from_origin(query.origin_filter).may_match_summary(source.origin_summary)


def query_needs_prompt_session_sources(query: SearchQuery) -> bool:
    """Return whether prompt scope needs origin-bearing conversation stores."""
    compiled_origin_fields = (
        bool(query.compiled.origin_fields) if query.compiled is not None else False
    )
    return query.scope == "prompts" and (query.origin_filter is not None or compiled_origin_fields)
