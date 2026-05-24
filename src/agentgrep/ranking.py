"""Relevance scoring, near-duplicate collapsing, and session grouping.

The search subcommand collects all engine matches eagerly, then passes
them through the three-stage pipeline exposed here:

1. :func:`rank_search_records` — score each record against the query
   text with rapidfuzz WRatio, filter by threshold, sort best-first.
2. :func:`collapse_near_duplicates` — pairwise WRatio between record
   bodies; records above the similarity ceiling are folded into the
   highest-scoring representative.
3. :func:`group_by_session` — bucket the surviving records by
   ``session_id``, preserving score order within each group.
"""

from __future__ import annotations

import collections
import typing as t

if t.TYPE_CHECKING:
    from agentgrep import SearchRecord

__all__ = [
    "collapse_near_duplicates",
    "group_by_session",
    "rank_search_records",
]


def rank_search_records(
    records: list[SearchRecord],
    query_text: str,
    *,
    threshold: int = 0,
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

    Returns
    -------
    list[tuple[SearchRecord, float]]
        ``(record, score)`` pairs sorted by descending score.
    """
    import rapidfuzz.fuzz

    scored: list[tuple[SearchRecord, float]] = [
        (r, float(rapidfuzz.fuzz.WRatio(query_text, r.text))) for r in records
    ]
    if threshold > 0:
        scored = [(r, s) for r, s in scored if s >= threshold]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def collapse_near_duplicates(
    scored: list[tuple[SearchRecord, float]],
    *,
    similarity_threshold: float = 90.0,
) -> list[tuple[SearchRecord, float, int]]:
    """Collapse near-duplicate records, keeping highest-scored representative.

    Parameters
    ----------
    scored : list[tuple[SearchRecord, float]]
        Pre-sorted ``(record, score)`` pairs (best-first).
    similarity_threshold : float
        WRatio ceiling — record pairs scoring above this are
        considered near-duplicates.

    Returns
    -------
    list[tuple[SearchRecord, float, int]]
        ``(record, score, similar_count)`` triples. ``similar_count``
        is the number of collapsed duplicates.
    """
    import rapidfuzz.fuzz

    if not scored:
        return []
    if len(scored) > 500:
        return [(r, s, 0) for r, s in scored]
    result: list[tuple[SearchRecord, float, int]] = []
    consumed: set[int] = set()
    for i, (record_i, score_i) in enumerate(scored):
        if i in consumed:
            continue
        similar_count = 0
        for j in range(i + 1, len(scored)):
            if j in consumed:
                continue
            record_j = scored[j][0]
            sim = float(rapidfuzz.fuzz.WRatio(record_i.text, record_j.text))
            if sim >= similarity_threshold:
                similar_count += 1
                consumed.add(j)
        result.append((record_i, score_i, similar_count))
    return result


def group_by_session(
    records: list[tuple[SearchRecord, float, int]],
) -> list[tuple[str | None, list[tuple[SearchRecord, float, int]]]]:
    """Group records by session_id, preserving score order within groups.

    Parameters
    ----------
    records : list[tuple[SearchRecord, float, int]]
        Collapsed ``(record, score, similar_count)`` triples.

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
