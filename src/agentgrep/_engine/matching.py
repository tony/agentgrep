"""Compiled record matching helpers."""

from __future__ import annotations

import dataclasses
import re

from agentgrep._engine.orchestration import build_record_match_surface, record_matches_scope
from agentgrep.origin import OriginMatcher
from agentgrep.records import SearchMatchSurface, SearchQuery, SearchRecord


@dataclasses.dataclass(frozen=True, slots=True)
class CompiledRecordMatcher:
    """Precomputed record matcher for one search query."""

    query: SearchQuery
    needles: tuple[str, ...]
    regexes: tuple[re.Pattern[str], ...]
    use_joined_surface: bool
    origin_filter_matcher: OriginMatcher | None

    def matches(self, record: SearchRecord) -> bool:
        """Return whether ``record`` satisfies the compiled query."""
        if not record_matches_scope(record, self.query.scope):
            return False
        compiled = self.query.compiled
        if compiled is not None and compiled.record_predicate is not None:
            # The record predicate evaluates text terms itself with the
            # query's AND/OR/NOT structure; pre-gating on the flat term
            # list would wrongly require every OR branch at once.
            return compiled.record_predicate(record) and self._matches_origin_filter(record)
        return self._matches_query_terms(record) and self._matches_origin_filter(record)

    def _matches_origin_filter(self, record: SearchRecord) -> bool:
        """Return whether ``record`` satisfies an explicit origin filter."""
        if self.origin_filter_matcher is None:
            return True
        return self.origin_filter_matcher.matches(record)

    def _matches_query_terms(self, record: SearchRecord) -> bool:
        """Return whether unfielded query terms match ``record``."""
        if not self.query.terms:
            return True
        if self.query.regex or self.use_joined_surface:
            return self._matches_joined_surface(record)
        fields = _record_literal_fields(record, self.query.match_surface)
        if self.query.case_sensitive:
            return self._matches_literal_fields(fields)
        return self._matches_literal_fields(tuple(field.casefold() for field in fields))

    def _matches_joined_surface(self, record: SearchRecord) -> bool:
        """Evaluate terms against the legacy joined text surface."""
        surface = build_record_match_surface(record, self.query.match_surface)
        if self.query.regex:
            results = [regex.search(surface) is not None for regex in self.regexes]
        else:
            haystack = surface if self.query.case_sensitive else surface.casefold()
            results = [needle in haystack for needle in self.needles]
        return any(results) if self.query.any_term else all(results)

    def _matches_literal_fields(self, fields: tuple[str, ...]) -> bool:
        """Evaluate literal terms against already-normalized fields."""
        results = [any(needle in field for field in fields) for needle in self.needles]
        return any(results) if self.query.any_term else all(results)


def compile_record_matcher(query: SearchQuery) -> CompiledRecordMatcher:
    """Compile a reusable matcher for one search query."""
    flags = 0 if query.case_sensitive else re.IGNORECASE
    regexes = tuple(re.compile(term, flags) for term in query.terms) if query.regex else ()
    needles = (
        query.terms if query.case_sensitive else tuple(term.casefold() for term in query.terms)
    )
    return CompiledRecordMatcher(
        query=query,
        needles=needles,
        regexes=regexes,
        use_joined_surface=_needs_joined_literal_surface(query.terms),
        origin_filter_matcher=(
            None if query.origin_filter is None else OriginMatcher.from_origin(query.origin_filter)
        ),
    )


def matches_record(record: SearchRecord, query: SearchQuery) -> bool:
    """Return whether ``record`` matches ``query``."""
    return compile_record_matcher(query).matches(record)


def _record_literal_fields(
    record: SearchRecord,
    surface: SearchMatchSurface,
) -> tuple[str, ...]:
    """Return literal-match fields for the selected match surface."""
    if surface == "text":
        return (record.text,)
    return (
        record.title or "",
        record.text,
        record.model or "",
        record.role or "",
        str(record.path),
    )


def _needs_joined_literal_surface(terms: tuple[str, ...]) -> bool:
    """Return whether literal terms may depend on joined-field separators."""
    return any("\n" in term for term in terms)
