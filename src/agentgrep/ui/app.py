"""Streaming Textual app entry points — ``run_ui`` and the app factory.

This module is the Textual-free factory facade: it builds the
:class:`~agentgrep.ui._context.UiContext`, wires the engine seam, and constructs
the :class:`~agentgrep.ui._shell.ExplorerApp` shell (which mounts the default
layout). The shell and the layouts import Textual at module scope, so they are
imported lazily here and the import error is deferred to the moment a UI is
actually built — keeping a bare ``import agentgrep`` Textual-free (ADR 0010).
"""

from __future__ import annotations

import pathlib
import typing as t

from agentgrep.ui._context import UiContext

if t.TYPE_CHECKING:
    from agentgrep._types import RunnableAppLike
    from agentgrep.progress import SearchControl
    from agentgrep.records import SearchQuery

__all__ = ["build_streaming_ui_app", "run_ui"]


def run_ui(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
    initial_search_text: str | None = None,
) -> None:
    """Launch the streaming Textual explorer for ``query``.

    Thin wrapper that builds the app via :func:`build_streaming_ui_app` and
    calls ``app.run()``. The factory split lets tests construct the app for a
    Textual ``Pilot`` smoke test without entering the blocking run loop.

    Parameters
    ----------
    home : pathlib.Path
        User home directory, passed through to the search engine.
    query : SearchQuery
        Search to run. Empty ``terms`` means "all records" (browse mode).
    control : SearchControl
        Shared cooperative-cancel flag seeding the first search.
    initial_search_text : str | None
        Initial value of the layout's primary input. When ``None``, defaults to
        the space-joined ``query.terms``.
    """
    app = build_streaming_ui_app(
        home,
        query,
        control=control,
        initial_search_text=initial_search_text,
    )
    t.cast("RunnableAppLike", app).run()


def build_streaming_ui_app(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
    initial_search_text: str | None = None,
) -> object:
    """Construct the streaming Textual app without entering its run loop.

    Returns the constructed :class:`~agentgrep.ui._shell.ExplorerApp` shell
    (typed ``object`` so this module need not import Textual). Callers invoke
    ``.run()`` for a real session or ``.run_test()`` for a Pilot smoke test. The
    shell and the default layout are imported lazily so the eager ``import
    agentgrep`` path stays Textual-free (ADR 0010).

    Parameters
    ----------
    home : pathlib.Path
        User home directory, passed through to the search engine.
    query : SearchQuery
        Search to run. Empty ``terms`` means "all records" (browse mode).
    control : SearchControl
        Shared cooperative-cancel flag seeding the first search.
    initial_search_text : str | None
        Initial value of the layout's primary input; defaults to the
        space-joined ``query.terms`` when ``None``.
    """
    try:
        from agentgrep.ui._seams import EngineSearchInvoker
        from agentgrep.ui._shell import ExplorerApp
    except ImportError as error:
        msg = "Textual is required for --ui. Install with `uv pip install --editable .`."
        raise RuntimeError(msg) from error
    ctx = UiContext(
        home=home,
        invoker=EngineSearchInvoker(home),
        query=query,
        control=control,
        initial_search_text=initial_search_text,
    )
    return ExplorerApp(ctx)
