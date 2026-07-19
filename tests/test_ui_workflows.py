"""Unit tests for the pluggable :class:`Workflow` strategies (ADR 0013).

A workflow drives a layout only through the ``WorkflowHost`` surface, so it is
testable against a plain recording host with no Textual app — the routing policy
(search vs. filter vs. reset) is verified by the sequence of host calls.
"""

from __future__ import annotations

import inspect
import pathlib
import typing as t

import pytest

from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery
from agentgrep.ui._context import UiContext
from agentgrep.ui.layouts._base import LayoutScreen
from agentgrep.ui.workflows.browse import BrowseWorkflow
from agentgrep.ui.workflows.search import SearchWorkflow

pytestmark = pytest.mark.tui

if t.TYPE_CHECKING:
    import collections.abc as cabc


def _query(*terms: str) -> SearchQuery:
    """Build a minimal :class:`SearchQuery` with ``terms``."""
    return SearchQuery(
        terms=terms,
        scope="prompts",
        any_term=False,
        regex=False,
        case_sensitive=False,
        agents=(),
        limit=None,
    )


class _NoopInvoker:
    """A :class:`~agentgrep.ui._seams.SearchInvoker` that runs nothing."""

    def run(
        self,
        query: SearchQuery,
        *,
        control: SearchControl,
        emit: cabc.Callable[[object], None],
    ) -> None:
        """No-op; the workflow tests never reach the engine."""
        del query, control, emit


class _RecordingHost:
    """Records the host calls a workflow makes (the ``WorkflowHost`` surface)."""

    def __init__(self, query: SearchQuery) -> None:
        self._ctx = UiContext(
            home=pathlib.Path("/nonexistent"),
            invoker=_NoopInvoker(),
            query=query,
            control=SearchControl(),
            base_scope=query.scope,
        )
        self.calls: list[tuple[str, object]] = []

    @property
    def context(self) -> UiContext:
        return self._ctx

    def build_query(self, text: str) -> SearchQuery:
        self.calls.append(("build_query", text))
        return _query(*text.split())

    def run_search(self, query: SearchQuery) -> None:
        self.calls.append(("run_search", query.terms))

    def filter_loaded(self, text: str) -> None:
        self.calls.append(("filter_loaded", text))

    def reset_view(self) -> None:
        self.calls.append(("reset_view", None))

    def record_history(self, text: str) -> None:
        self.calls.append(("record_history", text))

    def request_cancel(self) -> None:
        self.calls.append(("request_cancel", None))

    def kinds(self) -> tuple[str, ...]:
        return tuple(kind for kind, _ in self.calls)


class _RecordingWorkflow:
    """Records workflow attachment for ``LayoutScreen.set_workflow`` tests."""

    name: t.ClassVar[str] = "recording"
    summary: t.ClassVar[str] = "recording"
    BINDINGS: t.ClassVar[cabc.Sequence[object]] = ()

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def on_attach(self, host: t.Any) -> None:
        """Record attach without touching the host."""
        del host
        self._calls.append("attach")

    def on_query(self, host: t.Any, text: str) -> None:
        """Unused protocol method."""
        del host, text


class _WorkflowSwapLayout(LayoutScreen):
    """Minimal layout that records cancellation."""

    def __init__(self, calls: list[str]) -> None:
        ctx = UiContext(
            home=pathlib.Path("/nonexistent"),
            invoker=_NoopInvoker(),
            query=_query("seed"),
            control=SearchControl(),
            base_scope="prompts",
        )
        super().__init__(ctx, _RecordingWorkflow(calls))
        self._calls = calls

    def request_cancel(self) -> None:
        self._calls.append("cancel")


def test_layout_set_workflow_cancel_order() -> None:
    """Workflow replacement cancels active work before attached re-seeding."""
    parameters = inspect.signature(LayoutScreen.set_workflow).parameters
    assert "attach" not in parameters
    assert not hasattr(LayoutScreen, "attach_pending_workflow")
    calls: list[str] = []
    layout = _WorkflowSwapLayout(calls)
    workflow = _RecordingWorkflow(calls)
    layout.set_workflow(workflow)
    assert tuple(calls) == ("cancel", "attach")
    assert layout.workflow is workflow


class OnQueryCase(t.NamedTuple):
    """A primary-input submission and the host-call sequence it should produce."""

    test_id: str
    text: str
    expected_kinds: tuple[str, ...]


ON_QUERY_CASES = (
    OnQueryCase("empty-cancels-and-resets", "", ("request_cancel", "reset_view")),
    OnQueryCase(
        "terms-cancel-record-search",
        "bliss",
        ("request_cancel", "record_history", "build_query", "run_search"),
    ),
)


@pytest.mark.parametrize("case", ON_QUERY_CASES, ids=lambda c: c.test_id)
def test_search_workflow_on_query_routes_to_engine(case: OnQueryCase) -> None:
    """SearchWorkflow submits cancel + (record + search) for text, reset for empty."""
    host = _RecordingHost(_query())
    SearchWorkflow().on_query(host, case.text)
    assert host.kinds() == case.expected_kinds


class OnAttachCase(t.NamedTuple):
    """A launch query and the initial host call its attach should produce."""

    test_id: str
    terms: tuple[str, ...]
    expected_kinds: tuple[str, ...]


ON_ATTACH_CASES = (
    OnAttachCase("launch-terms-search", ("bliss",), ("run_search",)),
    OnAttachCase("launch-empty-resets", (), ("reset_view",)),
)


@pytest.mark.parametrize("case", ON_ATTACH_CASES, ids=lambda c: c.test_id)
def test_search_workflow_on_attach_seeds_initial_dispatch(case: OnAttachCase) -> None:
    """SearchWorkflow runs the launch query when it has terms, else resets."""
    host = _RecordingHost(_query(*case.terms))
    SearchWorkflow().on_attach(host)
    assert host.kinds() == case.expected_kinds


def test_search_workflow_on_attach_runs_compiled_only_query() -> None:
    """A field-only launch query reaches the engine without literal terms."""
    from agentgrep.query import build_query_from_input, default_registry

    result = build_query_from_input("agent:codex", _query(), default_registry())
    assert result.query is not None
    assert result.query.terms == ()
    host = _RecordingHost(result.query)
    SearchWorkflow().on_attach(host)
    assert host.kinds() == ("run_search",)


def test_search_workflow_on_attach_runs_origin_only_query() -> None:
    """An explicit project-origin launch filter reaches the engine."""
    from agentgrep.records import RecordOrigin

    query = _query()
    query.origin_filter = RecordOrigin(repo="/workspace/project")
    host = _RecordingHost(query)
    SearchWorkflow().on_attach(host)
    assert host.kinds() == ("run_search",)


def test_search_workflow_metadata() -> None:
    """The workflow exposes a stable registry id and a one-line summary."""
    assert SearchWorkflow.name == "search"
    assert SearchWorkflow.summary
    assert SearchWorkflow().BINDINGS == ()


class BrowseCase(t.NamedTuple):
    """A BrowseWorkflow entry point and the host call it should produce."""

    test_id: str
    method: str  # "attach" or "query"
    text: str
    expected_kinds: tuple[str, ...]


BROWSE_CASES = (
    BrowseCase("attach-loads-the-set-once", "attach", "", ("run_search",)),
    BrowseCase("query-filters-in-memory", "query", "foo", ("filter_loaded",)),
)


@pytest.mark.parametrize("case", BROWSE_CASES, ids=lambda c: c.test_id)
def test_browse_workflow_routes_to_filter(case: BrowseCase) -> None:
    """BrowseWorkflow loads once on attach and filters (never re-searches) on submit."""
    host = _RecordingHost(_query("seed"))
    workflow = BrowseWorkflow()
    if case.method == "attach":
        workflow.on_attach(host)
    else:
        workflow.on_query(host, case.text)
    assert host.kinds() == case.expected_kinds


def test_search_and_browse_diverge_on_submit() -> None:
    """The axis proof: identical host + input, Search searches while Browse filters."""
    search_host = _RecordingHost(_query())
    browse_host = _RecordingHost(_query())
    SearchWorkflow().on_query(search_host, "foo")
    BrowseWorkflow().on_query(browse_host, "foo")
    assert "run_search" in search_host.kinds()
    assert "filter_loaded" not in search_host.kinds()
    assert browse_host.kinds() == ("filter_loaded",)
    assert "run_search" not in browse_host.kinds()
