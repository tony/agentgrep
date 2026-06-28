"""Unit tests for the pluggable :class:`Workflow` strategies (ADR 0013).

A workflow drives a layout only through the ``WorkflowHost`` surface, so it is
testable against a plain recording host with no Textual app — the routing policy
(search vs. filter vs. reset) is verified by the sequence of host calls.
"""

from __future__ import annotations

import pathlib
import typing as t

import pytest

from agentgrep.progress import SearchControl
from agentgrep.records import SearchQuery
from agentgrep.ui._context import UiContext
from agentgrep.ui.workflows.search import SearchWorkflow

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


def test_search_workflow_metadata() -> None:
    """The workflow exposes a stable registry id and a one-line summary."""
    assert SearchWorkflow.name == "search"
    assert SearchWorkflow.summary
    assert SearchWorkflow().BINDINGS == ()
