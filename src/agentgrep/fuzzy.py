"""Fuzzy match helpers for the upcoming ``agentgrep fuzzy`` subcommand.

This module wraps :mod:`rapidfuzz` so the rest of the codebase never
imports it directly — keeping the choice of fuzzy backend swappable and
giving us one place to enforce fzf-compatible semantics (smart case,
extended-search tokens, score-descending sort).

Only filter-mode behavior is exposed here; the interactive fzf TUI shape
is provided by the Textual explorer instead.

Examples
--------
Smart-case scoring (case-insensitive when the query is lowercase):

>>> fuzzy_score("foo", "FOOBAR", case="smart") > 0
True
>>> fuzzy_score("FOO", "foobar", case="smart") == 0.0
True

Extended-search syntax matches positive tokens AND excludes ``!`` tokens:

>>> extended_match("foo !bar", "foobaz", case="ignore")
True
>>> extended_match("foo !bar", "foobar", case="ignore")
False

Ranking by descending score:

>>> ranked = list(
...     rank_lines("ab", ["xyz", "ab123", "abxx", "noop"], case="ignore"),
... )
>>> [line for line, _ in ranked]
['ab123', 'abxx']
"""

from __future__ import annotations

import collections.abc as cabc
import typing as t

import rapidfuzz.fuzz
import rapidfuzz.utils

CaseSensitivity = t.Literal["smart", "respect", "ignore"]
FuzzyAlgo = t.Literal["v1", "v2"]

__all__ = [
    "CaseSensitivity",
    "FuzzyAlgo",
    "extended_match",
    "fuzzy_score",
    "rank_lines",
    "resolve_case_sensitivity",
]


def resolve_case_sensitivity(query: str, case: CaseSensitivity) -> bool:
    """Return ``True`` when the match should be case-sensitive.

    Implements fzf's smart-case rule: a smart-case query is
    case-sensitive when it contains any uppercase character, otherwise
    case-insensitive.

    Examples
    --------
    >>> resolve_case_sensitivity("foo", "smart")
    False
    >>> resolve_case_sensitivity("Foo", "smart")
    True
    >>> resolve_case_sensitivity("foo", "respect")
    True
    >>> resolve_case_sensitivity("FOO", "ignore")
    False
    """
    if case == "respect":
        return True
    if case == "ignore":
        return False
    return any(ch.isupper() for ch in query)


def fuzzy_score(
    query: str,
    line: str,
    *,
    case: CaseSensitivity = "smart",
    algo: FuzzyAlgo = "v2",
) -> float:
    """Return a fuzzy match score in ``[0.0, 100.0]``.

    A score of ``0.0`` means no useful match; ``100.0`` is a perfect
    match. Higher is better. The ``algo`` selector mirrors fzf's
    ``--algo`` flag: ``v2`` is the default modern algorithm, ``v1`` a
    simpler fallback. Both currently dispatch to :mod:`rapidfuzz`'s
    quality-blend ratio so they remain functionally equivalent until a
    user-visible behavioral difference is justified.

    Parameters
    ----------
    query : str
        The search query to match against ``line``.
    line : str
        The candidate text to score.
    case : CaseSensitivity
        Case handling: ``"smart"`` (case-insensitive unless query
        contains uppercase), ``"ignore"``, or ``"respect"``.
    algo : FuzzyAlgo
        Algorithm selector: ``"v2"`` (WRatio) or ``"v1"``
        (partial_ratio).

    Returns
    -------
    float
        Match quality in ``[0.0, 100.0]``.
    """
    if not query:
        return 0.0
    case_sensitive = resolve_case_sensitivity(query, case)
    processor = None if case_sensitive else rapidfuzz.utils.default_process
    if algo == "v1":
        return float(rapidfuzz.fuzz.partial_ratio(query, line, processor=processor))
    return float(rapidfuzz.fuzz.WRatio(query, line, processor=processor))


def extended_match(
    query: str,
    line: str,
    *,
    case: CaseSensitivity = "smart",
) -> bool:
    """Return ``True`` when ``line`` satisfies fzf's extended-search query.

    Tokens are whitespace-separated. A bare token must match anywhere
    in the line (substring). A ``!``-prefixed token must NOT match. A
    ``^``-prefixed token must match the line prefix; a ``$``-suffixed
    token must match the line suffix. A ``'``-prefixed token forces an
    exact substring match (no fuzzy fallback).

    A line matches when every positive token's predicate is satisfied
    AND no negative token's predicate is.

    Parameters
    ----------
    query : str
        Extended-search query string (whitespace-separated tokens
        with optional ``!`` / ``^`` / ``$`` / ``'`` prefixes).
    line : str
        Candidate text to evaluate.
    case : CaseSensitivity
        Case handling mode (see :func:`fuzzy_score`).

    Returns
    -------
    bool
        ``True`` when all positive tokens match and no negative
        tokens match.

    Examples
    --------
    >>> extended_match("foo", "foobar", case="ignore")
    True
    >>> extended_match("^foo", "barfoo", case="ignore")
    False
    >>> extended_match("bar$", "foobar", case="ignore")
    True
    >>> extended_match("foo !bar", "foobaz", case="ignore")
    True
    """
    if not query.strip():
        return True
    case_sensitive = resolve_case_sensitivity(query, case)
    if not case_sensitive:
        line = line.casefold()
    for token in query.split():
        invert = token.startswith("!")
        if invert:
            token = token[1:]
        if not token:
            continue
        if not case_sensitive:
            token = token.casefold()
        anchor_prefix = token.startswith("^")
        if anchor_prefix:
            token = token[1:]
        anchor_suffix = token.endswith("$")
        if anchor_suffix:
            token = token[:-1]
        if token.startswith("'"):
            token = token[1:]
        if anchor_prefix:
            hit = line.startswith(token)
        elif anchor_suffix:
            hit = line.endswith(token)
        else:
            hit = token in line
        if invert and hit:
            return False
        if not invert and not hit:
            return False
    return True


def rank_lines(
    query: str,
    lines: cabc.Iterable[str],
    *,
    case: CaseSensitivity = "smart",
    algo: FuzzyAlgo = "v2",
    extended: bool = True,
    sort: bool = True,
    limit: int | None = None,
) -> cabc.Iterator[tuple[str, float]]:
    """Yield ``(line, score)`` pairs for lines matching ``query``.

    When ``extended`` is ``True``, ``query`` is parsed with fzf's
    extended-search syntax and only lines that satisfy all positive and
    no negative tokens are scored. With ``extended=False`` (or when the
    query is a single bare token), the fuzzy score alone gates inclusion
    — lines scoring ``0.0`` are dropped.

    An empty or whitespace-only query matches every line (yielded with
    score ``0.0``), mirroring ``fzf --filter ''``; the ``0.0`` drop only
    applies to non-empty queries.

    With ``sort=True`` (default) the iterator yields the highest-scoring
    line first; otherwise it preserves the input order. ``limit`` caps
    the number of yielded results when set.

    Parameters
    ----------
    query : str
        The search query (plain or extended-search syntax).
    lines : collections.abc.Iterable[str]
        Candidate lines to score and filter.
    case : CaseSensitivity
        Case handling mode (see :func:`fuzzy_score`).
    algo : FuzzyAlgo
        Algorithm selector (see :func:`fuzzy_score`).
    extended : bool
        When ``True``, apply :func:`extended_match` as a pre-filter
        before scoring. When ``False``, the fuzzy score alone gates.
    sort : bool
        When ``True``, yield highest-scoring lines first. When
        ``False``, preserve the input order.
    limit : int or None
        Cap on the number of yielded results. ``None`` yields all.

    Yields
    ------
    tuple[str, float]
        ``(line, score)`` pairs with ``score > 0.0`` (or ``0.0`` for an
        empty query, which matches every line).
    """
    candidates: list[tuple[str, float]] = []
    for line in lines:
        if extended and not extended_match(query, line, case=case):
            continue
        score = fuzzy_score(query, line, case=case, algo=algo)
        if score <= 0.0 and query.strip():
            continue
        candidates.append((line, score))
    if sort:
        candidates.sort(key=lambda pair: pair[1], reverse=True)
    if limit is not None:
        candidates = candidates[:limit]
    yield from candidates
