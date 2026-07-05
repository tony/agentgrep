"""Relevance scoring, near-duplicate collapsing, and session grouping.

The search subcommand collects all engine matches eagerly, then passes
them through the three-stage pipeline exposed here:

1. :func:`rank_search_records` — score each record against the query
   text with rapidfuzz WRatio, filter by threshold, sort best-first.
2. :func:`collapse_near_duplicates` — pairwise WRatio between record
   bodies; records at or above the similarity ceiling are folded into the
   highest-scoring representative.
3. :func:`group_by_session` — bucket the surviving records by
   ``session_id``, preserving score order within each group.
"""

from __future__ import annotations

import collections
import typing as t

if t.TYPE_CHECKING:
    from agentgrep.records import SearchRecord

__all__ = [
    "collapse_near_duplicates",
    "group_by_session",
    "rank_search_records",
    "score_by_similarity",
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


def score_by_similarity(
    seed_text: str,
    records: t.Iterable[SearchRecord],
    *,
    top_k: int = 20,
    threshold: float = 0.0,
    exclude_exact: bool = False,
    seed_content_id: str | None = None,
) -> list[tuple[SearchRecord, float]]:
    """Rank records by textual similarity to ``seed_text``, best-first.

    A Tier-0, dependency-free scorer: :class:`difflib.SequenceMatcher` with its
    ``real_quick_ratio`` -> ``quick_ratio`` -> ``ratio`` early-exit ladder
    (``set_seq2(seed)`` once, ``set_seq1(candidate)`` per record, like
    ``difflib.get_close_matches``). Scores are normalized to ``0..1``.

    Exclusion is by *identity*, not by text: pass ``seed_content_id`` to drop
    only the seed's own record, so verbatim matches in other stores — the
    "where else did I ask this?" answer — are retained by default.

    Parameters
    ----------
    seed_text : str
        The text every candidate is compared against.
    records : iterable of SearchRecord
        The candidate corpus (already scope-narrowed by the caller).
    top_k : int
        Maximum number of matches to return.
    threshold : float
        Minimum similarity in ``0..1``; the ladder prunes below it early.
    exclude_exact : bool
        When true, also drop records whose text is codepoint-identical to the
        seed (a plain string comparison, not Unicode-normalized).
    seed_content_id : str or None
        When set, drop the one record whose content id equals it (the seed).

    Returns
    -------
    list of (SearchRecord, float)
        ``(record, score)`` pairs, best-first, capped at ``top_k``.
    """
    if top_k <= 0:
        # A non-positive cap keeps nothing; return early so the size-k heap
        # guard below never indexes an empty best_scores.
        return []
    import difflib
    import heapq

    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(seed_text)
    content_id_of: t.Callable[[SearchRecord], str] | None = None
    if seed_content_id is not None:
        from agentgrep.identity import record_content_id

        content_id_of = record_content_id
    scored: list[tuple[SearchRecord, float]] = []
    # Running cutoff = the k-th best score seen so far. It lets the cheap
    # real_quick_ratio/quick_ratio upper bounds prune the expensive ratio()
    # even at the default threshold of 0.0, without changing the result: any
    # record pruned this way has an upper bound below a score already in the
    # top-k, so it could never have entered it. `best_scores` is a size-k
    # min-heap of the top scores, so best_scores[0] is that k-th best.
    best_scores: list[float] = []
    for record in records:
        if content_id_of is not None and content_id_of(record) == seed_content_id:
            continue
        if exclude_exact and record.text == seed_text:
            continue
        cutoff = threshold
        if len(best_scores) >= top_k and best_scores[0] > cutoff:
            cutoff = best_scores[0]
        matcher.set_seq1(record.text)
        if matcher.real_quick_ratio() < cutoff or matcher.quick_ratio() < cutoff:
            continue
        score = matcher.ratio()
        if score < cutoff:
            continue
        scored.append((record, score))
        heapq.heappush(best_scores, score)
        if len(best_scores) > top_k:
            heapq.heappop(best_scores)
    scored.sort(key=lambda pair: (-pair[1], pair[0].timestamp or "", pair[0].agent, pair[0].text))
    return scored[:top_k]


def collapse_near_duplicates(
    scored: list[tuple[SearchRecord, float]],
    *,
    similarity_threshold: float = 90.0,
) -> list[tuple[SearchRecord, float, int]]:
    """Collapse near-duplicate records, keeping highest-scored representative.

    Pairwise ``WRatio`` comparison between record texts (each call is
    C-accelerated by rapidfuzz). Records at or above the similarity
    threshold are folded into the highest-scoring representative.

    Parameters
    ----------
    scored : list[tuple[SearchRecord, float]]
        Pre-sorted ``(record, score)`` pairs (best-first).
    similarity_threshold : float
        WRatio ceiling — record pairs scoring at or above this are
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
