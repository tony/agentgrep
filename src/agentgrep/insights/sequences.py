"""Frequent-subsequence mining for workflow detection.

A small, dependency-free PrefixSpan over sequences of integer symbols. In
the graph engine each symbol is a *prompt archetype* (a cluster id), each
sequence is one conversation's ordered prompt archetypes, and a frequent
subsequence that recurs across conversations is a candidate **workflow**
("you run this chain of asks repeatedly").

PrefixSpan is used rather than the PyPI ``prefixspan`` package because that
package ships source-only (a build dependency) and the algorithm is small
when the alphabet is a handful of archetype ids.
"""

from __future__ import annotations

import collections
import typing as t

if t.TYPE_CHECKING:
    import collections.abc as cabc


class FrequentSequence(t.NamedTuple):
    """One mined pattern and how many input sequences contain it (in order)."""

    pattern: tuple[int, ...]
    support: int


def collapse_runs(symbols: cabc.Sequence[int]) -> tuple[int, ...]:
    """Collapse consecutive duplicate symbols (``a a b a`` -> ``a b a``).

    Recurring *chains* are about transitions between archetypes, so a run of
    the same archetype (a user rephrasing the same kind of ask) is one step.

    Examples
    --------
    >>> collapse_runs([1, 1, 2, 2, 2, 1])
    (1, 2, 1)
    >>> collapse_runs([])
    ()
    """
    out: list[int] = []
    for symbol in symbols:
        if not out or out[-1] != symbol:
            out.append(symbol)
    return tuple(out)


def _project(database: list[list[int]], item: int) -> list[list[int]]:
    """Return suffixes following the first occurrence of ``item`` per sequence."""
    projected: list[list[int]] = []
    for sequence in database:
        try:
            index = sequence.index(item)
        except ValueError:
            continue
        projected.append(sequence[index + 1 :])
    return projected


def prefixspan(
    sequences: cabc.Sequence[cabc.Sequence[int]],
    *,
    min_support: int,
    min_length: int = 1,
    max_length: int = 6,
) -> list[FrequentSequence]:
    """Mine frequent ordered subsequences via PrefixSpan.

    Parameters
    ----------
    sequences
        Each input sequence is an ordered list of integer symbols.
    min_support
        Minimum number of input sequences a pattern must appear in (in order,
        not necessarily contiguously) to be reported.
    min_length, max_length
        Inclusive bounds on reported pattern length.

    Returns
    -------
    list[FrequentSequence]
        Patterns with support >= ``min_support`` and length in
        ``[min_length, max_length]``, sorted by support then length, both
        descending.

    Examples
    --------
    >>> seqs = [[1, 2, 9], [1, 2, 8], [3, 3]]
    >>> [(p, s) for p, s in prefixspan(seqs, min_support=2, min_length=2)]
    [((1, 2), 2)]
    """
    if min_support < 1:
        min_support = 1
    results: list[FrequentSequence] = []
    database = [list(seq) for seq in sequences]

    def recurse(prefix: tuple[int, ...], projected: list[list[int]]) -> None:
        if len(prefix) >= max_length:
            return
        counts: collections.Counter[int] = collections.Counter()
        for sequence in projected:
            for item in set(sequence):
                counts[item] += 1
        for item, support in counts.items():
            if support < min_support:
                continue
            pattern = (*prefix, item)
            if len(pattern) >= min_length:
                results.append(FrequentSequence(pattern=pattern, support=support))
            recurse(pattern, _project(projected, item))

    recurse((), database)
    results.sort(key=lambda fs: (fs.support, len(fs.pattern)), reverse=True)
    return results


def maximal(patterns: cabc.Sequence[FrequentSequence]) -> list[FrequentSequence]:
    """Drop patterns that are a contiguous-order subsequence of a longer one.

    Keeps the report focused on the longest recurring chains instead of every
    frequent prefix. A pattern is dropped when another reported pattern of the
    same support contains it as an ordered subsequence.
    """

    def is_subsequence(small: tuple[int, ...], big: tuple[int, ...]) -> bool:
        iterator = iter(big)
        return all(symbol in iterator for symbol in small)

    kept: list[FrequentSequence] = []
    ordered = sorted(patterns, key=lambda fs: len(fs.pattern), reverse=True)
    for candidate in ordered:
        if any(
            other.support == candidate.support
            and len(other.pattern) > len(candidate.pattern)
            and is_subsequence(candidate.pattern, other.pattern)
            for other in kept
        ):
            continue
        kept.append(candidate)
    kept.sort(key=lambda fs: (fs.support, len(fs.pattern)), reverse=True)
    return kept
