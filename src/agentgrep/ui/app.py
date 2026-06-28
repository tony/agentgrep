"""Streaming Textual app entry points — ``run_ui`` and the app factory.

This module is the Textual-free factory facade: it builds the
:class:`~agentgrep.ui._context.UiContext`, wires the engine seam, validates the
selected layout and workflow against :mod:`agentgrep.ui.registry`, and constructs
the :class:`~agentgrep.ui._shell.ExplorerApp` shell. The shell and the layouts
import Textual at module scope, so they are imported lazily here and the import
error is deferred to the moment a UI is actually built — keeping a bare ``import
agentgrep`` Textual-free (ADR 0010).
"""

from __future__ import annotations

import pathlib
import typing as t

from agentgrep.ui import registry
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
    layout: str = registry.DEFAULT_LAYOUT,
    workflow: str = registry.DEFAULT_WORKFLOW,
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
    layout : str
        The layout to launch into (see :data:`agentgrep.ui.registry.LAYOUTS`).
    workflow : str
        The workflow to drive it (see :data:`agentgrep.ui.registry.WORKFLOWS`).
    """
    app = build_streaming_ui_app(
        home,
        query,
        control=control,
        initial_search_text=initial_search_text,
        layout=layout,
        workflow=workflow,
    )
    t.cast("RunnableAppLike", app).run()


def build_streaming_ui_app(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
    initial_search_text: str | None = None,
    layout: str = registry.DEFAULT_LAYOUT,
    workflow: str = registry.DEFAULT_WORKFLOW,
) -> object:
    """Construct the streaming Textual app without entering its run loop.

    Returns the constructed :class:`~agentgrep.ui._shell.ExplorerApp` shell
    (typed ``object`` so this module need not import Textual). Callers invoke
    ``.run()`` for a real session or ``.run_test()`` for a Pilot smoke test. The
    shell and the layouts are imported lazily so the eager ``import agentgrep``
    path stays Textual-free (ADR 0010).

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
    layout : str
        The layout to launch into; validated against the registry.
    workflow : str
        The workflow to drive it; validated against the registry.

    Raises
    ------
    ValueError
        If ``layout`` or ``workflow`` names an unregistered component.
    """
    if registry.layout_spec(layout) is None:
        msg = f"unknown layout {layout!r}; choose from {', '.join(registry.layout_names())}"
        raise ValueError(msg)
    if registry.workflow_spec(workflow) is None:
        msg = f"unknown workflow {workflow!r}; choose from {', '.join(registry.workflow_names())}"
        raise ValueError(msg)
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
    return ExplorerApp(ctx, layout=layout, workflow=workflow)
