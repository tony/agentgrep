"""Find records similar to a seed, across the scoped corpus.

The one shared orchestrator behind the CLI ``similar`` verb (and the deferred
TUI / MCP surfaces): narrow the corpus through the normal query path, then rank
it with the pure, stdlib scorer in :mod:`agentgrep.ranking`. Parity is by this
one helper, not duplicated scoring logic.

The engine and ranking imports are function-local so importing ``agentgrep``
never pulls the planner or the scorer onto the cold-start path.
"""

from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:
    import pathlib

    from agentgrep.records import AgentName, SearchRecord, SearchScope

__all__ = ["run_find_similar"]


def run_find_similar(
    home: pathlib.Path,
    *,
    seed_text: str,
    agents: tuple[AgentName, ...],
    scope: SearchScope,
    top_k: int = 20,
    threshold: float = 0.0,
    exclude_exact: bool = False,
) -> list[tuple[SearchRecord, float]]:
    """Return the records most similar to ``seed_text`` in the scoped corpus.

    Parameters
    ----------
    home : pathlib.Path
        The user home whose stores are searched.
    seed_text : str
        The text to find neighbors of.
    agents : tuple of AgentName
        The agents whose stores to include.
    scope : SearchScope
        ``prompts`` / ``conversations`` / ``all`` corpus narrowing.
    top_k : int
        Maximum number of neighbors to return.
    threshold : float
        Minimum similarity in ``0..1``.
    exclude_exact : bool
        When true, drop neighbors whose text is identical to the seed. The
        default keeps verbatim matches from other stores (the
        "where else did I ask this?" answer).

    Returns
    -------
    list of (SearchRecord, float)
        Neighbors best-first, capped at ``top_k``.

    Notes
    -----
    Seed-by-record-id (the scorer's ``seed_content_id`` identity exclusion) is
    a planned surface; today only the seed text is compared.
    """
    from agentgrep._engine.orchestration import run_search_query
    from agentgrep.progress import SearchControl, noop_search_progress
    from agentgrep.ranking import score_by_similarity
    from agentgrep.records import SearchQuery

    query = SearchQuery(
        terms=(),
        scope=scope,
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=agents,
        limit=None,
    )
    records = run_search_query(
        home,
        query,
        progress=noop_search_progress(),
        control=SearchControl(),
    )
    return score_by_similarity(
        seed_text,
        records,
        top_k=top_k,
        threshold=threshold,
        exclude_exact=exclude_exact,
    )
