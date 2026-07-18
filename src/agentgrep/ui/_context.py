"""Shared dependency context injected into pluggable TUI layouts (ADR 0013).

The App shell owns the session-fixed collaborators — the engine seam, the launch
query, and the cooperative-cancel control — and passes them to whichever
:class:`~agentgrep.ui.layouts._base.LayoutScreen` it mounts as one frozen
``UiContext``. A layout reaches the engine only through ``invoker`` (ADR 0012
RW-1), so it stays engine-agnostic and is constructable in a test with a fake
:class:`~agentgrep.ui._seams.SearchInvoker`.
"""

from __future__ import annotations

import dataclasses
import typing as t

if t.TYPE_CHECKING:
    import pathlib

    from agentgrep.progress import SearchControl
    from agentgrep.records import SearchQuery, SearchScope
    from agentgrep.ui._history import HistoryEntry
    from agentgrep.ui._seams import SearchInvoker

__all__ = ["UiContext"]


@dataclasses.dataclass(frozen=True, slots=True)
class UiContext:
    """Session-fixed dependencies every layout and workflow shares.

    Parameters
    ----------
    home : pathlib.Path
        User home directory, forwarded to the engine seam.
    invoker : SearchInvoker
        The narrow engine seam (ADR 0012 RW-1); the only path to a search.
    query : SearchQuery
        The launch query. A plan with no terms, compiled predicate, or origin
        filter opens in idle/browse mode.
    control : SearchControl
        The initial cooperative-cancel flag; a layout swaps in a fresh one
        per search, so this is only the seed.
    base_scope : SearchScope
        Discovery scope that an interactive query without a ``scope:``
        predicate returns to. This can differ from the launch query's
        effective scope.
    initial_search_text : str | None, optional
        Initial value of a layout's primary input. ``None`` defaults to the
        space-joined ``query.terms``.
    history : tuple[HistoryEntry, ...], optional
        Preloaded query-history snapshot for layouts that expose recall.
    history_disabled : bool, optional
        Whether persistent query history is disabled for this session.
    """

    home: pathlib.Path
    invoker: SearchInvoker
    query: SearchQuery
    control: SearchControl
    base_scope: SearchScope
    initial_search_text: str | None = None
    history: tuple[HistoryEntry, ...] = ()
    history_disabled: bool = False
