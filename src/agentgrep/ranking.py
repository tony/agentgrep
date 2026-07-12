"""Relevance scoring and session grouping.

The search subcommand collects all engine matches eagerly, then passes
them through the two-stage pipeline exposed here:

1. :func:`rank_search_records` — score each record against the query
   text with rapidfuzz WRatio, filter by threshold, sort best-first.
2. :func:`group_by_session` — bucket the ranked records by
   ``session_id``, preserving score order within each group.

Repeated record text is dropped by the engine's per-session dedupe
before ranking ever sees it; this module does no duplicate detection of
its own.
"""

from __future__ import annotations

import collections
import typing as t

from agentgrep.origin import OriginMatcher

if t.TYPE_CHECKING:
    from agentgrep.records import RecordOrigin, SearchRecord

__all__ = [
    "group_by_session",
    "rank_search_records",
]


def rank_search_records(
    records: list[SearchRecord],
    query_text: str,
    *,
    threshold: int = 0,
    origin_boost: RecordOrigin | None = None,
) -> list[tuple[SearchRecord, float]]:
    """Score records by relevance and sort best-first.

    Parameters
    ----------
    records : list[SearchRecord]
        Engine-matched records in discovery order.
    query_text : str
        The space-joined search terms for WRatio scoring.
    threshold : int
        Minimum fuzzy score (0-100). Records below are dropped.
    origin_boost : RecordOrigin | None
        Optional same-project context. Matching records receive a small
        additive boost after thresholding.

    Returns
    -------
    list[tuple[SearchRecord, float]]
        ``(record, score)`` pairs sorted by descending score.
    """
    import rapidfuzz.fuzz

    scored: list[tuple[SearchRecord, float]] = []
    origin_matcher = OriginMatcher.from_origin(origin_boost)
    for record in records:
        score = float(rapidfuzz.fuzz.WRatio(query_text, record.text))
        if threshold > 0 and score < threshold:
            continue
        if origin_matcher.matches(record):
            score += 10.0
        scored.append((record, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def group_by_session(
    records: list[tuple[SearchRecord, float, int]],
) -> list[tuple[str | None, list[tuple[SearchRecord, float, int]]]]:
    """Group records by session_id, preserving score order within groups.

    Parameters
    ----------
    records : list[tuple[SearchRecord, float, int]]
        Ranked ``(record, score, similar_count)`` triples, best-first.

    Returns
    -------
    list[tuple[str | None, list[...]]]
        ``(session_id, entries)`` pairs in first-seen order.
    """
    groups: collections.OrderedDict[
        str | None,
        list[tuple[SearchRecord, float, int]],
    ] = collections.OrderedDict()
    for record, score, similar in records:
        key = record.session_id
        if key not in groups:
            groups[key] = []
        groups[key].append((record, score, similar))
    return list(groups.items())
