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

from agentgrep.ui import _export_preferences, _history, preferences, registry
from agentgrep.ui._context import UiContext

if t.TYPE_CHECKING:
    from agentgrep._types import RunnableAppLike
    from agentgrep.progress import SearchControl
    from agentgrep.records import SearchQuery, SearchScope

__all__ = ["build_streaming_ui_app", "run_ui"]


class UiQueryTooLongError(ValueError):
    """Raised when a launch expression cannot fit in the TUI input."""


def run_ui(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
    initial_search_text: str | None = None,
    base_scope: SearchScope | None = None,
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
        Search plan to run. A plan with no terms, compiled predicate, or origin
        filter opens in idle/browse mode.
    control : SearchControl
        Shared cooperative-cancel flag seeding the first search.
    initial_search_text : str | None
        Initial value of the layout's primary input. When ``None``, defaults to
        the space-joined ``query.terms``.
    base_scope : SearchScope | None
        Scope used by later plain interactive queries. ``None`` preserves the
        launch query's scope.
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
        base_scope=base_scope,
        layout=layout,
        workflow=workflow,
        _offer_theme_setup=True,
    )
    t.cast("RunnableAppLike", app).run()


def build_streaming_ui_app(
    home: pathlib.Path,
    query: SearchQuery,
    *,
    control: SearchControl,
    initial_search_text: str | None = None,
    base_scope: SearchScope | None = None,
    layout: str = registry.DEFAULT_LAYOUT,
    workflow: str = registry.DEFAULT_WORKFLOW,
    _offer_theme_setup: bool = False,
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
        Search plan to run. A plan with no terms, compiled predicate, or origin
        filter opens in idle/browse mode.
    control : SearchControl
        Shared cooperative-cancel flag seeding the first search.
    initial_search_text : str | None
        Initial value of the layout's primary input; defaults to the
        space-joined ``query.terms`` when ``None``.
    base_scope : SearchScope | None
        Scope used by later plain interactive queries. ``None`` preserves the
        launch query's scope.
    layout : str
        The layout to launch into; validated against the registry.
    workflow : str
        The workflow to drive it; validated against the registry.

    Raises
    ------
    ValueError
        If ``layout`` or ``workflow`` names an unregistered component, or the
        launch query cannot fit in the interactive input.
    """
    layout_spec = registry.layout_spec(layout)
    if layout_spec is None:
        msg = f"unknown layout {layout!r}; choose from {', '.join(registry.layout_names())}"
        raise ValueError(msg)
    workflow_spec = registry.workflow_spec(workflow)
    if workflow_spec is None:
        msg = f"unknown workflow {workflow!r}; choose from {', '.join(registry.workflow_names())}"
        raise ValueError(msg)
    launch_text = " ".join(query.terms)
    if len(launch_text) > _history.QUERY_TEXT_MAX_CHARS:
        msg = f"launch query exceeds {_history.QUERY_TEXT_MAX_CHARS} characters"
        raise UiQueryTooLongError(msg)
    if initial_search_text is not None and len(initial_search_text) > _history.QUERY_TEXT_MAX_CHARS:
        msg = f"initial search text exceeds {_history.QUERY_TEXT_MAX_CHARS} characters"
        raise UiQueryTooLongError(msg)
    from agentgrep.ui._seams import EngineSearchInvoker

    try:
        from agentgrep.ui._shell import ExplorerApp
    except ModuleNotFoundError as error:
        missing = error.name or ""
        if missing != "textual" and not missing.startswith("textual."):
            raise
        msg = "Textual is required for --ui. Install with `uv pip install --editable .`."
        raise RuntimeError(msg) from error
    from agentgrep.ui import _terminal_compat

    _terminal_compat.install_terminal_input_compat()
    layout_type = layout_spec.loader()
    workflow_type = workflow_spec.loader()
    composition = registry._UiComposition(
        layout_type=layout_type,
        workflow_type=workflow_type,
    )
    history_disabled = False
    history: tuple[_history.HistoryEntry, ...] = ()
    if layout_spec.uses_history:
        history_disabled = _history.history_disabled()
        if not history_disabled:
            history = tuple(_history.load_history(_history.history_path(home)))
    export_preferences_load = _export_preferences.load_export_preferences(home)
    ctx = UiContext(
        home=home,
        invoker=EngineSearchInvoker(home),
        query=query,
        control=control,
        base_scope=query.scope if base_scope is None else base_scope,
        initial_search_text=initial_search_text,
        history=history,
        history_disabled=history_disabled,
        export_preferences=export_preferences_load.preferences,
        export_preferences_warning=export_preferences_load.warning,
    )
    config_path = preferences.theme_config_path(home=home)
    selected_theme = preferences.load_theme_name(config_path)
    return ExplorerApp(
        ctx,
        composition=composition,
        selected_theme=selected_theme,
        config_path=config_path,
        offer_theme_setup=_offer_theme_setup,
    )
