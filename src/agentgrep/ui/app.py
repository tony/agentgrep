"""Streaming Textual app entry points — ``run_ui`` and the app factory.

The ``ExplorerApp`` Textual app lives in ``agentgrep.ui.app_screen``; it is
imported lazily here so importing this module by itself does not require
Textual. The import error is deferred to the moment a UI is actually built.
"""

from __future__ import annotations

import pathlib
import typing as t

if t.TYPE_CHECKING:
    from agentgrep._types import RunnableAppLike
    from agentgrep.progress import SearchControl
    from agentgrep.records import SearchQuery


def run_ui(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
    initial_search_text: str | None = None,
) -> None:
    """Launch the streaming Textual explorer for ``query``.

    Thin wrapper that builds the app via :func:`build_streaming_ui_app` and
    calls ``app.run()``. The factory split lets tests construct the app for
    a Textual ``Pilot`` smoke test without entering the blocking run loop.

    Parameters
    ----------
    home : pathlib.Path
        User home directory, passed through to :func:`run_search_query`.
    query : SearchQuery
        Search to run. Empty ``terms`` means "all records" (browse mode).
    control : SearchControl
        Shared cooperative-cancel flag; ``Esc`` / ``Ctrl-C`` call
        ``request_answer_now`` to nudge the worker to wrap up.
    initial_search_text : str | None
        Initial value of the TUI search box. When ``None``, defaults
        to the space-joined ``query.terms``. The CLI passes the raw
        positional string here so a launch like
        ``agentgrep search --ui agent:codex bliss`` opens with the
        full query in the box (not just the text terms).
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

    Returns the constructed ``ExplorerApp`` instance (typed ``object`` so this
    module need not import Textual). Callers invoke ``.run()`` for a real
    session or ``.run_test()`` for a Pilot smoke test. ``ExplorerApp`` lives in
    ``agentgrep.ui.app_screen`` and is imported lazily so the eager
    ``import agentgrep`` path stays Textual-free (ADR 0010).

    Parameters
    ----------
    home : pathlib.Path
        User home directory, passed through to the search engine.
    query : SearchQuery
        Search to run. Empty ``terms`` means "all records" (browse mode).
    control : SearchControl
        Shared cooperative-cancel flag; ``Esc`` / ``Ctrl-C`` call
        ``request_answer_now`` to nudge the worker to wrap up.
    initial_search_text : str | None
        Initial value of the TUI search box; defaults to the space-joined
        ``query.terms`` when ``None``.
    """
    try:
        from agentgrep.ui._seams import EngineSearchInvoker
        from agentgrep.ui.app_screen import ExplorerApp
    except ImportError as error:
        msg = "Textual is required for --ui. Install with `uv pip install --editable .`."
        raise RuntimeError(msg) from error
    return ExplorerApp(
        home=home,
        query=query,
        control=control,
        invoker=EngineSearchInvoker(home),
        initial_search_text=initial_search_text,
    )
